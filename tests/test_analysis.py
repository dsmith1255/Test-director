"""Evidence boundaries for analysis of persisted runs."""

import tempfile
import unittest
from pathlib import Path

from wimna.analysis import analyze_store
from wimna.store import Store


CONFIG = {
    "program_version": "test",
    "track_order": ["monza_gp"],
    "tracks": {"monza_gp": {"blocks": [
        {"id": "MZ R", "kind": "reference", "laps": 5},
        {"id": "MZ F", "kind": "fuel", "laps": 2},
        {"id": "MZ E", "kind": "energy", "laps": 4},
        {"id": "MZ V", "kind": "validation", "laps": 3},
    ]}},
    "policy": {"battery_start_tolerance_j": 0},
    "thermal_scenarios": {
        "reference": {"track_temperature_c": 29, "ambient_c": None},
        "elevated": {"track_temperature_c": 33, "ambient_c": None},
        "severe": {"track_temperature_c": 36, "ambient_c": None},
    },
}


def make_run(store, run_id, block, mode, times, *, fuel_start=30, battery_start=4_000_000,
             battery_deltas=None, annotate=True, raw_phase=False, tire_hash="fresh-1", include_tires=False):
    manifest = {
        "physical_session_id": run_id, "track": "monza_gp", "block": block,
        "car_id": "fh26", "track_id": "monza", "layout_id": "gp",
        "profile_hash": "profile-1", "setup_hash": "setup-1",
        "tire_preparation_hash": tire_hash,
        "observed_initial_state": {"fuel_l": fuel_start, "battery_j": battery_start},
    }
    store.start_run(run_id, mode, manifest)
    deltas = battery_deltas or [0] * len(times)
    for index, (lap_time, delta) in enumerate(zip(times, deltas), 1):
        data = {"type": "lap_complete", "lap_index": index, "lap_time_s": lap_time,
                "valid_native": True, "flags": [], "fuel_used_l": 2.0,
                "battery_delta_j": delta}
        if raw_phase:
            data["phase"] = "program"
            data["eligible_for_comparison"] = True
        if include_tires:
            data["tire_state"] = {
                wheel: {"wear_fraction": 0.01 + index * 0.002, "temperature_c": 83 + index}
                for wheel in ("fl", "fr", "rl", "rr")}
        store.add_record({"run_id": run_id, "seq": index, "kind": "event",
                          "sim_time_s": index * lap_time, "wall_time_utc": None, "data": data})
        if annotate:
            store.annotate_lap(run_id, index, "program", True, [], "accepted from director")
    if block == "MZ R":
        for index in range(4):
            store.add_record({"run_id": run_id, "seq": 100 + index, "kind": "sample",
                              "sim_time_s": index * 2.0, "wall_time_utc": None,
                              "data": {"speed_m_s": {"value": 50 + index, "unit": "m/s"},
                                       "throttle": {"value": 0.6, "unit": "fraction"},
                                       "brake": {"value": 0.1, "unit": "fraction"},
                                       "battery_soc_native": {"value": 0.8, "unit": "fraction_native"}}})


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "store")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_empty_store_and_missing_ambient_stay_unavailable(self):
        forecasts, validation, parameters = analyze_store(self.store, CONFIG)
        self.assertIsNone(forecasts["tracks"]["monza_gp"]["reference"]["pace_mean_s"])
        self.assertEqual(len(forecasts["thermal_grid"]), 144)
        self.assertTrue(all(not row["result"]["available"] for row in forecasts["thermal_grid"]))
        self.assertIsNone(parameters["analysis_assumptions"]["ambient_c"]["value"])
        self.assertEqual(validation["status"], "unavailable")

    def test_measured_annotations_and_synthetic_are_not_pooled(self):
        make_run(self.store, "synthetic_r", "MZ R", "SYNTHETIC", [90, 91], annotate=False, raw_phase=True)
        make_run(self.store, "measured_r", "MZ R", "MEASURED", [100, 101], annotate=True)
        make_run(self.store, "synthetic_f", "MZ F", "SYNTHETIC", [92, 93],
                 fuel_start=40, annotate=False, raw_phase=True)
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        track = forecasts["tracks"]["monza_gp"]
        self.assertEqual(track["calibration_evidence_mode"], "MEASURED")
        self.assertEqual(track["reference"]["run_ids"], ["measured_r"])
        self.assertEqual(track["reference"]["pace_mean_s"], 100.5)
        self.assertEqual(track["fuel_contrast"]["status"], "unavailable")
        self.assertEqual(forecasts["synthetic_run_ids"], ["synthetic_f", "synthetic_r"])

    def test_unannotated_measured_lap_is_not_comparable(self):
        make_run(self.store, "raw_r", "MZ R", "MEASURED", [100], annotate=False, raw_phase=True)
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        ref = forecasts["tracks"]["monza_gp"]["reference"]
        self.assertIsNone(ref["pace_mean_s"])
        self.assertEqual(ref["excluded_lap_count"], 1)

    def test_immutable_director_assignment_supplies_lua_track_and_block(self):
        self.store.start_run("lua", "MEASURED", {"physical_session_id": "lua"})
        self.store.add_record({"run_id": "lua", "seq": 1, "kind": "event", "sim_time_s": 100,
                               "wall_time_utc": None,
                               "data": {"type": "lap_complete", "lap_index": 1,
                                        "lap_time_s": 100, "valid_native": True, "flags": []}})
        self.store.annotate_lap("lua", 1, "program", True, [], "director accepted")
        self.store.assign_run("lua", "monza_gp", "MZ R", {"assignment_status": "confirmed"})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        self.assertEqual(forecasts["tracks"]["monza_gp"]["reference"]["pace_mean_s"], 100)
        self.assertNotIn("track", next(self.store.iter_runs())["manifest"])

    def test_damage_carries_into_later_laps_until_restoration(self):
        self.store.start_run("damage", "MEASURED", {"physical_session_id": "damage", "track": "monza_gp",
                                                    "block": "MZ R"})
        for index, flags in ((1, ["damage"]), (2, []), (4, [])):
            self.store.add_record({"run_id": "damage", "seq": index, "kind": "event",
                                   "sim_time_s": index * 100, "wall_time_utc": None,
                                   "data": {"type": "lap_complete", "lap_index": index,
                                            "lap_time_s": 100, "valid_native": True, "flags": flags}})
            self.store.annotate_lap("damage", index, "program", True, [], "director accepted")
        self.store.add_record({"run_id": "damage", "seq": 3, "kind": "event",
                               "sim_time_s": 300, "wall_time_utc": None,
                               "data": {"type": "state_restored", "manual_confirmation": True}})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        reference = forecasts["tracks"]["monza_gp"]["reference"]
        self.assertEqual(reference["lap_count"], 1)
        self.assertEqual(reference["excluded_lap_count"], 2)

    def test_fuel_contrast_requires_match_and_density_is_explicit(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100, 101], include_tires=True)
        make_run(self.store, "f", "MZ F", "MEASURED", [102, 103], fuel_start=40)
        forecasts, _, _ = analyze_store(self.store, CONFIG, assumptions={"fuel_density_kg_per_l": 0.8})
        slope = forecasts["tracks"]["monza_gp"]["fuel_contrast"]
        self.assertEqual(slope["status"], "available")
        self.assertAlmostEqual(slope["seconds_per_litre"], 0.2)
        self.assertAlmostEqual(slope["seconds_per_kg"], 0.25)

    def test_fractional_soc_can_match_without_battery_capacity(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100, 101], battery_start=None)
        make_run(self.store, "f", "MZ F", "MEASURED", [102, 103],
                 fuel_start=40, battery_start=None)
        self.store.assign_run("r", "monza_gp", "MZ R", {
            "observed_initial_state": {
                "battery_soc_native": {"value": 0.80, "unit": "fraction_native"}}})
        self.store.assign_run("f", "monza_gp", "MZ F", {
            "observed_initial_state": {
                "battery_soc_native": {"value": 0.82, "unit": "fraction_native"}}})
        config = {**CONFIG, "policy": {**CONFIG["policy"],
                                       "battery_start_tolerance_fraction": 0.03}}
        forecasts, _, _ = analyze_store(self.store, config)
        self.assertEqual(forecasts["tracks"]["monza_gp"]["fuel_contrast"]["status"], "available")
        self.assertAlmostEqual(forecasts["tracks"]["monza_gp"]["fuel_contrast"]["seconds_per_litre"], 0.2)

    def test_unitless_soc_does_not_establish_fuel_match(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100], battery_start=None)
        make_run(self.store, "f", "MZ F", "MEASURED", [102], fuel_start=40,
                 battery_start=None)
        self.store.assign_run("r", "monza_gp", "MZ R", {
            "observed_initial_state": {"battery_soc_native": 0.8}})
        self.store.assign_run("f", "monza_gp", "MZ F", {
            "observed_initial_state": {"battery_soc_native": 0.8}})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        slope = forecasts["tracks"]["monza_gp"]["fuel_contrast"]
        self.assertEqual(slope["status"], "unavailable")
        self.assertTrue(any("battery state" in warning for warning in slope["warnings"]))

    def test_unmatched_tire_preparation_withholds_slope(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100])
        make_run(self.store, "f", "MZ F", "MEASURED", [102], fuel_start=40, tire_hash="different")
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        slope = forecasts["tracks"]["monza_gp"]["fuel_contrast"]
        self.assertIsNone(slope["seconds_per_litre"])
        self.assertTrue(any("tire" in warning for warning in slope["warnings"]))

    def test_energy_debt_and_thermal_grid_are_analysis_only(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100, 100], include_tires=True)
        make_run(self.store, "e", "MZ E", "MEASURED", [99, 99, 101, 101],
                 battery_deltas=[-500_000, -500_000, 200_000, 200_000])
        forecasts, _, parameters = analyze_store(self.store, CONFIG, assumptions={"ambient_c": 25})
        energy = forecasts["tracks"]["monza_gp"]["energy_cycle"]
        self.assertEqual(energy["ending_battery_j"], 3_400_000)
        self.assertEqual(energy["deployed_j"], 1_000_000)
        self.assertEqual(energy["recovered_j"], 400_000)
        self.assertEqual(energy["remaining_debt_j"], 600_000)
        self.assertEqual(energy["projected_repayment_time_cost_s"], 3.0)
        self.assertTrue(energy["conditional_on_energy_repayment"])
        self.assertTrue(all(row["physical_effect_status"] == "analysis_only" for row in forecasts["thermal_grid"]))
        self.assertTrue(any(row["result"]["available"] for row in forecasts["thermal_grid"]))
        self.assertEqual(parameters["analysis_assumptions"]["ambient_c"]["value"], 25)

    def test_scenario_ambient_is_independent_and_missing_remains_null(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100])
        forecasts, _, parameters = analyze_store(
            self.store, CONFIG, assumptions={"ambient_by_scenario_c": {"reference": 22}}
        )
        reference = [row for row in forecasts["thermal_grid"] if row["scenario"] == "reference"]
        elevated = [row for row in forecasts["thermal_grid"] if row["scenario"] == "elevated"]
        self.assertTrue(all(row["ambient_c"] == 22 and row["result"]["available"] for row in reference))
        self.assertTrue(all(row["ambient_c"] is None and not row["result"]["available"] for row in elevated))
        self.assertEqual(parameters["ambient_by_scenario_c"]["reference"]["value"], 22)
        self.assertIsNone(parameters["ambient_by_scenario_c"]["elevated"]["value"])

    def test_plan_c_airflow_range_and_tire_range_mark_assumption_sensitivity(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100])
        forecasts, _, parameters = analyze_store(
            self.store, CONFIG, assumptions={"ambient_c": 25,
                                             "plan_c_airflow_multiplier": 1.10,
                                             "plan_c_airflow_range": [0.95, 1.20],
                                             "tire_time_delta_plan_c_minus_baseline_s_range": [-2, 3]})
        grid = forecasts["thermal_grid"]
        self.assertEqual(len(grid), 144)
        self.assertEqual({row["package_assumption"] for row in grid},
                         {"baseline", "low", "base", "high"})
        comparison = forecasts["strategy_comparison"]
        self.assertEqual(comparison["status"], "computed")
        self.assertTrue(comparison["assumption_sensitive"])
        self.assertTrue(comparison["airflow_reversal"])
        self.assertTrue(comparison["tire_range_reversal"])
        self.assertEqual(parameters["tire_strategy_time_delta_range"]["low"], -2)

    def test_stint_requires_inputs_and_frozen_validation_uses_separate_run(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100, 100], include_tires=True)
        make_run(self.store, "v", "MZ V", "MEASURED", [101, 102, 103])
        self.store.register_prediction("p1", ["r"], {"model_id": "m1", "evidence": "MEASURED"},
                                       {"mean_lap_time_s": 101, "fuel_l_per_lap": 2.0})
        self.store.attach_validation("p1", "v")
        forecasts, validation, _ = analyze_store(self.store, CONFIG)
        self.assertEqual(forecasts["tracks"]["monza_gp"]["longer_stint"]["status"], "unavailable")
        self.assertEqual(forecasts["tracks"]["monza_gp"]["race_time"]["status"], "unavailable")
        result = validation["results"][0]
        self.assertEqual(result["fit_run_ids"], ["r"])
        self.assertEqual(result["validation_run_id"], "v")
        self.assertTrue(result["held_out"])
        self.assertEqual(result["residuals"]["mean_lap_time_s"]["residual_observed_minus_predicted"], 1)

    def test_intervention_acknowledgement_alone_is_not_verified(self):
        make_run(self.store, "v", "MZ V", "MEASURED", [100])
        self.store.add_record({"run_id": "v", "seq": 200, "kind": "event", "sim_time_s": 101,
                               "wall_time_utc": None,
                               "data": {"type": "control_effect", "command_acknowledged": True,
                                        "requested_reduction_fraction": 0.2,
                                        "observed_reduction_fraction": 0.2,
                                        "effect_verified": False}})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        self.assertEqual(forecasts["tracks"]["monza_gp"]["intervention"]["status"], "unverified")
        self.store.add_record({"run_id": "v", "seq": 201, "kind": "event", "sim_time_s": 102,
                               "wall_time_utc": None,
                               "data": {"type": "thermal_intervention_effect", "effect_verified": True,
                                        "requested_reduction_fraction": 0.2,
                                        "observed_reduction_fraction": 0.19,
                                        "battery_limit_excluded": True,
                                        "rule_limit_excluded": True,
                                        "baseline_comparable": True}})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        self.assertEqual(forecasts["tracks"]["monza_gp"]["intervention"]["status"], "verified")

    def test_assignment_initial_energy_requires_joule_unit(self):
        self.store.start_run("unitless", "MEASURED", {"physical_session_id": "unitless"})
        for index in range(1, 5):
            self.store.add_record({"run_id": "unitless", "seq": index, "kind": "event",
                                   "sim_time_s": index * 100, "wall_time_utc": None,
                                   "data": {"type": "lap_complete", "lap_index": index,
                                            "lap_time_s": 100, "valid_native": True, "flags": [],
                                            "battery_delta_j": -100}})
            self.store.annotate_lap("unitless", index, "program", True, [], "director accepted")
        self.store.assign_run("unitless", "monza_gp", "MZ E",
                              {"observed_initial_state": {"battery_j": 1000,
                                                          "battery_soc_native": 0.8}})
        forecasts, _, _ = analyze_store(self.store, CONFIG)
        energy = forecasts["tracks"]["monza_gp"]["energy_cycle"]
        self.assertIsNone(energy["starting_battery_j"])
        self.assertIsNone(energy["ending_battery_j"])

    def test_explicit_complete_stint_inputs_enable_projection(self):
        make_run(self.store, "r", "MZ R", "MEASURED", [100, 100])
        inputs = {
            "lap_count": 2, "starting_fuel_l": 10, "fuel_density_kg_per_l": 0.75,
            "fuel_seconds_per_kg": 0.01,
            "tire_model": {"wear_per_work_m": 0.00001, "heating_c_per_work_m": 0.0001,
                           "cooling_per_s": 0.01, "slip_gain": 1,
                           "track_coupling_fraction": 0.4,
                           "wear_pace_curve": [[0, 0], [1, 5]],
                           "temperature_pace_curve": [[0, 0], [100, 0]]},
            "initial_tires": {"fl": {"wear_fraction": 0.01, "temperature_c": 80}},
            "tire_samples_per_lap": {"fl": [{"dt_s": 10, "speed_m_s": 50, "load_ratio": 1,
                                               "slip_ratio": 0.01, "air_temp_c": 25,
                                               "track_temp_c": 29}]},
            "starting_battery_j": 1_000_000, "usable_battery_j": 2_000_000,
            "energy_samples_per_lap": [{"dt_s": 10, "battery_power_w": 0}],
            "energy_pace_delta_s_per_lap": 0,
            "thermal_samples_per_lap": [{"dt_s": 10, "workload_fraction": 0.5,
                                          "speed_m_s": 50, "ambient_c": 25}],
            "derate_seconds_at_full_reduction": 1,
            "restriction_speed_response": 0.2,
            "restriction_workload_response": 0.2,
            "restriction_energy_response": 0.2,
        }
        forecasts, _, _ = analyze_store(self.store, CONFIG,
                                        assumptions={"ambient_c": 25,
                                                     "stint_inputs": {"monza_gp": inputs},
                                                     "race_inputs": {"monza_gp": {
                                                         "race_laps": 2, "pit_stops": 0,
                                                         "full_pit_loss_s": 25,
                                                         "fuel_reserve_l": 5,
                                                         "battery_reserve_j": 500_000}}})
        stint = forecasts["tracks"]["monza_gp"]["longer_stint"]
        self.assertEqual(stint["status"], "computed")
        self.assertGreater(stint["result"]["total_time_s"], 200)
        self.assertEqual(stint["physical_effect_status"], "analysis_only")
        race_time = forecasts["tracks"]["monza_gp"]["race_time"]
        self.assertEqual(race_time["status"], "computed")
        self.assertEqual(race_time["total_time_s"], stint["result"]["total_time_s"])
        self.assertEqual(race_time["pit_time_s"], 0)
        self.assertFalse(race_time["winner_claimed"])

        one_lap = {**inputs, "lap_count": 1}
        with_stop, _, _ = analyze_store(self.store, CONFIG,
                                        assumptions={"ambient_c": 25,
                                                     "race_inputs": {"monza_gp": {
                                                         "race_laps": 2, "pit_stops": 1,
                                                         "full_pit_loss_s": 25,
                                                         "fuel_reserve_l": 5,
                                                         "battery_reserve_j": 500_000,
                                                         "stints": [one_lap, one_lap]}}})
        two_stints = with_stop["tracks"]["monza_gp"]["race_time"]
        self.assertEqual(two_stints["status"], "computed")
        self.assertEqual(two_stints["pit_time_s"], 25)
        self.assertAlmostEqual(two_stints["total_time_s"], sum(two_stints["stint_times_s"]) + 25)

        insufficient, _, _ = analyze_store(self.store, CONFIG,
                                           assumptions={"ambient_c": 25,
                                                        "stint_inputs": {"monza_gp": inputs},
                                                        "race_inputs": {"monza_gp": {
                                                            "race_laps": 60, "pit_stops": 0,
                                                            "full_pit_loss_s": 25,
                                                            "fuel_reserve_l": 5,
                                                            "battery_reserve_j": 500_000}}})
        withheld = insufficient["tracks"]["monza_gp"]["race_time"]
        self.assertEqual(withheld["status"], "unavailable")
        self.assertIsNone(withheld["total_time_s"])
        self.assertIn("modeled stint laps exactly cover selected race length", withheld["missing_inputs"])


if __name__ == "__main__":
    unittest.main()
