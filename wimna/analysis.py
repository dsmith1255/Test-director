"""Evidence-aware calibration and forecasts from persisted WIMNA runs.

``analyze_store`` is offline and deterministic for the same Store contents,
campaign configuration, and explicit assumptions.  It never promotes
synthetic runs to measured evidence, interprets native tire grip as life, or
assumes a requested power restriction affected the simulator.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, replace
from statistics import mean, median, stdev
from typing import Any, Mapping

from .collection_analysis import analyze_collections
from .models import (
    EnergySample, ModelParameter, StintConfig, ThermalSample, ThermalScenario,
    TireConfig, TireSample, TireState, derived_evidence, fuel_local_contrast,
    integrate_energy, project_energy_repayment, project_stint,
    run_thermal_scenario, signed_battery_power, thermal_assumption_profiles,
)
from .store import Store


MODEL_VERSION = "wimna_saved_run_analysis_v2"
WHEELS = ("fl", "fr", "rl", "rr")
LAP_EXCLUSION_FLAGS = frozenset({
    "off_track", "spin", "major_spin", "contact", "damage", "paused",
    "experiment_paused", "sim_paused", "reset", "car_reset", "car_jump",
    "pit", "pitlane", "pit_lap", "session_changed", "lap_counter_reset",
    "lap_counter_jump", "unexpected_setting_change", "missing_required_data",
    "carryover_unrestored", "native_invalid",
})
STATE_CONTAMINATION_FLAGS = frozenset({"major_spin", "contact", "damage", "reset", "car_reset"})
DEFAULT_ASSUMPTIONS: dict[str, Any] = {
    "ambient_c": None,
    "ai_thermal_ambient_c": None,
    "ai_thermal_track_c": None,
    "ambient_by_scenario_c": {},
    "plan_c_airflow_multiplier": 1.10,
    "plan_c_airflow_range": [0.95, 1.20],
    "tire_time_delta_plan_c_minus_baseline_s_range": None,
    "traffic_airflow_multiplier": 0.75,
    "higher_deployment_workload_multiplier": 1.08,
    "thermal_repeats": 3,
    "fuel_density_kg_per_l": None,
    "stint_inputs": {},
    "race_inputs": {},
}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _channel(data: Mapping[str, Any], name: str, units: tuple[str, ...]) -> float | None:
    raw = data.get(name)
    if not isinstance(raw, dict) or raw.get("unit") not in units:
        return None
    return _number(raw.get("value"))


def _parameter(value: float | None, unit: str, source: str, evidence: str,
               version: str = "FH26 unverified", low: float | None = None,
               high: float | None = None) -> dict[str, Any]:
    return asdict(ModelParameter(value, unit, source, version, evidence, low, high))


def _block_kinds(config: Mapping[str, Any]) -> dict[str, str]:
    return {block["id"]: block["kind"] for track in config.get("tracks", {}).values()
            for block in track.get("blocks", [])}


def _lap_evidence(store: Store, run_id: str, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    laps, excluded = [], []
    annotations = {item["lap_event_seq"]: item for item in store.iter_lap_annotations(run_id)}
    contaminated = False
    for record in store.iter_records(run_id, "event"):
        data = record["data"]
        if data.get("type") == "state_restored" and (
            data.get("observed_restoration") is True or data.get("manual_confirmation") is True
        ):
            contaminated = False
        if data.get("type") != "lap_complete":
            continue
        annotation = annotations.get(record["seq"])
        phase = annotation["phase"] if annotation else data.get("phase", "unknown")
        eligible = annotation["eligible"] if annotation else (
            mode == "SYNTHETIC" and data.get("eligible_for_comparison") is True)
        flags = set(str(flag) for flag in (data.get("flags") or []))
        if annotation:
            flags.update(str(flag) for flag in annotation["quality_flags"])
        if contaminated:
            flags.add("carryover_unrestored")
        if phase in {"warmup", "outlap", "pit"}:
            flags.add(str(phase))
        if data.get("valid_native") is False:
            flags.add("native_invalid")
        lap_time = _number(data.get("lap_time_s"))
        if lap_time is None or lap_time <= 0:
            flags.add("missing_required_data")
        if flags & STATE_CONTAMINATION_FLAGS:
            contaminated = True
        if not eligible or phase != "program" or flags & LAP_EXCLUSION_FLAGS:
            excluded.append({"lap_index": data.get("lap_index"), "flags": sorted(flags),
                             "reason": annotation["reason"] if annotation else
                                       "lap lacks explicit eligibility annotation or required data"})
            continue
        lap_end = _number(record.get("sim_time_s"))
        laps.append({"lap_index": data.get("lap_index"), "lap_time_s": lap_time,
                     "sim_start_s": lap_end - lap_time if lap_end is not None else None,
                     "sim_end_s": lap_end,
                     "fuel_used_l": _number(data.get("fuel_used_l")),
                     "battery_delta_j": _number(data.get("battery_delta_j")),
                     "tire_state": data.get("tire_state") if isinstance(data.get("tire_state"), dict) else None,
                     "flags": sorted(flags)})
    return laps, excluded


def _runs(store: Store, config: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, list[dict[str, Any]]]]]:
    kinds = _block_kinds(config)
    runs = list(store.iter_runs())
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for run in runs:
        run["raw_manifest"] = deepcopy(run["manifest"])
        assignment = store.get_run_assignment(run["run_id"])
        run["assignment"] = deepcopy(assignment)
        if assignment is not None:
            # Assignment is a later immutable director record. It identifies
            # the prescribed track/block without rewriting the raw Lua run
            # manifest or claiming the requested native state was observed.
            run["manifest"] = deepcopy(run["manifest"])
            run["manifest"]["track"] = assignment["track"]
            run["manifest"]["block"] = assignment["block"]
        manifest = run["manifest"]
        run["laps"], run["excluded_laps"] = _lap_evidence(store, run["run_id"], run["mode"])
        if run["raw_manifest"].get("selection") in {"ai_thermal", "collection"}:
            # A collection is a physical data source, independent of campaign
            # block assignments. Its laps and workload are analyzed separately.
            run["kind"] = None
            continue
        run["kind"] = kinds.get(manifest.get("block"))
        track = manifest.get("track")
        if track in config.get("tracks", {}) and run["kind"] is not None:
            grouped.setdefault(track, {}).setdefault(run["kind"], []).append(run)
    return runs, grouped


def _evidence(runs: list[dict[str, Any]]) -> list[str]:
    return sorted({run["mode"] for run in runs})


def _mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _reference(runs: list[dict[str, Any]]) -> dict[str, Any]:
    laps = [lap for run in runs for lap in run["laps"]]
    times = [lap["lap_time_s"] for lap in laps]
    fuel = [lap["fuel_used_l"] for lap in laps if lap["fuel_used_l"] is not None]
    return {
        "status": "available" if times else "unavailable",
        "run_ids": [run["run_id"] for run in runs],
        "evidence": _evidence(runs),
        "calculation_mode": "observed_summary",
        "physical_effect_status": "observed_trace" if any(run["mode"] == "MEASURED" for run in runs) else "synthetic_only",
        "lap_count": len(times),
        "excluded_lap_count": sum(len(run["excluded_laps"]) for run in runs),
        "pace_mean_s": _mean(times),
        "pace_spread_s": stdev(times) if len(times) > 1 else None,
        "pace_range_s": [min(times), max(times)] if times else None,
        "fuel_mean_l_per_lap": _mean(fuel),
        "fuel_range_l_per_lap": [min(fuel), max(fuel)] if fuel else None,
        "fuel_laps_with_data": len(fuel),
        "warnings": [] if times else ["clean timed reference laps unavailable"],
    }


def _initial(run: Mapping[str, Any], field: str) -> Any:
    state = run["raw_manifest"].get("observed_initial_state")
    if isinstance(state, dict) and state.get(field) is not None:
        raw_manifest_value = state[field]
        unit = {"fuel_l": "L", "battery_j": "J"}.get(field)
        if isinstance(raw_manifest_value, dict):
            return _number(raw_manifest_value.get("value")) if raw_manifest_value.get("unit") == unit else None
        return raw_manifest_value
    assignment = run.get("assignment") or {}
    details = assignment.get("details") or {}
    observed = details.get("observed_initial_state") if isinstance(details, dict) else None
    if not isinstance(observed, dict):
        return None
    raw = observed.get(field)
    unit = {"fuel_l": "L", "battery_j": "J"}.get(field)
    if unit is None:
        return None
    if isinstance(raw, dict) and raw.get("unit") == unit:
        return _number(raw.get("value"))
    units = details.get("observed_initial_state_units")
    if isinstance(units, dict) and units.get(field) == unit:
        return _number(raw)
    return None


def _initial_soc_fraction(run: Mapping[str, Any]) -> float | None:
    """Read SOC only with an explicit fractional unit; never infer joules."""
    sources = [run.get("raw_manifest") or {}]
    assignment = run.get("assignment") or {}
    details = assignment.get("details") if isinstance(assignment, dict) else None
    if isinstance(details, dict):
        sources.append(details)
    for source in sources:
        state = source.get("observed_initial_state")
        if not isinstance(state, dict):
            continue
        units = source.get("observed_initial_state_units")
        for field in ("battery_soc_native", "battery_soc_fraction"):
            raw = state.get(field)
            if isinstance(raw, dict):
                unit = raw.get("unit")
                value = _number(raw.get("value"))
            else:
                unit = units.get(field) if isinstance(units, dict) else None
                value = _number(raw)
            if unit in {"fraction", "fraction_native"} and value is not None and 0 <= value <= 1:
                return value
    return None


def _matched_conditions(ref: Mapping[str, Any], contrast: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for field in ("car_id", "track_id", "layout_id", "profile_hash", "setup_hash"):
        first = ref["manifest"].get(field)
        second = contrast["manifest"].get(field)
        if first is None or second is None:
            problems.append(f"{field} identity unavailable")
        elif first != second:
            problems.append(f"{field} differs")
    battery_a = _number(_initial(ref, "battery_j"))
    battery_b = _number(_initial(contrast, "battery_j"))
    battery_tolerance = _number(policy.get("battery_start_tolerance_j"))
    if battery_a is not None and battery_b is not None:
        if abs(battery_a - battery_b) > (battery_tolerance if battery_tolerance is not None else 0.0):
            problems.append("starting battery energy differs")
    elif battery_a is None and battery_b is None:
        soc_a = _initial_soc_fraction(ref)
        soc_b = _initial_soc_fraction(contrast)
        soc_tolerance = _number(policy.get("battery_start_tolerance_fraction"))
        if soc_a is None or soc_b is None:
            problems.append("starting battery state unavailable with verified matching units")
        elif abs(soc_a - soc_b) > (soc_tolerance if soc_tolerance is not None else 0.0):
            problems.append("starting battery SOC fraction differs")
    else:
        problems.append("starting battery units differ or are unavailable")
    # Either a verified common preparation identifier or explicit, observed
    # per-tire initial wear and temperature can establish a tire match.
    prep_a = ref["manifest"].get("tire_preparation_hash")
    prep_b = contrast["manifest"].get("tire_preparation_hash")
    if prep_a is not None and prep_a == prep_b:
        tire_matched = True
    else:
        tire_a = _initial(ref, "tires")
        tire_b = _initial(contrast, "tires")
        wear_tol = _number(policy.get("tire_start_tolerance"))
        temp_tol = _number(policy.get("tire_temperature_start_tolerance_c"))
        tire_matched = bool(isinstance(tire_a, dict) and isinstance(tire_b, dict) and
                            wear_tol is not None and temp_tol is not None)
        if tire_matched:
            for wheel in WHEELS:
                a = tire_a.get(wheel, {})
                b = tire_b.get(wheel, {})
                if not isinstance(a, dict) or not isinstance(b, dict):
                    tire_matched = False
                    break
                wear_a, wear_b = _number(a.get("wear_fraction")), _number(b.get("wear_fraction"))
                temp_a, temp_b = _number(a.get("temperature_c")), _number(b.get("temperature_c"))
                if None in (wear_a, wear_b, temp_a, temp_b) or abs(wear_a - wear_b) > wear_tol or abs(temp_a - temp_b) > temp_tol:
                    tire_matched = False
                    break
    if not tire_matched:
        problems.append("matched fresh-tire preparation not verified")
    for field in ("tire_compound", "weather_hash", "grip_hash", "assist_hash"):
        a = ref["manifest"].get(field)
        b = contrast["manifest"].get(field)
        if a is not None or b is not None:
            if a is None or b is None or a != b:
                problems.append(f"{field} differs or is unavailable")
    return not problems, problems


def _fuel_contrast(reference_runs: list[dict[str, Any]], contrast_runs: list[dict[str, Any]],
                   config: Mapping[str, Any], assumptions: Mapping[str, Any]) -> dict[str, Any]:
    result = {"status": "unavailable", "exploratory": True, "seconds_per_litre": None,
              "seconds_per_kg": None, "reference_run_ids": [r["run_id"] for r in reference_runs],
              "contrast_run_ids": [r["run_id"] for r in contrast_runs],
              "evidence": _evidence(reference_runs + contrast_runs),
              "calculation_mode": "local_exploratory_fit",
              "physical_effect_status": "observed_trace" if any(r["mode"] == "MEASURED" for r in reference_runs + contrast_runs) else "synthetic_only",
              "warnings": []}
    if len(reference_runs) != 1 or len(contrast_runs) != 1:
        result["warnings"].append("one matched reference and one fuel-contrast run required")
        return result
    ref, con = reference_runs[0], contrast_runs[0]
    if ref["mode"] != con["mode"]:
        result["warnings"].append("synthetic and measured runs cannot form a contrast")
        return result
    matched, problems = _matched_conditions(ref, con, config.get("policy", {}))
    result["warnings"].extend(problems)
    ref_fuel = _number(_initial(ref, "fuel_l"))
    con_fuel = _number(_initial(con, "fuel_l"))
    if ref_fuel is None or con_fuel is None:
        result["warnings"].append("observed initial fuel in liters unavailable")
        return result
    ref_times = [lap["lap_time_s"] for lap in ref["laps"]]
    con_times = [lap["lap_time_s"] for lap in con["laps"]]
    if not ref_times or not con_times:
        result["warnings"].append("clean timed laps missing")
        return result
    density = _number(assumptions.get("fuel_density_kg_per_l"))
    contrast = fuel_local_contrast(ref_times, con_times, ref_fuel, con_fuel,
                                   density_kg_per_l=density, tire_matched=matched,
                                   battery_matched=matched)
    result.update(status="available" if contrast.seconds_per_litre is not None else "unavailable",
                  seconds_per_litre=contrast.seconds_per_litre,
                  seconds_per_kg=contrast.seconds_per_kg,
                  reference_laps=contrast.reference_laps,
                  contrast_laps=contrast.contrast_laps)
    result["warnings"].extend(contrast.warnings)
    return result


def _samples(store: Store, run_id: str) -> list[dict[str, Any]]:
    return list(store.iter_records(run_id, "sample"))


def _program_samples(samples: list[dict[str, Any]], laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = [(lap["sim_start_s"], lap["sim_end_s"]) for lap in laps
               if lap["sim_start_s"] is not None and lap["sim_end_s"] is not None]
    return [sample for sample in samples
            if (time_s := _number(sample.get("sim_time_s"))) is not None and
            any(start <= time_s <= end for start, end in windows)]


def _energy_power_integration(samples: list[dict[str, Any]], start_j: float | None,
                              positive_means: str | None) -> dict[str, Any] | None:
    if start_j is None or positive_means not in {"discharge", "recovery"}:
        return None
    intervals: list[EnergySample] = []
    for first, second in zip(samples, samples[1:]):
        t0, t1 = _number(first.get("sim_time_s")), _number(second.get("sim_time_s"))
        if t0 is None or t1 is None or not 0 < t1 - t0 <= 5:
            continue
        power = _channel(first["data"], "battery_power_w", ("W",))
        if power is None:
            return None
        intervals.append(EnergySample(t1 - t0, signed_battery_power(power, positive_means=positive_means),
                                      _channel(first["data"], "requested_power_w", ("W",)),
                                      _channel(first["data"], "delivered_power_w", ("W",))))
    return asdict(integrate_energy(intervals, start_j)) if intervals else None


def _energy_summary(store: Store, run: dict[str, Any] | None, reference_pace_s: float | None) -> dict[str, Any]:
    result = {"status": "unavailable", "run_id": run["run_id"] if run else None,
              "starting_battery_j": None, "ending_battery_j": None, "deployed_j": None,
              "recovered_j": None, "net_discharge_j": None, "requested_j": None,
              "delivered_j": None, "deployment_time_gain_s": None,
              "recovery_time_cost_s": None, "remaining_debt_j": None,
              "projected_repayment_time_cost_s": None, "net_time_benefit_s": None,
              "conditional_on_energy_repayment": None,
              "evidence": [run["mode"]] if run else [],
              "calculation_mode": "observed_cycle_with_conditional_repayment",
              "physical_effect_status": "observed_trace" if run and run["mode"] == "MEASURED" else "synthetic_only" if run else "unavailable",
              "warnings": []}
    if run is None:
        result["warnings"].append("energy contrast run unavailable")
        return result
    laps = run["laps"]
    start = _number(_initial(run, "battery_j"))
    deltas = [lap["battery_delta_j"] for lap in laps]
    samples = _program_samples(_samples(store, run["run_id"]), laps)
    def battery_energy(sample: Mapping[str, Any]) -> float | None:
        data = sample["data"]
        direct = _channel(data, "battery_j", ("J",))
        return direct if direct is not None else _channel(data, "battery_energy_j", ("J",))
    if start is None:
        start = next((battery_energy(sample) for sample in samples if battery_energy(sample) is not None), None)
    result["starting_battery_j"] = start
    if start is not None and laps and all(delta is not None for delta in deltas):
        # Per-lap delta is ending minus starting energy. Positive is recovery.
        deployed = sum(max(0.0, -delta) for delta in deltas)
        recovered = sum(max(0.0, delta) for delta in deltas)
        end = start + sum(deltas)
        result.update(status="available", ending_battery_j=end, deployed_j=deployed,
                      recovered_j=recovered, net_discharge_j=deployed - recovered,
                      remaining_debt_j=max(0.0, start - end))
    else:
        endpoint = next((battery_energy(sample) for sample in reversed(samples)
                         if battery_energy(sample) is not None), None)
        if start is not None and endpoint is not None:
            result.update(status="partial", ending_battery_j=endpoint,
                          net_discharge_j=start - endpoint,
                          remaining_debt_j=max(0.0, start - endpoint))
        result["warnings"].append("per-lap energy delta unavailable; deployment and recovery cannot be separated")
    power = _energy_power_integration(samples, start, run["manifest"].get("battery_power_positive_means"))
    if power is not None:
        result["power_integration"] = power
        result["requested_j"] = power["requested_j"]
        result["delivered_j"] = power["delivered_j"]
    else:
        result["warnings"].append("signed battery-power integration unavailable or source convention undeclared")
    if reference_pace_s is not None and len(laps) >= 4:
        deploy_times = [lap["lap_time_s"] for lap in laps[:2]]
        recovery_times = [lap["lap_time_s"] for lap in laps[2:4]]
        gain = sum(reference_pace_s - item for item in deploy_times)
        cost = sum(item - reference_pace_s for item in recovery_times)
        result.update(deployment_time_gain_s=gain, recovery_time_cost_s=cost)
        recovery_deltas = [lap["battery_delta_j"] for lap in laps[2:4]]
        per_lap_recovery = _mean([delta for delta in recovery_deltas if delta is not None])
        per_lap_cost = _mean([item - reference_pace_s for item in recovery_times])
        debt = result["remaining_debt_j"]
        if debt is not None:
            repayment = project_energy_repayment(debt, net_recovery_j_per_lap=per_lap_recovery,
                                                  cost_s_per_recovery_lap=per_lap_cost)
            result["projected_repayment_time_cost_s"] = repayment.projected_time_cost_s
            result["conditional_on_energy_repayment"] = debt > 0
            if repayment.warning:
                result["warnings"].append(repayment.warning)
            if repayment.projected_time_cost_s is not None:
                result["net_time_benefit_s"] = gain - cost - repayment.projected_time_cost_s
    return result


def _tire_summary(runs: list[dict[str, Any]], store: Store) -> dict[str, Any]:
    laps = [lap for run in runs for lap in run["laps"]]
    tires = {wheel: [] for wheel in WHEELS}
    for lap in laps:
        state = lap["tire_state"]
        if not isinstance(state, dict):
            continue
        for wheel in WHEELS:
            wheel_state = state.get(wheel)
            if not isinstance(wheel_state, dict):
                continue
            wear = _number(wheel_state.get("wear_fraction"))
            temperature = _number(wheel_state.get("temperature_c"))
            if wear is not None or temperature is not None:
                tires[wheel].append({"wear_fraction": wear, "temperature_c": temperature})
    per_tire = {}
    for wheel, states in tires.items():
        first, last = (states[0], states[-1]) if states else (None, None)
        per_tire[wheel] = {
            "first": first, "last": last,
            "wear_change_fraction": last["wear_fraction"] - first["wear_fraction"]
            if first and last and first["wear_fraction"] is not None and last["wear_fraction"] is not None else None,
            "observed_laps": len(states),
        }
    raw_native = set()
    for run in runs:
        for sample in _samples(store, run["run_id"]):
            raw_native.update(name for name in sample["data"] if name.startswith(("tyre_wear_", "tyre_core_c_")))
    return {"status": "available" if any(tires.values()) else "unavailable",
            "per_tire": per_tire, "raw_native_channels": sorted(raw_native),
            "wear_definition": "explicit wear_fraction from lap event only; native tyre wear/grip is not interpreted as remaining life",
            "evidence": _evidence(runs),
            "calculation_mode": "observed_per_tire_state",
            "physical_effect_status": "observed_trace" if any(run["mode"] == "MEASURED" for run in runs) else "synthetic_only",
            "warnings": [] if any(tires.values()) else ["per-tire wear/temperature state unavailable"]}


def _intervention_status(store: Store, runs: list[dict[str, Any]]) -> dict[str, Any]:
    status = "unavailable" if not runs else "unverified"
    checks = []
    for run in runs:
        for record in store.iter_records(run["run_id"], "event"):
            data = record["data"]
            if data.get("type") not in {"thermal_intervention_effect", "control_effect"}:
                continue
            requested = _number(data.get("requested_reduction_fraction"))
            observed = _number(data.get("observed_reduction_fraction"))
            tolerance = _number(data.get("effect_tolerance_fraction"))
            verified = bool(
                run["mode"] == "MEASURED" and data.get("effect_verified") is True and
                data.get("battery_limit_excluded") is True and
                data.get("rule_limit_excluded") is True and
                data.get("baseline_comparable") is True and
                requested is not None and observed is not None and
                abs(requested - observed) <= (tolerance if tolerance is not None else 0.05)
            )
            checks.append({"run_id": run["run_id"], "requested_reduction_fraction": requested,
                           "observed_reduction_fraction": observed, "verified": verified})
            if verified:
                status = "verified"
    if runs and all(run["mode"] == "SYNTHETIC" for run in runs):
        status = "synthetic_only"
    return {"status": status, "run_ids": [run["run_id"] for run in runs],
            "checks": checks, "calculation_mode": "intervention_readback_check",
            "physical_effect_status": status,
            "warnings": [] if status == "verified" else
                        ["requested control has no verified live physical effect"]}


def _thermal_trace(store: Store, run: dict[str, Any] | None) -> tuple[list[ThermalSample], list[str]]:
    if run is None:
        return [], ["reference run unavailable"]
    samples = _program_samples(_samples(store, run["run_id"]), run["laps"])
    pieces: list[tuple[float, float, float]] = []
    for first, second in zip(samples, samples[1:]):
        t0, t1 = _number(first.get("sim_time_s")), _number(second.get("sim_time_s"))
        if t0 is None or t1 is None or not 0 < t1 - t0 <= 5:
            continue
        speed = _channel(first["data"], "speed_m_s", ("m/s",))
        if speed is None:
            speed_kmh = _channel(first["data"], "speed_kmh", ("km/h",))
            speed = speed_kmh / 3.6 if speed_kmh is not None else None
        throttle = _channel(first["data"], "throttle", ("fraction",))
        brake = _channel(first["data"], "brake", ("fraction",))
        if speed is None or throttle is None:
            continue
        # Brake recovery contributes to the lumped electrical workload.  The
        # weighting is an exposed engineering assumption, not a fitted power.
        workload = min(1.0, max(0.0, 0.8 * throttle + 0.2 * (brake or 0.0)))
        pieces.append((t1 - t0, workload, max(0.0, speed)))
    if not pieces:
        return [], ["speed/throttle trace with valid simulation-time intervals unavailable"]
    # Coalesce adjacent intervals to keep a full R stint computationally
    # bounded while preserving time-weighted load and total duration.
    step_limit = 400
    group_size = max(1, math.ceil(len(pieces) / step_limit))
    trace = []
    for start in range(0, len(pieces), group_size):
        group = pieces[start:start + group_size]
        duration = sum(item[0] for item in group)
        trace.append(ThermalSample(duration,
                                   sum(item[0] * item[1] for item in group) / duration,
                                   sum(item[0] * item[2] for item in group) / duration,
                                   0.0))
    return trace, ["workload proxy = 0.8 throttle + 0.2 brake; trace intervals aggregated"]


def _ai_channel(data: Mapping[str, Any], name: str, unit: str) -> float | None:
    """Read only a fresh, explicitly unit-labeled native observation."""
    raw = data.get(name)
    if (not isinstance(raw, dict) or raw.get("unit") != unit or
            raw.get("source") != "CSP_STATE" or raw.get("quality") != "available"):
        return None
    return _number(raw.get("value"))


def _ai_controlled(data: Mapping[str, Any]) -> bool:
    raw = data.get("is_ai_controlled")
    return (isinstance(raw, dict) and raw.get("value") is True and
            raw.get("unit") == "boolean" and raw.get("source") == "CSP_STATE" and
            raw.get("quality") == "available")


def _ai_boolean(data: Mapping[str, Any], name: str, source: str = "CSP_STATE") -> bool | None:
    raw = data.get(name)
    if (not isinstance(raw, dict) or raw.get("unit") != "boolean" or
            raw.get("source") != source or raw.get("quality") != "available" or
            not isinstance(raw.get("value"), bool)):
        return None
    return raw["value"]


def _ai_segments(store: Store, run_id: str) -> tuple[list[list[ThermalSample]], dict[str, Any]]:
    """Use AI readback, not lap validity or a requested autopilot command.

    Every recording gap or physical-session change starts a new segment. The
    model will initialize each segment independently rather than pretending a
    hidden native or modeled component state continued across that break.
    """
    samples = _samples(store, run_id)
    controlled = [item for item in samples if _ai_controlled(item["data"])]
    segments: list[list[ThermalSample]] = []
    current: list[ThermalSample] = []
    interval_count = 0
    omitted_intervals = 0
    speed_weighted = workload_weighted = duration = 0.0
    ambient_values: list[float] = []
    track_values: list[float] = []
    native_water_values: list[float] = []
    native_oil_values: list[float] = []
    for item in controlled:
        data = item["data"]
        ambient = _ai_channel(data, "ambient_air_c", "degC")
        track = _ai_channel(data, "track_surface_c", "degC")
        if ambient is not None:
            ambient_values.append(ambient)
        if track is not None:
            track_values.append(track)
        water = _ai_channel(data, "native_water_temperature_c_approx", "degC")
        if water is not None:
            native_water_values.append(water)
        oil = _ai_channel(data, "native_oil_temperature", "native_unit_unverified")
        if oil is not None:
            native_oil_values.append(oil)
    for first, second in zip(samples, samples[1:]):
        first_data, second_data = first["data"], second["data"]
        t0, t1 = _number(first.get("sim_time_s")), _number(second.get("sim_time_s"))
        dt = t1 - t0 if t0 is not None and t1 is not None else None
        same_physical_session = (first.get("physical_session_id") or run_id) == (second.get("physical_session_id") or run_id)
        paused = (_ai_boolean(first_data, "sim_paused") is True or
                  _ai_boolean(second_data, "sim_paused") is True or
                  _ai_boolean(first_data, "experiment_paused", "WIMNA_APP") is True or
                  _ai_boolean(second_data, "experiment_paused", "WIMNA_APP") is True)
        if (not _ai_controlled(first_data) or not _ai_controlled(second_data) or
                not same_physical_session or paused or dt is None or not 0 < dt <= 0.5):
            omitted_intervals += 1
            if current:
                segments.append(current)
                current = []
            continue
        speed_kmh = _ai_channel(first_data, "speed_kmh", "km/h")
        throttle = _ai_channel(first_data, "throttle", "fraction")
        brake = _ai_channel(first_data, "brake", "fraction")
        if (speed_kmh is None or speed_kmh < 0 or throttle is None or brake is None or
                not 0 <= throttle <= 1 or not 0 <= brake <= 1):
            omitted_intervals += 1
            if current:
                segments.append(current)
                current = []
            continue
        speed = speed_kmh / 3.6
        workload = 0.8 * throttle + 0.2 * brake
        current.append(ThermalSample(dt, workload, speed, 0.0))
        interval_count += 1
        duration += dt
        speed_weighted += speed * dt
        workload_weighted += workload * dt
    if current:
        segments.append(current)
    native_summary: dict[str, Any] = {}
    if native_water_values:
        native_summary["native_water_temperature_c_approx"] = {
            "minimum_c": min(native_water_values), "maximum_c": max(native_water_values),
            "unit": "degC", "sample_count": len(native_water_values),
            "evidence": "MEASURED", "source": "CSP_STATE",
            "limitation": "approximate original-AC water state; not a custom electrical or cooling node",
        }
    if native_oil_values:
        native_summary["native_oil_temperature"] = {
            "minimum_native": min(native_oil_values), "maximum_native": max(native_oil_values),
            "unit": "native_unit_unverified", "sample_count": len(native_oil_values),
            "evidence": "MEASURED", "source": "CSP_STATE",
            "limitation": "unit unverified; cannot compare with Celsius thermal thresholds or custom nodes",
        }
    metrics = {
        "sample_count": len(samples), "ai_controlled_sample_count": len(controlled),
        "valid_interval_count": interval_count, "omitted_interval_count": omitted_intervals,
        "segment_count": len(segments), "observed_duration_s": duration,
        "mean_speed_m_s": speed_weighted / duration if duration else None,
        "mean_workload_fraction": workload_weighted / duration if duration else None,
        "ambient_air_c": {"median_c": median(ambient_values) if ambient_values else None,
                          "range_c": [min(ambient_values), max(ambient_values)] if ambient_values else None,
                          "sample_count": len(ambient_values), "evidence": "MEASURED" if ambient_values else "UNAVAILABLE"},
        "track_surface_c": {"median_c": median(track_values) if track_values else None,
                            "range_c": [min(track_values), max(track_values)] if track_values else None,
                            "sample_count": len(track_values), "evidence": "MEASURED" if track_values else "UNAVAILABLE"},
        "native_component_observations": native_summary,
        "workload_proxy": "0.8 throttle fraction + 0.2 brake fraction; not measured electrical power",
    }
    return segments, metrics


def _ai_thermal_checks(store: Store, runs: list[dict[str, Any]],
                       assumptions: Mapping[str, Any]) -> dict[str, Any]:
    ai_runs = [run for run in runs if run["raw_manifest"].get("selection") == "ai_thermal"]
    output = []
    profiles = thermal_assumption_profiles()
    for run in ai_runs:
        run_id = run["run_id"]
        manifest = run["raw_manifest"]
        segments, metrics = _ai_segments(store, run_id) if run["mode"] == "MEASURED" else ([], {})
        observed_ambient = (metrics.get("ambient_air_c") or {}).get("median_c")
        observed_track = (metrics.get("track_surface_c") or {}).get("median_c")
        ambient = observed_ambient
        ambient_evidence = "MEASURED" if ambient is not None else "UNAVAILABLE"
        if ambient is None:
            ambient = _number(assumptions.get("ai_thermal_ambient_c"))
            if ambient is None:
                ambient = _number(assumptions.get("ambient_c"))
            if ambient is not None:
                ambient_evidence = "ASSUMED"
        track = observed_track
        track_evidence = "MEASURED" if track is not None else "UNAVAILABLE"
        if track is None:
            track = _number(assumptions.get("ai_thermal_track_c"))
            if track is not None:
                track_evidence = "ASSUMED"
        warnings = ["AI-controlled native-invalid laps are supplementary workload evidence, never Tyler pace or campaign calibration",
                    "custom thermal nodes, heat balances and thresholds are engineering assumptions; no native electrical temperature or live derating is verified"]
        if not segments:
            warnings.append("no usable interval with observed AI control and complete speed/throttle/brake channels")
        if ambient is None:
            warnings.append("measured ambient air or an explicit ambient assumption is required")
        if track is None:
            warnings.append("measured track temperature or an explicit track-temperature assumption is required by the scenario model")
        if run["mode"] != "MEASURED":
            warnings.append("synthetic AI fixture cannot be reported as measured AI workload")
        computed: dict[str, Any] = {}
        if segments and ambient is not None and track is not None and run["mode"] == "MEASURED":
            scenario = ThermalScenario("observed_ai_trace", track, ambient,
                                       evidence=("MEASURED", "ASSUMED"))
            for name, profile in profiles.items():
                segment_results = [run_thermal_scenario(segment, profile, scenario, repeats=1)
                                   for segment in segments]
                computed[name] = {
                    "available": all(result.available for result in segment_results),
                    "peak_temperatures_c": {
                        node: max(result.peak_temperatures_c[node] for result in segment_results)
                        for node in profile.nodes
                    },
                    "time_above_warning_s": sum(result.time_above_warning_s or 0 for result in segment_results),
                    "time_above_derate_s": sum(result.time_above_derate_s or 0 for result in segment_results),
                    "derating_duration_s": sum(result.derating_duration_s or 0 for result in segment_results),
                    "segment_count": len(segment_results),
                    "calculation_mode": "ai_trace_custom_thermal_projection",
                    "physical_effect_status": "analysis_only",
                    "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"],
                    "warnings": list(dict.fromkeys(warning for result in segment_results for warning in result.warnings)),
                }
        output.append({
            "run_id": run_id, "track_id": manifest.get("track_id"), "layout_id": manifest.get("layout_id"),
            "selection": "ai_thermal", "campaign_counted": False,
            "ai_control_origin_requested": manifest.get("ai_control_origin"),
            "ai_control_readback": "observed_in_sample" if metrics.get("ai_controlled_sample_count", 0) else "unavailable",
            "status": "computed" if computed else "unavailable",
            "observed_trace": metrics,
            "scenario_inputs": {"ambient_air_c": ambient, "ambient_evidence": ambient_evidence,
                                "track_surface_c": track, "track_evidence": track_evidence,
                                "constant_environment_summary": "median of observed AI-controlled samples"},
            "modeled_profiles": computed,
            "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"] if computed else
                        (["MEASURED"] if run["mode"] == "MEASURED" else ["SYNTHETIC"]),
            "calculation_mode": "ai_trace_custom_thermal_projection",
            "physical_effect_status": "analysis_only", "warnings": warnings,
        })
    return {"status": "computed" if any(item["status"] == "computed" for item in output) else "unavailable",
            "runs": output, "campaign_counted": False,
            "calculation_mode": "supplementary_ai_trace_thermal_analysis",
            "physical_effect_status": "analysis_only"}


def _thermal_grid(store: Store, grouped: dict[str, dict[str, list[dict[str, Any]]]],
                  config: Mapping[str, Any], case_data: Mapping[str, Any] | None,
                  assumptions: Mapping[str, Any]) -> list[dict[str, Any]]:
    profiles = thermal_assumption_profiles()
    case_verified = case_data is not None and case_data.get("authority") == "Update6 workbook"
    scenario_specs = config.get("thermal_scenarios", {})
    ambient_default = _number(assumptions.get("ambient_c"))
    ambient_by_scenario = assumptions.get("ambient_by_scenario_c") or {}
    if not isinstance(ambient_by_scenario, dict):
        raise ValueError("ambient_by_scenario_c must be an object")
    repeats = int(assumptions.get("thermal_repeats", 3))
    if repeats < 1 or repeats > 60:
        raise ValueError("thermal_repeats must be 1..60")
    plan_c_airflow = _number(assumptions.get("plan_c_airflow_multiplier"))
    airflow_range = assumptions.get("plan_c_airflow_range")
    if not isinstance(airflow_range, (list, tuple)) or len(airflow_range) != 2:
        raise ValueError("plan_c_airflow_range must contain low and high values")
    airflow_low, airflow_high = (_number(item) for item in airflow_range)
    traffic_airflow = _number(assumptions.get("traffic_airflow_multiplier"))
    higher_workload = _number(assumptions.get("higher_deployment_workload_multiplier"))
    if (None in (plan_c_airflow, airflow_low, airflow_high, traffic_airflow, higher_workload) or
            min(plan_c_airflow, airflow_low, airflow_high, traffic_airflow, higher_workload) <= 0 or
            not airflow_low <= plan_c_airflow <= airflow_high):
        raise ValueError("airflow and workload multipliers must be positive")
    packages = (("baseline", "baseline", 1.0),
                ("Plan C", "low", airflow_low),
                ("Plan C", "base", plan_c_airflow),
                ("Plan C", "high", airflow_high))
    grid = []
    for track in config.get("track_order", []):
        refs = grouped.get(track, {}).get("reference", [])
        source = refs[0] if len(refs) == 1 else None
        trace, trace_warnings = _thermal_trace(store, source)
        for stress, profile in profiles.items():
            for scenario_name, default_track_c in (("reference", 29.0), ("elevated", 33.0), ("severe", 36.0)):
                specified = scenario_specs.get(scenario_name, {})
                track_c = _number(specified.get("track_temperature_c"))
                if track_c is None:
                    track_c = default_track_c
                ambient = _number(ambient_by_scenario.get(scenario_name))
                if ambient is None:
                    ambient = _number(specified.get("ambient_c"))
                if ambient is None:
                    ambient = ambient_default
                for package, package_assumption, package_airflow in packages:
                    for strategy in ("baseline", "higher_deployment"):
                        for exposure in ("clear_air", "traffic_assumption"):
                            airflow = package_airflow * (
                                traffic_airflow if exposure == "traffic_assumption" else 1.0)
                            workload = higher_workload if strategy == "higher_deployment" else 1.0
                            adjusted = [replace(sample, workload_fraction=sample.workload_fraction * workload)
                                        for sample in trace]
                            scenario = ThermalScenario(scenario_name, track_c, ambient, airflow,
                                                       ("CASE", "ASSUMED") if case_verified else ("ASSUMED",))
                            computed = run_thermal_scenario(adjusted, profile, scenario, repeats=repeats)
                            grid.append({
                                "track": track, "source_run_id": source["run_id"] if source else None,
                                "source_evidence": source["mode"] if source else None,
                                "thermal_stress_profile": stress, "scenario": scenario_name,
                                "track_temperature_c": track_c, "ambient_c": ambient,
                                "package": package, "package_assumption": package_assumption,
                                "energy_strategy": strategy, "cooling_exposure": exposure,
                                "package_airflow_multiplier": package_airflow,
                                "airflow_multiplier": airflow, "workload_multiplier": workload,
                                "result": asdict(computed),
                                "evidence": list(derived_evidence(
                                    (source["mode"],) if source else (),
                                    ("CASE", "ASSUMED") if case_verified else ("ASSUMED",))),
                                "calculation_mode": "scenario_projection",
                                "physical_effect_status": "analysis_only",
                                "warnings": trace_warnings + list(computed.warnings),
                            })
    return grid


def _thermal_strategy_comparison(grid: list[dict[str, Any]], assumptions: Mapping[str, Any]) -> dict[str, Any]:
    """Compare modeled thermal risk under common traces and explicit ranges."""
    index = {
        (row["track"], row["thermal_stress_profile"], row["scenario"],
         row["energy_strategy"], row["cooling_exposure"], row["package"], row["package_assumption"]): row
        for row in grid
    }
    comparisons = []
    across_profiles: dict[tuple[str, str, str, str], list[float]] = {}
    for row in grid:
        if row["package"] != "baseline":
            continue
        key = (row["track"], row["thermal_stress_profile"], row["scenario"],
               row["energy_strategy"], row["cooling_exposure"])
        baseline_peak = (row["result"].get("peak_temperatures_c") or {}).get("electrical_lumped")
        deltas = {}
        for package_assumption in ("low", "base", "high"):
            plan_c = index.get((*key, "Plan C", package_assumption))
            plan_c_peak = ((plan_c or {}).get("result", {}).get("peak_temperatures_c") or {}).get("electrical_lumped")
            deltas[package_assumption] = (
                plan_c_peak - baseline_peak
                if baseline_peak is not None and plan_c_peak is not None else None
            )
        low, base, high = (deltas[name] for name in ("low", "base", "high"))
        reversal = bool(low is not None and base is not None and low * base < -1e-9)
        if base is not None:
            across_profiles.setdefault((row["track"], row["scenario"], row["energy_strategy"],
                                        row["cooling_exposure"]), []).append(base)
        comparisons.append({
            "track": key[0], "thermal_stress_profile": key[1], "scenario": key[2],
            "energy_strategy": key[3], "cooling_exposure": key[4],
            "plan_c_minus_baseline_peak_electrical_c": deltas,
            "low_assumption_reverses_base_ranking": reversal,
            "status": "computed" if base is not None else "unavailable",
            "metric": "custom lumped electrical peak temperature; lower is cooler, not race time",
        })
    thermal_profile_reversal = any(
        min(values) < -1e-9 and max(values) > 1e-9
        for values in across_profiles.values() if values
    )
    airflow_reversal = any(item["low_assumption_reverses_base_ranking"] for item in comparisons)
    tire_range = assumptions.get("tire_time_delta_plan_c_minus_baseline_s_range")
    tire_reversal = None
    if tire_range is not None:
        if not isinstance(tire_range, (list, tuple)) or len(tire_range) != 2:
            raise ValueError("tire_time_delta_plan_c_minus_baseline_s_range must have low/high bounds")
        tire_low, tire_high = (_number(item) for item in tire_range)
        if tire_low is None or tire_high is None or tire_low > tire_high:
            raise ValueError("invalid Plan C tire time-delta range")
        tire_reversal = tire_low < 0 < tire_high
    available = any(item["status"] == "computed" for item in comparisons)
    reasons = []
    if airflow_reversal:
        reasons.append("Plan C low airflow assumption reverses the base-case modeled thermal ranking")
    if thermal_profile_reversal:
        reasons.append("modeled thermal ranking changes across low/base/high thermal stress profiles")
    if tire_reversal:
        reasons.append("declared tire time-delta range spans both strategy rankings")
    if tire_reversal is None:
        reasons.append("tire strategy ranking unavailable without a declared plausible tire time-delta range")
    return {
        "status": "computed" if available else "unavailable",
        "assumption_sensitive": (airflow_reversal or thermal_profile_reversal or tire_reversal)
        if available or tire_reversal is not None else None,
        "airflow_reversal": airflow_reversal if available else None,
        "thermal_profile_reversal": thermal_profile_reversal if available else None,
        "tire_range_reversal": tire_reversal,
        "tire_time_delta_plan_c_minus_baseline_s_range": list(tire_range) if tire_range is not None else None,
        "comparisons": comparisons,
        "reasons": reasons,
        "calculation_mode": "modeled_thermal_risk_comparison",
        "physical_effect_status": "analysis_only",
        "evidence": ["ASSUMED", "PHYSICS_DERIVED"],
        "no_race_time_winner_claimed": True,
    }


def _decode_tire_model(raw: Mapping[str, Any]) -> TireConfig:
    def curve(key: str):
        value = raw.get(key)
        return tuple(tuple(float(item) for item in point) for point in value) if value is not None else None
    return TireConfig(
        float(raw["wear_per_work_m"]), float(raw["heating_c_per_work_m"]),
        float(raw["cooling_per_s"]), float(raw["slip_gain"]),
        float(raw["track_coupling_fraction"]), curve("wear_pace_curve"), curve("temperature_pace_curve"),
    )


def _stint_projection(track: str, reference: dict[str, Any], energy: dict[str, Any],
                      tire: dict[str, Any], inputs: Mapping[str, Any] | None,
                      config: Mapping[str, Any]) -> dict[str, Any]:
    required = ["lap_count", "starting_fuel_l", "fuel_density_kg_per_l", "fuel_seconds_per_kg",
                "tire_model", "initial_tires", "tire_samples_per_lap", "usable_battery_j",
                "energy_samples_per_lap", "energy_pace_delta_s_per_lap",
                "thermal_samples_per_lap", "derate_seconds_at_full_reduction",
                "restriction_speed_response", "restriction_workload_response",
                "restriction_energy_response"]
    provided = dict(inputs or {})
    missing = [key for key in required if provided.get(key) is None]
    if reference["pace_mean_s"] is None:
        missing.append("measured reference pace")
    if reference["fuel_mean_l_per_lap"] is None:
        missing.append("measured fuel burn")
    if energy["starting_battery_j"] is None and provided.get("starting_battery_j") is None:
        missing.append("starting battery energy")
    if missing:
        return {"status": "unavailable", "missing_inputs": missing, "result": None,
                "calculation_mode": "scenario_projection", "physical_effect_status": "analysis_only",
                "warnings": ["longer-stint total withheld until required vehicle inputs or explicit assumptions are available"]}
    try:
        tires = {wheel: TireState(**value) for wheel, value in provided["initial_tires"].items()}
        tire_samples = {wheel: tuple(TireSample(**item) for item in items)
                        for wheel, items in provided["tire_samples_per_lap"].items()}
        thermal_samples = tuple(ThermalSample(**item) for item in provided["thermal_samples_per_lap"])
        energy_samples = tuple(EnergySample(**item) for item in provided["energy_samples_per_lap"])
        model = thermal_assumption_profiles()[provided.get("thermal_profile", "base")]
        stint = StintConfig(
            lap_count=int(provided["lap_count"]), base_lap_time_s=reference["pace_mean_s"],
            starting_fuel_l=float(provided["starting_fuel_l"]),
            fuel_burn_l_per_lap=reference["fuel_mean_l_per_lap"],
            fuel_density_kg_per_l=float(provided["fuel_density_kg_per_l"]),
            fuel_seconds_per_kg=float(provided["fuel_seconds_per_kg"]),
            initial_tires=tires, tire_samples_per_lap=tire_samples,
            tire_model=_decode_tire_model(provided["tire_model"]),
            starting_battery_j=float(provided["starting_battery_j"] if provided.get("starting_battery_j") is not None
                                     else energy["starting_battery_j"]),
            usable_battery_j=float(provided["usable_battery_j"]),
            energy_samples_per_lap=energy_samples,
            energy_pace_delta_s_per_lap=float(provided["energy_pace_delta_s_per_lap"]),
            thermal_model=model, thermal_samples_per_lap=thermal_samples,
            derate_seconds_at_full_reduction=float(provided["derate_seconds_at_full_reduction"]),
            restriction_speed_response=float(provided["restriction_speed_response"]),
            restriction_workload_response=float(provided["restriction_workload_response"]),
            restriction_energy_response=float(provided["restriction_energy_response"]),
            evidence=tuple(sorted(set(reference["evidence"] + ["ASSUMED"]))),
        )
        result = project_stint(stint)
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "unavailable", "missing_inputs": [], "result": None,
                "calculation_mode": "scenario_projection", "physical_effect_status": "analysis_only",
                "warnings": [f"declared stint inputs invalid: {exc}"]}
    return {"status": "computed" if result.total_time_s is not None else "partial",
            "missing_inputs": [], "result": asdict(result), "calculation_mode": "scenario_projection",
            "physical_effect_status": "analysis_only",
            "warnings": list(result.warnings)}


def _race_time_projection(track: str, reference: dict[str, Any], energy: dict[str, Any],
                          tire: dict[str, Any], longer_stint: dict[str, Any],
                          inputs: Mapping[str, Any] | None,
                          config: Mapping[str, Any]) -> dict[str, Any]:
    """Sum only complete modeled stints under declared race and reserve rules."""
    supplied = dict(inputs or {})
    result: dict[str, Any] = {
        "status": "unavailable", "track": track, "total_time_s": None,
        "race_laps": supplied.get("race_laps"), "pit_stops": supplied.get("pit_stops"),
        "full_pit_loss_s": supplied.get("full_pit_loss_s"),
        "fuel_reserve_l": supplied.get("fuel_reserve_l"),
        "battery_reserve_j": supplied.get("battery_reserve_j"),
        "stint_times_s": [], "pit_time_s": None,
        "ending_fuel_l": None, "ending_battery_j": None,
        "missing_inputs": [], "warnings": [],
        "calculation_mode": "conditional_race_projection",
        "physical_effect_status": "analysis_only",
        "evidence": sorted(set(reference["evidence"] + ["ASSUMED", "PHYSICS_DERIVED"])),
        "winner_claimed": False,
    }
    race_laps = supplied.get("race_laps")
    pit_stops = supplied.get("pit_stops")
    pit_loss = _number(supplied.get("full_pit_loss_s"))
    fuel_reserve = _number(supplied.get("fuel_reserve_l"))
    battery_reserve = _number(supplied.get("battery_reserve_j"))
    if not isinstance(race_laps, int) or isinstance(race_laps, bool) or race_laps <= 0:
        result["missing_inputs"].append("explicit positive race_laps")
    if not isinstance(pit_stops, int) or isinstance(pit_stops, bool) or pit_stops < 0:
        result["missing_inputs"].append("explicit nonnegative pit_stops")
    if pit_loss is None or pit_loss < 0:
        result["missing_inputs"].append("full_pit_loss_s, not relative case pit delta")
    if fuel_reserve is None or fuel_reserve < 0:
        result["missing_inputs"].append("nonnegative fuel_reserve_l")
    if battery_reserve is None or battery_reserve < 0:
        result["missing_inputs"].append("nonnegative battery_reserve_j")
    if result["missing_inputs"]:
        return result
    declared_stints = supplied.get("stints")
    if declared_stints is None:
        projected = [longer_stint]
    elif isinstance(declared_stints, list) and declared_stints and all(
            isinstance(item, Mapping) for item in declared_stints):
        projected = [_stint_projection(track, reference, energy, tire, item, config)
                     for item in declared_stints]
    else:
        result["missing_inputs"].append("modeled stint coverage")
        return result
    if any(item["status"] != "computed" or item["result"] is None for item in projected):
        result["missing_inputs"].append("complete modeled stint time")
        result["warnings"].extend(warning for item in projected for warning in item.get("warnings", []))
        return result
    laps = [item["result"]["laps"] for item in projected]
    if sum(len(item) for item in laps) != race_laps:
        result["missing_inputs"].append("modeled stint laps exactly cover selected race length")
    if pit_stops != len(projected) - 1:
        result["missing_inputs"].append("pit_stops consistent with stint boundaries")
    if result["missing_inputs"]:
        return result
    final_lap = laps[-1][-1]
    ending_fuel = _number(final_lap.get("ending_fuel_l"))
    ending_battery = _number(final_lap.get("ending_battery_j"))
    result["ending_fuel_l"] = ending_fuel
    result["ending_battery_j"] = ending_battery
    if ending_fuel is None or ending_fuel + 1e-9 < fuel_reserve:
        result["missing_inputs"].append("modeled finish fuel meets declared reserve")
    if ending_battery is None or ending_battery + 1e-9 < battery_reserve:
        result["missing_inputs"].append("modeled finish battery meets declared reserve")
    if result["missing_inputs"]:
        return result
    stint_times = [float(item["result"]["total_time_s"]) for item in projected]
    pit_time = pit_stops * pit_loss
    result.update(status="computed", stint_times_s=stint_times, pit_time_s=pit_time,
                  total_time_s=sum(stint_times) + pit_time,
                  warnings=["conditional projection from repeated short-run patterns; full-race behavior unvalidated"])
    return result


def _validation(store: Store, runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {run["run_id"]: run for run in runs
             if run["raw_manifest"].get("selection") != "ai_thermal"}
    items = []
    for frozen in store.iter_predictions():
        prediction_id = frozen["prediction_id"]
        fit_ids = frozen["fit_run_ids"]
        validation_run_id = frozen["validation_run_id"]
        model = frozen["model"]
        predicted = frozen["prediction"]
        run = by_id.get(validation_run_id)
        if validation_run_id is not None and validation_run_id in fit_ids:
            raise ValueError("validation run is also a fit run")
        lap_values = run["laps"] if run else []
        observations = {
            "mean_lap_time_s": _mean([lap["lap_time_s"] for lap in lap_values]),
            "fuel_l_per_lap": _mean([lap["fuel_used_l"] for lap in lap_values if lap["fuel_used_l"] is not None]),
            "battery_delta_j_per_lap": _mean([lap["battery_delta_j"] for lap in lap_values if lap["battery_delta_j"] is not None]),
        }
        residuals = {}
        for key, forecast in predicted.items():
            observed = observations.get(key)
            forecast_number = _number(forecast)
            residuals[key] = {"predicted": forecast_number, "observed": observed,
                              "residual_observed_minus_predicted": observed - forecast_number
                              if observed is not None and forecast_number is not None else None,
                              "unit": "s" if key.endswith("time_s") else "L/lap" if key == "fuel_l_per_lap" else
                                      "J/lap" if key == "battery_delta_j_per_lap" else None}
        items.append({"prediction_id": prediction_id, "fit_run_ids": fit_ids,
                      "validation_run_id": validation_run_id,
                      "held_out": bool(run is not None and validation_run_id not in fit_ids),
                      "model": model, "residuals": residuals,
                      "status": "computed" if run is not None and lap_values else "pending_or_unavailable",
                      "evidence": [run["mode"]] if run else [],
                      "calculation_mode": "held_out_residual",
                      "physical_effect_status": "observed_trace" if run and run["mode"] == "MEASURED" else "synthetic_only" if run else "unavailable"})
    return {"status": "computed" if any(item["status"] == "computed" for item in items) else "unavailable",
            "results": items, "model_version": MODEL_VERSION,
            "synthetic_excluded_from_measured": True}


def analyze_store(
    store: Store, config: Mapping[str, Any], case_data: Mapping[str, Any] | None = None,
    assumptions: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return ``(forecasts, validation, parameters)`` from saved raw data.

    Ambient air temperature is required for thermal calculations and is never
    copied from a case track temperature.  An incomplete set of inputs yields
    explicit nulls and reasons rather than an invented estimate.
    """
    selected = dict(DEFAULT_ASSUMPTIONS)
    if assumptions is not None:
        selected.update(assumptions)
    runs, grouped = _runs(store, config)
    collection = analyze_collections(store)
    selected_grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for track in config.get("track_order", []):
        groups = grouped.get(track, {})
        all_track_runs = [run for kind_runs in groups.values() for run in kind_runs]
        selected_mode = "MEASURED" if any(run["mode"] == "MEASURED" for run in all_track_runs) else "SYNTHETIC"
        selected_grouped[track] = {
            kind: [run for run in kind_runs if run["mode"] == selected_mode]
            for kind, kind_runs in groups.items()
        }
    tracks = {}
    for track in config.get("track_order", []):
        groups = selected_grouped.get(track, {})
        refs = groups.get("reference", [])
        reference = _reference(refs)
        energy_run = groups.get("energy", [])
        energy = _energy_summary(store, energy_run[0] if len(energy_run) == 1 else None,
                                 reference["pace_mean_s"])
        tire = _tire_summary(refs, store)
        longer_stint = _stint_projection(track, reference, energy, tire,
                                         (selected.get("stint_inputs") or {}).get(track), config)
        tracks[track] = {
            "calibration_evidence_mode": "MEASURED" if any(
                run["mode"] == "MEASURED" for kind_runs in grouped.get(track, {}).values() for run in kind_runs
            ) else "SYNTHETIC" if grouped.get(track) else "UNAVAILABLE",
            "reference": reference,
            "fuel_contrast": _fuel_contrast(refs, groups.get("fuel", []), config, selected),
            "energy_cycle": energy,
            "tires": tire,
            "intervention": _intervention_status(store, groups.get("validation", [])),
            "longer_stint": longer_stint,
            "race_time": _race_time_projection(track, reference, energy, tire, longer_stint,
                                               (selected.get("race_inputs") or {}).get(track), config),
        }
    grid = _thermal_grid(store, selected_grouped, config, case_data, selected)
    strategy_comparison = _thermal_strategy_comparison(grid, selected)
    ai_thermal = _ai_thermal_checks(store, runs, selected)
    measured_runs = [run["run_id"] for run in runs if run["mode"] == "MEASURED"
                     and run["raw_manifest"].get("selection") != "ai_thermal"]
    synthetic_runs = [run["run_id"] for run in runs if run["mode"] == "SYNTHETIC"]
    forecasts = {
        "status": "partial" if runs else "unavailable",
        "model_version": MODEL_VERSION,
        "track_order": list(config.get("track_order", [])),
        "calculation_mode": "observed_stint_inventory_and_conditional_projection",
        "physical_effect_status": "analysis_only_for_projections",
        "measured_run_ids": measured_runs,
        "supplementary_ai_run_ids": [item["run_id"] for item in ai_thermal["runs"]],
        "synthetic_run_ids": synthetic_runs,
        "synthetic_excluded_from_measured": True,
        "tracks": tracks,
        "thermal_grid": grid,
        "collection": collection,
        "ai_thermal": ai_thermal,
        "strategy_comparison": strategy_comparison,
        "limitations": ["Stint observations are primary inputs; the optional campaign blocks are one collection protocol.",
                        "Five-lap reference and three-lap validation do not validate a full stint.",
                        "Thermal nodes and Plan C airflow response are engineering assumptions.",
                        "Traffic cooling is an assumption unless real following was measured.",
                        "No DNF probability is inferred from case observations."],
    }
    validation = _validation(store, runs)
    profiles = thermal_assumption_profiles()
    version = str(config.get("program_version") or "FH26 unverified")
    parameters = {
        "model_version": MODEL_VERSION,
        "track_order": list(config.get("track_order", [])),
        "thermal_profiles": {name: {key: asdict(parameter) for key, parameter in profile.parameter_records.items()}
                             for name, profile in profiles.items()},
        "analysis_assumptions": {
            "ambient_c": _parameter(_number(selected.get("ambient_c")), "deg C air", "independent user input; required for thermal sweep", "ASSUMED", version),
            "ai_thermal_ambient_c": _parameter(_number(selected.get("ai_thermal_ambient_c")), "deg C air", "explicit fallback for AI thermal trace; measured ambient takes priority", "ASSUMED", version),
            "ai_thermal_track_c": _parameter(_number(selected.get("ai_thermal_track_c")), "deg C track", "explicit fallback for AI thermal trace; measured track temperature takes priority", "ASSUMED", version),
            "plan_c_airflow_multiplier": _parameter(_number(selected.get("plan_c_airflow_multiplier")), "ratio", "WIMNA engineering assumption", "ASSUMED", version,
                                                    *(_number(value) for value in selected.get("plan_c_airflow_range", [None, None]))),
            "traffic_airflow_multiplier": _parameter(_number(selected.get("traffic_airflow_multiplier")), "ratio", "WIMNA traffic cooling assumption; not measured following", "ASSUMED", version),
            "higher_deployment_workload_multiplier": _parameter(_number(selected.get("higher_deployment_workload_multiplier")), "ratio", "WIMNA engineering assumption", "ASSUMED", version),
            "thermal_repeats": _parameter(_number(selected.get("thermal_repeats")), "reference trace repetitions", "analysis selection", "ASSUMED", version),
            "fuel_density_kg_per_l": _parameter(_number(selected.get("fuel_density_kg_per_l")), "kg/L", "explicit density assumption or sourced input required", "ASSUMED", version),
        },
        "ambient_by_scenario_c": {
            name: _parameter(_number((selected.get("ambient_by_scenario_c") or {}).get(name)), "deg C air",
                             f"independent user input for {name} scenario", "ASSUMED", version)
            for name in ("reference", "elevated", "severe")
        },
        "tire_strategy_time_delta_range": {
            **_parameter(None, "s per selected stint", "explicit plausible Plan C minus baseline tire time range",
                         "ASSUMED", version,
                         *([_number(item) for item in selected["tire_time_delta_plan_c_minus_baseline_s_range"]]
                           if selected.get("tire_time_delta_plan_c_minus_baseline_s_range") is not None else [None, None])),
            "interpretation": "negative favors Plan C; positive favors baseline; no tire range is inferred from short runs",
        },
        "race_inputs": {
            track: {
                name: _parameter(_number(values.get(name)), unit,
                                 "explicit user-selected race rule or assumption", "ASSUMED", version)
                for name, unit in (("race_laps", "laps"), ("pit_stops", "stops"),
                                   ("full_pit_loss_s", "s/stop"),
                                   ("fuel_reserve_l", "L"), ("battery_reserve_j", "J"))
            }
            for track, values in (selected.get("race_inputs") or {}).items()
            if isinstance(values, Mapping)
        },
        "missing_is_zero": False,
        "case_source_sha256": case_data.get("source_sha256") if case_data else None,
        "collection_model_version": collection["model_version"],
        "collection_measured_run_ids": sorted({item["run_id"] for item in collection["stints"]
                                                if item["evidence"] == "MEASURED"}),
        "measured_run_ids": measured_runs,
        "supplementary_ai_run_ids": [item["run_id"] for item in ai_thermal["runs"]],
        "synthetic_run_ids": synthetic_runs,
    }
    calibrated = {}
    for track, values in tracks.items():
        selected_runs = [run for kind_runs in selected_grouped.get(track, {}).values() for run in kind_runs]
        def source_for(*kinds: str) -> str:
            ids = [run["run_id"] for kind in kinds for run in selected_grouped.get(track, {}).get(kind, [])]
            return "saved run(s): " + (", ".join(ids) if ids else "unavailable")
        applicable = next((str(run["manifest"].get("car_version")) for run in selected_runs
                           if run["manifest"].get("car_version") is not None), version)
        mode = values["calibration_evidence_mode"]
        direct_evidence = mode if mode in {"MEASURED", "SYNTHETIC"} else "PHYSICS_DERIVED"
        pace = values["reference"]
        pace_range = pace["pace_range_s"] or [None, None]
        fuel_range = pace["fuel_range_l_per_lap"] or [None, None]
        calibrated[track] = {
            "reference_pace_s": _parameter(pace["pace_mean_s"], "s/lap", source_for("reference"), direct_evidence,
                                           applicable, *pace_range),
            "reference_fuel_burn_l_per_lap": _parameter(pace["fuel_mean_l_per_lap"], "L/lap", source_for("reference"),
                                                        direct_evidence, applicable, *fuel_range),
            "local_fuel_sensitivity_s_per_l": {
                **_parameter(values["fuel_contrast"]["seconds_per_litre"], "s/L", source_for("reference", "fuel"),
                             "PHYSICS_DERIVED", applicable),
                "input_evidence": [mode] if mode != "UNAVAILABLE" else [],
                "exploratory": True,
            },
            "remaining_energy_debt_j": {
                **_parameter(values["energy_cycle"]["remaining_debt_j"], "J", source_for("energy"),
                             "PHYSICS_DERIVED", applicable),
                "input_evidence": [mode] if mode != "UNAVAILABLE" else [],
            },
        }
    parameters["calibrated_by_track"] = calibrated
    return forecasts, validation, parameters
