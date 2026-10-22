"""CSP must register the director window for Start/Resume to be usable."""
from __future__ import annotations

import configparser
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


class LuaManifestTests(unittest.TestCase):
    def test_window_section_uses_csp_literal_name(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = configparser.ConfigParser()
        manifest.read(root / "ac_app" / "apps" / "lua" / "WimnaTestDirector" / "manifest.ini",
                      encoding="utf-8")
        self.assertIn("WINDOW_...", manifest)
        self.assertEqual(manifest["WINDOW_..."]["function_main"], "windowMain")
        self.assertNotIn("WINDOW_MAIN", manifest)

    def test_campaign_and_ai_modes_have_distinct_physics_in_source_and_package(self) -> None:
        root = Path(__file__).resolve().parents[1]
        campaign_path = Path("extension/lua/new-modes/wimna-test/manifest.ini")
        ai_path = Path("extension/lua/new-modes/wimna-ai-thermal/manifest.ini")

        def check_mode(manifest: configparser.ConfigParser, *, ai: bool) -> None:
            self.assertEqual(manifest["RULES"]["base_mode"], "PRACTICE")
            self.assertEqual(manifest["RULES"]["allow_physics_alterations"], "1")
            self.assertEqual(manifest["TWEAKS"]["associated_apps"], "WimnaTestDirector")
            if ai:
                self.assertEqual(manifest["TWEAKS"]["force_extended_physics"], "1")
                self.assertEqual(manifest["ABOUT"]["name"], "WIMNA AI Thermal")
            else:
                self.assertNotIn("force_extended_physics", manifest["TWEAKS"])
                self.assertEqual(manifest["ABOUT"]["name"], "WIMNA Test")

        for relative_path, ai in ((campaign_path, False), (ai_path, True)):
            source = root / "ac_app" / relative_path
            manifest = configparser.ConfigParser()
            manifest.read(source, encoding="utf-8")
            check_mode(manifest, ai=ai)
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
                for relative_path, ai in ((campaign_path, False), (ai_path, True)):
                    name = relative_path.as_posix()
                    self.assertIn(name, archive.namelist())
                    self.assertIn(relative_path.with_name("mode.lua").as_posix(), archive.namelist())
                    packaged = configparser.ConfigParser()
                    packaged.read_string(archive.read(name).decode("utf-8"))
                    check_mode(packaged, ai=ai)


if __name__ == "__main__":
    unittest.main()
