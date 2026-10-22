"""Persistent, deterministic WIMNA afternoon campaign scheduler.

The scheduler owns instructions and progress, not simulator I/O.  An adapter
supplies native session IDs, lap IDs, setting observations, and elapsed time.
``state`` and ``events`` are JSON-compatible snapshots suitable for append-only
storage.  Replaying the same commands produces the same snapshots.
"""

from __future__ import annotations

from copy import deepcopy
from statistics import median
from typing import Any, Mapping


class SchedulerError(ValueError):
    """An invalid command, configuration, or state transition."""


BLOCK_CODES = ("C", "R", "F", "E", "M", "V")
BLOCK_KINDS = {
    "C": "calibration",
    "R": "reference",
    "F": "fuel",
    "E": "energy",
    "M": "management",
    "V": "validation",
}
TRACK_DEFAULTS = (
    ("spa", "Spa GP", "SP", 65, 2.2, "spa", "default"),
    ("silverstone_gp", "Silverstone GP", "SI", 60, 2.0, "ks_silverstone", "gp"),
    ("imola", "Imola", "IM", 55, 1.9, "imola", "default"),
    ("monza_gp", "Modern Monza GP", "MZ", 55, 1.9, "monza", "default"),
)
DEFAULT_SCHEDULE = (
    ("preflight", 15), ("spa", 65), ("break", 10),
    ("silverstone_gp", 60), ("break", 10), ("imola", 55),
    ("break", 10), ("monza_gp", 55), ("reserve", 20),
    ("review_export", 15),
)
DISQUALIFYING_FLAGS = frozenset(
    {
        "off_track",
        "spin",
        "major_spin",
        "contact",
        "damage",
        "paused",
        "reset",
        "pit_lap",
        "unexpected_setting_change",
        "missing_required_data",
        "carryover_unrestored",
    }
)
CONTAMINATING_FLAGS = frozenset({"major_spin", "contact", "damage", "reset"})
CONTROL_CLASSES = frozenset(
    {
        "automatic_with_readback",
        "pit_session_boundary_only",
        "manually_confirmed",
        "unsupported",
        "unverified",
    }
)


def default_campaign_config() -> dict[str, Any]:
    """Return the 20–22 lap per track, four-track campaign defaults."""
    tracks: dict[str, Any] = {}
    for key, name, prefix, minutes, lap_estimate, track_id, layout_id in TRACK_DEFAULTS:
        blocks = []
        for code in BLOCK_CODES:
            block: dict[str, Any] = {"id": f"{prefix} {code}", "kind": BLOCK_KINDS[code]}
            if code == "C":
                block.update(laps_min=3, laps_max=5)
            else:
                block["laps"] = {"R": 5, "F": 2, "E": 4, "M": 3, "V": 3}[code]
            blocks.append(block)
        tracks[key] = {
            "display_name": name,
            "track_id": track_id,
            "layout_id": layout_id,
            "minutes": minutes,
            "estimated_lap_minutes": lap_estimate,
            "blocks": blocks,
        }
    return {
        "schema_version": 1,
        "time_budget_minutes": 315,
        "schedule": [{"activity": activity, "minutes": minutes} for activity, minutes in DEFAULT_SCHEDULE],
        "track_order": [item[0] for item in TRACK_DEFAULTS],
        "tracks": tracks,
        "policy": {
            "review_export_minutes_reserved": 15,
            "max_repeat_laps": 2,
            "transition_minutes_estimate": 1.5,
            "warmup_laps_estimate": 1,
        },
    }


def _normalize_config(config: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = deepcopy(dict(config)) if config is not None else default_campaign_config()
    if int(source.get("time_budget_minutes", 0)) <= 0:
        raise SchedulerError("time_budget_minutes must be positive")
    order = source.get("track_order")
    tracks = source.get("tracks")
    if not isinstance(order, list) or not order or not isinstance(tracks, dict):
        raise SchedulerError("track_order and tracks are required")
    program: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for track_index, track_key in enumerate(order):
        if track_key not in tracks or not isinstance(tracks[track_key], dict):
            raise SchedulerError(f"missing track configuration: {track_key}")
        track = tracks[track_key]
        blocks = track.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise SchedulerError(f"track {track_key} has no blocks")
        for block_index, raw_block in enumerate(blocks):
            block = dict(raw_block)
            block_id = str(block.get("id", ""))
            if not block_id or block_id in used_ids:
                raise SchedulerError(f"duplicate or empty block id: {block_id}")
            used_ids.add(block_id)
            kind = str(block.get("kind", ""))
            if kind == "calibration":
                minimum = int(block.get("laps_min", 3))
                maximum = int(block.get("laps_max", 5))
                if minimum < 1 or maximum < minimum:
                    raise SchedulerError(f"invalid calibration lap bounds: {block_id}")
                block["laps_min"], block["laps_max"] = minimum, maximum
            else:
                laps = int(block.get("laps", 0))
                if laps <= 0:
                    raise SchedulerError(f"invalid lap count: {block_id}")
                block["laps"] = laps
            block.update(track_key=track_key, track_index=track_index, block_index=block_index)
            program.append(block)
    source.setdefault("policy", {})
    if not isinstance(source["policy"], dict):
        raise SchedulerError("policy must be an object")
    return source, program


class CampaignScheduler:
    """A single afternoon campaign driven by explicit commands.

    ``dispatch(action, **payload)`` returns the appended event.  Pass
    ``elapsed_min`` (minutes since preflight began) for an advisory time
    forecast; the value must be monotonic.  The adapter should pass a stable
    ``lap_uid`` and a ``native_session_id`` for each lap.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        state: Mapping[str, Any] | None = None,
        events: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.config, self.program = _normalize_config(config)
        self._events = deepcopy(events) if events is not None else []
        self._state: dict[str, Any] = deepcopy(dict(state)) if state is not None else {
            "schema_version": 1,
            "phase": "idle",
            "block_cursor": 0,
            "elapsed_min": 0.0,
            "run_serial": 0,
            "active_run_id": None,
            "native_session_id": None,
            "physical_stint_id": None,
            "run_laps": 0,
            "accepted_laps": 0,
            "run_lap_uids": [],
            "seen_lap_uids": [],
            "lap_times_by_track": {},
            "transition_costs_min": [],
            "warmup_costs_min": [],
            "last_block_end_elapsed_min": None,
            "run_prepared_elapsed_min": None,
            "block_results": [],
            "partial_runs": [],
            "predictions": {},
            "settings_checks": [],
            "active_settings_check": None,
            "profile_check": None,
            "track_profiles": {},
            "pending_phase": None,
            "phase_before_pause": None,
            "phase_before_interrupt": None,
            "contaminated_state": False,
            "mode": "UNVERIFIED",
            "completed": False,
            "individual_mode": False,
        }
        self._check_snapshot()

    @property
    def state(self) -> dict[str, Any]:
        return deepcopy(self._state)

    @property
    def events(self) -> list[dict[str, Any]]:
        return deepcopy(self._events)

    @property
    def current_block(self) -> dict[str, Any] | None:
        cursor = self._state["block_cursor"]
        return deepcopy(self.program[cursor]) if cursor < len(self.program) else None

    def to_dict(self) -> dict[str, Any]:
        return {"config": deepcopy(self.config), "state": self.state, "events": self.events}

    def view(self) -> dict[str, Any]:
        """Compact, side-effect-free data for the in-game director overlay."""
        block = self.current_block
        phase = self._state["phase"]
        if block is None:
            progress = None
            target = None
        elif block["kind"] == "calibration":
            progress = {
                "attempted_laps": self._state["run_laps"],
                "accepted_learning_laps": self._state["accepted_laps"],
                "accepted_required": block["laps_min"],
                "planned_laps_max": block["laps_max"],
            }
            target = "Three explicitly accepted RSS learning laps, then FRZ AUTO or applied Manual profile"
        else:
            progress = {"attempted_laps": self._state["run_laps"],
                        "accepted_laps": self._state["accepted_laps"], "planned_laps": block["laps"]}
            target = block.get("target") or f"{block['laps']} accepted flying laps"
            if block["kind"] == "energy":
                map_key = "deployment_map" if self._state["accepted_laps"] < 2 else "recovery_map"
                target = {"map_phase": "deployment" if self._state["accepted_laps"] < 2 else "recovery",
                          "configured_map": self.config["policy"].get(map_key),
                          "map_change_due": phase == "map_transition"}
        next_actions = {
            "idle": "Start Full Program or Individual Test",
            "preflight": "Confirm recording and installation checks",
            "prepare_run": "Prepare car, tires, fuel and session",
            "verify_settings": "Read back requested settings",
            "warmup": "Complete out-lap and warmup",
            "calibration": "Complete and confirm RSS learning laps",
            "record_block": "Follow the current lap instruction",
            "map_transition": "Change to recovery map at lap boundary and verify effect",
            "box_save": "Box, save and analyze; confirm learning profile first for C",
            "analyze": "Analyze run and retain raw traces",
            "next_block": "Prepare next block",
            "driver_confirmation": "Confirm the setting or retry readback",
            "unavailable_capability": "Choose labeled driver-assisted or analysis-only fallback",
            "paused": "Resume Test; simulator pause is separate",
            "interrupted": "Resume with native session check",
            "ended": "Review partial run or resume with preparation",
            "review_export": "Review and export results",
            "complete": "Campaign complete",
        }
        return {
            "phase": phase,
            "track_key": block["track_key"] if block else None,
            "track_name": self.config["tracks"][block["track_key"]].get("display_name") if block else None,
            "block_id": block["id"] if block else None,
            "kind": block["kind"] if block else None,
            "instruction": block.get("instruction") if block else None,
            "progress": progress,
            "target": target,
            "recording_status": "recording" if phase in {"calibration", "record_block"} else
                                "experiment_paused" if phase == "paused" else "waiting",
            "next_action": next_actions.get(phase),
            "run_id": self._state["active_run_id"],
            "mode": self._state["mode"],
            "time": self.forecast_remaining_minutes(),
        }

    @classmethod
    def from_dict(cls, snapshot: Mapping[str, Any]) -> "CampaignScheduler":
        return cls(snapshot["config"], snapshot["state"], snapshot["events"])

    @classmethod
    def replay(cls, config: Mapping[str, Any], commands: list[Mapping[str, Any]]) -> "CampaignScheduler":
        scheduler = cls(config)
        for command in commands:
            payload = dict(command)
            action = payload.pop("action")
            scheduler.dispatch(action, **payload)
        return scheduler

    def _check_snapshot(self) -> None:
        if self._state.get("schema_version") != 1:
            raise SchedulerError("unsupported scheduler state version")
        cursor = self._state.get("block_cursor")
        if not isinstance(cursor, int) or not 0 <= cursor <= len(self.program):
            raise SchedulerError("invalid block cursor")
        if len(self._events) and self._events[-1].get("seq") != len(self._events):
            raise SchedulerError("event sequence is not contiguous")

    def _clock(self, payload: Mapping[str, Any]) -> None:
        elapsed = payload.get("elapsed_min")
        if elapsed is not None:
            value = float(elapsed)
            if value < self._state["elapsed_min"]:
                raise SchedulerError("elapsed_min cannot move backwards")
            self._state["elapsed_min"] = value

    def _review_reserve(self) -> float:
        policy = self.config["policy"]
        return float(policy.get("review_export_minutes_reserved", policy.get("review_minutes_reserved", 15)))

    def _event(
        self, action: str, before: str, reason: str, payload: Mapping[str, Any],
        block_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        block = block_context if block_context is not None else self.current_block
        event = {
            "seq": len(self._events) + 1,
            "action": action,
            "from_phase": before,
            "to_phase": self._state["phase"],
            "reason": reason,
            "track_key": block["track_key"] if block else None,
            "block_id": block["id"] if block else None,
            "run_id": self._state["active_run_id"],
            "native_session_id": self._state["native_session_id"],
            "elapsed_min": self._state["elapsed_min"],
            "wall_time_utc": payload.get("wall_time_utc"),
            "data": deepcopy({k: v for k, v in payload.items() if k not in {"elapsed_min", "wall_time_utc"}}),
        }
        self._events.append(event)
        return deepcopy(event)

    def _require(self, *phases: str) -> None:
        if self._state["phase"] not in phases:
            raise SchedulerError(f"action requires {', '.join(phases)}; phase is {self._state['phase']}")

    def _active_block(self) -> dict[str, Any]:
        block = self.current_block
        if block is None:
            raise SchedulerError("campaign has no remaining block")
        return block

    def _close_partial_run(self, reason: str) -> None:
        if self._state["active_run_id"] is not None:
            self._state["partial_runs"].append(
                {
                    "run_id": self._state["active_run_id"],
                    "block_id": self._active_block()["id"],
                    "native_session_id": self._state["native_session_id"],
                    "physical_stint_id": self._state["physical_stint_id"],
                    "attempted_laps": self._state["run_laps"],
                    "accepted_laps": self._state["accepted_laps"],
                    "lap_uids": list(self._state["run_lap_uids"]),
                    "reason": reason,
                }
            )

    def _reset_active_run(self) -> None:
        self._state.update(
            active_run_id=None,
            native_session_id=None,
            physical_stint_id=None,
            run_laps=0,
            accepted_laps=0,
            run_lap_uids=[],
            active_settings_check=None,
            profile_check=None,
            contaminated_state=False,
            run_prepared_elapsed_min=None,
        )

    @staticmethod
    def _equal(requested: Any, observed: Any, tolerance: Any) -> bool:
        if requested is None or observed is None:
            return False
        if isinstance(requested, (int, float)) and not isinstance(requested, bool):
            if not isinstance(observed, (int, float)) or isinstance(observed, bool):
                return False
            return abs(float(requested) - float(observed)) <= float(tolerance or 0)
        return requested == observed

    def _settings_check(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        requested = deepcopy(payload.get("requested") or {})
        observed = deepcopy(payload.get("observed") or {})
        classes = deepcopy(payload.get("control_classes") or {})
        confirmations = deepcopy(payload.get("manual_confirmations") or {})
        tolerances = deepcopy(payload.get("tolerances") or {})
        if not all(isinstance(value, dict) for value in (requested, observed, classes, confirmations, tolerances)):
            raise SchedulerError("settings check fields must be objects")
        outcomes = {}
        for key, target in requested.items():
            control_class = classes.get(key, "unverified")
            if control_class not in CONTROL_CLASSES:
                raise SchedulerError(f"unknown control class for {key}: {control_class}")
            readback = observed.get(key)
            if control_class in {"automatic_with_readback", "pit_session_boundary_only"}:
                status = "verified" if self._equal(target, readback, tolerances.get(key)) else "mismatch_or_missing"
            elif control_class == "manually_confirmed":
                status = "manually_confirmed" if confirmations.get(key) is True else "confirmation_required"
            else:
                status = control_class
            outcomes[key] = {
                "requested": target,
                "observed": readback,
                "control_class": control_class,
                "status": status,
                "manual_confirmation": confirmations.get(key) is True,
                "tolerance": tolerances.get(key),
            }
        check = {
            "run_id": self._state["active_run_id"],
            "requested": requested,
            "observed": observed,
            "outcomes": outcomes,
            "command_acknowledgements": deepcopy(payload.get("command_acknowledgements") or {}),
        }
        self._state["settings_checks"].append(check)
        self._state["active_settings_check"] = check
        return check

    def _lap_uid(self, payload: Mapping[str, Any]) -> str:
        if payload.get("lap_uid") is not None:
            return str(payload["lap_uid"])
        if payload.get("native_lap_index") is not None and payload.get("native_session_id") is not None:
            return f"{payload['native_session_id']}:{payload['native_lap_index']}"
        raise SchedulerError("lap requires lap_uid or native_session_id plus native_lap_index")

    def _record_lap(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        block = self._active_block()
        uid = self._lap_uid(payload)
        if uid in self._state["seen_lap_uids"]:
            return "duplicate_lap_ignored", "stable lap ID already recorded"
        session_id = payload.get("native_session_id")
        if self._state["native_session_id"] is not None and session_id != self._state["native_session_id"]:
            self._state["phase_before_interrupt"] = self._state["phase"]
            self._state["phase"] = "interrupted"
            return "session_reset_detected", "native session changed; physical stint cannot continue"
        self._state["seen_lap_uids"].append(uid)
        self._state["run_lap_uids"].append(uid)
        self._state["run_laps"] += 1
        flags = {str(flag) for flag in (payload.get("quality_flags") or [])}
        lap_time = payload.get("lap_time_s")
        if not isinstance(lap_time, (int, float)) or lap_time <= 0:
            flags.add("missing_required_data")
        if self._state["contaminated_state"]:
            flags.add("carryover_unrestored")
        if flags & CONTAMINATING_FLAGS:
            self._state["contaminated_state"] = True
        if block["kind"] == "calibration":
            # RSS learning acceptance is a distinct state; a clean timed lap
            # alone cannot prove that the adaptive learner accepted it.
            accepted = payload.get("accepted") is True and not bool(flags & DISQUALIFYING_FLAGS)
            if payload.get("accepted") is not True:
                flags.add("learning_acceptance_unconfirmed")
        else:
            accepted = not bool(flags & DISQUALIFYING_FLAGS) and payload.get("accepted") is not False
        if accepted:
            self._state["accepted_laps"] += 1
            self._state["lap_times_by_track"].setdefault(block["track_key"], []).append(float(lap_time))
        # The event retains every raw flag and the explicit acceptance decision.
        if isinstance(payload, dict):
            payload["lap_uid"] = uid
            payload["quality_flags"] = sorted(flags)
            payload["eligible_for_comparison"] = accepted
        if block["kind"] == "calibration":
            finished = self._state["accepted_laps"] >= block["laps_min"]
        else:
            finished = self._state["accepted_laps"] >= block["laps"]
        if finished:
            self._state["phase"] = "box_save"
        elif block["kind"] == "energy" and self._state["accepted_laps"] == 2 and accepted:
            self._state["phase"] = "map_transition"
            self._state["pending_phase"] = "record_block"
            return "lap_recorded", "planned recovery-map change at lap boundary"
        return "lap_recorded", "block lap target reached" if finished else "lap recorded"

    def _finish_block(self, reason: str) -> None:
        block = self._active_block()
        required = block["laps_min"] if block["kind"] == "calibration" else block["laps"]
        status = "complete" if self._state["accepted_laps"] >= required else "partial"
        self._state["block_results"].append(
            {
                "block_id": block["id"],
                "track_key": block["track_key"],
                "kind": block["kind"],
                "status": status,
                "run_id": self._state["active_run_id"],
                "native_session_id": self._state["native_session_id"],
                "attempted_laps": self._state["run_laps"],
                "accepted_laps": self._state["accepted_laps"],
                "lap_uids": list(self._state["run_lap_uids"]),
                "prediction_id": self._state["predictions"].get(block["id"], {}).get("prediction_id"),
                "profile_check": deepcopy(self._state["profile_check"]) if block["kind"] == "calibration" else None,
                "reason": reason,
            }
        )
        self._state["block_cursor"] += 1
        self._reset_active_run()

    def _estimated_lap_minutes(self, track_key: str) -> float:
        measured = self._state["lap_times_by_track"].get(track_key, [])
        if measured:
            return median(measured) / 60.0
        return float(self.config["tracks"][track_key].get("estimated_lap_minutes", 2.0))

    def forecast_remaining_minutes(self) -> dict[str, Any]:
        """Estimate effort from observed lap times; reserve remains explicit."""
        transition_observed = self._state["transition_costs_min"]
        transition = median(transition_observed) if transition_observed else float(
            self.config["policy"].get("transition_minutes_estimate", 1.5)
        )
        warmup_observed = self._state["warmup_costs_min"]
        warmup = int(self.config["policy"].get("warmup_laps_estimate", 1))
        total = 0.0
        validation = 0.0
        for cursor in range(self._state["block_cursor"], len(self.program)):
            block = self.program[cursor]
            lap_minutes = self._estimated_lap_minutes(block["track_key"])
            target = block.get("laps_min", block.get("laps"))
            completed_here = self._state["accepted_laps"] if cursor == self._state["block_cursor"] else 0
            estimate = max(0, target - completed_here) * lap_minutes
            if completed_here == 0:
                estimate += (median(warmup_observed) if warmup_observed else warmup * lap_minutes) + transition
            total += estimate
            if block["kind"] == "validation":
                validation += estimate
        cutoff = self.config["time_budget_minutes"] - self._review_reserve()
        available = max(0.0, cutoff - self._state["elapsed_min"])
        return {
            "estimated_program_minutes": round(total, 3),
            "estimated_validation_minutes": round(validation, 3),
            "available_before_review_minutes": round(available, 3),
            "review_minutes_reserved": self._review_reserve(),
            "at_risk": total > available,
        }

    def dispatch(self, action: str, **payload: Any) -> dict[str, Any]:
        """Apply one action and append one immutable audit event."""
        action = str(action)
        before = self._state["phase"]
        event_block = self.current_block
        # A failed transition must not consume elapsed time or mutate state.
        old_state = deepcopy(self._state)
        try:
            self._clock(payload)
            reason = action
            if action == "start":
                self._require("idle")
                self._state["mode"] = str(payload.get("mode", "UNVERIFIED"))
                selection = payload.get("selection", "full")
                if selection == "individual":
                    match = next(
                        (index for index, block in enumerate(self.program)
                         if block["id"] == payload.get("block_id") and
                         (payload.get("track_key") is None or block["track_key"] == payload["track_key"])),
                        None,
                    )
                    if match is None:
                        raise SchedulerError("individual test block not found")
                    self._state["block_cursor"] = match
                    self._state["individual_mode"] = True
                elif selection != "full":
                    raise SchedulerError("selection must be full or individual")
                self._state["phase"] = "preflight"
                reason = "campaign started"
            elif action == "confirm_preflight":
                self._require("preflight")
                self._state["phase"] = "prepare_run"
                reason = "recording check and target setup confirmed"
            elif action == "freeze_prediction":
                self._require("prepare_run", "next_block")
                block = self._active_block()
                if block["kind"] != "validation":
                    raise SchedulerError("predictions can be frozen only for the next validation block")
                if block["id"] in self._state["predictions"]:
                    raise SchedulerError("frozen prediction is immutable; create a new validation record")
                if not payload.get("model_id") or not isinstance(payload.get("predicted"), dict) or not payload["predicted"]:
                    raise SchedulerError("model_id and nonempty predicted quantities are required")
                record = {
                    "prediction_id": str(payload.get("prediction_id") or f"prediction-{block['id'].replace(' ', '-').lower()}"),
                    "block_id": block["id"],
                    "model_id": payload["model_id"],
                    "model_hash": payload.get("model_hash"),
                    "condition": deepcopy(payload.get("condition") or {}),
                    "predicted": deepcopy(payload["predicted"]),
                    "calibration_run_ids": deepcopy(payload.get("calibration_run_ids") or []),
                    "frozen_before_run": True,
                }
                self._state["predictions"][block["id"]] = record
                reason = "held-out prediction frozen before validation"
            elif action == "prepare":
                self._require("prepare_run", "next_block")
                block = self._active_block()
                if block["kind"] == "validation" and block["id"] not in self._state["predictions"]:
                    raise SchedulerError("validation prediction must be frozen before run preparation")
                self._state["run_serial"] += 1
                run_id = str(payload.get("run_id") or f"run-{self._state['run_serial']:04d}")
                if any(result.get("run_id") == run_id for result in self._state["block_results"]):
                    raise SchedulerError("run_id already used")
                native_session_id = payload.get("native_session_id")
                previous_end = self._state["last_block_end_elapsed_min"]
                if previous_end is not None and self._state["elapsed_min"] > previous_end:
                    self._state["transition_costs_min"].append(self._state["elapsed_min"] - previous_end)
                self._state.update(
                    active_run_id=run_id,
                    native_session_id=native_session_id,
                    physical_stint_id=str(payload.get("physical_stint_id") or native_session_id or run_id),
                    run_laps=0,
                    accepted_laps=0,
                    run_lap_uids=[],
                    contaminated_state=False,
                    run_prepared_elapsed_min=self._state["elapsed_min"],
                    phase="verify_settings",
                )
                reason = "new run prepared"
            elif action == "verify_settings":
                self._require("verify_settings", "driver_confirmation", "unavailable_capability")
                check = self._settings_check(payload)
                statuses = {result["status"] for result in check["outcomes"].values()}
                target_phase = self._state["pending_phase"] or "warmup"
                self._state["pending_phase"] = target_phase
                if "unsupported" in statuses:
                    self._state["phase"] = "unavailable_capability"
                    reason = "requested control unsupported; fallback required"
                elif statuses & {"mismatch_or_missing", "confirmation_required", "unverified"}:
                    self._state["phase"] = "driver_confirmation"
                    reason = "readback or manual confirmation pending"
                else:
                    self._state["phase"] = target_phase
                    self._state["pending_phase"] = None
                    reason = "requested settings checked"
            elif action == "verify_map_transition":
                self._require("map_transition", "driver_confirmation", "unavailable_capability")
                block = self._active_block()
                if block["kind"] != "energy" or self._state["accepted_laps"] != 2 or self._state["pending_phase"] != "record_block":
                    raise SchedulerError("recovery map change belongs after two accepted deployment laps")
                requested = payload.get("requested")
                if not isinstance(requested, dict) or not requested:
                    raise SchedulerError("recovery map transition needs an explicit requested value")
                expected = self.config["policy"].get("recovery_map")
                if expected is not None and expected not in requested.values():
                    raise SchedulerError("requested map does not match configured recovery map")
                check = self._settings_check(payload)
                statuses = {result["status"] for result in check["outcomes"].values()}
                self._state["pending_phase"] = "record_block"
                if "unsupported" in statuses:
                    self._state["phase"] = "unavailable_capability"
                    reason = "recovery-map control unsupported; fallback required"
                elif statuses & {"mismatch_or_missing", "confirmation_required", "unverified"}:
                    self._state["phase"] = "driver_confirmation"
                    reason = "recovery-map readback or manual confirmation pending"
                else:
                    self._state["phase"] = "record_block"
                    self._state["pending_phase"] = None
                    reason = "recovery map verified at lap boundary"
            elif action == "confirm_settings":
                self._require("driver_confirmation")
                confirmations = payload.get("manual_confirmations") or {}
                if not isinstance(confirmations, dict):
                    raise SchedulerError("manual_confirmations must be an object")
                check = deepcopy(self._state["active_settings_check"])
                if check is None:
                    raise SchedulerError("no settings check to confirm")
                unresolved = []
                for key, result in check["outcomes"].items():
                    if result["status"] == "confirmation_required" and confirmations.get(key) is True:
                        result["status"] = "manually_confirmed"
                        result["manual_confirmation"] = True
                    elif result["status"] not in {"verified", "manually_confirmed"}:
                        unresolved.append(key)
                if unresolved:
                    raise SchedulerError(f"setting readback still unresolved: {', '.join(unresolved)}")
                check["confirmation_record"] = deepcopy(confirmations)
                self._state["settings_checks"].append(check)
                self._state["active_settings_check"] = check
                self._state["phase"] = self._state["pending_phase"] or "warmup"
                self._state["pending_phase"] = None
                reason = "manual settings confirmed"
            elif action == "resolve_capability":
                self._require("unavailable_capability", "driver_confirmation")
                fallback = payload.get("fallback")
                if fallback not in {"analysis_only", "driver_assisted"}:
                    raise SchedulerError("fallback must be analysis_only or driver_assisted")
                self._state["phase"] = self._state["pending_phase"] or "warmup"
                self._state["pending_phase"] = None
                self._state["mode"] = fallback.upper()
                reason = f"unsupported or unverified control handled as {fallback}"
            elif action == "warmup_done":
                self._require("warmup")
                block = self._active_block()
                prepared = self._state["run_prepared_elapsed_min"]
                if prepared is not None and self._state["elapsed_min"] > prepared:
                    self._state["warmup_costs_min"].append(self._state["elapsed_min"] - prepared)
                self._state["phase"] = "calibration" if block["kind"] == "calibration" else "record_block"
                reason = "warmup/out-laps logged outside comparable laps"
            elif action == "confirm_profile":
                self._require("calibration", "box_save")
                block = self._active_block()
                if block["kind"] != "calibration" or self._state["accepted_laps"] < block["laps_min"]:
                    raise SchedulerError("three explicitly accepted learning laps are required before profile confirmation")
                mode = payload.get("mode")
                if mode not in {"FRZ_AUTO", "MANUAL_APPLIED"}:
                    raise SchedulerError("profile mode must be FRZ_AUTO or MANUAL_APPLIED")
                observed_mode = payload.get("observed_mode")
                manual = payload.get("manual_confirmation") is True
                if observed_mode != mode and not manual:
                    raise SchedulerError("profile state needs readback or explicit manual confirmation")
                if mode == "MANUAL_APPLIED" and (not payload.get("applied") or not (payload.get("profile_id") or payload.get("profile_hash"))):
                    raise SchedulerError("saved Manual profile identity and applied state are required")
                record = {
                    "track_key": block["track_key"],
                    "run_id": self._state["active_run_id"],
                    "mode": mode,
                    "observed_mode": observed_mode,
                    "verification": "readback" if observed_mode == mode else "manual_confirmation",
                    "profile_id": payload.get("profile_id"),
                    "profile_hash": payload.get("profile_hash"),
                    "applied": mode == "FRZ_AUTO" or bool(payload.get("applied")),
                }
                self._state["profile_check"] = record
                self._state["track_profiles"][block["track_key"]] = deepcopy(record)
                reason = "learning profile frozen or applied after three accepted laps"
            elif action == "lap":
                self._require("calibration", "record_block")
                mutable_payload = dict(payload)
                action, reason = self._record_lap(mutable_payload)
                payload = mutable_payload
            elif action == "restore_state":
                self._require("calibration", "record_block", "warmup", "verify_settings")
                if not payload.get("observed_restoration") and not payload.get("manual_confirmation"):
                    raise SchedulerError("restoration needs observed state or manual confirmation")
                self._state["contaminated_state"] = False
                reason = "carryover state restored"
            elif action == "box":
                self._require("box_save")
                block = self._active_block()
                if block["kind"] == "calibration" and self._state["profile_check"] is None:
                    raise SchedulerError("calibration profile must be confirmed before box/save")
                self._state["phase"] = "analyze"
                reason = "run boxed and saved"
            elif action == "analyze":
                self._require("analyze")
                block = self._active_block()
                if block["kind"] == "calibration" and self._state["profile_check"] is None:
                    raise SchedulerError("calibration profile was not confirmed")
                self._finish_block("planned block ended")
                self._state["last_block_end_elapsed_min"] = self._state["elapsed_min"]
                self._state["phase"] = "next_block" if self.current_block and not self._state["individual_mode"] else "review_export"
                reason = "block analyzed; progress retained"
            elif action == "next_block":
                self._require("next_block")
                self._state["phase"] = "prepare_run"
                reason = "next configured block selected"
            elif action == "pause":
                self._require("preflight", "prepare_run", "verify_settings", "warmup", "calibration", "record_block", "map_transition", "box_save", "analyze", "next_block")
                self._state["phase_before_pause"] = self._state["phase"]
                self._state["phase"] = "paused"
                reason = "experiment paused; simulator may continue"
            elif action == "resume":
                self._require("paused")
                if payload.get("native_session_id") not in {None, self._state["native_session_id"]}:
                    self._state["phase_before_interrupt"] = self._state["phase_before_pause"]
                    self._state["phase"] = "interrupted"
                    reason = "native session changed while experiment paused"
                else:
                    self._state["phase"] = self._state["phase_before_pause"]
                    reason = "experiment resumed"
                self._state["phase_before_pause"] = None
            elif action == "interrupt":
                self._require("preflight", "prepare_run", "verify_settings", "warmup", "calibration", "record_block", "map_transition", "box_save", "analyze", "next_block", "paused")
                self._state["phase_before_interrupt"] = self._state["phase"]
                self._state["phase"] = "interrupted"
                reason = str(payload.get("reason") or "recording interrupted")
            elif action == "resume_interrupted":
                self._require("interrupted", "ended")
                same_session = (
                    self._state["native_session_id"] is not None
                    and payload.get("native_session_id") == self._state["native_session_id"]
                )
                if same_session and self._state["phase_before_interrupt"] in {"calibration", "record_block", "map_transition", "box_save"}:
                    self._state["phase"] = self._state["phase_before_interrupt"]
                    reason = "same native session resumed; lap IDs prevent duplicates"
                else:
                    self._close_partial_run("fresh native session after interruption")
                    self._reset_active_run()
                    self._state["phase"] = "prepare_run"
                    reason = "fresh native session requires a new physical stint"
                self._state["phase_before_interrupt"] = None
            elif action == "end_run":
                self._require("preflight", "prepare_run", "verify_settings", "warmup", "calibration", "record_block", "map_transition", "box_save", "analyze", "next_block", "paused", "interrupted")
                self._state["phase_before_interrupt"] = self._state["phase"]
                self._state["phase"] = "ended"
                reason = str(payload.get("reason") or "driver ended run; partial work retained")
            elif action == "report_issue":
                reason = str(payload.get("issue") or "driver issue recorded")
            elif action == "skip":
                self._require("next_block", "prepare_run", "calibration", "record_block", "map_transition", "box_save")
                block = self._active_block()
                if self._state["phase"] == "box_save" and block["kind"] != "calibration":
                    raise SchedulerError("completed timed block must be boxed and analyzed")
                if block["kind"] == "validation" and not payload.get("unavoidable"):
                    raise SchedulerError("validation is protected; provide unavoidable reason")
                if not payload.get("reason"):
                    raise SchedulerError("skipped work needs an explicit reason")
                self._state["block_results"].append(
                    {"block_id": block["id"], "track_key": block["track_key"], "kind": block["kind"],
                     "status": "partial" if self._state["run_laps"] else "incomplete",
                     "run_id": self._state["active_run_id"],
                     "native_session_id": self._state["native_session_id"],
                     "attempted_laps": self._state["run_laps"],
                     "accepted_laps": self._state["accepted_laps"],
                     "lap_uids": list(self._state["run_lap_uids"]),
                     "reason": str(payload["reason"])}
                )
                self._state["block_cursor"] += 1
                self._reset_active_run()
                self._state["phase"] = "next_block" if self.current_block and not self._state["individual_mode"] else "review_export"
                reason = "block closed with incomplete or partial evidence"
            elif action == "request_repeat":
                self._require("next_block", "prepare_run", "analyze")
                count = int(payload.get("laps", 0))
                max_laps = int(self.config["policy"].get("max_repeat_laps", 2))
                if count < 1 or count > max_laps or not payload.get("question") or not payload.get("decision_impact"):
                    raise SchedulerError("repeat needs 1–2 laps and a decision-relevant question")
                reason = "bounded repeat offered; remaining time is advisory"
            elif action == "enter_review":
                self._require("preflight", "prepare_run", "next_block", "analyze", "review_export", "ended", "interrupted")
                self._state["phase"] = "review_export"
                reason = "review and export started"
            elif action == "export_complete":
                self._require("review_export")
                self._state["phase"] = "complete"
                self._state["completed"] = True
                reason = "results exported; campaign complete"
            else:
                raise SchedulerError(f"unknown action: {action}")
            return self._event(action, before, reason, payload, event_block)
        except Exception:
            self._state = old_state
            raise
