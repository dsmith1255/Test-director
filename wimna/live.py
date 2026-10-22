"""Local companion director for the CSP JSONL bridge.

The Lua app records physical runs.  This module consumes immutable chunks,
drives the deterministic scheduler, and publishes only instructions.  It never
writes an RSS control or treats a driver's confirmation as native readback.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, median
from typing import Any, Callable, Mapping

from .config import load_campaign
from .scheduler import CampaignScheduler, SchedulerError
from .store import Store, ingest_bridge

CAMPAIGN_ID = "live-afternoon-v1"
SESSION_BREAK_EVENTS = frozenset({"session_changed", "car_reset", "lap_counter_reset", "lap_counter_jump"})
ISSUE_FLAG_MAP = {"pit": "pit_lap", "pitlane": "pit_lap", "sim_paused": "paused",
                  "experiment_paused": "paused", "car_jump": "reset", "car_reset": "reset",
                  "lap_counter_reset": "reset", "session_changed": "reset"}


def documents_root() -> Path:
    """Use Windows' Documents known folder, including redirected locations."""
    if sys.platform == "win32":
        try:
            import ctypes
            path = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, path) == 0 and path.value:
                return Path(path.value)
        except (AttributeError, OSError, ValueError):
            pass
    return Path.home() / "Documents"


def default_bridge_root() -> Path:
    return documents_root() / "WIMNA Test Director" / "bridge"


def default_data_root() -> Path:
    return documents_root() / "WIMNA Test Director" / "data"


def _wall_seconds(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _prior_diagnostic_run(run_id: str, manifest: Mapping[str, Any], processed: Mapping[str, int]) -> bool:
    """Protect runs handled by the old director, or lacking proof of a 0.2+ app start."""
    if run_id in processed:
        return True
    version = manifest.get("app_version")
    if not isinstance(version, str):
        return True
    parts = version.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return True
    return (int(parts[0]), int(parts[1])) < (0, 2)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _channel_value(sample: Mapping[str, Any], key: str) -> Any:
    channel = sample.get(key)
    return channel.get("value") if isinstance(channel, dict) else None


def _numeric_channel(sample: Mapping[str, Any], key: str, units: set[str]) -> float | None:
    channel = sample.get(key)
    if not isinstance(channel, dict) or channel.get("unit") not in units:
        return None
    if channel.get("source") in {"RSS_UNVERIFIED", "UNVERIFIED", "ASSUMED"}:
        return None
    if channel.get("quality") in {"unavailable", "stale", "invalid"}:
        return None
    value = channel.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _trace_points(samples: list[dict]) -> list[dict]:
    points: list[dict] = []
    previous_distance = -1.0
    for record in samples:
        data = record["data"]
        distance = _numeric_channel(data, "track_distance_m", {"m"})
        speed = _numeric_channel(data, "speed_kmh", {"km/h"})
        brake = _numeric_channel(data, "brake", {"fraction"})
        throttle = _numeric_channel(data, "throttle", {"fraction"})
        if None in (distance, speed, brake, throttle) or distance < previous_distance - 5:
            continue
        previous_distance = distance
        slips = [_numeric_channel(data, f"wheel_slip_{wheel}",
                                  {"ratio_verified", "fraction_verified"})
                 for wheel in ("fl", "fr", "rl", "rr")]
        verified_slip = max((abs(value) for value in slips if value is not None), default=None)
        points.append({"distance_m": distance, "speed_kmh": speed, "brake": brake,
                       "throttle": throttle, "verified_slip": verified_slip})
    return points


def _candidate_zones(points: list[dict], kind: str, lap_seq: int) -> list[dict]:
    candidates: list[dict] = []
    armed = False
    for index, point in enumerate(points):
        distance = point["distance_m"]
        control = point["brake"] if kind == "brake" else point["throttle"]
        if control <= (0.10 if kind == "brake" else 0.20):
            armed = True
            continue
        if not armed or control < (0.30 if kind == "brake" else 0.35):
            continue
        armed = False
        if point["speed_kmh"] < (100 if kind == "brake" else 50):
            continue
        horizon = 260 if kind == "brake" else 100
        following = [later for later in points[index + 1:]
                     if 0 < later["distance_m"] - distance <= horizon]
        if not following:
            continue
        if kind == "brake":
            speed_drop = point["speed_kmh"] - min(later["speed_kmh"] for later in following)
            if speed_drop < 25:
                continue
            strength = speed_drop
        else:
            peak = max(later["throttle"] for later in following)
            if peak < 0.75 or peak - control < 0.25:
                continue
            peak_slip = max((later["verified_slip"] for later in following
                             if later["verified_slip"] is not None), default=None)
            strength = peak - control
        candidates.append({"distance_m": distance, "strength": strength,
                           "lap_event_seq": lap_seq,
                           "verified_slip": peak_slip if kind == "exit" else None})
    return candidates


def _cluster_zones(candidates: list[dict], lap_count: int) -> list[dict]:
    clusters: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["distance_m"]):
        cluster = next((item for item in clusters
                        if abs(candidate["distance_m"] - median(
                            entry["distance_m"] for entry in item["entries"].values())) <= 45), None)
        if cluster is None:
            cluster = {"entries": {}}
            clusters.append(cluster)
        lap_seq = candidate["lap_event_seq"]
        previous = cluster["entries"].get(lap_seq)
        if previous is None or candidate["strength"] > previous["strength"]:
            cluster["entries"][lap_seq] = candidate
    minimum_support = 2 if lap_count >= 2 else 1
    result = []
    for cluster in clusters:
        entries = list(cluster["entries"].values())
        if len(entries) < minimum_support:
            continue
        distances = [item["distance_m"] for item in entries]
        result.append({"distance_m": median(distances), "spread_m": max(distances) - min(distances),
                       "support_laps": len(entries),
                       "lap_event_seqs": sorted(item["lap_event_seq"] for item in entries),
                       "strength": fmean(item["strength"] for item in entries),
                       "verified_slip_support": sum(item["verified_slip"] is not None for item in entries),
                       "verified_slip": fmean(item["verified_slip"] for item in entries
                                              if item["verified_slip"] is not None)
                       if any(item["verified_slip"] is not None for item in entries) else None})
    return result


class LiveDirector:
    """Pollable director. One Lua physical run corresponds to one program block."""

    def __init__(
        self, bridge_root: str | Path | None = None, data_root: str | Path | None = None,
        *, config: Mapping[str, Any] | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self.bridge_root = Path(bridge_root or default_bridge_root()).resolve()
        self.data_root = Path(data_root or default_data_root()).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_root / "store")
        self.run_manifests = {run["run_id"]: run["manifest"] for run in self.store.iter_runs()}
        self.clock = clock
        requested_config = config or load_campaign()
        saved = self.store.load_campaign(CAMPAIGN_ID)
        self.legacy_diagnostic_runs: set[str] = set()
        self.legacy_campaign_notice: str | None = None
        if saved:
            self.scheduler = CampaignScheduler.from_dict(saved["scheduler"])
            self.processed: dict[str, int] = {str(k): int(v) for k, v in saved.get("processed", {}).items()}
            self.pending_learning: list[dict] = list(saved.get("pending_learning", []))
            self.campaign_started_wall_s = saved.get("campaign_started_wall_s")
            self.live_run_id = saved.get("live_run_id")
            self.session_indices: dict[str, Any] = dict(saved.get("session_indices", {}))
            self.messages: list[str] = list(saved.get("messages", []))[-30:]
            self.legacy_diagnostic_runs = set(saved.get("legacy_diagnostic_runs", []))
            if self.scheduler.config["track_order"] != requested_config["track_order"]:
                state = self.scheduler.state
                untouched = (state["phase"] == "idle" and state["block_cursor"] == 0
                             and state.get("active_run_id") is None and not state["block_results"]
                             and not self.scheduler.events and self.live_run_id is None
                             and self.campaign_started_wall_s is None)
                if untouched:
                    # Runs already recorded under the old campaign stay diagnostic, including
                    # bridge chunks that the companion has not imported yet.
                    self.legacy_diagnostic_runs.update(
                        run["run_id"] for run in self.store.iter_runs()
                        if self.store.get_run_assignment(run["run_id"]) is None
                        and _prior_diagnostic_run(run["run_id"], run["manifest"], self.processed))
                    runs_dir = self.bridge_root / "runs"
                    if runs_dir.is_dir():
                        self.legacy_diagnostic_runs.update(
                            path.name for path in runs_dir.iterdir() if path.is_dir()
                            and self.store.get_run_assignment(path.name) is None
                            and _prior_diagnostic_run(path.name, self._run_manifest(path.name), self.processed))
                    self.scheduler = CampaignScheduler(requested_config)
                    self._note("Idle campaign updated to the configured track order; earlier diagnostic runs remain unassigned.")
                else:
                    self.legacy_campaign_notice = (
                        "Active earlier campaign retains its original track order; "
                        "the new track order has not been applied to this campaign.")
                    self._note(self.legacy_campaign_notice)
        else:
            self.scheduler = CampaignScheduler(requested_config)
            self.processed = {}
            self.pending_learning = []
            self.campaign_started_wall_s = None
            self.live_run_id = None
            self.session_indices = {}
            self.messages = []
        self.last_ingest: dict = {"files": 0, "new_records": 0, "errors": []}
        self.last_current_state: dict = {}
        self._publish_state()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "LiveDirector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _note(self, message: str) -> None:
        self.messages.append(message)
        self.messages = self.messages[-30:]

    def _save(self) -> None:
        self.store.save_campaign(CAMPAIGN_ID, {
            "schema": 1, "scheduler": self.scheduler.to_dict(), "processed": self.processed,
            "pending_learning": self.pending_learning,
            "campaign_started_wall_s": self.campaign_started_wall_s,
            "live_run_id": self.live_run_id, "session_indices": self.session_indices,
            "messages": self.messages,
            "legacy_diagnostic_runs": sorted(self.legacy_diagnostic_runs),
        })

    def _elapsed(self, record: Mapping[str, Any] | None = None) -> float:
        if self.campaign_started_wall_s is None:
            return self.scheduler.state["elapsed_min"]
        candidate = _wall_seconds(record.get("wall_time_utc")) if record else None
        now = candidate if candidate is not None else self.clock()
        return max(self.scheduler.state["elapsed_min"], max(0.0, now - self.campaign_started_wall_s) / 60)

    def _dispatch(self, action: str, record: Mapping[str, Any] | None = None, **payload: Any) -> dict:
        if "elapsed_min" not in payload:
            payload["elapsed_min"] = self._elapsed(record)
        if record and "wall_time_utc" not in payload:
            payload["wall_time_utc"] = record.get("wall_time_utc")
        event = self.scheduler.dispatch(action, **payload)
        self._note(f"{event['block_id'] or 'campaign'}: {event['reason']}")
        return event

    def _read_current(self) -> dict:
        path = self.bridge_root / "current_state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) and value.get("schema") == 1 else {}

    def _run_manifest(self, run_id: str) -> dict:
        stored = self.run_manifests.get(run_id)
        if stored is not None:
            return stored
        path = self.bridge_root / "runs" / run_id / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self.run_manifests[run_id] = value
                return value
            return {}
        except (OSError, ValueError):
            return {}

    def _is_ai_thermal_run(self, run_id: str) -> bool:
        return self._run_manifest(run_id).get("selection") == "ai_thermal"

    def _is_collection_run(self, run_id: str) -> bool:
        return self._run_manifest(run_id).get("selection") == "collection"

    def _track_key(self, run_id: str) -> str | None:
        manifest = self._run_manifest(run_id)
        track_id, layout_id = manifest.get("track_id"), manifest.get("layout_id")
        for key in self.scheduler.config["track_order"]:
            target = self.scheduler.config["tracks"][key]
            if target.get("track_id") is not None and target.get("track_id") == track_id and target.get("layout_id") == layout_id:
                return key
        return None

    def _check_native_identity(self, run_id: str) -> bool:
        block = self.scheduler.current_block
        if block is None:
            self._note("No program block remains; review and export results.")
            return False
        manifest = self._run_manifest(run_id)
        selected = self._track_key(run_id)
        if selected is None:
            self._note("Exact track/layout is not configured or does not match this run; resolve it in the companion.")
            return False
        if selected != block["track_key"]:
            self._note(f"Selected track is {selected}; next block requires {block['track_key']}.")
            return False
        car_target = self.scheduler.config["policy"].get("car_id")
        if car_target is not None and manifest.get("car_id") != car_target:
            self._note("Installed car ID differs from configured FH26 car; no run prepared.")
            return False
        return True

    def _start_choice(self, record: dict) -> None:
        run_id = record["run_id"]
        data = record["data"]
        event_type = data["type"]
        phase = self.scheduler.state["phase"]
        if phase in {"review_export", "complete"}:
            self._note("Campaign is in review/export; a new bridge run cannot restart it.")
            return
        if event_type == "ui_individual" and phase != "idle":
            self._note("Individual Test selection requires a new campaign; current progress is retained.")
            return
        selected = None
        block = None
        if phase == "idle":
            if event_type == "ui_individual":
                letter = str(data.get("block_id") or "").split()[-1]
                selected = self._track_key(run_id)
                if selected is None:
                    self._note("Configure exact track/layout before Individual Test. Diagnostic recording only; campaign progress remains idle.")
                    return
                block = next((b for b in self.scheduler.program if b["track_key"] == selected and b["id"].endswith(" " + letter)), None)
                if block is None:
                    self._note("Individual Test block was not recognized; campaign progress remains idle.")
                    return
        if event_type != "ui_individual" and not self._check_native_identity(run_id):
            self._note("Diagnostic recording only; this run does not change campaign progress.")
            return
        self.live_run_id = run_id
        if self.campaign_started_wall_s is None:
            self.campaign_started_wall_s = _wall_seconds(record.get("wall_time_utc")) or self.clock()
        if phase == "idle":
            if event_type == "ui_individual":
                self._dispatch("start", record, mode="MEASURED", selection="individual", block_id=block["id"], track_key=selected)
            else:
                self._dispatch("start", record, mode="MEASURED")
            return
        if phase == "next_block":
            self._dispatch("next_block", record)
        elif phase in {"interrupted", "ended"}:
            self._dispatch("resume_interrupted", record, native_session_id=run_id)
        elif phase == "paused":
            self._dispatch("resume", record, native_session_id=run_id)
            if self.scheduler.state["phase"] == "interrupted":
                self._dispatch("resume_interrupted", record, native_session_id=run_id)
        elif self.scheduler.state["active_run_id"] != run_id:
            if phase in {"driver_confirmation", "unavailable_capability"}:
                self._dispatch("resolve_capability", record, fallback="analysis_only")
            self._dispatch("interrupt", record, reason="fresh physical bridge run")
            self._dispatch("resume_interrupted", record, native_session_id=run_id)

    def _requested_settings(self, block: dict) -> dict:
        policy = self.scheduler.config["policy"]
        kind = block["kind"]
        fuel_key = "contrasting_fuel_l" if kind == "fuel" else "reference_fuel_l"
        map_key = "deployment_map" if kind == "energy" else "baseline_map"
        target = {
            "driver_preparation": "confirmed",
            "fuel_l": policy.get(fuel_key),
            "tire_compound": policy.get("reference_compound"),
            "energy_map": policy.get(map_key),
            "target_soc_band_fraction": policy.get("target_soc_band_fraction"),
            "target_soc_band_j": policy.get("target_soc_band_j"),
            "setup_hash": policy.get("setup_hash"),
        }
        return {key: value for key, value in target.items() if value is not None}

    def _missing_targets(self, block: dict) -> list[str]:
        policy = self.scheduler.config["policy"]
        fuel_key = "contrasting_fuel_l" if block["kind"] == "fuel" else "reference_fuel_l"
        map_key = "deployment_map" if block["kind"] == "energy" else "baseline_map"
        required = [fuel_key, "reference_compound", map_key]
        if block["kind"] == "energy":
            required.append("recovery_map")
        missing = [key for key in required if policy.get(key) is None]
        if policy.get("target_soc_band_fraction") is None and policy.get("target_soc_band_j") is None:
            missing.append("target_soc_band_fraction_or_j")
        return missing

    def _last_sample(self, run_id: str, *, through_seq: int | None = None) -> dict:
        last: dict = {}
        for record in self.store.iter_records(run_id, "sample"):
            if through_seq is not None and record["seq"] > through_seq:
                break
            last = record["data"]
        return last

    def _reference_traces(self, track_key: str) -> tuple[str | None, list[tuple[int, list[dict]]]]:
        reference = next((result for result in reversed(self.scheduler.state["block_results"])
                          if result.get("track_key") == track_key and result.get("kind") == "reference"
                          and result.get("status") == "complete" and result.get("run_id")), None)
        if reference is None:
            return None, []
        run_id = reference["run_id"]
        run = next((item for item in self.store.iter_runs() if item["run_id"] == run_id), None)
        if run is None or run["mode"] != "MEASURED":
            return run_id, []
        eligible = {item["lap_event_seq"] for item in self.store.iter_lap_annotations(run_id)
                    if item["phase"] == "program" and item["eligible"] is True}
        traces: list[tuple[int, list[dict]]] = []
        samples: list[dict] = []
        for record in self.store.iter_records(run_id):
            if record["kind"] == "sample":
                samples.append(record)
            elif record["kind"] == "event" and record["data"].get("type") == "lap_complete":
                if record["seq"] in eligible:
                    traces.append((record["seq"], _trace_points(samples)))
                samples = []
        return run_id, traces

    def _management_cues(self, block: dict) -> dict:
        track_key = block["track_key"]
        is_exit = track_key == "silverstone_gp"
        source_run_id, traces = self._reference_traces(track_key)
        policy = self.scheduler.config["policy"]
        distance_key = "management_throttle_ramp_distance_m" if is_exit else "management_lift_distance_m"
        intervention_distance = policy.get(distance_key)
        result = {
            "status": "unavailable", "block_id": block["id"],
            "source_run_id": source_run_id,
            "source_lap_event_seqs": [seq for seq, _ in traces],
            "source_mode": "MEASURED" if traces else "unavailable",
            "distance_source": "CSP spline fraction × track length; PHYSICS_DERIVED",
            "cue_type": "throttle_ramp" if is_exit else "lift_coast",
            "intervention_distance_m": intervention_distance,
            "intervention_distance_evidence": "ASSUMED",
            "cues": [], "limitations": [],
        }
        if not isinstance(intervention_distance, (int, float)) or isinstance(intervention_distance, bool) \
                or not math.isfinite(intervention_distance) or not 5 <= intervention_distance <= 150:
            result["limitations"].append(f"{distance_key} is missing or outside 5–150 m")
            return result
        usable = [(seq, points) for seq, points in traces if len(points) >= 8]
        if not usable:
            result["limitations"].append("No eligible reference lap has enough distance/speed/control samples")
            return result
        kind = "exit" if is_exit else "brake"
        candidates = [candidate for seq, points in usable
                      for candidate in _candidate_zones(points, kind, seq)]
        clusters = _cluster_zones(candidates, len(usable))
        if not is_exit:
            clusters = [item for item in clusters if item["distance_m"] > intervention_distance]
        selected: list[dict] = []
        for candidate in sorted(clusters, key=lambda item: (item["support_laps"], item["strength"]),
                                reverse=True):
            if all(abs(candidate["distance_m"] - earlier["distance_m"]) >= 180 for earlier in selected):
                selected.append(candidate)
            if len(selected) == 2:
                break
        if len(selected) < 2:
            result["limitations"].append("Fewer than two repeatable candidate zones in accepted reference laps")
            return result
        selected.sort(key=lambda item: item["distance_m"])
        slip_supported = is_exit and all(item["verified_slip_support"] == item["support_laps"]
                                         for item in selected)
        for index, item in enumerate(selected, 1):
            landmark = round(item["distance_m"], 1)
            if is_exit:
                cue_distance = landmark
                instruction = (f"At {cue_distance:g} m, open throttle gradually over the next "
                               f"{intervention_distance:g} m; keep braking and the baseline map unchanged.")
            else:
                cue_distance = round(landmark - intervention_distance, 1)
                instruction = (f"At {cue_distance:g} m, lift and coast to your usual braking point "
                               f"near {landmark:g} m; keep the baseline map unchanged.")
            result["cues"].append({
                "number": index, "distance_m": cue_distance,
                "reference_landmark_m": landmark,
                "support_laps": item["support_laps"],
                "observed_spread_m": round(item["spread_m"], 1),
                "source_lap_event_seqs": item["lap_event_seqs"],
                "instruction": instruction,
                "evidence": ["MEASURED", "PHYSICS_DERIVED", "ASSUMED"],
                "proxy": "verified wheel-slip ratio" if slip_supported else
                         ("reference throttle ramp" if is_exit else "reference braking/deceleration"),
            })
        result["status"] = "ready"
        if is_exit and not slip_supported:
            result["limitations"].append(
                "Wheel slip units are unverified or absent; exits selected from the reference throttle ramp proxy")
        result["limitations"].append("Cue adherence and resulting resource effect require post-run telemetry checks")
        return result

    def _active_management_cues(self) -> dict | None:
        block = self.scheduler.current_block
        if block is None or block["kind"] != "management":
            return None
        run_id = self.scheduler.state.get("active_run_id")
        assignment = self.store.get_run_assignment(run_id) if run_id else None
        if assignment:
            return assignment["details"].get("management_cues")
        return None

    def _observed_settings(self, run_id: str, through_seq: int) -> dict:
        sample = self._last_sample(run_id, through_seq=through_seq)
        return {"fuel_l": None, "fuel_native": sample.get("fuel"),
                "tire_compound": _channel_value(sample, "tyre_compound"),
                "energy_map": None, "setup_hash": None}

    def _reference_prediction(self, record: dict) -> None:
        block = self.scheduler.current_block
        if block is None or block["kind"] != "validation":
            return
        if block["id"] in self.scheduler.state["predictions"]:
            return
        reference = next((result for result in reversed(self.scheduler.state["block_results"])
                          if result["track_key"] == block["track_key"] and result["kind"] == "reference"
                          and result["status"] == "complete"), None)
        fit_ids = [reference["run_id"]] if reference and reference.get("run_id") else []
        times = [event["data"]["lap_time_s"] for event in self.scheduler.events
                 if reference and event["block_id"] == reference["block_id"]
                 and event["run_id"] == reference["run_id"] and event["action"] == "lap_recorded"
                 and event["data"].get("eligible_for_comparison") is True
                 and isinstance(event["data"].get("lap_time_s"), (int, float))]
        predicted = {"mean_lap_time_s": fmean(times) if times else None,
                     "fuel_l_per_lap": None, "battery_delta_j_per_lap": None,
                     "tire_state": None}
        condition = {"block_id": block["id"], "condition": "held-out baseline/management check",
                     "thermal_intervention": "analysis_only_unverified",
                     "evidence": ["MEASURED", "PHYSICS_DERIVED"] if times else ["ASSUMED"],
                     "missing_predictions": [key for key, value in predicted.items() if value is None]}
        prediction_id = f"live-{block['id'].replace(' ', '-').lower()}-v1"
        self._dispatch("freeze_prediction", record, prediction_id=prediction_id,
                       model_id="reference_mean_fallback_v1" if times else "unavailable_fallback_v1",
                       predicted=predicted, condition=condition, calibration_run_ids=fit_ids)
        existing = self.store.db.execute("SELECT 1 FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
        if existing is None:
            self.store.register_prediction(prediction_id, fit_ids,
                                           {"model_id": "reference_mean_fallback_v1" if times else "unavailable_fallback_v1",
                                            "evidence": condition["evidence"], "limitations": condition["missing_predictions"]},
                                           predicted)

    def _prepare(self, record: dict) -> None:
        run_id = record["run_id"]
        if not self._check_native_identity(run_id):
            return
        if any(result.get("run_id") == run_id for result in self.scheduler.state["block_results"]):
            self._note("This physical run already belongs to a completed block; start a fresh WIMNA run.")
            return
        block = self.scheduler.current_block
        if block and block["kind"] == "validation":
            self._reference_prediction(record)
        self._dispatch("prepare", record, run_id=run_id, native_session_id=run_id,
                       physical_stint_id=run_id)
        sample = self._last_sample(run_id, through_seq=record["seq"])
        policy = self.scheduler.config["policy"]
        source_manifest = self._run_manifest(run_id)
        management_cues = self._management_cues(block) if block and block["kind"] == "management" else None
        assignment = {
            "source": "live companion assignment at block preparation",
            "source_mode": "MEASURED", "physical_session_id": run_id,
            "car_id": source_manifest.get("car_id"),
            "track_id": source_manifest.get("track_id"),
            "layout_id": source_manifest.get("layout_id"),
            "car_version": source_manifest.get("car_version"),
            "requested_initial_state": {
                "fuel_l": policy.get("contrasting_fuel_l") if block and block["kind"] == "fuel" else policy.get("reference_fuel_l"),
                "target_soc_band_j": policy.get("target_soc_band_j"),
                "target_soc_band_fraction": policy.get("target_soc_band_fraction"),
                "tire_compound": policy.get("reference_compound"),
            },
            "observed_initial_state": {
                "fuel_l": None,
                "fuel_native": sample.get("fuel"),
                "battery_j": None,
                "battery_energy_native": sample.get("battery_energy_j"),
                "battery_soc_native": sample.get("battery_soc_native"),
                "tire_compound": _channel_value(sample, "tyre_compound"),
            },
            "requested_setup_hash": policy.get("setup_hash"),
            "observed_setup_hash": None,
            "profile_hash": None,
            "profile_identity_verification": "unverified_until_driver_confirmation",
            "tire_preparation_hash": None,
            "conditions_verification": "manual_or_unverified",
            "management_cues": management_cues,
        }
        self.store.assign_run(run_id, block["track_key"], block["id"], assignment)
        if management_cues and management_cues["status"] != "ready":
            self._note("Management cues unavailable from accepted reference telemetry; M laps will be incomplete.")
        if block and block["kind"] == "validation":
            prediction_id = self.scheduler.state["predictions"][block["id"]]["prediction_id"]
            self.store.attach_validation(prediction_id, run_id)

    def _confirm(self, record: dict) -> None:
        phase = self.scheduler.state["phase"]
        if phase == "preflight":
            current = self._read_current()
            if current.get("run_id") != record["run_id"] or current.get("write_ok") is not True:
                self._note("Recording check has no matching writable bridge state; preflight remains open.")
                return
            if not self._check_native_identity(record["run_id"]):
                return
            self._dispatch("confirm_preflight", record, recording_confirmed_manually=True)
        elif phase == "prepare_run":
            self._prepare(record)
        elif phase == "verify_settings":
            block = self.scheduler.current_block
            if block is None:
                return
            requested = self._requested_settings(block)
            missing = self._missing_targets(block)
            for name in missing:
                requested[f"missing:{name}"] = "unconfigured"
            classes = {key: ("unsupported" if key.startswith("missing:") else "manually_confirmed")
                       for key in requested}
            confirmations = {key: True for key in requested if not key.startswith("missing:")}
            self._dispatch("verify_settings", record, requested=requested,
                           observed=self._observed_settings(record["run_id"], record["seq"]),
                           control_classes=classes, manual_confirmations=confirmations)
            if missing:
                self._note("Required targets unconfigured: " + ", ".join(missing) +
                           ". The next confirmation selects analysis-only/incomplete fallback.")
        elif phase == "warmup":
            self._dispatch("warmup_done", record, driver_confirmed=True)
        elif phase == "map_transition":
            recovery = self.scheduler.config["policy"].get("recovery_map")
            if recovery is None:
                self._dispatch("verify_map_transition", record, requested={"energy_map": "unconfigured"},
                               observed={}, control_classes={"energy_map": "unsupported"})
                self._dispatch("resolve_capability", record, fallback="analysis_only")
                self._note("Recovery map identity missing; energy contrast stays analysis-only.")
            else:
                self._dispatch("verify_map_transition", record, requested={"energy_map": recovery},
                               observed={"energy_map": None},
                               control_classes={"energy_map": "manually_confirmed"},
                               manual_confirmations={"energy_map": True})
        elif phase == "driver_confirmation":
            check = self.scheduler.state.get("active_settings_check") or {}
            confirmations = {key: True for key, outcome in check.get("outcomes", {}).items()
                             if outcome.get("status") == "confirmation_required"}
            if confirmations:
                self._dispatch("confirm_settings", record, manual_confirmations=confirmations)
            else:
                self._note("Readback mismatch remains unresolved; use a labeled fallback or recheck settings.")
        elif phase == "unavailable_capability":
            self._dispatch("resolve_capability", record, fallback="analysis_only")
        elif phase == "box_save":
            if self.pending_learning:
                self._flush_pending_learning("unconfirmed learning acceptance")
            self._dispatch("box", record)
            self._dispatch("analyze", record)
            self._note("End this physical bridge run, then start a fresh WIMNA run for the next block.")
        elif phase == "next_block":
            self._note("End this physical run and start a fresh WIMNA run before preparing the next block.")
        elif phase in {"calibration", "record_block"}:
            self._note("Laps advance from recorded crossings. Use the separate learning/profile confirmations for C.")
        elif phase == "review_export":
            self._note("Use the companion Export Results button after review.")
        else:
            self._note(f"Confirm has no action in phase {phase}.")

    def _quality_flags(self, data: Mapping[str, Any]) -> list[str]:
        flags = {str(flag) for flag in (data.get("flags") or [])}
        flags.update(ISSUE_FLAG_MAP[flag] for flag in tuple(flags) if flag in ISSUE_FLAG_MAP)
        if "partial_run_lap" in flags:
            flags.add("missing_required_data")
        cuts = data.get("last_lap_cuts_count")
        if isinstance(cuts, (int, float)) and not isinstance(cuts, bool) and cuts > 0:
            flags.add("off_track")
        if data.get("valid_native") is False:
            flags.add("native_invalid")
        if data.get("lap_time_s") is None:
            flags.add("missing_required_data")
        return sorted(flags)

    def _annotate(self, record: dict, phase: str, eligible: bool, flags: list[str], reason: str) -> None:
        annotate = getattr(self.store, "annotate_lap", None)
        if annotate is not None:
            annotation_phase = phase if phase in {"program", "warmup", "outlap", "pit"} else "unknown"
            annotate(record["run_id"], record["seq"], annotation_phase, eligible,
                     flags, reason if annotation_phase == phase else f"{phase}: {reason}")

    def _lap(self, record: dict) -> None:
        data = record["data"]
        run_id = record["run_id"]
        phase = self.scheduler.state["phase"]
        native_index = data.get("session_index")
        previous_index = self.session_indices.get(run_id)
        if previous_index is None and native_index is not None:
            self.session_indices[run_id] = native_index
        elif native_index is not None and previous_index != native_index:
            self._session_break(record, "session index changed across completed laps")
            self._annotate(record, "interrupted", False, ["session_changed"], "native session changed")
            return
        flags = self._quality_flags(data)
        if phase == "calibration":
            if self.scheduler.state["active_run_id"] != run_id:
                self._annotate(record, "unknown", False, flags + ["run_mismatch"],
                               "physical run does not match calibration block")
                return
            if self.pending_learning:
                self._reject_oldest_learning(record, "new lap arrived before acceptance confirmation")
            if self.scheduler.state["phase"] != "calibration":
                self._annotate(record, "unknown", False, flags,
                               "calibration is no longer active for this run")
                return
            self.pending_learning.append(record)
            self._note("Learning lap recorded. Confirm RSS acceptance for this lap in the overlay.")
            return
        if phase != "record_block":
            self._annotate(record, "warmup" if phase == "warmup" else phase, False,
                           flags, "outside scheduled program recording")
            return
        if self.scheduler.state["active_run_id"] != run_id:
            self._annotate(record, "interrupted", False, flags + ["run_mismatch"],
                           "physical run does not match prepared block")
            return
        lap_index = data.get("lap_index")
        uid = f"{run_id}:{native_index}:{lap_index}"
        accepted = False if data.get("valid_native") is False else None
        block = self.scheduler.current_block
        if block and block["kind"] == "management":
            cues = self._active_management_cues()
            if not cues or cues.get("status") != "ready":
                flags.append("management_cues_unavailable")
                accepted = False
        result = self._dispatch("lap", record, lap_uid=uid, native_session_id=run_id,
                                lap_time_s=data.get("lap_time_s"), quality_flags=flags,
                                accepted=accepted, native_lap_index=lap_index)
        eligible = result["data"].get("eligible_for_comparison") is True
        self._annotate(record, "program", eligible, result["data"].get("quality_flags", flags),
                       "scheduler accepted" if eligible else "quality or validity exclusion")
        if self.scheduler.state["phase"] == "map_transition":
            self._note("Two deployment laps complete; change to the configured recovery map at this boundary.")

    def _reject_oldest_learning(self, trigger: dict, reason: str) -> None:
        if not self.pending_learning:
            return
        lap_record = self.pending_learning.pop(0)
        data = lap_record["data"]
        flags = self._quality_flags(data) + ["learning_acceptance_unconfirmed"]
        run_id = lap_record["run_id"]
        uid = f"{run_id}:{data.get('session_index')}:{data.get('lap_index')}"
        if self.scheduler.state["phase"] == "calibration":
            self._dispatch("lap", trigger, lap_uid=uid, native_session_id=run_id,
                           lap_time_s=data.get("lap_time_s"), quality_flags=flags,
                           accepted=False, native_lap_index=data.get("lap_index"))
        self._annotate(lap_record, "program", False, flags, reason)

    def _confirm_learning(self, record: dict) -> None:
        if self.scheduler.state["phase"] != "calibration" or not self.pending_learning:
            self._note("No pending calibration lap can be confirmed.")
            return
        lap_record = self.pending_learning.pop(0)
        data = lap_record["data"]
        flags = self._quality_flags(data)
        index = data.get("session_index")
        uid = f"{lap_record['run_id']}:{index}:{data.get('lap_index')}"
        accepted = data.get("valid_native") is not False and data.get("lap_time_s") is not None
        result = self._dispatch("lap", record, lap_uid=uid, native_session_id=lap_record["run_id"],
                                lap_time_s=data.get("lap_time_s"), quality_flags=flags,
                                accepted=accepted, native_lap_index=data.get("lap_index"),
                                learning_confirmation="driver_attestation")
        eligible = result["data"].get("eligible_for_comparison") is True
        self._annotate(lap_record, "program", eligible, result["data"].get("quality_flags", flags),
                       "RSS acceptance driver-confirmed" if eligible else "learning lap excluded")

    def _flush_pending_learning(self, reason: str) -> None:
        while self.pending_learning:
            lap_record = self.pending_learning.pop(0)
            flags = self._quality_flags(lap_record["data"]) + ["learning_acceptance_unconfirmed"]
            self._annotate(lap_record, "program", False, flags, reason)

    def _session_break(self, record: dict, reason: str) -> None:
        phase = self.scheduler.state["phase"]
        if phase in {"preflight", "prepare_run", "verify_settings", "warmup", "calibration",
                     "record_block", "map_transition", "box_save", "analyze", "next_block", "paused"}:
            self._flush_pending_learning(reason)
            self._dispatch("interrupt", record, reason=reason)
        self._note("Native physical state changed. End this run and start a fresh prepared run.")

    def _process_event(self, record: dict) -> None:
        data = record.get("data") or {}
        event_type = data.get("type")
        if (record["run_id"] in self.legacy_diagnostic_runs
                or self._is_ai_thermal_run(record["run_id"])
                or self._is_collection_run(record["run_id"])):
            return
        start_choice = event_type in {"ui_start_full", "ui_individual"} or (
            event_type == "ui_resume" and not data.get("from_pause"))
        if not start_choice and record["run_id"] != self.live_run_id:
            self._note("Event from a diagnostic or stale physical run did not change campaign progress.")
            return
        if event_type in {"ui_start_full", "ui_individual"} or (event_type == "ui_resume" and not data.get("from_pause")):
            self._start_choice(record)
        elif event_type == "ui_confirm":
            self._confirm(record)
        elif event_type == "ui_confirm_learning":
            self._confirm_learning(record)
        elif event_type == "ui_confirm_profile":
            if self.scheduler.state["phase"] not in {"calibration", "box_save"}:
                self._note("Profile confirmation is only valid after calibration laps.")
                return
            mode = data.get("mode")
            if mode not in {"FRZ_AUTO", "MANUAL_APPLIED"}:
                self._note("Profile mode not specified; no profile confirmation recorded.")
                return
            self._dispatch("confirm_profile", record, mode=mode, observed_mode=None,
                           manual_confirmation=True, applied=mode == "MANUAL_APPLIED",
                           profile_id=data.get("profile_name") if mode == "MANUAL_APPLIED" else None)
        elif event_type == "lap_complete":
            self._lap(record)
        elif event_type in SESSION_BREAK_EVENTS:
            self._session_break(record, event_type)
        elif event_type == "ui_pause":
            if self.scheduler.state["phase"] in {"preflight", "prepare_run", "verify_settings", "warmup",
                                                   "calibration", "record_block", "map_transition", "box_save",
                                                   "analyze", "next_block"}:
                self._dispatch("pause", record)
        elif event_type == "ui_resume" and data.get("from_pause"):
            if self.scheduler.state["phase"] == "paused":
                self._dispatch("resume", record, native_session_id=record["run_id"])
        elif event_type == "ui_skip":
            block = self.scheduler.current_block
            reason = data.get("reason")
            if self.live_run_id != record["run_id"] or block is None or data.get("block_id") != block["id"]:
                self._note("Skip ignored because the physical run or block does not match the director.")
            elif not isinstance(reason, str) or not reason.strip():
                self._note("Skip requires a driver-entered reason; block remains active.")
            else:
                self._flush_pending_learning("block explicitly skipped before learning confirmation")
                self._dispatch("skip", record, reason=reason.strip(),
                               unavoidable=block["kind"] == "validation" and data.get("unavoidable") is True)
                self._note("Block explicitly skipped. End this physical run before the next block.")
        elif event_type == "ui_end":
            self._flush_pending_learning("run ended before learning acceptance")
            if self.scheduler.state["phase"] in {"driver_confirmation", "unavailable_capability"}:
                self._dispatch("resolve_capability", record, fallback="analysis_only")
            if self.scheduler.state["phase"] not in {"idle", "next_block", "review_export", "complete", "ended"}:
                self._dispatch("end_run", record, reason=data.get("reason") or "driver ended run")
        elif event_type == "ui_issue":
            issue = str(data.get("issue") or "Issue")
            detail = str(data.get("detail") or "")
            self._dispatch("report_issue", record, issue=f"{issue}: {detail}".strip())
        elif event_type == "recording_gap":
            self._dispatch("report_issue", record, issue="recording gap reported by CSP bridge")

    def _publish_state(self) -> dict:
        view = self.scheduler.view()
        current = self._read_current()
        self.last_current_state = current
        current_run_id = current.get("run_id") if isinstance(current.get("run_id"), str) else None
        ended_unassigned_run = (current_run_id is not None and
                                current_run_id != self.live_run_id and
                                current.get("status") == "ended" and
                                not self._is_collection_run(current_run_id))
        bridge_run_id = self.live_run_id if ended_unassigned_run else (current_run_id or self.live_run_id)
        if current_run_id and current_run_id != self.live_run_id and not ended_unassigned_run:
            manifest = self._run_manifest(current_run_id)
            if manifest.get("selection") == "collection":
                track_id = current.get("track_id") or manifest.get("track_id") or "unknown"
                layout_id = current.get("layout_id") or manifest.get("layout_id")
                track_label = f"{track_id} / {layout_id}" if layout_id else str(track_id)
                stint_id = current.get("collection_stint_id")
                active_stint = isinstance(stint_id, str) and bool(stint_id)
                compound = current.get("observed_tyre_compound")
                requested = current.get("requested_tyre_compound")
                tire_label = current.get("planned_tire_label")
                condition = current.get("condition_label")
                collection_ended = current.get("status") == "ended"
                controller = ("AI" if current.get("is_ai_controlled") is True else
                              "human" if current.get("is_user_controlled") is True else "unknown")
                conditions = [f"observed tire {compound or 'unavailable'}",
                              f"requested tire {requested or 'unspecified'}",
                              f"controller {controller}"]
                if tire_label:
                    conditions.insert(0, f"label {tire_label}")
                if condition:
                    conditions.append(f"condition {condition}")
                status = {
                    "schema": 1, "run_id": current_run_id, "block_id": None,
                    "state": ("collection_saved" if collection_ended else
                              "collection_stint_recording" if active_stint else "collection_ready"),
                    "diagnostic_only": False, "collection_mode": True,
                    "campaign_counted": False,
                    "collection_id": current.get("collection_id") or manifest.get("collection_id") or current_run_id,
                    "collection_stint_id": stint_id if active_stint else None,
                    "instruction": (f"Collection saved for {track_label}. Review the recorded stints and "
                                    "their observed conditions." if collection_ended else
                                    f"Collecting {track_label} stint {stint_id}. Preserve this tire and condition "
                                    "until the planned pit stop." if active_stint else
                                    f"Data collection ready at {track_label}. Select a tire and condition, "
                                    "then start a stint."),
                    "lap_progress": "Recorded laps and samples are retained without a fixed lap target.",
                    "target": "; ".join(conditions),
                    "next_action": ("Review or export the saved collection. A new game session starts a new "
                                    "physical record." if collection_ended else
                                    "When ending the stint, take manual control before disabling AI at speed; "
                                    "box, change tires, then start the next stint." if active_stint else
                                    "Start a collection stint after confirming the mounted tire."),
                    "recording_status": ("unavailable" if current.get("write_ok") is False else
                                         "ended" if current.get("status") == "ended" else "recording"),
                    "calculation_mode": "observed_data_collection",
                    "physical_effect_status": "observed_car_state",
                    "mode": "DATA_COLLECTION", "ai_control_status": (
                        "observed_on" if current.get("is_ai_controlled") is True else
                        "observed_off" if current.get("is_ai_controlled") is False else "unavailable"),
                    "pending_learning_laps": 0, "unconfigured_targets": [], "management_cues": None,
                    "campaign_notice": self.legacy_campaign_notice,
                    "last_message": self.messages[-1] if self.messages else None,
                    "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                _atomic_json(self.bridge_root / "director_state.json", status)
                return status
            ai_thermal = manifest.get("selection") == "ai_thermal"
            ai_stopping = ai_thermal and current.get("status") == "ending_ai"
            observed_ai = current.get("is_ai_controlled")
            ai_status = ("observed_on" if observed_ai is True else
                         "observed_off" if observed_ai is False else "unavailable")
            track_id = current.get("track_id") or manifest.get("track_id") or "unknown"
            layout_id = current.get("layout_id") or manifest.get("layout_id")
            track_label = f"{track_id} / {layout_id}" if layout_id else str(track_id)
            recording_status = ("unavailable" if current.get("write_ok") is False else
                                "ended" if current.get("status") == "ended" else "recording")
            status = {
                "schema": 1, "run_id": current_run_id, "block_id": None,
                "state": ("diagnostic_ai_thermal_stopping" if ai_stopping else
                          "diagnostic_ai_thermal_recording" if ai_thermal else
                          "diagnostic_recording"),
                "diagnostic_only": True,
                "instruction": (f"AI thermal diagnostic on {track_label}: stop AI control and verify "
                                "native readback OFF before another campaign run. No campaign laps "
                                "are counted." if ai_stopping else
                                f"AI thermal diagnostic recording on {track_label}. Engage the configured "
                                "AI driver control and check the native AI-control readback. "
                                "No campaign laps are counted." if ai_thermal else
                                f"Diagnostic recording on {track_label}. This physical run is not assigned "
                                "to a WIMNA campaign block."),
                "lap_progress": "—",
                "target": (("Native AI control observed ON; record AI workload and thermal inputs"
                            if ai_status == "observed_on" else
                            "Native AI control observed OFF; engage Toggle AI Driver before an AI trace"
                            if ai_status == "observed_off" else
                            "AI control readback unavailable; do not label this trace AI-driven")
                           if ai_thermal else "No campaign lap target"),
                "next_action": ("Wait for native AI control OFF; use the configured Toggle AI Driver "
                                "binding if automatic stop fails." if ai_stopping else
                                "Record the intended diagnostic stint, then End Run. Compare AI traces "
                                "separately from Tyler's campaign data." if ai_thermal else
                                "End this diagnostic run, then select the configured campaign "
                                "track/layout and start a new WIMNA run."),
                "recording_status": recording_status,
                "calculation_mode": ("diagnostic_ai_thermal_recording" if ai_thermal
                                     else "diagnostic_recording"),
                "physical_effect_status": "unavailable",
                "mode": "AI_THERMAL_DIAGNOSTIC" if ai_thermal else "DIAGNOSTIC",
                "ai_control_status": ai_status if ai_thermal else None,
                "pending_learning_laps": 0,
                "unconfigured_targets": [], "management_cues": None,
                "campaign_notice": self.legacy_campaign_notice,
                "last_message": self.messages[-1] if self.messages else None,
                "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            _atomic_json(self.bridge_root / "director_state.json", status)
            return status
        progress = view.get("progress") or {}
        if "accepted_learning_laps" in progress:
            lap_progress = f"{progress['accepted_learning_laps']}/{progress['accepted_required']} accepted; {progress['attempted_laps']} attempted"
        elif progress:
            lap_progress = f"{progress['accepted_laps']}/{progress['planned_laps']} accepted; {progress['attempted_laps']} attempted"
        else:
            lap_progress = "—"
        target = view.get("target")
        if isinstance(target, dict):
            target = (f"{progress.get('planned_laps')} accepted flying laps; "
                      f"{target.get('map_phase')} map: {target.get('configured_map') or 'unconfigured'}")
        next_action = view.get("next_action") or "Review campaign"
        missing_targets = self._missing_targets(self.scheduler.current_block) if self.scheduler.current_block else []
        instruction = view.get("instruction") or "Follow the current run card."
        if view["phase"] == "verify_settings":
            requested = self._requested_settings(self.scheduler.current_block)
            targets = "; ".join(f"{key}={value}" for key, value in requested.items())
            instruction = "Check these values in RSS/pits, then press Confirm / Next only after applying them: " + targets
            next_action = "Manual driver attestation. No automatic setting readback is claimed."
        if view["phase"] == "unavailable_capability" and missing_targets:
            next_action = "Missing targets: " + ", ".join(missing_targets) + ". Confirm for analysis-only/incomplete fallback."
        if self.pending_learning:
            next_action = f"Confirm RSS acceptance for {len(self.pending_learning)} pending learning lap(s)."
        if view["phase"] == "next_block":
            next_action = "End this physical run; start a fresh WIMNA run for the next block."
        if view["phase"] == "interrupted":
            next_action = "End this run and start a fresh run; physical tire/fuel state cannot continue."
        if view["phase"] == "map_transition" and self.scheduler.config["policy"].get("recovery_map") is None:
            next_action = "Recovery map is unconfigured. Confirm to mark this transition analysis-only."
        management = self._active_management_cues()
        if self.scheduler.current_block and self.scheduler.current_block["kind"] == "management":
            if management and management.get("status") == "ready":
                instructions = [cue["instruction"] for cue in management["cues"]]
                if view["phase"] in {"warmup", "record_block"}:
                    instruction = "Management: " + " ".join(instructions)
                    target = "; ".join(f"cue {cue['number']} at {cue['distance_m']:g} m"
                                       for cue in management["cues"])
                    next_action = "Follow both distance cues on each measured M lap. " + "; ".join(management["limitations"])
                elif view["phase"] == "verify_settings":
                    next_action += " Cue plan: " + "; ".join(instructions)
            elif view["phase"] != "prepare_run":
                instruction = "Reference telemetry did not yield two defensible management cues."
                next_action = ("M laps remain incomplete. Review missing reference channels, then enter a reason "
                               "and use Skip Incomplete Block if this block cannot be completed.")
        status = {
            "schema": 1, "run_id": bridge_run_id, "block_id": view.get("block_id"),
            "state": view["phase"], "diagnostic_only": False, "instruction": instruction,
            "lap_progress": lap_progress, "target": target or "Numeric target pending configuration",
            "next_action": next_action,
            "recording_status": view.get("recording_status"), "calculation_mode": "live_director",
            "physical_effect_status": "unverified" if view["phase"] != "idle" else "unavailable",
            "mode": view.get("mode"), "pending_learning_laps": len(self.pending_learning),
            "unconfigured_targets": missing_targets,
            "management_cues": management,
            "campaign_notice": self.legacy_campaign_notice,
            "last_message": self.messages[-1] if self.messages else None,
            "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _atomic_json(self.bridge_root / "director_state.json", status)
        return status

    def poll(self) -> dict:
        """Ingest completed chunks, advance each event once, publish a run-ID-bound view."""
        self.last_ingest = ingest_bridge(self.bridge_root, self.store)
        # The Store copy is immutable after ingestion; prefer it to a bridge file
        # that could disappear before an interrupted poll finishes.
        self.run_manifests.update({run["run_id"]: run["manifest"] for run in self.store.iter_runs()})
        for error in self.last_ingest.get("errors", []):
            self._note(f"Bridge ingest error: {error['error']}")
        for record in self.store.iter_records(kind="event"):
            run_id, seq = record["run_id"], record["seq"]
            if seq <= self.processed.get(run_id, -1):
                continue
            try:
                self._process_event(record)
            except (SchedulerError, ValueError, KeyError, TypeError) as exc:
                self._note(f"Event {run_id}/{seq} rejected: {exc}")
            self.processed[run_id] = seq
            self._save()
        state = self._publish_state()
        self._save()
        return {"director": state, "ingest": self.last_ingest,
                "messages": self.messages[-5:], "counts": self.store.counts()}

    def configure(self, config: Mapping[str, Any]) -> None:
        """Apply one-time campaign setup before any measured run begins."""
        if self.scheduler.state["phase"] != "idle":
            raise ValueError("active campaign configuration is frozen; export/review before a new campaign")
        self.scheduler = CampaignScheduler(config)
        self.legacy_campaign_notice = None
        self._save()
        self._publish_state()
