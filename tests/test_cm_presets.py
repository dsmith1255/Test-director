"""CM preset builder checks using a local schema fixture, never launching CM."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wimna.cm_presets import (CAR_ID, CUSTOM_MODE_PREFIX, MODES, TRACKS,
                              PresetError, build_campaign_pairs,
                              build_spa_pair)


class CmPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.ac_root = self.root / "ac"
        for path in (self.ac_root / "content" / "cars" / CAR_ID,
                     self.ac_root / "content" / "weather" / "3_clear"):
            path.mkdir(parents=True)
        for folder_id, _, name, ui_relative, model_relative in TRACKS.values():
            folder = self.ac_root / "content" / "tracks" / folder_id
            ui_file = folder / ui_relative
            ui_file.parent.mkdir(parents=True, exist_ok=True)
            ui_file.write_text(json.dumps({"name": name}), encoding="utf-8")
            (folder / model_relative).write_text("[MODEL_0]\nFILE=x.kn5\n",
                                                 encoding="utf-8")
        self.source = self.root / "user-presets" / "modern f1.cmpreset"
        self.source.parent.mkdir()
        self.template = {
            "Mode": "/Pages/Drive/QuickDrive_Trackday.xaml",
            "ModeData": json.dumps({"Version": 2,
                                    "RaceGridSerialized": "{\"ModeId\":\"same_car\"}"}),
            "CarId": "old-car", "TrackId": "old-track",
            "AssistsData": json.dumps({"TyreWear": 1.0,
                                       "FuelConsumption": 1.0,
                                       "Damage": 100.0}),
            "AssistsPresetFilename": "C:\\Users\\private\\assists.cmpreset",
            "TrackPropertiesData": json.dumps({"s": 1.0, "t": 1.0}),
            "TrackPropertiesPresetFilename": "C:\\Users\\private\\track.cmpreset",
        }
        self.source.write_text(json.dumps(self.template), encoding="utf-8")
        self.output = self.root / "build" / "cm-presets"

    def test_pair_is_matched_except_mode_and_contains_no_user_paths(self) -> None:
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        manifest = build_spa_pair(self.source, self.output, self.ac_root)
        self.assertEqual(manifest["source_preset_sha256"], source_hash)
        self.assertEqual(manifest["status"], "GENERATED_NOT_LIVE_VERIFIED")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        first, second = [], []
        for item in manifest["presets"]:
            path = self.output / item["file"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("C:\\Users", text)
            preset = json.loads(text)
            self.assertEqual(preset["CarId"], CAR_ID)
            self.assertEqual(preset["TrackId"], "spa")
            self.assertEqual(preset["WeatherId"], "3_clear")
            self.assertEqual(preset["Temperature"], 26.0)
            self.assertEqual(preset["crtv"], 29.0)
            self.assertTrue(preset["crt"])
            self.assertFalse(preset["RealConditions"])
            self.assertEqual(preset["Mode"], CUSTOM_MODE_PREFIX + item["mode_id"])
            self.assertEqual(json.loads(preset["ModeData"])["Version"], 2)
            self.assertNotIn("LapsNumber", json.loads(preset["ModeData"]))
            self.assertNotIn("RaceGridSerialized", json.loads(preset["ModeData"]))
            first.append(preset)
        self.assertEqual({item["mode_id"] for item in manifest["presets"]},
                         set(MODES.values()))
        a, b = first
        a.pop("Mode")
        b.pop("Mode")
        self.assertEqual(a, b)

    def test_refuses_user_preset_directory_and_implicit_overwrite(self) -> None:
        with self.assertRaises(PresetError):
            build_spa_pair(self.source, self.source.parent, self.ac_root)
        build_spa_pair(self.source, self.output, self.ac_root)
        with self.assertRaises(PresetError):
            build_spa_pair(self.source, self.output, self.ac_root)

    def test_refuses_non_unit_assist_multiplier(self) -> None:
        self.template["AssistsData"] = json.dumps({"TyreWear": 2.0,
                                                   "FuelConsumption": 1.0})
        self.source.write_text(json.dumps(self.template), encoding="utf-8")
        with self.assertRaisesRegex(PresetError, "TyreWear"):
            build_spa_pair(self.source, self.output, self.ac_root)

    def test_refuses_missing_installed_target(self) -> None:
        with self.assertRaisesRegex(PresetError, "not installed"):
            build_spa_pair(self.source, self.output, self.root)

    def test_four_track_pairs_use_exact_installed_layouts(self) -> None:
        original = self.source.read_bytes()
        manifest = build_campaign_pairs(self.source, self.output, self.ac_root)
        self.assertEqual(len(manifest["presets"]), 8)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(manifest["status"], "GENERATED_NOT_LIVE_VERIFIED")
        self.assertEqual(manifest["duration_status"],
                         "PRACTICE_PRESET_HAS_NO_HOURLY_END_CONTROL")
        self.assertEqual([x["content_manager_track_id"] for x in
                          manifest["installed_track_identities"]], list(TRACKS))
        for cm_track_id in TRACKS:
            records = [x for x in manifest["presets"]
                       if x["content_manager_track_id"] == cm_track_id]
            self.assertEqual(len(records), 2)
            self.assertEqual({x["mode_id"] for x in records}, set(MODES.values()))
            presets = []
            for record in records:
                path = self.output / record["file"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                                 record["sha256"])
                preset = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(preset["TrackId"], cm_track_id)
                self.assertEqual(preset["CarId"], CAR_ID)
                self.assertEqual(preset["Temperature"], 26.0)
                self.assertEqual(preset["crtv"], 29.0)
                self.assertNotIn("LapsNumber", json.loads(preset["ModeData"]))
                self.assertNotIn("RaceGridSerialized", json.loads(preset["ModeData"]))
                presets.append(preset)
            for preset in presets:
                preset.pop("Mode")
            self.assertEqual(presets[0], presets[1])
        silverstone = next(x for x in manifest["installed_track_identities"]
                           if x["content_manager_track_id"] == "ks_silverstone/gp")
        self.assertEqual(silverstone["track_id"], "ks_silverstone")
        self.assertEqual(silverstone["layout_id"], "gp")
        self.assertEqual(len(silverstone["models_ini_sha256"]), 64)

    def test_refuses_wrong_silverstone_layout_identity(self) -> None:
        ui = self.ac_root / "content" / "tracks" / "ks_silverstone" / "ui" / "gp" / "ui_track.json"
        ui.write_text(json.dumps({"name": "Silverstone - National"}), encoding="utf-8")
        with self.assertRaisesRegex(PresetError, "identity differs"):
            build_campaign_pairs(self.source, self.output, self.ac_root)


if __name__ == "__main__":
    unittest.main()
