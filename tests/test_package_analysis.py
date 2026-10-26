"""Paired prototype comparisons require exact identity and measured matching conditions."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from wimna.collection_analysis import analyze_collections
from wimna.package_analysis import (
    PROTOTYPE_HASHES, analyze_package_pairs, prototype_from_samples,
)
from wimna.store import Store


MODE_DIR = Path(__file__).resolve().parents[1] / "ac_app" / "extension" / "lua" / "new-modes"


def mode_data(variant):
    path = MODE_DIR / ("wimna-ai-plan-c" if variant == "plan_c" else "wimna-ai-baseline") / "mode.lua"
    source = path.read_text(encoding="utf-8")
    return (re.search(r"local parameters = \[\[(.*?)\]\]", source).group(1),
            re.search(r"local parameterHash = '([0-9a-f]+)'", source).group(1))


def shared(value, unit):
    return {"value": value, "unit": unit, "quality": "available",
            "source": "CSP_SESSION_SHARED_STORE"}


def prototype(variant, *, parameter_text=None, reported_hash=None, effect=None):
    parameters, digest = mode_data(variant)
    parameter_text = parameters if parameter_text is None else parameter_text
    reported_hash = digest if reported_hash is None else reported_hash
    effect = ("none" if variant == "baseline" else "worker_requested_effect_unverified") if effect is None else effect
    manifest = {"variant_id": variant, "parameter_hash_reported": reported_hash,
                "parameters_json_requested": parameter_text}
    samples = [{"prototype_variant_requested": shared(variant, "identifier"),
                "prototype_parameter_hash_reported": shared(reported_hash, "sha256_reported"),
                "prototype_effect_status": shared(effect, "status_not_physical_effect"),
                "prototype_physics_permission_reported": shared(1, "boolean_0_or_1"),
                "force_native_lap_invalid_expected": shared(variant == "plan_c", "boolean")}
               for _ in range(30)]
    return prototype_from_samples(manifest, samples,
                                  "wimna-ai-plan-c" if variant == "plan_c" else "wimna-ai-baseline")


def lap(index, seconds, *, cuts=0, controller="AI", measured=True):
    return {"lap_index": index, "lap_time_s": seconds,
            "controller_observed": controller, "native_valid": False,
            "native_cuts": cuts, "usable_for_exploratory_pace": True,
            "quality_reasons": [], "pace_use": "exploratory_ai_native_invalid",
            "evidence": "MEASURED" if measured else "SYNTHETIC"}


def stint(variant, run_id, times, *, air=30.0, track=34.0, fuel=50.0,
          evidence="MEASURED", car_version="v2a"):
    return {"run_id": run_id, "collection_stint_id": run_id + "-s0",
            "physical_session_id": run_id + "-native0", "evidence": evidence,
            "track_id": "spa", "layout_id": "default", "car_id": "rss_formula_hybrid_2026",
            "car_version": car_version, "csp_version_code": 3465,
            "extended_physics": True, "compound_observed": "C3",
            "setup_hash": "unchanged", "profile_hash": "unchanged",
            "ambient_air_c": {"median": air}, "track_surface_c": {"median": track},
            "fuel_l": {"first": fuel, "refill_detected": False},
            "road_grip_native": {"median": 0.98},
            "rain_wetness_native": {"median": 0.0},
            "time_multiplier_native": {"first": 1.0},
            "energy_native": {"mguk_delivery_native": {"first": 0.5},
                              "mguk_recovery_native": {"first": 0.5},
                              "kers_current_kj_native": {"first": 300.0},
                              "kers_max_kj_native": {"first": 1000.0}},
            "tires": {wheel: {"native_wear": {"first": 0.98},
                              "core_temperature_c": {"first": 90.0}}
                      for wheel in ("fl", "fr", "rl", "rr")},
            "prototype": prototype(variant),
            "laps": [lap(index, seconds) for index, seconds in enumerate(times, 1)]}


class PackageAnalysisTests(unittest.TestCase):
    def test_mode_json_and_exact_hash_must_match_shipped_prototype(self):
        for variant in ("baseline", "plan_c"):
            with self.subTest(variant=variant):
                parameters, digest = mode_data(variant)
                self.assertEqual(json.loads(parameters)["variant"], variant)
                self.assertEqual(digest, PROTOTYPE_HASHES[variant])
                self.assertEqual(prototype(variant)["status"], "identity_verified")
                self.assertFalse(prototype(variant)["physical_effect_verified"])
                self.assertEqual(prototype(variant, reported_hash="f" * 64)["status"],
                                 "identity_unverified")
                self.assertEqual(prototype(variant, parameter_text="[]")["status"],
                                 "unavailable")

    def test_complete_zero_cut_ai_laps_form_exploratory_pair_only(self):
        base = stint("baseline", "base", [100.0, 101.0])
        upgrade = stint("plan_c", "upgrade", [99.0, 99.5])
        upgrade["laps"].append(lap(3, 98.0, cuts=1))
        human_anchor = {"track_id": "spa", "layout_id": "default",
                        "car_id": "rss_formula_hybrid_2026", "compound_observed": "C3",
                        "median_clean_lap_s": 95.0, "run_ids": ["human"],
                        "evidence": ["MEASURED"],
                        "context": {"car_version": "v2a", "extended_physics": True,
                                    "csp_version_code": 3465, "setup_hash": "unchanged",
                                    "ambient_air_c": {"median": 30.0},
                                    "track_surface_c": {"median": 34.0},
                                    "fuel_l": {"first": 50.0}}}
        result = analyze_package_pairs([base, upgrade], [human_anchor])
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["pairs"]), 1)
        pair = result["pairs"][0]
        self.assertAlmostEqual(pair["delta_plan_c_minus_baseline_s"], -1.25)
        self.assertIsNone(pair["confidence_interval_95_s"])
        self.assertEqual(pair["plan_c"]["pace"]["count"], 2)
        self.assertEqual(pair["physical_effect_status"], "requested_force_worker_effect_unverified")
        self.assertEqual(len(result["lap_inventory"]), 5)
        self.assertEqual(result["lap_inventory"][-1]["package_pace_use"], "excluded")
        self.assertEqual(result["lap_inventory"][-1]["native_cuts"], 1)
        transfer = result["human_anchor_scenarios"][0]
        self.assertAlmostEqual(transfer["candidate_human_plan_c_median_s"], 93.75)
        self.assertEqual(transfer["status"], "conditional_assumed_transfer_needs_human_check")

    def test_mismatched_conditions_and_synthetic_runs_remain_unavailable(self):
        base = stint("baseline", "base", [100.0])
        hot = stint("plan_c", "hot", [99.0], track=38.0)
        result = analyze_package_pairs([base, hot])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("track_surface_c outside pairing tolerance",
                      result["rejected_candidate_pairs"][0]["reasons"])
        synthetic = stint("plan_c", "synthetic", [99.0], evidence="SYNTHETIC")
        result = analyze_package_pairs([base, synthetic])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["lap_inventory"][-1]["package_pace_use"], "excluded")
        self.assertEqual(result["human_anchor_scenarios"], [])

    def test_identical_empty_input_is_explicitly_unavailable(self):
        self.assertEqual(analyze_package_pairs([])["status"], "unavailable")

    def test_collection_report_includes_verified_prototype_inventory(self):
        parameters, digest = mode_data("baseline")
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            try:
                store.start_run("base", "MEASURED", {
                    "physical_session_id": "native-0", "collection_enabled": True,
                    "car_id": "rss_formula_hybrid_2026", "car_version": "v2a",
                    "track_id": "spa", "layout_id": "default",
                    "session_mode": "wimna-ai-baseline", "extended_physics": True,
                    "prototype": {"variant_id": "baseline",
                                  "parameter_hash_reported": digest,
                                  "parameters_json_requested": parameters},
                })
                def record(seq, kind, when, data):
                    store.add_record({"run_id": "base", "seq": seq, "kind": kind,
                                      "physical_session_id": "native-0",
                                      "sim_time_s": when, "data": data})
                record(1, "event", 0.0, {"type": "collection_stint_start",
                                            "collection_stint_id": "base-s0",
                                            "stint_session_mode": "wimna-ai-baseline",
                                            "stint_extended_physics": True})
                for offset in range(30):
                    data = {
                        "collection_stint_id": {"value": "base-s0", "unit": "identifier",
                                                "source": "WIMNA_APP", "quality": "available"},
                        "is_ai_controlled": {"value": True, "unit": "boolean",
                                             "source": "CSP_STATE", "quality": "available"},
                        "is_user_controlled": {"value": False, "unit": "boolean",
                                               "source": "CSP_STATE", "quality": "available"},
                        "tyre_compound": {"value": "C3", "unit": "name",
                                          "source": "CSP_STATE", "quality": "available"},
                        "prototype_variant_requested": shared("baseline", "identifier"),
                        "prototype_parameter_hash_reported": shared(digest, "sha256_reported"),
                        "prototype_effect_status": shared("none", "status_not_physical_effect"),
                        "prototype_physics_permission_reported": shared(1, "boolean_0_or_1"),
                        "force_native_lap_invalid_expected": shared(False, "boolean"),
                    }
                    record(offset + 2, "sample", offset + 0.1, data)
                record(32, "event", 30.0, {"type": "collection_stint_end",
                                               "collection_stint_id": "base-s0"})
                analysis = analyze_collections(store)
                self.assertEqual(analysis["stints"][0]["prototype"]["status"],
                                 "identity_verified", analysis["stints"][0]["prototype"]["issues"])
                self.assertEqual(analysis["package_prototype_analysis"]["status"],
                                 "unavailable")
                self.assertEqual(analysis["package_prototype_analysis"]["unpaired_stints"][0]
                                 ["variant_id"], "baseline")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
