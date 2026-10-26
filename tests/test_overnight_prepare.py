"""Four-track overnight preparation is read-only outside its output folder."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wimna.cm_presets import CAR_ID, TRACKS, build_campaign_pairs
from wimna.overnight import dry_run, load_queue
from wimna.overnight_prepare import CAMPAIGN_ORDER, prepare_campaign


class CampaignPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ac_root = self.root / "assettocorsa"
        car = self.ac_root / "content" / "cars" / CAR_ID
        (car / "ui").mkdir(parents=True)
        (car / "ui" / "ui_car.json").write_text(
            json.dumps({"description": "FH26<br>Version: 2<br>"}), encoding="utf-8")
        (car / "data.acd").write_bytes(b"protected car physics fixture")
        weather = self.ac_root / "content" / "weather" / "3_clear"
        weather.mkdir(parents=True)
        (weather / "weather.ini").write_text("[WEATHER]\n", encoding="utf-8")
        for cm_id, (folder_id, layout, name, ui_rel, model_rel) in TRACKS.items():
            track = self.ac_root / "content" / "tracks" / folder_id
            ui = track / ui_rel
            ui.parent.mkdir(parents=True, exist_ok=True)
            ui.write_text(json.dumps({"name": name}), encoding="utf-8")
            model_ini = track / model_rel
            model_ini.write_text("[MODEL_0]\nFILE=layout.kn5\n", encoding="utf-8")
            (track / "layout.kn5").write_bytes(cm_id.encode("utf-8"))
            surfaces = track / ("data" if layout == "default" else layout + "/data") / "surfaces.ini"
            surfaces.parent.mkdir(parents=True, exist_ok=True)
            surfaces.write_text("[SURFACE_0]\n", encoding="utf-8")
        app = self.ac_root / "apps" / "lua" / "WimnaTestDirector"
        app.mkdir(parents=True)
        (app / "manifest.ini").write_text("[ABOUT]\n", encoding="utf-8")
        (app / "WimnaTestDirector.lua").write_text("return true\n", encoding="utf-8")
        for mode in ("wimna-ai-baseline", "wimna-ai-plan-c"):
            folder = self.ac_root / "extension" / "lua" / "new-modes" / mode
            folder.mkdir(parents=True)
            (folder / "manifest.ini").write_text("[ABOUT]\n", encoding="utf-8")
            (folder / "mode.lua").write_text("return true\n", encoding="utf-8")
            if mode.endswith("plan-c"):
                (folder / "aero_worker.lua").write_text("return true\n", encoding="utf-8")
        self.source = self.root / "source.cmpreset"
        self.source.write_text(json.dumps({
            "Mode": "/Pages/Drive/QuickDrive_Practice.xaml",
            "ModeData": "{}",
            "AssistsData": json.dumps({"TyreWear": 1.0, "FuelConsumption": 1.0}),
            "TrackPropertiesData": json.dumps({"s": 1.0}),
        }), encoding="utf-8")
        self.presets = self.root / "cm-presets"
        build_campaign_pairs(self.source, self.presets, self.ac_root)
        self.cm = self.root / "Content Manager.exe"
        self.cm.write_bytes(b"CM fixture")
        self.bridge = self.root / "documents" / "bridge"
        self.bridge.mkdir(parents=True)
        self.race_ini = self.root / "race.ini"
        self.race_ini.write_text("[RACE]\n", encoding="utf-8")
        self.output = self.root / "prepared"

    def prepare(self, output: Path | None = None) -> dict:
        return prepare_campaign(
            ac_root=self.ac_root, cm_exe=self.cm, preset_dir=self.presets,
            bridge_root=self.bridge, race_ini=self.race_ini,
            car_version="2", output=output or self.output,
            version_reader=lambda _: "1.2.3.4")

    def test_eight_session_shape_and_synthetic_completion(self) -> None:
        result = self.prepare()
        self.assertEqual(result["sessions"], 8)
        self.assertEqual(result["status"], "prepared_not_approved_or_launched")
        queue_path = Path(result["queue"])
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(queue["launch_mode"], "verified_local")
        self.assertEqual(queue["defaults"]["planned_switch_after_s"], 3600)
        self.assertEqual(queue["defaults"]["max_session_s"], 3960)
        self.assertEqual(queue["defaults"]["min_lap_crossings"], 2)
        self.assertEqual(queue["defaults"]["max_dropped_samples"], 100)
        self.assertEqual(
            [(s["expected"]["track_id"], s["expected"]["layout_id"],
              s["expected"]["variant_id"]) for s in queue["sessions"]],
            [(TRACKS[track][0], TRACKS[track][1], variant)
             for track, variant in CAMPAIGN_ORDER])
        self.assertEqual(len({s["session_id"] for s in queue["sessions"]}), 8)
        self.assertEqual(len({s["expected"]["preset_id"] for s in queue["sessions"]}), 8)
        for s in queue["sessions"]:
            self.assertEqual(s["expected"]["ambient_air_c"], 26.0)
            self.assertEqual(s["expected"]["track_surface_c"], 29.0)
            self.assertEqual(s["expected"]["fuel_start_l"], 50.0)
            self.assertEqual(s["expected"]["tire_compound"], "C4")
        _, _, loaded = load_queue(queue_path, allow_verified_local=True)
        self.assertEqual(len(loaded), 8)
        preflight = json.loads(Path(result["preflight"]).read_text(encoding="utf-8"))
        self.assertEqual(preflight["status"], "unverified")
        self.assertIsNone(preflight["verified_at_utc"])
        self.assertFalse(any(preflight[key] for key in (
            "uri_launch_smoke_tested", "two_stage_load_go_smoke_tested",
            "recording_save_smoke_tested", "session_close_smoke_tested",
            "readbacks_smoke_tested")))
        self.assertFalse((self.bridge.parent / "overnight.checkpoint.json").exists())
        config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
        self.assertEqual(Path(config["checkpoint_path"]).resolve(),
                         (self.output / "overnight.checkpoint.json").resolve())
        self.assertFalse((self.output / "overnight.checkpoint.json").exists())
        clone = dict(queue, launch_mode="dry_run")
        clone_path = self.root / "synthetic-clone.queue.json"
        clone_path.write_text(json.dumps(clone), encoding="utf-8")
        state = dry_run(clone_path, self.root / "synthetic.checkpoint.json",
                        tick_s=30, lap_s=97)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(len(state["completed"]), 8)
        self.assertTrue(all(row["evidence"] == "SYNTHETIC"
                            for row in state["completed"]))
        starts = {row["session_id"]: row["at_s"] for row in state["events"]
                  if row["type"] == "readbacks_verified"}
        finalizes = [row for row in state["events"]
                     if row["type"] == "finalize_intent"]
        self.assertEqual(len(finalizes), 8)
        self.assertTrue(all(row["at_s"] > starts[row["session_id"]] + 3600
                            for row in finalizes))

    def test_installed_file_hashes_are_recorded(self) -> None:
        result = self.prepare()
        for variant, path_text in result["variant_manifests"].items():
            manifest = json.loads(Path(path_text).read_text(encoding="utf-8"))
            self.assertEqual(manifest["variant_id"], variant)
            files = manifest["files"]
            paths = [row["path"] for row in files]
            self.assertTrue(any(path.endswith("data.acd") for path in paths))
            self.assertEqual(sum(path.endswith("layout.kn5") for path in paths), 4)
            self.assertEqual(sum(path.endswith("surfaces.ini") for path in paths), 4)
            self.assertTrue(any(path.endswith("weather.ini") for path in paths))
            for row in files:
                path = Path(row["path"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                                 row["sha256"])
            if variant == "plan_c":
                self.assertTrue(any(path.endswith("aero_worker.lua") for path in paths))
            else:
                self.assertFalse(any(path.endswith("aero_worker.lua") for path in paths))

    def test_stale_preset_or_track_file_is_rejected_without_output(self) -> None:
        preset = self.presets / "wimna-spa-baseline.cmpreset"
        preset.write_text(preset.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "generated preset changed"):
            self.prepare()
        self.assertFalse(self.output.exists())

        build_campaign_pairs(self.source, self.presets, self.ac_root, overwrite=True)
        track_ui = self.ac_root / "content" / "tracks" / "spa" / "ui" / "ui_track.json"
        track_ui.write_text(json.dumps({"name": "Spa", "edited": True}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "installed track file changed"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_requested_environment_change_cannot_be_silently_rehashed(self) -> None:
        preset = self.presets / "wimna-monza-plan_c.cmpreset"
        data = json.loads(preset.read_text(encoding="utf-8"))
        data["Temperature"] = 36.0
        preset.write_text(json.dumps(data), encoding="utf-8")
        manifest_path = self.presets / "four-track-preset-pairs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        row = next(row for row in manifest["presets"] if row["file"] == preset.name)
        row["sha256"] = hashlib.sha256(preset.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "environment differs"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_previous_pilot_checkpoint_is_preserved_and_wrong_car_version_rejected(self) -> None:
        checkpoint = self.bridge.parent / "overnight.checkpoint.json"
        checkpoint.write_text("keep pilot evidence", encoding="utf-8")
        self.prepare()
        self.assertEqual(checkpoint.read_text(encoding="utf-8"), "keep pilot evidence")
        car_ui = self.ac_root / "content" / "cars" / CAR_ID / "ui" / "ui_car.json"
        car_ui.write_text(json.dumps({"version": "different"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "car version differs"):
            self.prepare(self.root / "prepared-wrong-car")
        self.assertFalse((self.root / "prepared-wrong-car").exists())

    def test_drop_threshold_is_explicit_and_bounded(self) -> None:
        arguments = dict(
            ac_root=self.ac_root, cm_exe=self.cm, preset_dir=self.presets,
            bridge_root=self.bridge, race_ini=self.race_ini,
            car_version="2", output=self.output,
            version_reader=lambda _: "1.2.3.4")
        for invalid in (0, -1, 1001, 1.5, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ValueError, "drop threshold"):
                prepare_campaign(**arguments, max_dropped_samples=invalid)
        result = prepare_campaign(**arguments, max_dropped_samples=75)
        queue = json.loads(Path(result["queue"]).read_text(encoding="utf-8"))
        self.assertEqual(queue["defaults"]["max_dropped_samples"], 75)


if __name__ == "__main__":
    unittest.main()
