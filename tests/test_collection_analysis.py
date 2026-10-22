"""Data-first tire contrasts keep controller, conditions and provenance explicit."""

import tempfile
import unittest

from wimna.collection_analysis import analyze_collections
from wimna.store import Store


def field(value, unit, source="CSP_STATE"):
    return {"value": value, "unit": unit, "source": source,
            "quality": "available" if value is not None else "unavailable"}


class CollectionAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.start_run("tire_collection", "MEASURED", {
            "physical_session_id": "native-s0", "collection_enabled": True,
            "collection_id": "tire_collection", "selection": "collection",
            "car_id": "fh26", "car_version": "v2a", "track_id": "spa",
            "layout_id": "default", "setup_hash": "fixed-setup",
            "session_mode": "wimna-ai-thermal", "extended_physics": True,
            "csp_version_code": 3465,
        })
        self.seq = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, kind, when, data):
        self.seq += 1
        self.store.add_record({"run_id": "tire_collection", "seq": self.seq,
                               "kind": kind, "physical_session_id": "native-s0",
                               "sim_time_s": when, "data": data})

    def stint(self, identifier, compound, controller, start, lap_time, *, fuel=50.0,
              cuts=0, native_valid=True, observed_compound=True,
              air=32.0, track=36.0, sparse=False, switch_controller_halfway=False):
        self.add("event", start, {"type": "collection_stint_start",
                                  "collection_stint_id": identifier,
                                  "requested_tyre_compound": compound,
                                  "planned_tire_label": identifier,
                                  "condition_label": "hot"})
        sample_times = ([start + lap_time - offset for offset in (1.0, 0.8, 0.6, 0.4)]
                        if sparse else
                        [start + 0.5 * offset for offset in range(int(round(lap_time * 2)) + 1)])
        for offset, when in enumerate(sample_times):
            progress = offset / max(1, len(sample_times) - 1)
            observed_controller = ("HUMAN" if controller == "AI" else "AI") \
                if switch_controller_halfway and progress >= 0.5 else controller
            self.add("sample", when, {
                "collection_stint_id": field(identifier, "identifier", "WIMNA_APP"),
                "is_ai_controlled": field(observed_controller == "AI", "boolean"),
                "is_user_controlled": field(observed_controller == "HUMAN", "boolean"),
                "tyre_compound": field(compound if observed_compound else None, "name"),
                "ambient_air_c": field(air, "degC"),
                "track_surface_c": field(track, "degC"),
                "fuel": field(fuel - 0.3 * progress, "L"),
                "speed_kmh": field(200.0, "km/h"),
                "throttle": field(0.7, "fraction"),
                "brake": field(0.1, "fraction"),
                "sim_paused": field(False, "boolean"),
                "experiment_paused": field(False, "boolean", "WIMNA_APP"),
                "tyre_core_c_fl": field(95.0 + 3.0 * progress, "degC"),
                "tyre_wear_fl": field(0.95 - 0.03 * progress, "native_fraction_not_life"),
                "in_pitlane": field(False, "boolean"),
                "in_pit": field(False, "boolean"),
            })
        self.add("event", start + lap_time, {
            "type": "lap_complete", "collection_stint_id": identifier,
            "lap_index": 1, "lap_time_s": lap_time, "valid_native": native_valid,
            "last_lap_cuts_count": cuts,
            "flags": ["ai_controlled"] if controller == "AI" else [],
        })
        self.add("event", start + lap_time + 0.1, {
            "type": "collection_stint_end", "collection_stint_id": identifier,
            "reason": "pit_tire_change"})

    def test_ai_tire_delta_can_be_conditionally_anchored_to_human_lap(self):
        self.stint("X_AI", "C3", "AI", 0.0, 90.0, native_valid=False)
        self.stint("Y_AI", "C4", "AI", 100.0, 88.0, native_valid=False)
        self.stint("X_HUMAN", "C3", "HUMAN", 200.0, 86.0)
        analysis = analyze_collections(self.store)
        self.assertEqual(analysis["measured_stint_count"], 3)
        self.assertEqual(analysis["synthetic_stint_count"], 0)
        self.assertEqual([s["compound_observed"] for s in analysis["stints"]], ["C3", "C4", "C3"])
        self.assertEqual(analysis["stints"][0]["controller_sample_counts"]["AI"], 181)
        self.assertEqual(analysis["stints"][0]["tires"]["fl"]["core_temperature_c"]["last"], 98.0)
        self.assertEqual(analysis["stints"][0]["thermal"]["status"], "computed")
        contrast = next(row for row in analysis["tire_comparisons"] if row["controller_observed"] == "AI")
        self.assertEqual(contrast["status"], "conditional_observed")
        self.assertAlmostEqual(contrast["observed_median_delta_right_minus_left_s"], -2.0)
        anchor = analysis["human_pace_anchors"][0]
        self.assertAlmostEqual(anchor["best_clean_lap_s"], 86.0)
        candidate = analysis["anchored_tire_scenarios"][0]
        self.assertAlmostEqual(candidate["candidate_human_median_s"], 84.0)
        self.assertEqual(candidate["validation_status"], "awaiting_held_out_human_check")
        self.assertEqual(candidate["physical_effect_status"], "analysis_only")
        ai_lap = analysis["stints"][0]["laps"][0]
        self.assertFalse(ai_lap["comparable_for_pace"])
        self.assertTrue(ai_lap["usable_for_exploratory_pace"])
        self.assertEqual(ai_lap["pace_use"], "exploratory_ai_native_invalid")

    def test_sparse_or_native_unverified_lap_cannot_enter_pace(self):
        self.stint("sparse", "C3", "AI", 0.0, 90.0, sparse=True)
        self.stint("unverified", "C4", "HUMAN", 100.0, 88.0, native_valid=None)
        analysis = analyze_collections(self.store)
        sparse_lap = analysis["stints"][0]["laps"][0]
        unknown_lap = analysis["stints"][1]["laps"][0]
        self.assertEqual(sparse_lap["pace_use"], "excluded")
        self.assertIn("insufficient_lap_sample_coverage", sparse_lap["quality_reasons"])
        self.assertEqual(unknown_lap["pace_use"], "excluded")
        self.assertIn("native_validity_unavailable", unknown_lap["quality_reasons"])

    def test_controller_handoff_inside_lap_stays_in_raw_data_but_exits_pace(self):
        self.stint("handoff", "C3", "AI", 0.0, 90.0,
                   switch_controller_halfway=True)
        analysis = analyze_collections(self.store)
        lap = analysis["stints"][0]["laps"][0]
        self.assertEqual(lap["controller_observed"], "MIXED")
        self.assertEqual(lap["pace_use"], "excluded")
        self.assertIn("controller_not_stable_or_unobserved", lap["quality_reasons"])
        self.assertGreater(analysis["stints"][0]["controller_sample_counts"]["AI"], 0)
        self.assertGreater(analysis["stints"][0]["controller_sample_counts"]["HUMAN"], 0)

    def test_synthetic_lap_keeps_fixture_evidence(self):
        self.store.start_run("fixture", "SYNTHETIC", {
            "physical_session_id": "fixture-s0", "collection_enabled": True,
            "collection_id": "fixture", "selection": "collection",
            "car_id": "fh26", "track_id": "spa", "layout_id": "default"})
        seq = 0
        def append(kind, when, data):
            nonlocal seq
            seq += 1
            self.store.add_record({"run_id": "fixture", "seq": seq, "kind": kind,
                                   "physical_session_id": "fixture-s0",
                                   "sim_time_s": when, "data": data})
        append("event", 0.0, {"type": "collection_stint_start", "collection_stint_id": "fx"})
        for when in range(31):
            append("sample", float(when), {
                "collection_stint_id": field("fx", "identifier", "WIMNA_APP"),
                "is_ai_controlled": field(False, "boolean"),
                "is_user_controlled": field(True, "boolean"),
                "tyre_compound": field("C3", "name")})
        append("event", 30.0, {"type": "lap_complete", "collection_stint_id": "fx",
                               "lap_index": 1, "lap_time_s": 30.0,
                               "valid_native": True, "last_lap_cuts_count": 0, "flags": []})
        append("event", 30.1, {"type": "collection_stint_end", "collection_stint_id": "fx"})
        fixture = next(item for item in analyze_collections(self.store)["stints"]
                       if item["run_id"] == "fixture")
        self.assertEqual(fixture["evidence"], "SYNTHETIC")
        self.assertEqual(fixture["laps"][0]["evidence"], "SYNTHETIC")

    def test_requested_tire_never_substitutes_for_missing_observed_compound(self):
        self.stint("X_AI", "C3", "AI", 0.0, 90.0, native_valid=False,
                   observed_compound=False)
        analysis = analyze_collections(self.store)
        self.assertIsNone(analysis["stints"][0]["compound_observed"])
        self.assertEqual(analysis["stints"][0]["compound_requested"], "C3")
        self.assertEqual(analysis["tire_comparisons"], [])

    def test_heat_mismatch_keeps_tire_difference_descriptive(self):
        self.stint("X_AI", "C3", "AI", 0.0, 90.0, native_valid=False)
        self.stint("Y_AI", "C4", "AI", 100.0, 88.0, native_valid=False,
                   air=40.0, track=44.0)
        self.stint("X_HUMAN", "C3", "HUMAN", 200.0, 86.0)
        analysis = analyze_collections(self.store)
        contrast = next(row for row in analysis["tire_comparisons"] if row["controller_observed"] == "AI")
        self.assertEqual(contrast["status"], "descriptive_only")
        self.assertTrue(any("track_surface_c differs" in issue for issue in contrast["match_issues"]))
        self.assertEqual(analysis["anchored_tire_scenarios"], [])

    def test_human_anchor_at_different_heat_cannot_inherit_ai_tire_delta(self):
        self.stint("X_AI", "C3", "AI", 0.0, 90.0, native_valid=False)
        self.stint("Y_AI", "C4", "AI", 100.0, 88.0, native_valid=False)
        self.stint("X_HUMAN", "C3", "HUMAN", 200.0, 86.0,
                   air=40.0, track=44.0)
        analysis = analyze_collections(self.store)
        self.assertEqual(len(analysis["tire_comparisons"]), 1)
        self.assertEqual(len(analysis["human_pace_anchors"]), 1)
        self.assertEqual(analysis["anchored_tire_scenarios"], [])


if __name__ == "__main__":
    unittest.main()
