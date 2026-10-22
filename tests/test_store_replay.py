import json
import gzip
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from wimna.case_import import import_update6
from wimna.config import load_campaign, unresolved_live_inputs
from wimna.replay import replay_campaign
from wimna.store import Store, ingest_bridge
from wimna.vehicle_data import inspect_readable_car


class StoreTests(unittest.TestCase):
    def test_compact_bridge_roundtrip_preserves_metadata_and_old_text_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "bridge" / "runs" / "compact_1"
            folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(
                json.dumps({"physical_session_id": "compact_1-s0"}), encoding="utf-8")
            header = {"$": "WIMNA_COMPACT_JSONL_V1", "schema": 1,
                      "run_id": "compact_1", "physical_session_id": "compact_1-s0",
                      "channels": [["battery_energy_j", "J", "RSS_UNVERIFIED"],
                                   ["speed_kmh", "km/h", "CSP_STATE"],
                                   ["brake_temperature_c_fl", "degC", "CSP_STATE"]]}
            sample = {"$": "s", "s": 2, "t": 1.05, "w": "2026-10-22T01:00:00Z",
                      "v": [None, 212.5, 0], "q": [[3, "zero_may_mean_unavailable"]]}
            reset_sample = {"$": "s", "s": 4, "t": 1.15, "w": "2026-10-22T01:00:01Z",
                            "p": "compact_1-s1", "v": [None, 0, None], "q": []}
            event = {"schema": 1, "run_id": "compact_1", "physical_session_id": "compact_1-s0",
                     "seq": 3, "sim_time_s": 1.1, "wall_time_utc": None, "kind": "event",
                     "data": {"type": "lap_complete", "valid": False}}
            footer = {"$": "e", "n": 3}
            (folder / "000001.jsonl").write_text("\n".join(map(json.dumps, (header, sample, event, reset_sample, footer))) + "\n",
                                                encoding="utf-8")
            with Store(root / "store") as store:
                first = ingest_bridge(root / "bridge", store)
                self.assertEqual(first["new_records"], 3)
                self.assertEqual(first["errors"], [])
                decoded = next(store.iter_records("compact_1", "sample"))
                self.assertEqual(decoded["physical_session_id"], "compact_1-s0")
                self.assertEqual(decoded["data"]["speed_kmh"],
                                 {"value": 212.5, "unit": "km/h", "source": "CSP_STATE", "quality": "available"})
                self.assertEqual(decoded["data"]["battery_energy_j"],
                                 {"value": None, "unit": "J", "source": "RSS_UNVERIFIED", "quality": "unavailable"})
                self.assertEqual(decoded["data"]["brake_temperature_c_fl"]["quality"],
                                 "zero_may_mean_unavailable")
                self.assertEqual(list(store.iter_records("compact_1", "sample"))[-1]["physical_session_id"],
                                 "compact_1-s1")
                stored = store.db.execute("SELECT raw_json,data_json FROM records WHERE seq=2").fetchone()
                self.assertTrue(all(isinstance(value, bytes) for value in stored))
                self.assertEqual(ingest_bridge(root / "bridge", store)["new_records"], 0)
                # An earlier release stored ordinary TEXT JSON in the same table.
                legacy = {"run_id": "compact_1", "seq": 5, "kind": "event",
                          "sim_time_s": 2.0, "data": {"type": "legacy"}}
                store.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                                 ("compact_1", 5, "event", 2.0, None,
                                  json.dumps(legacy["data"]), json.dumps(legacy, sort_keys=True)))
                self.assertEqual(list(store.iter_records("compact_1", "event"))[-1]["data"],
                                 {"type": "legacy"})

    def test_compact_chunk_rejects_missing_channel_values_without_false_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "bridge" / "runs" / "bad_compact"
            folder.mkdir(parents=True)
            header = {"$": "WIMNA_COMPACT_JSONL_V1", "schema": 1,
                      "run_id": "bad_compact", "physical_session_id": "bad_compact-s0",
                      "channels": [["fuel", "L", "CSP_STATE"]]}
            sample = {"$": "s", "s": 1, "t": 1.0, "w": None, "v": [], "q": []}
            (folder / "000001.jsonl").write_text("\n".join(map(json.dumps, (header, sample, {"$": "e", "n": 1}))) + "\n",
                                                encoding="utf-8")
            with Store(root / "store") as store:
                result = ingest_bridge(root / "bridge", store)
                self.assertEqual(store.counts()["samples"], 0)
                self.assertIn("wrong value count", result["errors"][0]["error"])

    def test_compact_chunk_waits_for_footer_before_any_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "bridge" / "runs" / "unfinished"
            folder.mkdir(parents=True)
            header = {"$": "WIMNA_COMPACT_JSONL_V1", "schema": 1,
                      "run_id": "unfinished", "physical_session_id": "unfinished-s0",
                      "channels": [["fuel", "L", "CSP_STATE"]]}
            sample = {"$": "s", "s": 1, "t": 1.0, "w": None, "v": [2.0], "q": []}
            path = folder / "000001.jsonl"
            path.write_text("\n".join(map(json.dumps, (header, sample))) + "\n", encoding="utf-8")
            with Store(root / "store") as store:
                incomplete = ingest_bridge(root / "bridge", store)
                self.assertIn("missing footer", incomplete["errors"][0]["error"])
                self.assertEqual(store.counts()["samples"], 0)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"$": "e", "n": 1}) + "\n")
                complete = ingest_bridge(root / "bridge", store)
                self.assertEqual(complete["new_records"], 1)
                self.assertEqual(complete["errors"], [])

    def test_completed_chunk_with_later_invalid_record_imports_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "bridge" / "runs" / "atomic_compact"
            folder.mkdir(parents=True)
            header = {"$": "WIMNA_COMPACT_JSONL_V1", "schema": 1,
                      "run_id": "atomic_compact", "physical_session_id": "atomic_compact-s0",
                      "channels": [["fuel_l", "L", "CSP_STATE"]]}
            sample = {"$": "s", "s": 1, "t": 1.0, "w": None, "v": [5.0], "q": []}
            event = {"schema": 1, "run_id": "atomic_compact", "seq": -1,
                     "sim_time_s": 1.1, "kind": "event", "data": {"type": "marker"}}
            footer = {"$": "e", "n": 2}
            path = folder / "000001.jsonl"

            def write_chunk():
                path.write_text("\n".join(map(json.dumps, (header, sample, event, footer))) + "\n",
                                encoding="utf-8")

            write_chunk()
            with Store(root / "store") as store:
                rejected = ingest_bridge(root / "bridge", store)
                self.assertEqual(rejected["new_records"], 0)
                self.assertIn("sequence", rejected["errors"][0]["error"])
                self.assertEqual(store.counts()["samples"], 0)
                self.assertEqual(store.counts()["events"], 0)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0], 0)

                event["seq"] = 2
                write_chunk()
                recovered = ingest_bridge(root / "bridge", store)
                self.assertEqual(recovered["errors"], [])
                self.assertEqual(recovered["new_records"], 2)
                self.assertEqual(store.counts()["samples"], 1)
                self.assertEqual(store.counts()["events"], 1)
                self.assertEqual(ingest_bridge(root / "bridge", store)["new_records"], 0)

    def test_bridge_recovery_and_distinct_native_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = root / "bridge" / "runs"
            for run in ("native_1", "native_2"):
                folder = bridge / run
                folder.mkdir(parents=True)
                (folder / "manifest.json").write_text(json.dumps({"physical_session_id": run, "track": "monza_gp"}), encoding="utf-8")
                sample = {"schema": 1, "run_id": run, "seq": 0, "sim_time_s": 1.0,
                          "wall_time_utc": None, "kind": "sample",
                          "data": {"fuel_l": {"value": None, "unit": "L", "source": "CSP", "quality": "unavailable"}}}
                (folder / "000001.jsonl").write_text(json.dumps(sample) + "\n", encoding="utf-8")
            with Store(root / "store") as store:
                first = ingest_bridge(root / "bridge", store)
                self.assertEqual(first["new_records"], 2)
                self.assertEqual(store.counts()["runs"], 2)
                self.assertIsNone(next(store.iter_records("native_1"))["data"]["fuel_l"]["value"])
                store.save_campaign("campaign", {"phase": "interrupted", "cursor": 3})
            with Store(root / "store") as store:
                second = ingest_bridge(root / "bridge", store)
                self.assertEqual(second["new_records"], 0)
                self.assertEqual(store.counts()["samples"], 2)
                self.assertEqual(store.load_campaign("campaign")["cursor"], 3)
                store.register_prediction("p1", ["native_1"], {"id": "m1"}, {"time_s": 90})
                with self.assertRaises(ValueError):
                    store.attach_validation("p1", "native_1")
                store.attach_validation("p1", "native_2")
                store.assign_run("native_1", "monza_gp", "MZ R", {"verification": "manual"})
                self.assertEqual(store.get_run_assignment("native_1")["block"], "MZ R")
                with self.assertRaises(ValueError):
                    store.assign_run("native_1", "imola", "IM R")

    def test_changed_record_and_nan_rejected(self):
        with tempfile.TemporaryDirectory() as temp, Store(temp) as store:
            store.start_run("r1", "SYNTHETIC", {"physical_session_id": "r1"})
            record = {"run_id": "r1", "seq": 1, "kind": "sample", "sim_time_s": 1.0,
                      "data": {"x": {"value": None, "unit": "m"}}}
            self.assertTrue(store.add_record(record))
            self.assertFalse(store.add_record(record))
            changed = dict(record, data={"x": {"value": 0, "unit": "m"}})
            with self.assertRaises(ValueError):
                store.add_record(changed)
            with self.assertRaises(ValueError):
                store.add_record(dict(record, seq=2, data={"x": {"value": float("nan")}}))


class ConfigAndReplayTests(unittest.TestCase):
    def test_update6_import_retains_hash_cells_and_units(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "WIMNA_Update6.xlsx"
            sheets = {
                "04.A": {"B12": "P6", "D12": 46, "G12": 3, "H12": 5.4,
                         "I12": 0.31125, "D11": 68, "D9": 119},
                "04.C": {
                    **{f"D{row}": value for row, value in enumerate((31, 34, 27, 29, 36, 24, 30, 33), 7)},
                    **{f"G{row}": value for row, value in enumerate((0.4, 0.5, 0.2, 0.3, 0.5, 0.1, 0.2, 0.29), 7)},
                    **{f"I{row}": value for row, value in enumerate(("P5", "DNF", 6, 5, "DNF", 4, 7, "DNF"), 7)},
                    **{f"B{row}": row + 9 - 16 for row in range(16, 24)},
                    **{f"C{row}": "Mixed Layout" for row in range(16, 24)},
                },
                "04.D": {"B7": "energy-control"},
                "04.E": {"C6": "0.12–0.20", "G6": 10, "C7": "0.00–0.04", "G7": 12,
                         "C8": "0.06–0.12", "G8": 10},
            }
            with zipfile.ZipFile(path, "w") as archive:
                workbook_sheets = "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
                                          for i, name in enumerate(sheets, 1))
                archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + workbook_sheets + '</sheets></workbook>')
                rels = "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>'
                               for i in range(1, len(sheets) + 1))
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + rels + '</Relationships>')
                for i, cells in enumerate(sheets.values(), 1):
                    xml_cells = "".join((f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                                         if isinstance(value, str) else f'<c r="{ref}"><v>{value}</v></c>')
                                        for ref, value in cells.items())
                    archive.writestr(f"xl/worksheets/sheet{i}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">' + xml_cells + '</row></sheetData></worksheet>')
            imported = import_update6(path)
            self.assertEqual(imported["summary"]["gap_to_p3_points"], 73)
            self.assertEqual(imported["summary"]["historical_track_temperature_c"], [31, 34, 27, 29, 36, 24, 30, 33])
            self.assertEqual(imported["cells"]["04.A"]["B12"], "P6")
            self.assertEqual(len(imported["source_sha256"]), 64)

    def test_visible_vehicle_tables_are_inventoried_without_assumed_units(self):
        with tempfile.TemporaryDirectory() as temp:
            car = Path(temp) / "rss_formula_hybrid_2026"
            (car / "data").mkdir(parents=True)
            (car / "data" / "tyres.ini").write_text("[FRONT]\nWEAR_CURVE=wear.lut\n", encoding="utf-8")
            (car / "data" / "wear.lut").write_text("0|1\n1|0.8\n", encoding="utf-8")
            observed = inspect_readable_car(car)
            self.assertEqual(len(observed["files"]), 2)
            self.assertFalse(observed["model_ready"])
            self.assertIsNone(observed["files"][0]["entries"][0]["unit"])

    def test_template_budget_and_unresolved_native_inputs(self):
        config = load_campaign()
        self.assertEqual(config["track_order"], ["spa", "silverstone_gp", "imola", "monza_gp"])
        self.assertEqual(config["tracks"]["spa"]["track_id"], "spa")
        self.assertEqual(config["tracks"]["spa"]["layout_id"], "default")
        self.assertEqual(sum(item["minutes"] for item in config["schedule"]),
                         config["time_budget_minutes"])
        self.assertEqual(config["time_budget_minutes"], 315)
        self.assertNotIn("tracks.monza_gp.layout_id", unresolved_live_inputs(config))
        self.assertIn("policy.reference_compound", unresolved_live_inputs(config))

    def test_old_workbook_name_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ForReview.xlsx"
            path.write_bytes(b"not an xlsx")
            with self.assertRaisesRegex(ValueError, "Update6"):
                import_update6(path)

    def test_replay_is_deterministic_and_synthetic(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            a = replay_campaign(first)
            b = replay_campaign(second)
            self.assertEqual(a["block_results"], 24)
            self.assertEqual(a["scheduler_phase"], "complete")
            self.assertEqual(a["counts"], b["counts"])
            for name in ("scheduler_events.json", "scheduler_state.json", "forecasts.json", "validation.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
            summaries = json.loads((first / "results" / "run_summaries.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["evidence"] == "SYNTHETIC" for item in summaries))
            manifest = json.loads((first / "results" / "bundle_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["evidence_modes"], ["SYNTHETIC"])
            self.assertTrue(manifest["synthetic_counts_excluded_from_measured"])
            compressed = sorted((first / "results" / "runs").glob("*/telemetry-*.jsonl.gz"))
            self.assertTrue(compressed)
            with gzip.open(compressed[0], "rt", encoding="utf-8") as stream:
                self.assertEqual(json.loads(stream.readline())["kind"], "sample")
            for path in compressed:
                counterpart = second / "results" / path.relative_to(first / "results")
                self.assertEqual(path.read_bytes(), counterpart.read_bytes())
            self.assertTrue(any(item["path"].endswith(".jsonl.gz") for item in manifest["files"]))


if __name__ == "__main__":
    unittest.main()
