"""Data-first summaries of physical stints, independent of the driving controller.

The controller is an observed covariate.  An AI-driven car still produces real
simulator telemetry; a replay fixture is the separate SYNTHETIC evidence mode.
No tire difference is promoted to a causal effect by this module.
"""

from __future__ import annotations

import math
import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from .collection_index import scan_collection
from .models import ThermalSample, ThermalScenario, run_thermal_scenario, thermal_assumption_profiles
from .package_analysis import analyze_package_pairs, prototype_from_samples
from .store import Store


WHEELS = ("fl", "fr", "rl", "rr")
# Pace labels require near-complete observed control coverage.  The adaptive
# gap allowance accommodates a deliberately reduced recorder rate while still
# rejecting an unnoticed controller handoff or a truncated lap trace.
LAP_MIN_COVERAGE_FRACTION = 0.95
LAP_ENDPOINT_TOLERANCE_S = 2.0
LAP_MIN_CONTROLLER_SAMPLES = 20
LAP_MAX_GAP_FLOOR_S = 0.5
LAP_MAX_GAP_CEILING_S = 2.0
LAP_STABLE_CONTROLLER_FRACTION = 0.99
LAP_FAULTS = frozenset({
    "off_track", "spin", "major_spin", "contact", "damage", "paused",
    "experiment_paused", "sim_paused", "reset", "car_reset", "car_jump",
    "pit", "pitlane", "pit_lap", "partial_run_lap", "session_changed",
    "lap_counter_reset", "lap_counter_jump", "physical_state_changed",
    "missing_required_data", "carryover_unrestored", "unexpected_setting_change",
    "compound_changed_within_stint",
})


def _number(value: Any) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _channel(data: Mapping[str, Any], name: str, unit: str) -> Any:
    raw = data.get(name)
    if not isinstance(raw, Mapping) or raw.get("unit") != unit or raw.get("quality") != "available":
        return None
    return raw.get("value")


def _numeric_channel(data: Mapping[str, Any], name: str, unit: str) -> float | None:
    return _number(_channel(data, name, unit))


def _range(values: list[float]) -> dict[str, Any]:
    return {"first": values[0] if values else None,
            "last": values[-1] if values else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "median": median(values) if values else None,
            "sample_count": len(values)}


def _record_data(item: Mapping[str, Any]) -> dict[str, Any]:
    record = item.get("record") or {}
    data = record.get("data")
    return data if isinstance(data, dict) else {}


def _controller_for_lap(samples: list[dict[str, Any]], start: float, end: float) -> tuple[str, dict[str, int]]:
    counts = {"AI": 0, "HUMAN": 0, "UNKNOWN": 0, "CONFLICT": 0}
    for sample in samples:
        when = _number(sample.get("sim_time_s"))
        if when is not None and start <= when <= end:
            value = sample.get("controller_observed") or "UNKNOWN"
            counts[value if value in counts else "UNKNOWN"] += 1
    observed = sum(counts.values())
    if observed == 0:
        return "UNKNOWN", counts
    if counts["AI"] / observed >= LAP_STABLE_CONTROLLER_FRACTION:
        return "AI", counts
    if counts["HUMAN"] / observed >= LAP_STABLE_CONTROLLER_FRACTION:
        return "HUMAN", counts
    return "MIXED", counts


def _lap_sample_coverage(samples: list[dict[str, Any]], start: float, end: float) -> dict[str, Any]:
    times = sorted(when for item in samples
                   if (when := _number(item.get("sim_time_s"))) is not None and start <= when <= end)
    duration = end - start
    gaps = [right - left for left, right in zip(times, times[1:]) if right > left]
    median_interval = median(gaps) if gaps else None
    max_gap = max(gaps) if gaps else None
    allowed_gap = (min(LAP_MAX_GAP_CEILING_S,
                       max(LAP_MAX_GAP_FLOOR_S, 4 * median_interval))
                   if median_interval is not None else None)
    span_fraction = (times[-1] - times[0]) / duration if len(times) >= 2 and duration > 0 else None
    first_offset = times[0] - start if times else None
    last_offset = end - times[-1] if times else None
    sufficient = (len(times) >= LAP_MIN_CONTROLLER_SAMPLES and
                  span_fraction is not None and span_fraction >= LAP_MIN_COVERAGE_FRACTION and
                  first_offset is not None and first_offset <= LAP_ENDPOINT_TOLERANCE_S and
                  last_offset is not None and last_offset <= LAP_ENDPOINT_TOLERANCE_S and
                  max_gap is not None and allowed_gap is not None and max_gap <= allowed_gap)
    return {"sufficient": sufficient, "sample_count": len(times),
            "span_fraction": span_fraction, "first_sample_offset_s": first_offset,
            "last_sample_offset_s": last_offset, "maximum_gap_s": max_gap,
            "allowed_maximum_gap_s": allowed_gap,
            "policy": {"minimum_span_fraction": LAP_MIN_COVERAGE_FRACTION,
                       "endpoint_tolerance_s": LAP_ENDPOINT_TOLERANCE_S,
                       "minimum_samples": LAP_MIN_CONTROLLER_SAMPLES,
                       "maximum_gap_floor_s": LAP_MAX_GAP_FLOOR_S,
                       "maximum_gap_ceiling_s": LAP_MAX_GAP_CEILING_S,
                       "stable_controller_fraction": LAP_STABLE_CONTROLLER_FRACTION}}


def _laps_for_stint(store: Store, stint: Mapping[str, Any], samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start_seq = stint.get("start_seq")
    end_seq = stint.get("end_seq")
    start_sim = _number(stint.get("start_sim_time_s"))
    result = []
    for event in store.iter_records(stint["run_id"], "event"):
        if event["data"].get("type") != "lap_complete":
            continue
        if start_seq is not None and event["seq"] < start_seq:
            continue
        if end_seq is not None and event["seq"] > end_seq:
            continue
        data = event["data"]
        explicit_stint = data.get("collection_stint_id")
        if explicit_stint is not None and explicit_stint != stint["collection_stint_id"]:
            continue
        explicit_physical = event.get("physical_session_id")
        if explicit_physical is not None and explicit_physical != stint["physical_session_id"]:
            continue
        lap_time = _number(data.get("lap_time_s"))
        lap_end = _number(event.get("sim_time_s"))
        lap_start = lap_end - lap_time if lap_end is not None and lap_time is not None else None
        flags = sorted({str(item) for item in (data.get("flags") or [])})
        reasons = sorted(LAP_FAULTS.intersection(flags))
        if lap_time is None or lap_time <= 0 or lap_start is None:
            reasons.append("lap_time_unavailable")
        if lap_start is not None and start_sim is not None and lap_start < start_sim - 0.05:
            reasons.append("started_before_stint")
        controller, controller_counts = (
            _controller_for_lap(samples, lap_start, lap_end)
            if lap_start is not None and lap_end is not None else ("UNKNOWN", {}))
        coverage = (_lap_sample_coverage(samples, lap_start, lap_end)
                    if lap_start is not None and lap_end is not None else None)
        if coverage is None or not coverage["sufficient"]:
            reasons.append("insufficient_lap_sample_coverage")
        if controller not in {"AI", "HUMAN"}:
            reasons.append("controller_not_stable_or_unobserved")
        cuts = _number(data.get("last_lap_cuts_count"))
        if cuts is not None and cuts > 0:
            reasons.append("native_cuts")
        native_valid = data.get("valid_native")
        ai_native_invalid = native_valid is False and controller == "AI" and cuts == 0
        if not isinstance(native_valid, bool):
            reasons.append("native_validity_unavailable")
        elif native_valid is False and not ai_native_invalid:
            reasons.append("native_invalid_unexplained")
        # Native validity can be false under AI for several reasons.  A fully
        # observed zero-cut lap remains useful as exploratory pace evidence,
        # but cannot be called a clean comparable lap without the exact cause.
        comparable = not reasons and native_valid is True
        exploratory = not reasons and ai_native_invalid
        pace_use = ("clean_comparable" if comparable else
                    "exploratory_ai_native_invalid" if exploratory else "excluded")
        if ai_native_invalid:
            reasons.append("native_invalid_ai_origin_unverified")
        result.append({"lap_index": data.get("lap_index"), "lap_time_s": lap_time,
                       "sim_start_s": lap_start, "sim_end_s": lap_end,
                       "controller_observed": controller,
                       "controller_sample_counts": controller_counts,
                       "sample_coverage": coverage,
                       "native_valid": native_valid, "native_cuts": cuts,
                       "quality_caveats": (["native_invalid_with_ai_control"]
                                           if native_valid is False and controller == "AI" and cuts == 0 else []),
                       "flags": flags, "comparable_for_pace": comparable,
                       "usable_for_exploratory_pace": comparable or exploratory,
                       "pace_use": pace_use,
                       "quality_reasons": sorted(set(reasons)),
                       "evidence": (stint.get("evidence_mode")
                                    if stint.get("evidence_mode") in {"MEASURED", "SYNTHETIC"}
                                    else "UNAVAILABLE")})
    return result


def _thermal_from_samples(samples: list[dict[str, Any]], ambient: float | None,
                          track: float | None, evidence: str) -> dict[str, Any]:
    segments: list[list[ThermalSample]] = []
    current: list[ThermalSample] = []
    intervals = 0
    duration = 0.0
    for first, second in zip(samples, samples[1:]):
        d0, d1 = _record_data(first), _record_data(second)
        t0, t1 = _number(first.get("sim_time_s")), _number(second.get("sim_time_s"))
        dt = t1 - t0 if t0 is not None and t1 is not None else None
        paused = (_channel(d0, "sim_paused", "boolean") is True or
                  _channel(d1, "sim_paused", "boolean") is True)
        speed = _numeric_channel(d0, "speed_kmh", "km/h")
        throttle = _numeric_channel(d0, "throttle", "fraction")
        brake = _numeric_channel(d0, "brake", "fraction")
        if (dt is None or not 0 < dt <= 0.5 or paused or speed is None or speed < 0 or
                throttle is None or brake is None or not 0 <= throttle <= 1 or
                not 0 <= brake <= 1):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(ThermalSample(dt, 0.8 * throttle + 0.2 * brake, speed / 3.6, 0.0))
        intervals += 1
        duration += dt
    if current:
        segments.append(current)
    timed = [when for item in samples if (when := _number(item.get("sim_time_s"))) is not None]
    recorded_span = max(timed) - min(timed) if len(timed) >= 2 else None
    coverage = min(1.0, duration / recorded_span) if recorded_span and recorded_span > 0 else None
    result: dict[str, Any] = {
        "status": "unavailable", "valid_interval_count": intervals,
        "observed_duration_s": duration, "recorded_span_s": recorded_span,
        "integration_coverage_fraction": coverage,
        "segment_count": len(segments),
        "scenario_inputs": {"ambient_air_c": ambient, "track_surface_c": track,
                            "environment_summary": "median of observed stint samples"},
        "profiles": {}, "calculation_mode": "observed_stint_custom_thermal_projection",
        "physical_effect_status": "analysis_only",
        "evidence": [evidence, "ASSUMED", "PHYSICS_DERIVED"],
        "warnings": ["workload is 0.8 throttle + 0.2 brake, not measured electrical power",
                     "each uninterrupted recording segment begins at an assumed initial thermal state; gaps are not reconstructed"],
    }
    if evidence != "MEASURED":
        result["warnings"].append("synthetic fixture cannot be presented as measured workload")
    if not segments:
        result["warnings"].append("contiguous speed, throttle and brake intervals unavailable")
    if ambient is None or track is None:
        result["warnings"].append("measured ambient and track temperature are required for this projection")
    if evidence != "MEASURED" or not segments or ambient is None or track is None:
        return result
    scenario = ThermalScenario("observed_collection_stint", track, ambient,
                               evidence=("MEASURED", "ASSUMED"))
    for name, profile in thermal_assumption_profiles().items():
        pieces = [run_thermal_scenario(segment, profile, scenario, repeats=1)
                  for segment in segments]
        result["profiles"][name] = {
            "peak_temperatures_c": {
                node: max(piece.peak_temperatures_c[node] for piece in pieces)
                for node in profile.nodes},
            "time_above_warning_s": sum(piece.time_above_warning_s or 0 for piece in pieces),
            "time_above_derate_s": sum(piece.time_above_derate_s or 0 for piece in pieces),
            "derating_duration_s": sum(piece.derating_duration_s or 0 for piece in pieces),
            "physical_effect_status": "analysis_only",
            "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"],
        }
    result["status"] = ("computed" if len(segments) == 1 and
                        coverage is not None and coverage >= 0.95 else "computed_partial")
    if result["status"] == "computed_partial":
        result["warnings"].append("thermal peaks cover recorded segments only; state across gaps is not reconstructed")
    return result


def _summarize_stint(store: Store, stint: Mapping[str, Any], samples: list[dict[str, Any]],
                     manifest: Mapping[str, Any], evidence: str) -> dict[str, Any]:
    data = [_record_data(item) for item in samples]
    ambient = [_numeric_channel(item, "ambient_air_c", "degC") for item in data]
    track = [_numeric_channel(item, "track_surface_c", "degC") for item in data]
    fuel = [_numeric_channel(item, "fuel", "L") for item in data]
    speed = [_numeric_channel(item, "speed_kmh", "km/h") for item in data]
    ambient_values = [value for value in ambient if value is not None]
    track_values = [value for value in track if value is not None]
    fuel_values = [value for value in fuel if value is not None]
    speed_values = [value for value in speed if value is not None]
    fuel_refill = any(after > before + 0.1 for before, after in zip(fuel_values, fuel_values[1:]))
    energy_channels = (("battery_soc_native", "native_unit_unverified"),
                       ("kers_current_kj_native", "kJ_native"),
                       ("kers_max_kj_native", "kJ_native"),
                       ("kers_input_native", "native_unit_unverified"),
                       ("kers_load_native", "native_unit_unverified"),
                       ("mguk_delivery_native", "native_index"),
                       ("mguk_recovery_native", "native_index"),
                       ("requested_power_w", "W"),
                       ("delivered_power_w", "W"),
                       ("recovery_power_w", "W"),
                       ("battery_energy_j", "J"))
    energy = {name: {**_range([number for item in data
                                 if (number := _numeric_channel(item, name, unit)) is not None]),
                     "unit": unit}
              for name, unit in energy_channels}
    driving_channels = (("throttle", "fraction"), ("brake", "fraction"),
                        ("rpm", "rev/min"), ("steering_native", "deg"),
                        ("gear", "gear_index"), ("track_distance_m", "m"))
    driving = {name: {**_range([number for item in data
                                  if (number := _numeric_channel(item, name, unit)) is not None]),
                      "unit": unit}
               for name, unit in driving_channels}
    tires = {}
    for wheel in WHEELS:
        channels = (("native_wear", "tyre_wear_" + wheel, "native_fraction_not_life"),
                    ("virtual_distance", "tyre_virtual_km_" + wheel, "virtual_km_not_distance"),
                    ("grain_native", "tyre_grain_" + wheel, "native_unit_unverified"),
                    ("blister_native", "tyre_blister_" + wheel, "native_unit_unverified"),
                    ("flat_spot_native", "tyre_flat_spot_" + wheel, "native_unit_unverified"),
                    ("core_temperature_c", "tyre_core_c_" + wheel, "degC"),
                    ("inside_temperature_c", "tyre_inside_c_" + wheel, "degC"),
                    ("middle_temperature_c", "tyre_middle_c_" + wheel, "degC"),
                    ("outside_temperature_c", "tyre_outside_c_" + wheel, "degC"),
                    ("pressure_native", "tyre_pressure_" + wheel, "native_unit_unverified"),
                    ("load_n", "tyre_load_n_" + wheel, "N"),
                    ("slip_proxy", "wheel_slip_" + wheel, "native_proxy_unverified"),
                    ("slip_angle_deg", "wheel_slip_angle_deg_" + wheel, "deg"))
        tires[wheel] = {label: {**_range([number for item in data
                                            if (number := _numeric_channel(item, channel, unit)) is not None]),
                                "unit": unit}
                        for label, channel, unit in channels}
    laps = _laps_for_stint(store, stint, samples)
    for lap in laps:
        start, end = lap["sim_start_s"], lap["sim_end_s"]
        lap_data = [_record_data(item) for item in samples
                    if start is not None and end is not None and
                    (when := _number(item.get("sim_time_s"))) is not None and start <= when <= end]
        lap["telemetry_sample_count"] = len(lap_data)
        lap["prototype_force_call_pairs"] = _range([
            number for item in lap_data
            if (item.get("prototype_force_call_pairs") or {}).get("source") == "CSP_SESSION_SHARED_STORE"
            and (number := _numeric_channel(item, "prototype_force_call_pairs",
                                               "count_worker_issued")) is not None])
        lap["fuel_l"] = _range([number for item in lap_data
                                 if (number := _numeric_channel(item, "fuel", "L")) is not None])
        lap["battery_state_native"] = {
            name: {**_range([number for item in lap_data
                             if (number := _numeric_channel(item, name, unit)) is not None]),
                   "unit": unit}
            for name, unit in energy_channels[:2]}
        lap["tire_state_by_wheel"] = {
            wheel: {
                name: {**_range([number for item in lap_data
                                  if (number := _numeric_channel(item, channel, unit)) is not None]),
                       "unit": unit}
                for name, channel, unit in (
                    ("native_wear", "tyre_wear_" + wheel, "native_fraction_not_life"),
                    ("virtual_distance", "tyre_virtual_km_" + wheel, "virtual_km_not_distance"),
                    ("core_temperature_c", "tyre_core_c_" + wheel, "degC"),
                    ("pressure_native", "tyre_pressure_" + wheel, "native_unit_unverified"))}
            for wheel in WHEELS}
    comparable = [lap for lap in laps if lap["comparable_for_pace"]]
    exploratory_ai = [lap for lap in laps if lap["pace_use"] == "exploratory_ai_native_invalid"]
    by_controller = {controller: [lap["lap_time_s"] for lap in laps
                                  if lap["controller_observed"] == controller and
                                  (lap["comparable_for_pace"] or
                                   (controller == "AI" and lap["usable_for_exploratory_pace"]))]
                     for controller in ("AI", "HUMAN")}
    compounds = sorted(set(stint.get("observed_tyre_compounds") or []))
    compound = compounds[0] if len(compounds) == 1 else None
    controller_counts = {name: stint.get(key, 0) for name, key in
                         (("AI", "ai_sample_count"), ("HUMAN", "human_sample_count"),
                          ("UNKNOWN", "unknown_controller_sample_count"),
                          ("CONFLICT", "conflicting_controller_sample_count"))}
    result = {
        **dict(stint), "evidence": evidence,
        "car_id": manifest.get("car_id"), "car_version": manifest.get("car_version"),
        "track_id": manifest.get("track_id") or manifest.get("track"),
        "layout_id": manifest.get("layout_id"),
        "setup_hash": manifest.get("setup_hash"),
        "profile_hash": manifest.get("profile_hash"),
        "start_trigger": manifest.get("start_trigger"),
        "session_mode": (stint.get("session_mode_observed")
                         if stint.get("boundary_source") == "explicit_start_event"
                         else stint.get("session_mode_observed") or manifest.get("session_mode")),
        "session_mode_source": ("STINT_BOUNDARY_READBACK" if stint.get("session_mode_observed") is not None
                                else "UNAVAILABLE" if stint.get("boundary_source") == "explicit_start_event"
                                else "RUN_MANIFEST_START"),
        "extended_physics": (stint.get("extended_physics_observed")
                             if stint.get("boundary_source") == "explicit_start_event" or
                             stint.get("extended_physics_observed") is not None
                             else manifest.get("extended_physics")),
        "extended_physics_source": ("STINT_BOUNDARY_READBACK" if stint.get("extended_physics_observed") is not None
                                    else "UNAVAILABLE" if stint.get("boundary_source") == "explicit_start_event"
                                    else "RUN_MANIFEST_START"),
        "csp_version_code": manifest.get("csp_version_code"),
        "compound_observed": compound,
        "compound_evidence": "MEASURED" if compound is not None else "UNAVAILABLE",
        "compound_requested": stint.get("requested_tyre_compound") or stint.get("planned_tire_label"),
        "controller_sample_counts": controller_counts,
        "experiment_paused_sample_count": sum(
            _channel(item, "experiment_paused", "boolean") is True for item in data),
        "ambient_air_c": _range(ambient_values),
        "track_surface_c": _range(track_values),
        "fuel_l": {**_range(fuel_values),
                   "used_l": fuel_values[0] - fuel_values[-1] if len(fuel_values) >= 2 and not fuel_refill else None,
                   "refill_detected": fuel_refill},
        "road_grip_native": _range([number for item in data
                                    if (number := _numeric_channel(item, "road_grip_native", "fraction")) is not None]),
        "time_multiplier_native": _range([number for item in data
                                           if (number := _numeric_channel(item, "time_multiplier_native", "native_multiplier")) is not None]),
        "rain_wetness_native": _range([number for item in data
                                       if (number := _numeric_channel(item, "rain_wetness_native", "native_unit_unverified")) is not None]),
        "speed_kmh": _range(speed_values),
        "tires": tires, "energy_native": energy, "driving_inputs": driving,
        "laps": laps,
        "comparable_lap_count": len(comparable),
        "exploratory_ai_lap_count": len(exploratory_ai),
        "lap_time_median_s_by_controller": {
            controller: median(times) if times else None
            for controller, times in by_controller.items()},
        "best_lap_s_by_controller": {
            controller: min(times) if times else None
            for controller, times in by_controller.items()},
        "thermal": _thermal_from_samples(samples,
                                         median(ambient_values) if ambient_values else None,
                                         median(track_values) if track_values else None,
                                         evidence),
        "calculation_mode": "observed_physical_stint_summary",
        "physical_effect_status": "observed_trace" if evidence == "MEASURED" else "synthetic_only",
        "warnings": [],
    }
    result["prototype"] = prototype_from_samples(
        manifest.get("prototype"), data, result["session_mode"])
    if compound is None:
        result["warnings"].append("one consistent observed tire compound is unavailable; requested label is not readback")
    if fuel_refill:
        result["warnings"].append("fuel rose inside stint; net fuel consumption withheld")
    if not comparable:
        result["warnings"].append("no complete comparable lap yet; continuous traces remain usable")
    if exploratory_ai:
        result["warnings"].append("AI native-invalid zero-cut laps enter exploratory pace only; native invalidity cause is unverified")
    if controller_counts["CONFLICT"]:
        result["warnings"].append("conflicting AI/human control readbacks require inspection")
    if result["experiment_paused_sample_count"]:
        result["warnings"].append("experiment-paused telemetry retained as physical data; pace laps remain quality flagged")
    return result


def _match_issues(a: Mapping[str, Any], b: Mapping[str, Any], controller: str) -> list[str]:
    issues = _context_issues(a, b)
    if (a["lap_time_median_s_by_controller"].get(controller) is None or
            b["lap_time_median_s_by_controller"].get(controller) is None):
        issues.append("complete usable laps unavailable")
    if controller == "AI" and (a.get("exploratory_ai_lap_count", 0) or
                               b.get("exploratory_ai_lap_count", 0)):
        issues.append("AI native-invalid laps used for exploratory pace; validity reason unverified")
    return issues


MATCH_POLICY = {
    "ambient_air_c": 2.0,
    "track_surface_c": 2.0,
    "fuel_l": 5.0,
    "road_grip_native": 0.005,
    "rain_wetness_native": 0.02,
    "battery_state_fraction_of_capacity": 0.05,
    "tire_native_wear": 0.02,
    "tire_core_temperature_c": 5.0,
    "tire_pressure_relative": 0.05,
}


def _context_issues(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """Exploratory matching policy, not instrument accuracy or causal proof."""
    issues = []
    for item in (a, b):
        if item.get("session_mode_changed_within_stint"):
            issues.append("session mode changed within stint")
        if item.get("extended_physics_changed_within_stint"):
            issues.append("extended physics changed within stint")
        if item.get("compound_change_count", 0):
            issues.append("compound changed within stint")
        if (item.get("fuel_l") or {}).get("refill_detected"):
            issues.append("fuel refill within stint")
    if a.get("evidence") != b.get("evidence"):
        issues.append("measured and synthetic evidence differ")
    for field in ("track_id", "layout_id", "car_id", "car_version",
                  "session_mode", "extended_physics", "csp_version_code"):
        if a.get(field) in (None, "") or b.get(field) in (None, ""):
            issues.append(field + " unverified")
        elif a[field] != b[field]:
            issues.append(field + " differs")
    if a.get("setup_hash") not in (None, "") and b.get("setup_hash") not in (None, ""):
        if a["setup_hash"] != b["setup_hash"]:
            issues.append("setup_hash differs")
    elif a.get("setup_label_requested") and b.get("setup_label_requested"):
        if a["setup_label_requested"] != b["setup_label_requested"]:
            issues.append("setup_label_requested differs")
        else:
            issues.append("setup identity is driver-entered label only")
    else:
        issues.append("setup identity unverified")
    if a.get("profile_hash") not in (None, "") and b.get("profile_hash") not in (None, ""):
        if a["profile_hash"] != b["profile_hash"]:
            issues.append("profile_hash differs")
    else:
        issues.append("profile identity unverified")
    if a.get("condition_label") != b.get("condition_label"):
        issues.append("condition label differs")
    for field in ("ambient_air_c", "track_surface_c", "fuel_l",
                  "road_grip_native", "rain_wetness_native"):
        limit = MATCH_POLICY[field]
        left = (a.get(field) or {}).get("first" if field == "fuel_l" else "median")
        right = (b.get(field) or {}).get("first" if field == "fuel_l" else "median")
        if left is None or right is None:
            issues.append(field + " unavailable")
        elif abs(left - right) > limit:
            issues.append(field + " differs beyond exploratory tolerance")
    first_a = (a.get("time_multiplier_native") or {}).get("first")
    first_b = (b.get("time_multiplier_native") or {}).get("first")
    if first_a is None or first_b is None:
        issues.append("time_multiplier_native unavailable")
    elif first_a != first_b:
        issues.append("time_multiplier_native differs")
    energy_a, energy_b = a.get("energy_native") or {}, b.get("energy_native") or {}
    charge_a = (energy_a.get("kers_current_kj_native") or {}).get("first")
    charge_b = (energy_b.get("kers_current_kj_native") or {}).get("first")
    capacity_a = (energy_a.get("kers_max_kj_native") or {}).get("first")
    capacity_b = (energy_b.get("kers_max_kj_native") or {}).get("first")
    if None in (charge_a, charge_b, capacity_a, capacity_b) or min(capacity_a, capacity_b) <= 0:
        issues.append("starting battery energy or capacity unavailable")
    elif abs(charge_a - charge_b) > MATCH_POLICY["battery_state_fraction_of_capacity"] * min(capacity_a, capacity_b):
        issues.append("starting battery energy differs beyond exploratory tolerance")
    for name in ("mguk_delivery_native", "mguk_recovery_native"):
        left = (energy_a.get(name) or {}).get("first")
        right = (energy_b.get(name) or {}).get("first")
        if left is None or right is None:
            issues.append(name + " unavailable")
        elif left != right:
            issues.append(name + " differs")
    for wheel in WHEELS:
        tire_a = (a.get("tires") or {}).get(wheel) or {}
        tire_b = (b.get("tires") or {}).get(wheel) or {}
        for name, limit in (("native_wear", MATCH_POLICY["tire_native_wear"]),
                            ("core_temperature_c", MATCH_POLICY["tire_core_temperature_c"])):
            left = (tire_a.get(name) or {}).get("first")
            right = (tire_b.get(name) or {}).get("first")
            if left is None or right is None:
                issues.append(wheel + " " + name + " unavailable")
            elif abs(left - right) > limit:
                issues.append(wheel + " " + name + " differs beyond exploratory tolerance")
        left = (tire_a.get("pressure_native") or {}).get("first")
        right = (tire_b.get("pressure_native") or {}).get("first")
        if left is None or right is None:
            issues.append(wheel + " pressure_native unavailable")
        elif abs(left - right) > MATCH_POLICY["tire_pressure_relative"] * max(abs(left), abs(right), 1):
            issues.append(wheel + " pressure_native differs beyond exploratory tolerance")
    return issues


def _hard_mismatch(issues: list[str]) -> bool:
    return any(" differs" in issue or "evidence differ" in issue or
               "beyond exploratory tolerance" in issue or
               "changed within stint" in issue or "refill within stint" in issue
               for issue in issues)


def _comparisons(stints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index, a in enumerate(stints):
        for b in stints[index + 1:]:
            if not a.get("compound_observed") or not b.get("compound_observed"):
                continue
            if a["compound_observed"] == b["compound_observed"]:
                continue
            if a.get("track_id") != b.get("track_id") or a.get("layout_id") != b.get("layout_id"):
                continue
            for controller in ("AI", "HUMAN"):
                left = a["lap_time_median_s_by_controller"].get(controller)
                right = b["lap_time_median_s_by_controller"].get(controller)
                if left is None or right is None:
                    continue
                issues = _match_issues(a, b, controller)
                hard_mismatch = _hard_mismatch(issues)
                rows.append({
                    "track_id": a["track_id"], "layout_id": a["layout_id"],
                    "car_id": a.get("car_id"),
                    "condition_label": a.get("condition_label"),
                    "setup_hash": a.get("setup_hash"),
                    "profile_hash": a.get("profile_hash"),
                    "setup_label_requested": a.get("setup_label_requested"),
                    "session_mode": a.get("session_mode"),
                    "extended_physics": a.get("extended_physics"),
                    "controller_observed": controller,
                    "left_stint_id": a["collection_stint_id"], "left_run_id": a["run_id"],
                    "left_compound": a["compound_observed"],
                    "right_stint_id": b["collection_stint_id"], "right_run_id": b["run_id"],
                    "right_compound": b["compound_observed"],
                    "observed_median_delta_right_minus_left_s": right - left,
                    "status": "descriptive_only" if hard_mismatch else
                              "conditional_observed" if issues else "matched_exploratory",
                    "match_issues": issues,
                    "independent_stints": 2,
                    "evidence": sorted({a["evidence"], b["evidence"], "PHYSICS_DERIVED"}),
                    "calculation_mode": "within_controller_tire_stint_contrast",
                    "physical_effect_status": "observed_trace" if a["evidence"] == b["evidence"] == "MEASURED"
                    else "synthetic_or_mixed",
                    "warning": "a pair of stints is exploratory; controller consistency does not remove all confounding",
                })
    return rows


def _context_snapshot(stint: Mapping[str, Any]) -> dict[str, Any]:
    names = ("evidence", "track_id", "layout_id", "car_id", "car_version",
             "session_mode", "extended_physics", "csp_version_code", "setup_hash",
             "setup_label_requested", "profile_hash", "condition_label", "ambient_air_c",
             "track_surface_c", "fuel_l", "road_grip_native", "rain_wetness_native",
             "time_multiplier_native", "energy_native", "tires",
             "session_mode_changed_within_stint", "extended_physics_changed_within_stint",
             "compound_change_count")
    return {name: stint.get(name) for name in names}


def _human_anchors(stints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each physical stint distinct; free-text labels cannot merge conditions."""
    result = []
    for stint in stints:
        if not stint.get("compound_observed") or stint["evidence"] != "MEASURED":
            continue
        if (stint.get("session_mode_changed_within_stint") or
                stint.get("extended_physics_changed_within_stint") or
                stint.get("compound_change_count", 0) or
                (stint.get("fuel_l") or {}).get("refill_detected")):
            continue
        times = [lap["lap_time_s"] for lap in stint["laps"]
                 if lap["controller_observed"] == "HUMAN" and lap["comparable_for_pace"]]
        if not times:
            continue
        result.append({"track_id": stint.get("track_id"), "layout_id": stint.get("layout_id"),
                       "car_id": stint.get("car_id"), "compound_observed": stint["compound_observed"],
                       "condition_label": stint.get("condition_label"),
                       "setup_hash": stint.get("setup_hash"),
                       "setup_label_requested": stint.get("setup_label_requested"),
                       "session_mode": stint.get("session_mode"),
                       "extended_physics": stint.get("extended_physics"),
                       "best_clean_lap_s": min(times), "median_clean_lap_s": median(times),
                       "clean_lap_count": len(times), "run_ids": [stint["run_id"]],
                       "stint_ids": [stint["collection_stint_id"]],
                       "context": _context_snapshot(stint),
                       "evidence": ["MEASURED"],
                       "calculation_mode": "human_competitive_lap_observation",
                       "physical_effect_status": "observed_trace",
                       "warning": "best lap is an observed anchor, not a full-stint pace or transferable AI tire effect"})
    return result


def _anchored_tire_scenarios(comparisons: list[dict[str, Any]],
                           anchors: list[dict[str, Any]],
                           stints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate an AI tire contrast onto a human baseline as an explicit assumption.

    This is a candidate equation for a future held-out human check, not a fitted
    or validated tire effect.  Known condition mismatches prevent translation;
    unknown setup/version fields remain visible in the assumption list.
    """
    result = []
    indexed_stints = {(item["run_id"], item["collection_stint_id"]): item for item in stints}
    for contrast in comparisons:
        if contrast["controller_observed"] != "AI" or contrast["physical_effect_status"] != "observed_trace":
            continue
        if _hard_mismatch(contrast["match_issues"]):
            continue
        ai_baseline = indexed_stints.get((contrast["left_run_id"], contrast["left_stint_id"]))
        if ai_baseline is None:
            continue
        for baseline in anchors:
            if any(baseline.get(field) != contrast.get(field)
                   for field in ("track_id", "layout_id", "car_id")):
                continue
            if baseline["compound_observed"] != contrast["left_compound"]:
                continue
            if baseline.get("condition_label") != contrast.get("condition_label"):
                continue
            cross_issues = _context_issues(_context_snapshot(ai_baseline), baseline["context"])
            if _hard_mismatch(cross_issues):
                continue
            delta = contrast["observed_median_delta_right_minus_left_s"]
            right_anchors = [item for item in anchors
                             if item.get("track_id") == baseline.get("track_id")
                             and item.get("layout_id") == baseline.get("layout_id")
                             and item.get("car_id") == baseline.get("car_id")
                             and item.get("compound_observed") == contrast["right_compound"]
                             and item.get("condition_label") == baseline.get("condition_label")
                             and item.get("session_mode") == baseline.get("session_mode")
                             and item.get("setup_hash") == baseline.get("setup_hash")
                             and item.get("setup_label_requested") == baseline.get("setup_label_requested")
                             and item.get("extended_physics") == baseline.get("extended_physics")
                             and not _hard_mismatch(_context_issues(baseline["context"], item["context"]))]
            predicted_median = baseline["median_clean_lap_s"] + delta
            result.append({
                "track_id": baseline["track_id"], "layout_id": baseline["layout_id"],
                "condition_label": baseline.get("condition_label"),
                "baseline_compound": contrast["left_compound"],
                "candidate_compound": contrast["right_compound"],
                "human_anchor_run_ids": baseline["run_ids"],
                "ai_contrast_run_ids": [contrast["left_run_id"], contrast["right_run_id"]],
                "human_baseline_median_s": baseline["median_clean_lap_s"],
                "ai_observed_relative_delta_s": delta,
                "candidate_human_median_s": predicted_median,
                "candidate_human_best_s": baseline["best_clean_lap_s"] + delta,
                "retrospective_human_candidate_median_s": right_anchors[0]["median_clean_lap_s"] if right_anchors else None,
                "retrospective_residual_s": right_anchors[0]["median_clean_lap_s"] - predicted_median
                if right_anchors else None,
                "validation_status": "retrospective_only_not_held_out" if right_anchors else "awaiting_held_out_human_check",
                "status": "conditional_assumed_transfer",
                "unverified_controls": sorted(set([*contrast["match_issues"], *cross_issues])),
                "ai_to_human_context_issues": cross_issues,
                "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"],
                "calculation_mode": "human_anchor_plus_ai_relative_tire_contrast",
                "physical_effect_status": "analysis_only",
                "warning": "AI-to-human tire response transfer is an unvalidated assumption; do not use as measured human pace",
            })
    return result


def _attach_overnight_comparison(stints: list[dict[str, Any]],
                                 checkpoint_path: Path) -> None:
    """Apply queue limits only to runs the checkpoint identifies by run ID."""
    rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    claimed_runs: set[str] = set()
    current_run_id = None
    current_physical_id = None
    current_limits: list[str] = []
    checkpoint_hash = None
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (isinstance(checkpoint, dict) and checkpoint.get("schema_version") == 1
                and isinstance(checkpoint.get("completed"), list)):
            checkpoint_hash = checkpoint.get("queue_sha256")
            for row in checkpoint["completed"]:
                if isinstance(row, dict) and isinstance(row.get("run_id"), str) and row["run_id"]:
                    claimed_runs.add(row["run_id"])
                if (isinstance(row, dict) and row.get("evidence") == "MEASURED"
                        and isinstance(row.get("run_id"), str) and row["run_id"]
                        and isinstance(row.get("physical_session_id"), str)
                        and row["physical_session_id"]):
                    rows[(row["run_id"], row["physical_session_id"])].append(row)
            if isinstance(checkpoint.get("run_id"), str) and checkpoint["run_id"]:
                current_run_id = checkpoint["run_id"]
                current_physical_id = checkpoint.get("physical_session_id")
                raw_limits = checkpoint.get("comparison_limits")
                if isinstance(raw_limits, list) and all(isinstance(item, str) for item in raw_limits):
                    current_limits = raw_limits
    except (OSError, UnicodeError, ValueError):
        pass
    for stint in stints:
        if stint.get("start_trigger") != "overnight_mode_auto":
            continue
        run_id, physical_id = stint.get("run_id"), stint.get("physical_session_id")
        matches = rows.get((run_id, physical_id), [])
        if not matches and run_id not in claimed_runs and run_id != current_run_id:
            # Automatic CSP app startup also happens during supervised CM
            # launches. The app trigger alone is not proof the queue owns it.
            stint["overnight_comparison"] = {"status": "not_queue_claimed",
                                               "queue_owned": False, "limits": []}
            continue
        reasons = []
        if len(matches) == 1:
            row = matches[0]
            limits = row.get("comparison_limits")
            if not isinstance(limits, list) or any(not isinstance(item, str) for item in limits):
                reasons.append("overnight comparison limits unavailable")
            else:
                reasons.extend(limits)
            if row.get("comparison_metadata_verified") is not True:
                reasons.append("overnight comparison metadata unverified")
        elif len(matches) > 1:
            reasons.append("overnight comparison checkpoint has duplicate run/native entries")
        elif run_id in claimed_runs:
            reasons.append("overnight checkpoint names run with a different native session ID")
        else:
            reasons.extend(current_limits)
            reasons.append("overnight queue session has no saved completion record")
            if current_physical_id != physical_id:
                reasons.append("overnight in-progress native session ID differs")
        stint["overnight_comparison"] = {
            "status": "metadata_verified" if not reasons else "unmatched_raw_only",
            "queue_owned": True,
            "limits": sorted(set(reasons)),
            "queue_sha256": checkpoint_hash,
            "checkpoint_file": checkpoint_path.name,
        }


def analyze_collections(store: Store, *,
                        overnight_checkpoint: str | Path | None = None) -> dict[str, Any]:
    """Summarize all physical stints, including legacy runs, without controller gates."""
    manifests = {run["run_id"]: run for run in store.iter_runs()}
    samples_by_stint: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    unassigned_samples = 0
    stints = []
    for kind, item in scan_collection(store):
        key = (item["run_id"], item["physical_session_id"], item["collection_stint_id"])
        if kind == "sample":
            if item["collection_stint_id"] is None:
                unassigned_samples += 1
            else:
                samples_by_stint[key].append(item)
            continue
        run = manifests.get(item["run_id"])
        if run is not None:
            stints.append(_summarize_stint(store, item, samples_by_stint.pop(key, []),
                                          run["manifest"], run["mode"]))
    _attach_overnight_comparison(
        stints, Path(overnight_checkpoint) if overnight_checkpoint is not None
        else store.directory.parent / "overnight.checkpoint.json")
    comparisons = _comparisons(stints)
    anchors = _human_anchors(stints)
    package_prototype_analysis = analyze_package_pairs(stints, anchors)
    return {"status": "available" if stints else "unavailable",
            "model_version": "wimna_collection_analysis_v1",
            "stints": stints, "tire_comparisons": comparisons,
            "human_pace_anchors": anchors,
            "package_prototype_analysis": package_prototype_analysis,
            "anchored_tire_scenarios": _anchored_tire_scenarios(comparisons, anchors, stints),
            "matching_policy": {"tolerances": MATCH_POLICY,
                                "category": "exploratory_policy_not_instrument_resolution"},
            "unassigned_sample_count": unassigned_samples,
            "measured_stint_count": sum(item["evidence"] == "MEASURED" for item in stints),
            "synthetic_stint_count": sum(item["evidence"] == "SYNTHETIC" for item in stints),
            "calculation_mode": "observed_stint_inventory_and_exploratory_contrasts",
            "physical_effect_status": "observed_trace_for_inputs_analysis_only_for_thermal",
            "limitations": [
                "raw AI and human telemetry are both physical simulator observations; controller is a covariate",
                "native tire wear is retained as an unverified state, not remaining tire life",
                "pairwise tire contrasts are exploratory until conditions and independent repeats are matched",
                "human competitive laps anchor absolute pace; AI relative tire effects are not validated as transferable",
                "custom thermal nodes are engineering assumptions, not native component measurements",
                "samples outside a labeled stint remain in raw runs and are counted as unassigned",
            ]}
