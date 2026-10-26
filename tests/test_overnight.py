"""Fail-closed overnight queue checks using only a deterministic fixture."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from wimna.overnight import (ContentManagerUriLauncher, FixtureLauncher,
                             OvernightQueue, QueueError, dry_run, load_queue)


def fixture_queue(path: Path, *, sessions: int = 2) -> None:
    expected = {
        "preset_id": "fixture-preset", "preset_sha256": "1" * 64,
        "car_id": "fixture-car", "car_version": "fixture-v1",
        "track_id": "spa", "layout_id": "default",
        "variant_id": "fixture-stock", "variant_sha256": "2" * 64,
        "mode_id": "fixture-ai", "tire_compound": "C4",
        "energy_profile_id": "fixture-map", "ambient_air_c": 20.0,
        "track_surface_c": 25.0, "fuel_start_l": 35.0,
    }
    path.write_text(json.dumps({
        "schema_version": 1, "queue_id": "fixture", "launch_mode": "dry_run",
        "defaults": {
            "expected": expected,
            "tolerances": {"ambient_air_c": 0.5, "track_surface_c": 0.5,
                           "fuel_start_l": 0.5},
            "min_lap_crossings": 2, "planned_switch_after_s": 15,
            "max_session_s": 40, "start_timeout_s": 8,
            "finalize_timeout_s": 8, "watchdog_s": 3,
            "max_dropped_samples": 0,
        },
        "sessions": [{"session_id": f"session-{index}"} for index in range(sessions)],
    }), encoding="utf-8")


class WrongVariant(FixtureLauncher):
    def observe(self, spec, now_s):
        observed = super().observe(spec, now_s)
        if observed is None:
            return None
        return replace(observed, values={**observed.values, "variant_sha256": "3" * 64})


class StalledRecorder(FixtureLauncher):
    def observe(self, spec, now_s):
        observed = super().observe(spec, now_s)
        return None if observed is None else replace(observed, sample_seq=0)


class UnfinishedRecorder(FixtureLauncher):
    def observe(self, spec, now_s):
        observed = super().observe(spec, now_s)
        if observed is None:
            return None
        if spec.session_id in self.finalized:
            return replace(observed, recorder_active=True, recorder_saved=False,
                           session_closed=False)
        return observed


class ReusedNativeSession(FixtureLauncher):
    def observe(self, spec, now_s):
        observed = super().observe(spec, now_s)
        return None if observed is None else replace(observed, physical_session_id="same-native-id")


class OvernightQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue_path = self.root / "queue.json"
        self.checkpoint_path = self.root / "checkpoint.json"
        fixture_queue(self.queue_path)

    def run_clock(self, launcher, *, until=150):
        queue = OvernightQueue(self.queue_path, self.checkpoint_path, launcher)
        for tick in range(until):
            state = queue.tick(float(tick))
            if state["phase"] in {"complete", "stopped"}:
                return state
        self.fail("fixture queue did not reach a terminal state")

    def test_deterministic_two_session_rehearsal(self):
        first = dry_run(self.queue_path, self.checkpoint_path, tick_s=1.0, lap_s=5.0)
        self.assertEqual(first["phase"], "complete")
        self.assertEqual([row["session_id"] for row in first["completed"]],
                         ["session-0", "session-1"])
        self.assertEqual([row["lap_crossings"] for row in first["completed"]], [4, 4])
        self.assertEqual({row["physical_session_id"] for row in first["completed"]},
                         {"fixture-session-0", "fixture-session-1"})
        self.assertTrue(all(row["evidence"] == "SYNTHETIC" for row in first["completed"]))
        second = OvernightQueue(self.queue_path, self.checkpoint_path, FixtureLauncher(5))
        self.assertEqual(second.tick(999)["completed"], first["completed"])

    def test_wrong_variant_stops_before_recording(self):
        state = self.run_clock(WrongVariant(5))
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["failure_reason"], "readback_mismatch:variant_sha256")
        self.assertEqual(state["completed"], [])

    def test_progress_watchdog_stops_dispatch(self):
        state = self.run_clock(StalledRecorder(5))
        self.assertEqual(state["failure_reason"], "sample_progress_watchdog_expired")
        self.assertEqual(state["index"], 0)

    def test_final_chunk_must_be_saved_before_next_launch(self):
        state = self.run_clock(UnfinishedRecorder(5))
        self.assertEqual(state["failure_reason"], "finalize_readback_timeout")
        self.assertEqual(state["index"], 0)
        self.assertFalse(state["completed"])

    def test_reusing_native_session_id_stops_second_session(self):
        state = self.run_clock(ReusedNativeSession(5))
        self.assertEqual(state["failure_reason"], "missing_or_reused_physical_session_id")
        self.assertEqual(len(state["completed"]), 1)

    def test_restart_during_active_session_cannot_relaunch(self):
        launcher = FixtureLauncher(5)
        queue = OvernightQueue(self.queue_path, self.checkpoint_path, launcher)
        queue.tick(0)
        queue.tick(1)
        restarted_launcher = FixtureLauncher(5)
        restarted = OvernightQueue(self.queue_path, self.checkpoint_path,
                                   restarted_launcher)
        self.assertEqual(restarted.state["failure_reason"],
                         "runner_restarted_during_active_physical_session")
        restarted.tick(100)
        self.assertEqual(restarted_launcher.launched, [])

    def test_queue_change_and_unverified_live_mode_are_rejected(self):
        OvernightQueue(self.queue_path, self.checkpoint_path, FixtureLauncher())
        raw = json.loads(self.queue_path.read_text(encoding="utf-8"))
        raw["queue_id"] = "changed"
        self.queue_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "queue changed"):
            OvernightQueue(self.queue_path, self.checkpoint_path, FixtureLauncher())
        raw["launch_mode"] = "verified_local"
        self.queue_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "unverified"):
            load_queue(self.queue_path)

    def test_seed_is_valid_but_synthetic(self):
        seed = Path(__file__).resolve().parents[1] / "overnight.queue.example.json"
        queue_id, _, sessions = load_queue(seed)
        self.assertEqual(len(sessions), 8)
        self.assertIn("fixture", queue_id)
        per_track = {}
        for spec in sessions:
            per_track.setdefault(spec.expected["track_id"], set()).add(spec.expected["variant_id"])
        self.assertTrue(all(variants == {"fixture-stock", "fixture-c1-unbuilt"}
                            for variants in per_track.values()))

    def live_files(self):
        fixture_queue(self.queue_path, sessions=1)
        preset = self.root / "CM Presets" / "overnight-preset.cmpreset"
        preset.parent.mkdir()
        preset.write_text('{"fixture":true}', encoding="utf-8")
        car_file = self.root / "installed-car-data.bin"
        car_file.write_bytes(b"fixture-vehicle-physics")
        variant = self.root / "variant.json"
        variant.write_text(json.dumps({
            "schema_version": 1, "variant_id": "prototype-c1",
            "car_id": "fixture-car", "car_version": "fixture-v1",
            "files": [{"path": str(car_file.resolve()),
                       "sha256": hashlib.sha256(car_file.read_bytes()).hexdigest()}],
        }), encoding="utf-8")
        executable = self.root / "Content Manager.exe"
        executable.write_bytes(b"fixture-cm-binary")
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        raw = json.loads(self.queue_path.read_text(encoding="utf-8"))
        raw["launch_mode"] = "verified_local"
        raw["defaults"]["expected"].update(
            preset_id=preset.stem, preset_sha256=digest(preset),
            variant_id="prototype-c1", variant_sha256=digest(variant))
        self.queue_path.write_text(json.dumps(raw), encoding="utf-8")
        preflight = self.root / "preflight.json"
        record = {
            "schema_version": 1, "status": "verified_live",
            "verified_at_utc": "2026-10-25T12:00:00Z",
            "queue_sha256": digest(self.queue_path),
            "cm_executable_path": str(executable.resolve()),
            "cm_executable_sha256": digest(executable),
            "cm_file_version": "0.8.2895.40668",
            "uri_launch_smoke_tested": True,
            "recording_save_smoke_tested": True,
            "session_close_smoke_tested": True,
            "readbacks_smoke_tested": True,
        }
        preflight.write_text(json.dumps(record), encoding="utf-8")
        return executable, preset, variant, preflight, record

    def live_adapter(self, executable, preset, variant, preflight, issued):
        return ContentManagerUriLauncher(
            queue_path=self.queue_path, preflight_path=preflight,
            cm_executable=executable,
            preset_files={preset.stem: preset},
            variant_manifest_files={"prototype-c1": variant},
            observation_provider=lambda spec, now_s: None,
            session_finalizer=lambda spec, now_s: None,
            version_reader=lambda path: "0.8.2895.40668",
            process_spawner=lambda command, cwd: issued.append(command),
            clock_utc=lambda: datetime(2026, 10, 25, 12, 10, tzinfo=timezone.utc))

    def test_live_uri_adapter_is_dormant_until_matching_preflight(self):
        executable, preset, variant, preflight, record = self.live_files()
        issued = []
        record["readbacks_smoke_tested"] = False
        preflight.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "readbacks_smoke_tested"):
            self.live_adapter(executable, preset, variant, preflight, issued)
        self.assertEqual(issued, [])
        record["readbacks_smoke_tested"] = True
        preflight.write_text(json.dumps(record), encoding="utf-8")
        adapter = self.live_adapter(executable, preset, variant, preflight, issued)
        queue = OvernightQueue(self.queue_path, self.checkpoint_path, adapter)
        self.assertEqual(queue.tick(0)["phase"], "verifying")
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0][0], str(executable.resolve()))
        self.assertEqual(urlsplit(issued[0][1]).scheme, "acmanager")
        self.assertEqual(urlsplit(issued[0][1]).netloc, "race")
        self.assertEqual(urlsplit(issued[0][1]).path, "/quick")
        self.assertEqual(parse_qs(urlsplit(issued[0][1]).query)["presetFile"],
                         [str(preset.resolve())])
        self.assertEqual(queue.state["evidence"], "MEASURED")

    def test_live_uri_adapter_rechecks_preset_and_cm_version(self):
        executable, preset, variant, preflight, record = self.live_files()
        issued = []
        record["cm_file_version"] = "different"
        preflight.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(QueueError, "version mismatch"):
            self.live_adapter(executable, preset, variant, preflight, issued)
        record["cm_file_version"] = "0.8.2895.40668"
        preflight.write_text(json.dumps(record), encoding="utf-8")
        adapter = self.live_adapter(executable, preset, variant, preflight, issued)
        preset.write_text("changed after preflight", encoding="utf-8")
        state = OvernightQueue(self.queue_path, self.checkpoint_path, adapter).tick(0)
        self.assertEqual(state["failure_reason"], "launcher_error:QueueError")
        self.assertEqual(issued, [])

    def test_live_uri_adapter_checks_installed_variant_files(self):
        executable, preset, variant, preflight, _ = self.live_files()
        issued = []
        adapter = self.live_adapter(executable, preset, variant, preflight, issued)
        (self.root / "installed-car-data.bin").write_bytes(b"changed")
        state = OvernightQueue(self.queue_path, self.checkpoint_path, adapter).tick(0)
        self.assertEqual(state["failure_reason"], "launcher_error:QueueError")
        self.assertEqual(issued, [])


if __name__ == "__main__":
    unittest.main()
