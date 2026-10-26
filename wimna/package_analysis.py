"""Exploratory paired analysis for the WIMNA baseline/Plan C physics prototype.

The package is identified by its exact, locally shipped parameter hashes.  A
shared-store command status identifies what was requested, not whether CSP
applied the added force or whether a real upgrade would have the same effect.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from statistics import median
from typing import Any, Mapping


PROTOTYPE_HASHES = {
    "baseline": "1b0fd209d1c46087bdbf3f8ff0ac6fe30472ae7938b29d662371772e0496bb12",
    "plan_c": "4cafb3a4cf76afe426126b660996fd29777a58f031322c4ccc9a2ef974697cd9",
}
MODES = {"baseline": "wimna-ai-baseline", "plan_c": "wimna-ai-plan-c"}
EXPECTED_EFFECT = {"baseline": "none", "plan_c": "worker_requested_effect_unverified"}
CONDITION_TOLERANCES = {
    "ambient_air_c": 1.0, "track_surface_c": 1.0, "fuel_l": 0.5,
    "road_grip_native": 0.005, "rain_wetness_native": 0.02,
    "tire_native_wear": 0.02, "tire_core_temperature_c": 5.0,
    "starting_battery_fraction": 0.05,
}


def _number(value: Any) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _field(data: Mapping[str, Any], name: str, unit: str) -> Any:
    item = data.get(name)
    if (not isinstance(item, Mapping) or item.get("unit") != unit or
            item.get("quality") != "available" or
            item.get("source") != "CSP_SESSION_SHARED_STORE"):
        return None
    return item.get("value")


def prototype_from_samples(manifest: Any, samples: list[Mapping[str, Any]],
                           session_mode: Any) -> dict[str, Any]:
    """Verify a mode's parameter identity and consistency across raw samples."""
    result: dict[str, Any] = {
        "status": "unavailable", "variant_id": None, "parameter_hash": None,
        "parameter_hash_calculated": None, "parameters": None,
        "sample_count": len(samples), "identity_sample_count": 0,
        "effect_statuses": [], "physics_permission_reported": None,
        "force_native_lap_invalid_expected": None,
        "physical_effect_verified": False, "issues": [],
    }
    if not isinstance(manifest, Mapping):
        result["issues"].append("prototype run manifest unavailable")
        return result
    variant = manifest.get("variant_id")
    reported_hash = manifest.get("parameter_hash_reported")
    parameters_text = manifest.get("parameters_json_requested")
    result["variant_id"] = variant
    result["parameter_hash"] = reported_hash
    if not isinstance(parameters_text, str) or not isinstance(reported_hash, str):
        result["issues"].append("parameter JSON or reported hash unavailable")
        return result
    try:
        parameters = json.loads(parameters_text)
    except (ValueError, TypeError):
        result["issues"].append("parameter JSON is invalid")
        return result
    if not isinstance(parameters, Mapping):
        result["issues"].append("parameter JSON must be an object")
        return result
    result["parameters"] = parameters
    result["parameter_hash_calculated"] = hashlib.sha256(
        parameters_text.encode("utf-8")).hexdigest()
    if variant not in PROTOTYPE_HASHES or parameters.get("variant") != variant:
        result["issues"].append("prototype variant unrecognized or parameter variant differs")
    elif (reported_hash != PROTOTYPE_HASHES[variant] or
          result["parameter_hash_calculated"] != reported_hash):
        result["issues"].append("prototype parameter hash differs from shipped version")
    if variant in MODES and session_mode != MODES[variant]:
        result["issues"].append("prototype mode and variant differ")

    observed_variants, observed_hashes, statuses = set(), set(), set()
    invalid_values, permissions = set(), set()
    for data in samples:
        sample_variant = _field(data, "prototype_variant_requested", "identifier")
        sample_hash = _field(data, "prototype_parameter_hash_reported", "sha256_reported")
        status = _field(data, "prototype_effect_status", "status_not_physical_effect")
        permission = _field(data, "prototype_physics_permission_reported", "boolean_0_or_1")
        invalid = _field(data, "force_native_lap_invalid_expected", "boolean")
        if sample_variant is not None and sample_hash is not None:
            result["identity_sample_count"] += 1
        if sample_variant is not None:
            observed_variants.add(sample_variant)
        if sample_hash is not None:
            observed_hashes.add(sample_hash)
        if status is not None:
            statuses.add(status)
        if permission is not None:
            permissions.add(permission)
        if invalid is not None:
            invalid_values.add(invalid)
    result["effect_statuses"] = sorted(statuses)
    result["physics_permission_reported"] = next(iter(permissions)) if len(permissions) == 1 else None
    result["force_native_lap_invalid_expected"] = (
        next(iter(invalid_values)) if len(invalid_values) == 1 else None)
    if not samples or result["identity_sample_count"] / len(samples) < 0.95:
        result["issues"].append("prototype identity absent from more than 5% of stint samples")
    if observed_variants != {variant} or observed_hashes != {reported_hash}:
        result["issues"].append("sample prototype variant or hash differs from manifest")
    if statuses - {"pending", EXPECTED_EFFECT.get(variant)} or EXPECTED_EFFECT.get(variant) not in statuses:
        result["issues"].append("prototype worker status unavailable or failed")
    if permissions != {1}:
        result["issues"].append("physics permission not observed throughout stint")
    if invalid_values != {variant == "plan_c"}:
        result["issues"].append("native invalidation expectation inconsistent")
    result["status"] = "identity_verified" if not result["issues"] else "identity_unverified"
    return result


def _median_state(stint: Mapping[str, Any], key: str, point: str = "median") -> float | None:
    return _number((stint.get(key) or {}).get(point))


def _eligible_laps(stint: Mapping[str, Any]) -> tuple[list[float], list[dict[str, Any]]]:
    times: list[float] = []
    inventory = []
    identity = (stint.get("prototype") or {}).get("status") == "identity_verified"
    for lap in stint.get("laps") or []:
        reasons = []
        if stint.get("evidence") != "MEASURED":
            reasons.append("not measured simulator evidence")
        if lap.get("evidence") != "MEASURED":
            reasons.append("lap is not measured simulator evidence")
        if not identity:
            reasons.append("prototype identity unverified")
        if lap.get("controller_observed") != "AI":
            reasons.append("lap not wholly AI controlled")
        if lap.get("native_cuts") != 0:
            reasons.append("zero native cuts not observed")
        if not lap.get("usable_for_exploratory_pace"):
            reasons.extend(lap.get("quality_reasons") or ["lap trace not usable for pace"])
        lap_time = _number(lap.get("lap_time_s"))
        if lap_time is None or lap_time <= 0:
            reasons.append("lap time unavailable")
        included = not reasons
        if included:
            times.append(lap_time)
        inventory.append({
            "run_id": stint.get("run_id"),
            "stint_id": stint.get("collection_stint_id"),
            "physical_session_id": stint.get("physical_session_id"),
            "variant_id": (stint.get("prototype") or {}).get("variant_id"),
            "lap_index": lap.get("lap_index"), "lap_time_s": lap_time,
            "native_valid": lap.get("native_valid"),
            "native_cuts": lap.get("native_cuts"),
            "original_pace_use": lap.get("pace_use"),
            "package_pace_use": "exploratory_ai" if included else "excluded",
            "included_in_pair": included, "exclusion_reasons": sorted(set(reasons)),
            "quality_reasons": lap.get("quality_reasons") or [],
            "evidence": lap.get("evidence"),
        })
    return times, inventory


def _conditions(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[list[str], list[str], float]:
    issues: list[str] = []
    limits: list[str] = []
    score = 0.0
    for field in ("track_id", "layout_id", "car_id", "car_version",
                  "csp_version_code", "extended_physics", "compound_observed"):
        if a.get(field) in (None, "") or b.get(field) in (None, ""):
            issues.append(field + " unavailable")
        elif a[field] != b[field]:
            issues.append(field + " differs")
    if a.get("extended_physics") is not True or b.get("extended_physics") is not True:
        issues.append("extended physics not observed true")
    for item in (a, b):
        if item.get("session_mode_changed_within_stint") or item.get("extended_physics_changed_within_stint"):
            issues.append("native mode or physics changed during stint")
        if item.get("compound_change_count", 0):
            issues.append("compound changed during stint")
        if (item.get("fuel_l") or {}).get("refill_detected"):
            issues.append("fuel refilled during stint")
    for field, point in (("ambient_air_c", "median"), ("track_surface_c", "median"),
                         ("fuel_l", "first")):
        left, right = _median_state(a, field, point), _median_state(b, field, point)
        if left is None or right is None:
            issues.append(field + " observed value unavailable")
        else:
            difference = abs(left - right)
            score += difference / CONDITION_TOLERANCES[field]
            if difference > CONDITION_TOLERANCES[field]:
                issues.append(field + " outside pairing tolerance")
    for field in ("road_grip_native", "rain_wetness_native"):
        left, right = _median_state(a, field), _median_state(b, field)
        if left is None or right is None:
            limits.append(field + " unavailable")
        elif abs(left - right) > CONDITION_TOLERANCES[field]:
            issues.append(field + " differs")
    left_time = _median_state(a, "time_multiplier_native", "first")
    right_time = _median_state(b, "time_multiplier_native", "first")
    if left_time is None or right_time is None:
        limits.append("time multiplier unavailable")
    elif left_time != right_time:
        issues.append("time multiplier differs")
    for field in ("setup_hash", "profile_hash"):
        if a.get(field) and b.get(field):
            if a[field] != b[field]:
                issues.append(field + " differs")
        else:
            limits.append(field + " unavailable; setup/profile matching unverified")
    if a.get("condition_label") != b.get("condition_label"):
        limits.append("requested condition labels differ; observed temperatures govern pairing")
    energy_a, energy_b = a.get("energy_native") or {}, b.get("energy_native") or {}
    for field in ("mguk_delivery_native", "mguk_recovery_native"):
        left = _number((energy_a.get(field) or {}).get("first"))
        right = _number((energy_b.get(field) or {}).get("first"))
        if left is None or right is None:
            limits.append(field + " unavailable")
        elif left != right:
            issues.append(field + " differs")
    charge_a = _number((energy_a.get("kers_current_kj_native") or {}).get("first"))
    charge_b = _number((energy_b.get("kers_current_kj_native") or {}).get("first"))
    cap_a = _number((energy_a.get("kers_max_kj_native") or {}).get("first"))
    cap_b = _number((energy_b.get("kers_max_kj_native") or {}).get("first"))
    if None in (charge_a, charge_b, cap_a, cap_b) or min(cap_a, cap_b) <= 0:
        limits.append("starting battery energy/capacity unavailable or native semantics unverified")
    elif abs(charge_a - charge_b) > CONDITION_TOLERANCES["starting_battery_fraction"] * min(cap_a, cap_b):
        issues.append("starting native KERS state differs")
    for wheel in ("fl", "fr", "rl", "rr"):
        tire_a = (a.get("tires") or {}).get(wheel) or {}
        tire_b = (b.get("tires") or {}).get(wheel) or {}
        for name, limit in (("native_wear", CONDITION_TOLERANCES["tire_native_wear"]),
                            ("core_temperature_c", CONDITION_TOLERANCES["tire_core_temperature_c"])):
            left = _number((tire_a.get(name) or {}).get("first"))
            right = _number((tire_b.get(name) or {}).get("first"))
            if left is None or right is None:
                limits.append(wheel + " " + name + " unavailable")
            elif abs(left - right) > limit:
                issues.append(wheel + " " + name + " differs")
    return sorted(set(issues)), sorted(set(limits)), score


def _spread(values: list[float]) -> dict[str, Any]:
    center = median(values)
    return {"count": len(values), "median_s": center, "minimum_s": min(values),
            "maximum_s": max(values),
            "median_absolute_deviation_s": median(abs(value - center) for value in values)}


def _human_scenarios(pairs: list[dict[str, Any]],
                     anchors: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    scenarios = []
    for pair in pairs:
        baseline = pair["baseline"]
        for anchor in anchors:
            context = anchor.get("context") or {}
            if anchor.get("evidence") != ["MEASURED"]:
                continue
            if any(anchor.get(field) != pair.get(field)
                   for field in ("track_id", "layout_id", "car_id")):
                continue
            if (anchor.get("compound_observed") != pair["compound_observed"] or
                    context.get("car_version") != pair["car_version"] or
                    context.get("extended_physics") is not True or
                    context.get("csp_version_code") != pair["csp_version_code"]):
                continue
            anchor_setup = context.get("setup_hash")
            pair_setup = baseline.get("setup_hash")
            if anchor_setup and pair_setup and anchor_setup != pair_setup:
                continue
            air = _number((context.get("ambient_air_c") or {}).get("median"))
            track = _number((context.get("track_surface_c") or {}).get("median"))
            fuel = _number((context.get("fuel_l") or {}).get("first"))
            if None in (air, track, fuel):
                continue
            reference = baseline["observed_conditions"]
            if (abs(air - reference["ambient_air_c"]) > CONDITION_TOLERANCES["ambient_air_c"] or
                abs(track - reference["track_surface_c"]) > CONDITION_TOLERANCES["track_surface_c"] or
                abs(fuel - reference["fuel_start_l"]) > CONDITION_TOLERANCES["fuel_l"]):
                continue
            human = _number(anchor.get("median_clean_lap_s"))
            if human is None:
                continue
            scenarios.append({
                "track_id": pair["track_id"], "layout_id": pair["layout_id"],
                "car_id": pair["car_id"], "car_version": pair["car_version"],
                "human_anchor_run_ids": anchor.get("run_ids") or [],
                "ai_pair_run_ids": [baseline["run_id"], pair["plan_c"]["run_id"]],
                "human_baseline_median_s": human,
                "ai_prototype_delta_s": pair["delta_plan_c_minus_baseline_s"],
                "candidate_human_plan_c_median_s": human + pair["delta_plan_c_minus_baseline_s"],
                "status": "conditional_assumed_transfer_needs_human_check",
                "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"],
                "physical_effect_status": "analysis_only",
                "warning": "AI-to-human package response transfer is unvalidated; candidate pace is not measured Plan C human performance",
            })
    return scenarios


def analyze_package_pairs(stints: list[dict[str, Any]],
                          human_anchors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pair independent prototype stints; never fit an upgrade to the case range."""
    candidates = [item for item in stints if
                  (item.get("prototype") or {}).get("variant_id") in PROTOTYPE_HASHES]
    inventory = []
    lap_times: dict[tuple[Any, Any], list[float]] = {}
    for stint in candidates:
        times, laps = _eligible_laps(stint)
        lap_times[(stint.get("run_id"), stint.get("collection_stint_id"))] = times
        inventory.extend(laps)
    baselines = [item for item in candidates if item["prototype"]["variant_id"] == "baseline"]
    upgrades = [item for item in candidates if item["prototype"]["variant_id"] == "plan_c"]
    possible = []
    rejected = []
    for base in baselines:
        for upgrade in upgrades:
            if base.get("track_id") != upgrade.get("track_id") or base.get("layout_id") != upgrade.get("layout_id"):
                continue
            issues, limits, score = _conditions(base, upgrade)
            if (base.get("run_id"), base.get("physical_session_id")) == (
                    upgrade.get("run_id"), upgrade.get("physical_session_id")):
                issues.append("both variants share one native session")
            base_times = lap_times[(base.get("run_id"), base.get("collection_stint_id"))]
            upgrade_times = lap_times[(upgrade.get("run_id"), upgrade.get("collection_stint_id"))]
            if not base_times or not upgrade_times:
                issues.append("complete zero-cut AI laps unavailable for one variant")
            if ((base.get("prototype") or {}).get("status") != "identity_verified" or
                    (upgrade.get("prototype") or {}).get("status") != "identity_verified"):
                issues.append("prototype identity not verified")
            if issues:
                rejected.append({"baseline_run_id": base.get("run_id"),
                                 "plan_c_run_id": upgrade.get("run_id"),
                                 "track_id": base.get("track_id"),
                                 "layout_id": base.get("layout_id"),
                                 "reasons": sorted(set(issues))})
            else:
                possible.append((score, base, upgrade, limits))
    # One-to-one matching prevents a single reference stint being counted as
    # several independent experimental repeats.
    used_baselines, used_upgrades = set(), set()
    pairs = []
    for _, base, upgrade, limits in sorted(possible, key=lambda row: (
            row[0], str(row[1].get("run_id")), str(row[2].get("run_id")))):
        base_key = (base.get("run_id"), base.get("collection_stint_id"))
        upgrade_key = (upgrade.get("run_id"), upgrade.get("collection_stint_id"))
        if base_key in used_baselines or upgrade_key in used_upgrades:
            continue
        used_baselines.add(base_key)
        used_upgrades.add(upgrade_key)
        base_times, upgrade_times = lap_times[base_key], lap_times[upgrade_key]
        baseline_spread, upgrade_spread = _spread(base_times), _spread(upgrade_times)
        delta = upgrade_spread["median_s"] - baseline_spread["median_s"]
        conditions = lambda item: {"ambient_air_c": _median_state(item, "ambient_air_c"),
                                   "track_surface_c": _median_state(item, "track_surface_c"),
                                   "fuel_start_l": _median_state(item, "fuel_l", "first"),
                                   "road_grip_native": _median_state(item, "road_grip_native")}
        pairs.append({
            "track_id": base["track_id"], "layout_id": base["layout_id"],
            "car_id": base["car_id"], "car_version": base["car_version"],
            "compound_observed": base["compound_observed"],
            "csp_version_code": base["csp_version_code"],
            "baseline": {"run_id": base["run_id"], "stint_id": base["collection_stint_id"],
                         "physical_session_id": base.get("physical_session_id"),
                         "setup_hash": base.get("setup_hash"),
                         "parameter_hash": base["prototype"]["parameter_hash"],
                         "observed_conditions": conditions(base),
                         "pace": baseline_spread},
            "plan_c": {"run_id": upgrade["run_id"], "stint_id": upgrade["collection_stint_id"],
                       "physical_session_id": upgrade.get("physical_session_id"),
                       "parameter_hash": upgrade["prototype"]["parameter_hash"],
                       "observed_conditions": conditions(upgrade),
                       "pace": upgrade_spread},
            "delta_plan_c_minus_baseline_s": delta,
            "descriptive_lap_envelope_delta_s": [min(upgrade_times) - max(base_times),
                                                 max(upgrade_times) - min(base_times)],
            "confidence_interval_95_s": None,
            "independent_stint_pairs": 1,
            "status": "matched_exploratory_simulator_pair",
            "evidence": ["MEASURED", "PHYSICS_DERIVED", "ASSUMED"],
            "calculation_mode": "matched_ai_prototype_stint_median_contrast",
            "physical_effect_status": "requested_force_worker_effect_unverified",
            "limits": sorted(set([*limits,
                "native-invalid AI/force laps are exploratory, never clean",
                "paired runs do not isolate driver learning, AI adaptation or all car settings",
                "one stint pair has no inferential confidence interval",
                "aero force is an assumed prototype; cooling multiplier is analysis-only"])),
        })
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        weather = pair["baseline"]["observed_conditions"]
        key = (pair["track_id"], pair["layout_id"], pair["car_id"], pair["car_version"],
               pair["compound_observed"], pair["baseline"]["parameter_hash"],
               pair["plan_c"]["parameter_hash"], round(weather["ambient_air_c"]),
               round(weather["track_surface_c"]))
        groups[key].append(pair)
    summaries = []
    for key, group in sorted(groups.items(), key=lambda row: str(row[0])):
        deltas = [item["delta_plan_c_minus_baseline_s"] for item in group]
        summaries.append({"track_id": key[0], "layout_id": key[1], "car_id": key[2],
                          "car_version": key[3], "compound_observed": key[4],
                          "baseline_parameter_hash": key[5], "plan_c_parameter_hash": key[6],
                          "observed_ambient_bin_c": key[7], "observed_track_bin_c": key[8],
                          "independent_stint_pairs": len(group),
                          "median_delta_plan_c_minus_baseline_s": median(deltas),
                          "observed_pair_delta_range_s": [min(deltas), max(deltas)],
                          "confidence_interval_95_s": None,
                          "status": "descriptive_only", "evidence": ["MEASURED", "ASSUMED", "PHYSICS_DERIVED"]})
    paired_keys = used_baselines | used_upgrades
    unpaired = [{"run_id": item.get("run_id"), "stint_id": item.get("collection_stint_id"),
                 "variant_id": item["prototype"]["variant_id"],
                 "prototype_issues": item["prototype"].get("issues") or []}
                for item in candidates
                if (item.get("run_id"), item.get("collection_stint_id")) not in paired_keys]
    return {
        "status": "available" if pairs else "unavailable",
        "model_version": "wimna_package_pair_analysis_v1",
        "known_parameter_hashes": PROTOTYPE_HASHES,
        "lap_inventory": inventory, "pairs": pairs, "groups": summaries,
        "rejected_candidate_pairs": rejected, "unpaired_stints": unpaired,
        "human_anchor_scenarios": _human_scenarios(pairs, human_anchors or []),
        "matching_policy": {"tolerances": CONDITION_TOLERANCES,
                            "category": "exploratory_policy_not_instrument_resolution"},
        "calculation_mode": "one_to_one_ai_prototype_stint_pairing",
        "physical_effect_status": "requested_force_worker_effect_unverified",
        "limitations": [
            "a recorded physics-worker request does not verify added force; confirm with a live response check",
            "the prototype's native aero force is assumed, while its cooling multiplier is analysis-only",
            "AI lap times are exploratory even with full zero-cut traces",
            "a human pace anchor can receive an AI delta only as an unvalidated conditional transfer",
            "no pair is available when exact variant identity or observed matching conditions fail",
        ],
    }
