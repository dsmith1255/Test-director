"""Local runner fixtures: no Content Manager or Assetto Corsa process starts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wimna.overnight import (ContentManagerUriLauncher, Observation,
                             OvernightQueue, QueueError)
from wimna.overnight_live import LiveOvernightRunner, main

class Clock:
    def __init__(self):
        self.seconds = 0.0

    def time(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class FakeLiveLauncher(ContentManagerUriLauncher):
    """Only a type-compatible fixture; deliberately bypasses CM construction."""

    def __init__(self, *, wrong_mode=False, recorder_lost=False,
                 stalled=False, close_on_finalize=True):
        self.wrong_mode = wrong_mode
        self.recorder_lost = recorder_lost
        self.stalled = stalled
        self.close_on_finalize = close_on_finalize
        self.running = False
        self.launched = []
        self.started = {}
        self.finalized = set()

    def launch(self, spec, now_s):
        self.launched.append(spec.session_id)
        self.started[spec.session_id] = now_s
        self.running = True

    def observe(self, spec, now_s):
        started = self.started.get(spec.session_id)
        if started is None:
            return None
        elapsed = max(0, now_s - started)
        laps = int(elapsed // 2)
        done = spec.session_id in self.finalized
        values = {**spec.expected, "run_id": "run-" + spec.session_id}
        if self.wrong_mode:
            values["mode_id"] = "ordinary-practice"
        recorder_active = not done and not (self.recorder_lost and elapsed >= 3)
        seq = int(min(elapsed, 1) * 20) if self.stalled else int(elapsed * 20)
        return Observation(
            now_s, "native-" + spec.session_id, values, True, recorder_active,
            done, not self.running if done else False, seq, laps,
            started + laps * 2 if laps else None, 0)

    def finalize(self, spec, now_s, observed):
        self.finalized.add(spec.session_id)
        if self.close_on_finalize:
            self.running = False


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue_path = self.root / "queue.json"
        self.queue_path.write_text(json.dumps({
            "schema_version": 1, "queue_id": "local-fixture",
            "launch_mode": "verified_local",
            "defaults": {
                "expected": {
                    "preset_id": "fixture-preset", "preset_sha256": "1" * 64,
                    "car_id": "fixture-car", "car_version": "fixture-v1",
                    "track_id": "spa", "layout_id": "default",
                    "variant_id": "fixture-stock", "variant_sha256": "2" * 64,
                    "mode_id": "fixture-ai", "tire_compound": "C4",
                    "energy_profile_id": "fixture-map", "ambient_air_c": 20.0,
                    "track_surface_c": 25.0, "fuel_start_l": 35.0,
                },
                "tolerances": {"ambient_air_c": 0.5, "track_surface_c": 0.5,
                               "fuel_start_l": 0.5},
                "min_lap_crossings": 2, "planned_switch_after_s": 15,
                "max_session_s": 40, "start_timeout_s": 8,
                "finalize_timeout_s": 8, "watchdog_s": 3,
                "max_dropped_samples": 0,
            },
            "sessions": [{"session_id": f"session-{index}"} for index in range(2)],
        }), encoding="utf-8")
        self.checkpoint = self.root / "overnight.checkpoint.json"

    def runner(self, adapter):
        clock = Clock()
        queue = OvernightQueue(self.queue_path, self.checkpoint, adapter)
        return LiveOvernightRunner(
            queue, process_running=lambda: adapter.running,
            clock_s=clock.time, sleep=clock.sleep, poll_s=1.0,
            max_total_s=100)

    def test_sessions_switch_only_after_save_and_process_exit(self):
        adapter = FakeLiveLauncher()
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(adapter.launched, ["session-0", "session-1"])
        self.assertEqual(len(state["completed"]), 2)
        self.assertTrue(all(row["run_id"] == "run-" + row["session_id"]
                            for row in state["completed"]))

    def test_wrong_mode_stops_before_next_dispatch_and_needs_manual_action(self):
        adapter = FakeLiveLauncher(wrong_mode=True)
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["failure_reason"], "readback_mismatch:mode_id")
        self.assertEqual(state["failure_handling"]["status"], "manual_action_required")
        self.assertEqual(adapter.launched, ["session-0"])
        self.assertEqual(adapter.finalized, set())

    def test_recorder_loss_stops_without_unverified_shutdown(self):
        adapter = FakeLiveLauncher(recorder_lost=True)
        state = self.runner(adapter).run()
        self.assertEqual(state["failure_reason"], "ai_or_recorder_lost")
        self.assertEqual(state["failure_handling"]["status"], "manual_action_required")
        self.assertEqual(adapter.launched, ["session-0"])
        self.assertEqual(adapter.finalized, set())

    def test_stalled_progress_requests_guarded_save_and_stops_queue(self):
        adapter = FakeLiveLauncher(stalled=True)
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["failure_reason"], "sample_progress_watchdog_expired")
        self.assertEqual(state["failure_handling"]["status"],
                         "guarded_save_shutdown_verified")
        self.assertEqual(adapter.launched, ["session-0"])
        self.assertEqual(adapter.finalized, {"session-0"})

    def test_process_must_exit_before_next_session(self):
        adapter = FakeLiveLauncher(close_on_finalize=False)
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["failure_reason"], "finalize_readback_timeout")
        self.assertEqual(adapter.launched, ["session-0"])
        self.assertEqual(state["failure_handling"]["status"], "manual_action_required")

    def test_runner_requires_closed_game_before_first_launch(self):
        adapter = FakeLiveLauncher()
        adapter.running = True
        with self.assertRaisesRegex(QueueError, "verified stopped"):
            self.runner(adapter).run()
        self.assertEqual(adapter.launched, [])

    def test_command_needs_explicit_activation_flag(self):
        with self.assertRaises(SystemExit):
            main(["run", "--config", str(self.root / "missing.json")])


if __name__ == "__main__":
    unittest.main()
