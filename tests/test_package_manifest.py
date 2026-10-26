"""CSP must register the director window for Start/Resume to be usable."""
from __future__ import annotations

import configparser
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import package_source


class LuaManifestTests(unittest.TestCase):
    def test_window_section_uses_csp_literal_name(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = configparser.ConfigParser()
        manifest.read(root / "ac_app" / "apps" / "lua" / "WimnaTestDirector" / "manifest.ini",
                      encoding="utf-8")
        self.assertIn("WINDOW_...", manifest)
        self.assertEqual(manifest["WINDOW_..."]["function_main"], "windowMain")
        self.assertNotIn("WINDOW_MAIN", manifest)

    def test_all_four_modes_are_in_source_and_content_manager_package(self) -> None:
        root = Path(__file__).resolve().parents[1]
        modes = (
            ("wimna-test", "WIMNA Test", False),
            ("wimna-ai-thermal", "WIMNA AI Thermal", True),
            ("wimna-ai-baseline", "WIMNA AI Baseline", True),
            ("wimna-ai-plan-c", "WIMNA AI Plan C Prototype", True),
        )

        def check_mode(manifest: configparser.ConfigParser, *, name: str,
                       extended_physics: bool) -> None:
            self.assertEqual(manifest["RULES"]["base_mode"], "PRACTICE")
            self.assertEqual(manifest["RULES"]["allow_physics_alterations"], "1")
            self.assertEqual(manifest["TWEAKS"]["associated_apps"], "WimnaTestDirector")
            self.assertEqual(manifest["ABOUT"]["name"], name)
            self.assertEqual(manifest["ABOUT"]["version"], "0.5.0")
            if extended_physics:
                self.assertEqual(manifest["TWEAKS"]["force_extended_physics"], "1")
            else:
                self.assertNotIn("force_extended_physics", manifest["TWEAKS"])

        for mode_id, name, extended_physics in modes:
            relative_path = Path(f"extension/lua/new-modes/{mode_id}/manifest.ini")
            source = root / "ac_app" / relative_path
            manifest = configparser.ConfigParser()
            manifest.read(source, encoding="utf-8")
            check_mode(manifest, name=name, extended_physics=extended_physics)
            self.assertTrue(source.with_name("mode.lua").is_file())

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is required to verify the Content Manager package")
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "WIMNA-Test-Director-CM.zip"
            subprocess.run(
                [powershell, "-NoProfile", "-File", str(root / "ac_app" / "Build-AppPackage.ps1"),
                 "-OutputPath", str(archive_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            with zipfile.ZipFile(archive_path) as archive:
                for mode_id, expected_name, extended_physics in modes:
                    relative_path = Path(f"extension/lua/new-modes/{mode_id}/manifest.ini")
                    archive_name = relative_path.as_posix()
                    self.assertIn(archive_name, archive.namelist())
                    script_path = relative_path.with_name("mode.lua")
                    self.assertIn(script_path.as_posix(), archive.namelist())
                    self.assertEqual(archive.read(script_path.as_posix()),
                                     (root / "ac_app" / script_path).read_bytes())
                    packaged = configparser.ConfigParser()
                    packaged.read_string(archive.read(archive_name).decode("utf-8"))
                    check_mode(packaged, name=expected_name, extended_physics=extended_physics)

    def test_source_archive_includes_overnight_queue_example(self) -> None:
        output = package_source.OUTPUT
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                package_source.OUTPUT = Path(temp_dir) / "WIMNA-Test-Director-source.zip"
                package_source.main()
                with zipfile.ZipFile(package_source.OUTPUT) as archive:
                    self.assertIn("overnight.queue.example.json", archive.namelist())
                    for source in ("wimna/overnight_live.py",
                                   "wimna/overnight_prepare.py",
                                   "wimna/bridge_observation.py",
                                   "tests/test_overnight_live.py"):
                        self.assertIn(source, archive.namelist())
                    for mode_id in ("wimna-ai-baseline", "wimna-ai-plan-c"):
                        self.assertIn(
                            f"ac_app/extension/lua/new-modes/{mode_id}/mode.lua",
                            archive.namelist())
            finally:
                package_source.OUTPUT = output


if __name__ == "__main__":
    unittest.main()
