"""Detached start must remain explicit, single-owner, and observable."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from wimna.overnight import QueueError
from wimna.overnight_background import start_background, status
from wimna.overnight_live import prevent_idle_sleep, runner_lock


class BackgroundStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkpoint = self.root / "overnight.checkpoint.json"
        self.queue_path = self.root / "queue.json"
        self.queue_path.write_text('{"queue":"fixture"}', encoding="utf-8")
        self.queue_hash = hashlib.sha256(self.queue_path.read_bytes()).hexdigest()
        self.config = self.root / "runner.json"
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "queue_path": str(self.queue_path),
            "bridge_root": str(self.root / "bridge"),
            "checkpoint_path": str(self.checkpoint),
        }), encoding="utf-8")
        self.spawned: list[list[str]] = []

    def _runner(self, _config: Path):
        self.checkpoint.write_text(json.dumps({
            "phase": "pending", "queue_sha256": self.queue_hash, "completed": [],
        }), encoding="utf-8")
        return SimpleNamespace(queue=SimpleNamespace(
            state={"phase": "pending"}, queue_id="four-track-plan-c-paired-overnight",
            sessions=[object() for _ in range(8)], queue_hash=self.queue_hash))

    def _spawn(self, command: list[str], cwd: Path):
        self.spawned.append(command)
        self.assertEqual(cwd.resolve(), self.root.resolve())
        return SimpleNamespace(pid=43210)

    def test_explicit_start_records_pid_without_launching_in_parent(self) -> None:
        record = start_background(
            self.config, runner_builder=self._runner,
            process_running=lambda: False, spawner=self._spawn)
        self.assertEqual(record["status"], "spawned_unverified")
        self.assertEqual(record["worker_pid"], 43210)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("--activate-local-live", self.spawned[0])
        self.assertIn("--overnight-runner", self.spawned[0])
        self.assertEqual(status(self.config, pid_alive=lambda _pid: True)["phase"], "pending")

    def test_duplicate_start_is_rejected_without_spawning(self) -> None:
        start_background(self.config, runner_builder=self._runner,
                         process_running=lambda: False, spawner=self._spawn)
        with self.assertRaisesRegex(QueueError, "already attempted"):
            start_background(self.config, runner_builder=self._runner,
                             process_running=lambda: False, spawner=self._spawn)
        self.assertEqual(len(self.spawned), 1)

    def test_preflight_builder_rejection_never_spawns(self) -> None:
        def reject(_config: Path):
            raise QueueError("preflight expired")

        with self.assertRaisesRegex(QueueError, "preflight expired"):
            start_background(self.config, runner_builder=reject,
                             process_running=lambda: False, spawner=self._spawn)
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.checkpoint.with_name(
            self.checkpoint.name + ".background.json").exists())

    def test_active_checkpoint_and_open_game_refuse_start(self) -> None:
        self.checkpoint.write_text(json.dumps({"phase": "recording"}), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "pending checkpoint"):
            start_background(self.config, runner_builder=self._runner,
                             process_running=lambda: False, spawner=self._spawn)
        self.checkpoint.write_text(json.dumps({"phase": "pending"}), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "verified stopped"):
            start_background(self.config, runner_builder=self._runner,
                             process_running=lambda: True, spawner=self._spawn)
        self.assertEqual(self.spawned, [])

    def test_status_flags_worker_loss_without_modifying_checkpoint(self) -> None:
        start_background(self.config, runner_builder=self._runner,
                         process_running=lambda: False, spawner=self._spawn)
        state = {"phase": "recording", "queue_sha256": self.queue_hash, "completed": []}
        self.checkpoint.write_text(json.dumps(state), encoding="utf-8")
        result = status(self.config, pid_alive=lambda _pid: False)
        self.assertTrue(result["attention_required"])
        self.assertEqual(result["reason"], "active_queue_worker_missing_or_unknown")
        self.assertEqual(json.loads(self.checkpoint.read_text(encoding="utf-8")), state)

    def test_status_rejects_unrelated_checkpoint_without_mutating_it(self) -> None:
        unrelated = {"phase": "complete", "queue_sha256": "a" * 64,
                     "completed": [1]}
        self.checkpoint.write_text(json.dumps(unrelated), encoding="utf-8")
        result = status(self.config)
        self.assertTrue(result["attention_required"])
        self.assertEqual(result["reason"], "checkpoint_queue_mismatch")
        self.assertEqual(json.loads(self.checkpoint.read_text(encoding="utf-8")), unrelated)

    def test_process_lock_blocks_second_owner_before_queue_build(self) -> None:
        with runner_lock(self.checkpoint):
            with self.assertRaisesRegex(QueueError, "another overnight runner"):
                with runner_lock(self.checkpoint):
                    pass

    def test_temporary_idle_sleep_request_releases_on_exit(self) -> None:
        with prevent_idle_sleep():
            pass


if __name__ == "__main__":
    unittest.main()
