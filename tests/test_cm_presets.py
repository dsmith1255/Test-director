"""CM preset builder checks using a local schema fixture, never launching CM."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wimna.cm_presets import (CAR_ID, CUSTOM_MODE_PREFIX, MODES, PresetError,
                              build_spa_pair)


class CmPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.ac_root = self.root / "ac"
        for path in (self.ac_root / "content" / "cars" / CAR_ID,
                     self.ac_root / "content" / "tracks" / "spa",
                     self.ac_root / "content" / "weather" / "3_clear"):
            path.mkdir(parents=True)
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


if __name__ == "__main__":
    unittest.main()
