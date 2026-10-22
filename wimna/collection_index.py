"""Deterministic collection-stint views over immutable bridge records.

These views are deliberately independent of the campaign scheduler.  Both a
person and native AI produce measured car-state samples; the controller is an
observed condition on each sample, not a separate evidence class.  A native
session reset always breaks a stint, even if the app repeats its stint ID.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _channel(data: Mapping[str, Any], name: str, unit: str | None = None) -> Any:
    field = data.get(name)
    if not isinstance(field, Mapping):
        return None
    if unit is not None and field.get("unit") != unit:
        return None
    if field.get("quality") in {"unavailable", "stale", "invalid"}:
        return None
    if field.get("source") in {"RSS_UNVERIFIED", "UNVERIFIED", "ASSUMED"}:
        return None
    value = field.get("value")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _numeric(data: Mapping[str, Any], name: str, unit: str) -> float | None:
    value = _channel(data, name, unit)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _boolean(data: Mapping[str, Any], name: str) -> bool | None:
    value = _channel(data, name, "boolean")
    return value if isinstance(value, bool) else None


def _controller(data: Mapping[str, Any]) -> str:
    ai = _boolean(data, "is_ai_controlled")
    human = _boolean(data, "is_user_controlled")
    if ai is True and human is True:
        return "CONFLICT"
    if ai is True:
        return "AI"
    if human is True:
        return "HUMAN"
    return "UNKNOWN"


def _boundary_value(info: Mapping[str, Any], *names: str) -> Any:
    """Use only the boundary readback; a run-start value may have gone stale."""
    for name in names:
        value = info.get(name)
        if value is not None and not isinstance(value, (dict, list)):
            return value
    return None


def _session(record: Mapping[str, Any], manifest: Mapping[str, Any], run_id: str,
             previous: str | None) -> str:
    return (_label(record.get("physical_session_id"))
            or _label((record.get("data") or {}).get("physical_session_id"))
            or previous or _label(manifest.get("physical_session_id")) or run_id)


def scan_collection(store: Store, run_id: str | None = None) -> Iterator[tuple[str, dict]]:
    """Yield (``sample`` or ``stint``, projection) in raw sequence order.

    Explicit ``collection_stint_start/end`` events take priority.  Older runs
    without collection events receive clearly marked implicit stints.  Raw
    records remain unchanged; these views can be rebuilt after an interruption.
    """
    for run in store.iter_runs():
        rid = run["run_id"]
        if run_id is not None and rid != run_id:
            continue
        manifest = run["manifest"]
        collection_id = _label(manifest.get("collection_id")) or rid
        collection_enabled = manifest.get("collection_enabled") is True
        physical: str | None = None
        active: dict | None = None
        implicit_number = 0
        last_seq: int | None = None
        last_sim_time: float | None = None

        def open_stint(record: Mapping[str, Any], identifier: str, source: str,
                       event_data: Mapping[str, Any] | None = None) -> dict:
            info = event_data or {}
            requested_setup = (_label(info.get("setup_label_requested"))
                               or _label(manifest.get("setup_label_requested")))
            return {
                "run_id": rid,
                "evidence_mode": run["mode"],
                "car_id": manifest.get("car_id"),
                "track_id": manifest.get("track_id"),
                "layout_id": manifest.get("layout_id"),
                "physical_session_id": physical,
                "collection_id": collection_id,
                "collection_stint_id": identifier,
                "boundary_source": source,
                "start_seq": record["seq"],
                "end_seq": None,
                "start_sim_time_s": record.get("sim_time_s"),
                "end_sim_time_s": None,
                "status": "open_or_interrupted",
                "start_reason": _label(info.get("reason")),
                "end_reason": None,
                "requested_tyre_compound": _label(info.get("requested_tyre_compound"))
                    or _label(manifest.get("requested_tyre_compound")),
                "planned_tire_label": _label(info.get("planned_tire_label"))
                    or _label(manifest.get("planned_tire_label")),
                "condition_label": _label(info.get("condition_label"))
                    or _label(manifest.get("condition_label")),
                "setup_label_requested": requested_setup,
                "setup_label_source": (_label(info.get("setup_label_source"))
                    or _label(manifest.get("setup_label_source"))
                    or ("DRIVER_ENTERED_UNVERIFIED" if requested_setup else None)),
                "setup_hash": None,
                "session_mode_observed": _boundary_value(
                    info, "stint_session_mode", "session_mode_id", "session_mode"),
                "extended_physics_observed": _boundary_value(
                    info, "stint_extended_physics", "extended_physics_observed", "extended_physics"),
                "session_mode_observed_at_end": None,
                "extended_physics_observed_at_end": None,
                "session_mode_changed_within_stint": None,
                "extended_physics_changed_within_stint": None,
                "observed_tyre_compound_at_start": (
                    _label(info.get("observed_tyre_compound_at_start"))
                    or _label(info.get("observed_tyre_compound"))),
                "observed_air_c_at_start": info.get("observed_air_c"),
                "observed_track_c_at_start": info.get("observed_track_c"),
                "observed_tyre_compounds": [],
                "sample_count": 0,
                "ai_sample_count": 0,
                "human_sample_count": 0,
                "unknown_controller_sample_count": 0,
                "conflicting_controller_sample_count": 0,
                "lap_count": 0,
                "pit_transition_count": 0,
                "pit_sample_count": 0,
                "handoff_request_count": 0,
                "compound_change_count": 0,
                "compound_label_mismatch_count": 0,
            }

        def close_stint(status: str, record: Mapping[str, Any] | None = None) -> dict | None:
            nonlocal active
            if active is None:
                return None
            active["status"] = status
            active["end_seq"] = record.get("seq") if record else last_seq
            active["end_sim_time_s"] = record.get("sim_time_s") if record else last_sim_time
            closed, active = active, None
            return closed

        for record in store.iter_records(rid):
            seq = record["seq"]
            session = _session(record, manifest, rid, physical)
            if physical is not None and session != physical:
                closed = close_stint("physical_session_changed")
                if closed is not None:
                    yield "stint", closed
                implicit_number = 0
            physical = session
            last_seq = seq
            last_sim_time = record.get("sim_time_s")
            data = record.get("data") or {}
            if record["kind"] == "event":
                event_type = data.get("type")
                if event_type == "collection_stint_start":
                    closed = close_stint("superseded_by_new_stint", record)
                    if closed is not None:
                        yield "stint", closed
                    identifier = _label(data.get("collection_stint_id"))
                    if identifier is None:
                        implicit_number += 1
                        identifier = f"event_missing_id:{implicit_number}"
                    active = open_stint(record, identifier, "explicit_start_event", data)
                elif event_type == "collection_stint_end":
                    identifier = _label(data.get("collection_stint_id"))
                    if active is not None and (identifier is None or identifier == active["collection_stint_id"]):
                        active["end_reason"] = _label(data.get("reason"))
                        end_mode = _boundary_value(data, "session_mode_id", "session_mode")
                        end_physics = _boundary_value(data, "extended_physics_observed", "extended_physics")
                        active["session_mode_observed_at_end"] = end_mode
                        active["extended_physics_observed_at_end"] = end_physics
                        start_mode = active["session_mode_observed"]
                        start_physics = active["extended_physics_observed"]
                        active["session_mode_changed_within_stint"] = (
                            start_mode != end_mode if start_mode is not None and end_mode is not None else None)
                        active["extended_physics_changed_within_stint"] = (
                            start_physics != end_physics if start_physics is not None and end_physics is not None else None)
                        closed = close_stint("complete", record)
                        if closed is not None:
                            yield "stint", closed
                elif event_type in {"session_changed", "car_reset", "lap_counter_reset", "lap_counter_jump"}:
                    closed = close_stint("physical_state_changed", record)
                    if closed is not None:
                        yield "stint", closed
                elif event_type == "lap_complete" and active is not None:
                    event_stint = _label(data.get("collection_stint_id"))
                    if event_stint is None or event_stint == active["collection_stint_id"]:
                        active["lap_count"] += 1
                elif event_type == "collection_pit_transition" and active is not None:
                    active["pit_transition_count"] += 1
                elif event_type == "collection_handoff_requested" and active is not None:
                    active["handoff_request_count"] += 1
                elif event_type == "collection_compound_changed" and active is not None:
                    active["compound_change_count"] += 1
                elif event_type == "collection_compound_label_mismatch" and active is not None:
                    active["compound_label_mismatch_count"] += 1
                elif event_type == "ui_end":
                    closed = close_stint("run_ended_without_stint_end", record)
                    if closed is not None:
                        yield "stint", closed
                continue
            if record["kind"] != "sample":
                continue

            sample_stint = _label(_channel(data, "collection_stint_id"))
            if sample_stint is not None and (active is None or sample_stint != active["collection_stint_id"]):
                closed = close_stint("sample_stint_id_changed", record)
                if closed is not None:
                    yield "stint", closed
                active = open_stint(record, sample_stint, "sample_stint_id")
            elif active is None and not collection_enabled:
                implicit_number += 1
                active = open_stint(record, f"legacy_implicit:{implicit_number}", "legacy_implicit")

            controller = _controller(data)
            observed_compound = _label(_channel(data, "tyre_compound", "name"))
            air = _numeric(data, "ambient_air_c", "degC")
            track = _numeric(data, "track_surface_c", "degC")
            in_pitlane = _boolean(data, "in_pitlane")
            in_pit = _boolean(data, "in_pit")
            if active is not None:
                active["sample_count"] += 1
                active[{"AI": "ai_sample_count", "HUMAN": "human_sample_count",
                        "UNKNOWN": "unknown_controller_sample_count",
                        "CONFLICT": "conflicting_controller_sample_count"}[controller]] += 1
                if observed_compound is not None and observed_compound not in active["observed_tyre_compounds"]:
                    active["observed_tyre_compounds"].append(observed_compound)
                if in_pitlane is True or in_pit is True:
                    active["pit_sample_count"] += 1
            yield "sample", {
                "run_id": rid,
                "evidence_mode": run["mode"],
                "car_id": manifest.get("car_id"),
                "track_id": manifest.get("track_id"),
                "layout_id": manifest.get("layout_id"),
                "seq": seq,
                "sim_time_s": record.get("sim_time_s"),
                "physical_session_id": physical,
                "collection_id": collection_id,
                "collection_stint_id": active["collection_stint_id"] if active else None,
                "controller_observed": controller,
                "requested_tyre_compound": active["requested_tyre_compound"] if active else None,
                "planned_tire_label": active["planned_tire_label"] if active else None,
                "condition_label": active["condition_label"] if active else None,
                "setup_label_requested": active["setup_label_requested"] if active else None,
                "setup_label_source": active["setup_label_source"] if active else None,
                "setup_hash": None,
                "session_mode_observed": active["session_mode_observed"] if active else None,
                "extended_physics_observed": active["extended_physics_observed"] if active else None,
                "observed_tyre_compound": observed_compound,
                "ambient_air_c": air,
                "track_surface_c": track,
                "in_pitlane": in_pitlane,
                "in_pit": in_pit,
                "record": record,
            }
        closed = close_stint("open_or_interrupted")
        if closed is not None:
            yield "stint", closed
