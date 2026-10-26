"""The frozen companion exposes explicit local queue commands without opening its GUI."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_gui


class CompanionCommandTests(unittest.TestCase):
    def test_preset_generation_dispatch_never_starts_runner_or_gui(self) -> None:
        arguments = [r"C:\WIMNA\source.cmpreset", r"D:\Games\assettocorsa",
                     "--output", r"C:\WIMNA\presets"]
        with patch("wimna.cm_presets.main", return_value=0) as generate, \
                patch("wimna.overnight_live.main") as run, \
                patch("wimna.gui.main") as gui:
            self.assertEqual(launch_gui.main(["--generate-presets", *arguments]), 0)
        generate.assert_called_once_with(arguments)
        run.assert_not_called()
        gui.assert_not_called()

    def test_runner_dispatch_adds_explicit_run_verb(self) -> None:
        arguments = ["--config", r"C:\WIMNA\runner.json", "--activate-local-live"]
        with patch("wimna.overnight_live.main", return_value=0) as run, \
                patch("wimna.gui.main") as gui:
            self.assertEqual(launch_gui.main(["--overnight-runner", *arguments]), 0)
        run.assert_called_once_with(["run", *arguments])
        gui.assert_not_called()

    def test_preparation_dispatch_never_calls_runner_or_gui(self) -> None:
        arguments = ["--campaign", "--output", r"C:\WIMNA\overnight"]
        with patch("wimna.overnight_prepare.main", return_value=0) as prepare, \
                patch("wimna.overnight_live.main") as run, \
                patch("wimna.gui.main") as gui:
            self.assertEqual(launch_gui.main(["--prepare-overnight", *arguments]), 0)
        prepare.assert_called_once_with(arguments)
        run.assert_not_called()
        gui.assert_not_called()

    def test_windowed_command_writes_local_failure_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "companion-command.log"
            with patch.object(launch_gui, "_command_log_path", return_value=log_path), \
                    patch.object(launch_gui.sys, "stdout", None), \
                    patch.object(launch_gui.sys, "stderr", None), \
                    patch("wimna.overnight_live.main", side_effect=ValueError("fixture failure")):
                result = launch_gui.main(["--overnight-runner", "--config", "missing"])
            self.assertEqual(result, 2)
            self.assertIn("--overnight-runner", log_path.read_text(encoding="utf-8"))
            self.assertIn("ValueError: fixture failure", log_path.read_text(encoding="utf-8"))

    def test_default_launch_still_opens_gui(self) -> None:
        with patch("wimna.gui.main", return_value=0) as gui:
            self.assertEqual(launch_gui.main([]), 0)
        gui.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
