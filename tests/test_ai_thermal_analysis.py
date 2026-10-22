"""AI thermal traces remain supplementary and carry explicit evidence limits."""

import json
import tempfile
import unittest
from pathlib import Path

from wimna.analysis import analyze_store
from wimna.report import export_bundle
from wimna.store import Store


CONFIG = {
    "program_version": "test",
    "track_order": ["spa"],
    "tracks": {"spa": {"display_name": "Spa", "blocks": [
        {"id": "SP R", "kind": "reference", "laps": 5},
    ]}},
    "thermal_scenarios": {
        "reference": {"track_temperature_c": 29, "ambient_c": None},
        "elevated": {"track_temperature_c": 33, "ambient_c": None},
        "severe": {"track_temperature_c": 36, "ambient_c": None},
    },
}


def channel(value, unit, source="CSP_STATE", quality=None):
    return {"value": value, "unit": unit, "source": source,
            "quality": quality or ("available" if value is not None else "unavailable")}


def add_ai_run(store, *, ai_values=(True, True, True, True), ambient=25.0,
               physical_ids=None, native_temperature=True, pause_indices=()):
    run_id = "ai_spa"
    store.start_run(run_id, "MEASURED", {
        "physical_session_id": "ai_spa-s0", "selection": "ai_thermal",
        "campaign_counted": False, "ai_control_origin": "csp_hotkey_request",
        "track_id": "spa", "layout_id": "default",
    })
    # An intentionally mistaken campaign assignment and accepted native lap
    # cannot promote an AI run into Tyler's measured reference pace.
    store.assign_run(run_id, "spa", "SP R", {"assignment_status": "manual"})
    for index, ai_value in enumerate(ai_values, 1):
        values = {
            "is_ai_controlled": channel(ai_value, "boolean"),
            "speed_kmh": channel(180.0 + index, "km/h"),
            "throttle": channel(0.8, "fraction"),
            "brake": channel(0.1, "fraction"),
            "sim_paused": channel(False, "boolean"),
            "experiment_paused": channel(index in pause_indices, "boolean", "WIMNA_APP"),
            "ambient_air_c": channel(ambient, "degC"),
            "track_surface_c": channel(30.0, "degC"),
        }
        if native_temperature:
            values["native_water_temperature_c_approx"] = channel(88.0 + index, "degC")
            values["native_oil_temperature"] = channel(93.0 + index, "native_unit_unverified")
        store.add_record({"run_id": run_id, "physical_session_id":
                          physical_ids[index - 1] if physical_ids else "ai_spa-s0",
                          "seq": index, "kind": "sample", "sim_time_s": index * 0.1,
                          "wall_time_utc": None, "data": values})
    store.add_record({"run_id": run_id, "seq": 100, "kind": "event", "sim_time_s": 100,
                      "wall_time_utc": None,
                      "data": {"type": "lap_complete", "lap_index": 1, "lap_time_s": 100.0,
                               "valid_native": True, "flags": ["ai_thermal_run"]}})
    store.annotate_lap(run_id, 100, "program", True, [], "mistaken campaign annotation")
    return run_id


class AiThermalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "store")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_ai_intervals_model_separately_and_report_evidence(self):
        run_id = add_ai_run(self.store)
        forecasts, validation, parameters = analyze_store(self.store, CONFIG)
        self.assertEqual(forecasts["tracks"]["spa"]["reference"]["lap_count"], 0)
        self.assertNotIn(run_id, forecasts["measured_run_ids"])
        self.assertEqual(forecasts["supplementary_ai_run_ids"], [run_id])
        self.assertEqual(validation["status"], "unavailable")
        check = forecasts["ai_thermal"]["runs"][0]
        self.assertEqual(check["status"], "computed")
        self.assertFalse(check["campaign_counted"])
        self.assertEqual(check["ai_control_readback"], "observed_in_sample")
        self.assertEqual(check["observed_trace"]["valid_interval_count"], 3)
        self.assertAlmostEqual(check["observed_trace"]["observed_duration_s"], 0.3)
        self.assertEqual(check["scenario_inputs"]["ambient_evidence"], "MEASURED")
        self.assertEqual(set(check["modeled_profiles"]), {"low", "base", "high"})
        self.assertEqual(check["modeled_profiles"]["base"]["physical_effect_status"], "analysis_only")
        self.assertIn("native_water_temperature_c_approx", check["observed_trace"]["native_component_observations"])
        native_oil = check["observed_trace"]["native_component_observations"]["native_oil_temperature"]
        self.assertEqual(native_oil["unit"], "native_unit_unverified")
        self.assertNotIn("minimum_c", native_oil)
        self.assertEqual(parameters["supplementary_ai_run_ids"], [run_id])
        first_json = json.dumps(forecasts["ai_thermal"], sort_keys=True)
        second_json = json.dumps(analyze_store(self.store, CONFIG)[0]["ai_thermal"], sort_keys=True)
        self.assertEqual(first_json, second_json)
        output = Path(self.temp.name) / "bundle"
        export_bundle(self.store, output, config=CONFIG, forecasts=forecasts,
                      validation=validation, parameters=parameters)
        report = (output / "report.md").read_text(encoding="utf-8")
        summaries = json.loads((output / "run_summaries.json").read_text(encoding="utf-8"))
        self.assertEqual(summaries[0]["laps_included"], 0)
        self.assertFalse(summaries[0]["campaign_counted"])
        self.assertIn("Supplementary AI thermal checks", report)
        self.assertIn("AI-controlled intervals", report)
        self.assertIn("Above derate", report)
        self.assertIn("Ambient air 25.00 °C [MEASURED]", report)
        self.assertIn("native_oil_temperature: 94.00 to 97.00 native units", report)

    def test_null_ai_readback_never_qualifies_requested_control(self):
        add_ai_run(self.store, ai_values=(None, None, None, None))
        check = analyze_store(self.store, CONFIG)[0]["ai_thermal"]["runs"][0]
        self.assertEqual(check["status"], "unavailable")
        self.assertEqual(check["ai_control_readback"], "unavailable")
        self.assertEqual(check["observed_trace"]["valid_interval_count"], 0)
        self.assertEqual(check["modeled_profiles"], {})

    def test_missing_ambient_needs_explicit_assumption(self):
        add_ai_run(self.store, ambient=None, native_temperature=False)
        unavailable = analyze_store(self.store, CONFIG)[0]["ai_thermal"]["runs"][0]
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["scenario_inputs"]["ambient_evidence"], "UNAVAILABLE")
        computed = analyze_store(self.store, CONFIG,
                                 assumptions={"ai_thermal_ambient_c": 27.0})[0]["ai_thermal"]["runs"][0]
        self.assertEqual(computed["status"], "computed")
        self.assertEqual(computed["scenario_inputs"]["ambient_evidence"], "ASSUMED")
        self.assertEqual(computed["scenario_inputs"]["ambient_air_c"], 27.0)

    def test_physical_session_change_breaks_thermal_continuity(self):
        add_ai_run(self.store, physical_ids=("s0", "s0", "s1", "s1"))
        check = analyze_store(self.store, CONFIG)[0]["ai_thermal"]["runs"][0]
        self.assertEqual(check["observed_trace"]["valid_interval_count"], 2)
        self.assertEqual(check["observed_trace"]["segment_count"], 2)
        self.assertAlmostEqual(check["observed_trace"]["observed_duration_s"], 0.2)

    def test_experiment_pause_omits_interval(self):
        add_ai_run(self.store, pause_indices=(2,))
        check = analyze_store(self.store, CONFIG)[0]["ai_thermal"]["runs"][0]
        self.assertEqual(check["observed_trace"]["valid_interval_count"], 1)
        self.assertAlmostEqual(check["observed_trace"]["observed_duration_s"], 0.1)


if __name__ == "__main__":
    unittest.main()
