"""Report labels for diagnostic runs outside the core campaign."""

import json
import tempfile
import unittest
from pathlib import Path

from wimna.analysis import analyze_store
from wimna.report import export_bundle, summarize_run
from wimna.store import Store


class DiagnosticTrackReportTests(unittest.TestCase):
    def test_assigned_spa_reference_is_campaign_evidence_in_four_track_order(self):
        order = ["spa", "silverstone_gp", "imola", "monza_gp"]
        names = {"spa": "Spa GP", "silverstone_gp": "Silverstone GP",
                 "imola": "Imola", "monza_gp": "Modern Monza GP"}
        prefixes = {"spa": "SP", "silverstone_gp": "SI",
                    "imola": "IM", "monza_gp": "MZ"}
        config = {"program_version": "four-track-test", "track_order": order,
                  "tracks": {track: {"display_name": names[track], "blocks": [
                      {"id": f"{prefixes[track]} R", "kind": "reference", "laps": 5}]}
                             for track in order},
                  "thermal_scenarios": {}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root / "store") as store:
                store.start_run("spa-reference", "MEASURED", {
                    "physical_session_id": "spa-reference", "track_id": "spa",
                    "layout_id": "default", "car_id": "rss_formula_hybrid_2026"})
                store.add_record({"run_id": "spa-reference", "seq": 1, "kind": "event",
                                  "sim_time_s": 132.0, "wall_time_utc": None,
                                  "data": {"type": "lap_complete", "lap_index": 1,
                                           "lap_time_s": 132.0, "valid_native": True,
                                           "flags": [], "fuel_used_l": 2.8}})
                store.annotate_lap("spa-reference", 1, "program", True, [], "accepted")
                store.assign_run("spa-reference", "spa", "SP R",
                                 {"assignment_status": "confirmed"})
                store.start_run("spa-diagnostic", "MEASURED", {
                    "physical_session_id": "spa-diagnostic", "track_id": "spa",
                    "layout_id": "default"})

                forecasts, validation, parameters = analyze_store(store, config)
                self.assertEqual(forecasts["track_order"], order)
                self.assertEqual(list(forecasts["tracks"]), order)
                self.assertEqual(forecasts["tracks"]["spa"]["reference"]["run_ids"],
                                 ["spa-reference"])
                self.assertEqual(forecasts["tracks"]["spa"]["reference"]["pace_mean_s"], 132.0)
                self.assertEqual(forecasts["tracks"]["spa"]["reference"]["evidence"], ["MEASURED"])
                self.assertEqual(parameters["calibrated_by_track"]["spa"]["reference_pace_s"]["evidence"],
                                 "MEASURED")

                output = root / "export"
                export_bundle(store, output, config=config, forecasts=forecasts,
                              validation=validation, parameters=parameters)
                report = (output / "report.md").read_text(encoding="utf-8")
                summaries = json.loads((output / "run_summaries.json").read_text(encoding="utf-8"))
                self.assertIn("Campaign order: Spa GP → Silverstone GP → Imola → Modern Monza GP.", report)
                self.assertIn("## Spa GP — SP R [MEASURED]", report)
                self.assertIn("### Spa GP [MEASURED]", report)
                self.assertLess(report.index("### Spa GP [MEASURED]"),
                                report.index("### Silverstone GP [unavailable]"))
                self.assertLess(report.index("### Silverstone GP [unavailable]"),
                                report.index("### Imola [unavailable]"))
                self.assertLess(report.index("### Imola [unavailable]"),
                                report.index("### Modern Monza GP [unavailable]"))
                self.assertEqual(summaries[0]["run_id"], "spa-reference")
                self.assertTrue(summaries[0]["campaign_block"])
                self.assertFalse(summaries[1]["campaign_block"])

    def test_unassigned_spa_run_uses_manifest_track_without_campaign_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Store(root / "store") as store:
                store.start_run("spa-diagnostic-1", "MEASURED", {
                    "physical_session_id": "spa-diagnostic-1",
                    "track_id": "spa", "layout_id": "gp", "car_id": "rss_formula_hybrid_2026",
                    "selection": "individual_diagnostic",
                })
                run = next(store.iter_runs())
                summary = summarize_run(store, run)
                self.assertEqual(summary["track"], "spa")
                self.assertEqual(summary["layout_id"], "gp")
                self.assertEqual(summary["track_source"], "manifest.track_id")
                self.assertIsNone(summary["block"])
                self.assertIsNone(store.get_run_assignment(run["run_id"]))

                output = root / "export"
                export_bundle(store, output, config={"track_order": [], "tracks": {}})
                report = (output / "report.md").read_text(encoding="utf-8")
                summaries = json.loads((output / "run_summaries.json").read_text(encoding="utf-8"))
                assignments = json.loads((output / "run_assignments.json").read_text(encoding="utf-8"))
                self.assertIn("## spa / gp — spa-diagnostic-1 [MEASURED]", report)
                self.assertNotIn("Unknown track", report)
                self.assertEqual(summaries[0]["track_source"], "manifest.track_id")
                self.assertEqual(assignments, [])


if __name__ == "__main__":
    unittest.main()
