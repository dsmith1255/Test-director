"""Native AC replay archiving fixtures; no game process is launched."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_overnight import fixture_queue
from wimna.cli import _checkpoint_from_runner_config
from wimna.collection_analysis import _attach_overnight_comparison
from wimna.overnight import FixtureLauncher, Observation, OvernightQueue, load_queue
from wimna.replay_capture import ReplayArchiver
from wimna.report import export_bundle
from wimna.store import Store


class ReplayCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.replay_temp = self.root / "replay" / "temp"
        self.replay_temp.mkdir(parents=True)
        self.output = self.root / "archived"
        self.replay_ini = self.root / "cfg" / "replay.ini"
        self.replay_ini.parent.mkdir()
        self.replay_ini.write_text("[REPLAY]\nMAX_SIZE_MB=26\n[AUTOSAVE]\n"
                                   "ENABLED=1\nOTHERS=1\nMIN_TIME_SECONDS=30\n",
                                   encoding="utf-8")
        self.queue_file = self.root / "queue.json"
        fixture_queue(self.queue_file, sessions=1)
        self.spec = load_queue(self.queue_file)[2][0]
        self.archiver = ReplayArchiver(self.replay_temp, self.output, self.replay_ini,
                                       sleep=lambda _: None, poll_attempts=2)

    def observed(self, *, saved=True, closed=True):
        return Observation(20.0, "native-1", {**self.spec.expected, "run_id": "run-1"},
                           True, False, saved, closed, 400, 3, 19.0, 0)

    def replay(self, name="AC_251026-224900_O_fixture-car_spa_.acreplay",
               data=b"native replay bytes"):
        path = self.replay_temp / name
        path.write_bytes(data)
        return path

    def test_fresh_exact_replay_is_archived_with_explicit_content_limits(self):
        self.replay("AC_251026-220000_O_fixture-car_spa_.acreplay", b"old replay")
        self.archiver.prepare(self.spec, 0.0)
        self.replay("AC_251026-224900_O_fixture-car_monza_gp.acreplay")
        fresh = self.replay()
        row = self.archiver.archive(self.spec, self.observed(), 20.0)
        self.assertEqual(row["status"], "archived_content_unverified")
        self.assertEqual((self.output / row["archive_file"]).read_bytes(), fresh.read_bytes())
        self.assertEqual(row["sha256"], hashlib.sha256(fresh.read_bytes()).hexdigest())
        self.assertEqual(row["autosave_config"]["max_size_mb"], 26)
        self.assertFalse(row["full_lap_verified"])
        self.assertFalse(row["tv_camera_view_embedded_verified"])

    def test_stale_or_ambiguous_autosave_is_not_misattributed(self):
        self.replay()
        self.archiver.prepare(self.spec, 0.0)
        self.assertEqual(self.archiver.archive(self.spec, self.observed(), 20.0)["reason"],
                         "fresh_matching_replay_not_observed")
        self.replay("AC_251026-224901_O_fixture-car_spa_.acreplay")
        self.replay("AC_251026-224902_O_fixture-car_spa_default.acreplay")
        self.assertEqual(self.archiver.archive(self.spec, self.observed(), 20.0)["reason"],
                         "multiple_fresh_matching_replays")
        self.assertFalse(self.output.exists())

    def test_archive_requires_prepared_saved_closed_session(self):
        self.replay()
        self.assertEqual(self.archiver.archive(self.spec, self.observed(), 20.0)["reason"],
                         "session_not_prepared_saved_and_closed")
        self.archiver.prepare(self.spec, 0.0)
        self.assertEqual(self.archiver.archive(self.spec, self.observed(saved=False), 20.0)["reason"],
                         "session_not_prepared_saved_and_closed")
        self.assertEqual(self.archiver.archive(self.spec, self.observed(closed=False), 20.0)["reason"],
                         "session_not_prepared_saved_and_closed")

    def test_queue_archives_after_finalize_before_next_launch(self):
        fixture_queue(self.queue_file, sessions=2)
        archiver = ReplayArchiver(self.replay_temp, self.output, self.replay_ini,
                                  sleep=lambda _: None, poll_attempts=2)
        replay_temp = self.replay_temp

        class SavesReplayOnFinalize(FixtureLauncher):
            def finalize(self, spec, now_s, observed):
                super().finalize(spec, now_s, observed)
                (replay_temp / f"AC_251026-{len(self.finalized):06d}_O_"
                               "fixture-car_spa_.acreplay").write_bytes(
                                   f"replay-{spec.session_id}".encode())

        launcher = SavesReplayOnFinalize(5)
        queue = OvernightQueue(self.queue_file, self.root / "checkpoint.json",
                               launcher, replay_archiver=archiver)
        for tick in range(150):
            state = queue.tick(float(tick))
            if state["phase"] in {"complete", "stopped"}:
                break
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(len(state["completed"]), 2)
        self.assertEqual([row["replay_capture"]["status"] for row in state["completed"]],
                         ["archived_content_unverified"] * 2)
        self.assertEqual(len(list(self.output.glob("*.acreplay"))), 2)
        checkpoint = json.loads((self.root / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["completed"][0]["replay_capture"]["run_id"],
                         "fixture-run-session-0")

    def test_explicit_report_config_checks_queue_hash_and_shows_replay(self):
        checkpoint = self.root / "checkpoint.json"
        replay_capture = {"status": "archived_content_unverified",
                          "archive_file": "run-1_spa_default.acreplay",
                          "full_lap_verified": False,
                          "tv_camera_view_embedded_verified": False}
        checkpoint.write_text(json.dumps({
            "schema_version": 1,
            "queue_sha256": hashlib.sha256(self.queue_file.read_bytes()).hexdigest(),
            "completed": [{"run_id": "run-1", "physical_session_id": "native-1",
                           "evidence": "MEASURED", "comparison_metadata_verified": True,
                           "comparison_limits": [], "replay_capture": replay_capture}],
        }), encoding="utf-8")
        runner = self.root / "runner.json"
        runner.write_text(json.dumps({"schema_version": 1,
                                      "queue_path": str(self.queue_file.resolve()),
                                      "checkpoint_path": str(checkpoint.resolve())}),
                          encoding="utf-8")
        self.assertEqual(_checkpoint_from_runner_config(runner).resolve(),
                         checkpoint.resolve())
        stint = {"run_id": "run-1", "physical_session_id": "native-1",
                 "collection_stint_id": "s0", "start_trigger": "overnight_mode_auto",
                 "evidence": "MEASURED", "laps": []}
        _attach_overnight_comparison([stint], _checkpoint_from_runner_config(runner))
        self.assertEqual(stint["overnight_comparison"]["replay_capture"], replay_capture)
        with Store(self.root / "store") as store:
            export_bundle(store, self.root / "report", config={"track_order": [], "tracks": {}},
                          forecasts={"collection": {"status": "available", "stints": [stint]}})
        report = (self.root / "report" / "report.md").read_text(encoding="utf-8")
        self.assertIn("Native replay archives", report)
        self.assertIn("run-1_spa_default.acreplay", report)
        self.assertIn("full-lap coverage or TV camera playback", report)
        self.queue_file.write_text(self.queue_file.read_text(encoding="utf-8") + "\n",
                                   encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            _checkpoint_from_runner_config(runner)


if __name__ == "__main__":
    unittest.main()
