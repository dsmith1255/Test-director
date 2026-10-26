"""Explicit local overnight runner; importing this module never launches AC.

The queue owns the schedule and checkpoint. This wrapper supplies actual bridge
readbacks, a two-stage Content Manager launcher, and bounded stop handling.
It stops dispatch after any mismatch; an active failed session is asked to
save and shut down only when its exact AI/recorder identity is still verified.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .bridge_observation import (BridgeObservationProvider,
                                 MODE_VARIANTS, assetto_corsa_process_running)
from .overnight import (BridgeFinalizeRequestWriter, ContentManagerUriLauncher,
                        Observation, OvernightQueue, QueueError, SessionSpec,
                        load_queue)
from .replay_capture import ReplayArchiver


class LiveOvernightRunner:
    """Poll a verified local queue until complete, failure, or a hard deadline."""

    def __init__(self, queue: OvernightQueue, *,
                 process_running: Callable[[], bool | None],
                 clock_s: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_s: float = 1.0,
                 max_total_s: float | None = None):
        if queue.evidence != "MEASURED":
            raise QueueError("live runner requires a verified-local queue")
        if not math.isfinite(poll_s) or not 0.2 <= poll_s <= 5.0:
            raise QueueError("poll interval must be 0.2–5 seconds")
        if max_total_s is None:
            max_total_s = sum(
                spec.start_timeout_s + spec.max_session_s +
                spec.finalize_timeout_s + 20 for spec in queue.sessions) + 60
        if not math.isfinite(max_total_s) or max_total_s <= 0:
            raise QueueError("positive total runner deadline required")
        self.queue = queue
        self.process_running = process_running
        self.clock_s = clock_s
        self.sleep = sleep
        self.poll_s = poll_s
        self.max_total_s = max_total_s

    def _record_handling(self, status: str, message: str) -> dict[str, Any]:
        self.queue.state["failure_handling"] = {
            "status": status,
            "message": message,
            "original_failure_reason": self.queue.state.get("failure_reason"),
        }
        self.queue._save()
        return dict(self.queue.state)

    def _checked_active(self, spec: SessionSpec, now_s: float) -> Observation | None:
        try:
            observed = self.queue.launcher.observe(spec, now_s)
        except Exception:
            return None
        if observed is None or observed.ai_controlled is not True or observed.recorder_active is not True:
            return None
        if observed.session_closed is not False or not observed.physical_session_id:
            return None
        if any(observed.values.get(key) != spec.expected[key] for key in
               ("car_id", "car_version", "track_id", "layout_id", "mode_id", "variant_id")):
            return None
        native = self.queue.state.get("physical_session_id")
        if native is not None and observed.physical_session_id != native:
            return None
        if (not isinstance(observed.lap_crossings, int) or
                isinstance(observed.lap_crossings, bool) or observed.lap_crossings < 1):
            return None
        return observed

    def _handle_stop(self, now_s: float) -> dict[str, Any]:
        process = self.process_running()
        if process is False:
            return self._record_handling(
                "process_exited_without_verified_save",
                "The simulator is closed; this session did not pass the save check. Review its raw files.")
        if process is None:
            return self._record_handling(
                "manual_action_required",
                "Simulator process state is unavailable. Check the game and end the session manually.")
        index = self.queue.state.get("index")
        if not isinstance(index, int) or not 0 <= index < len(self.queue.sessions):
            return self._record_handling(
                "manual_action_required", "Check and end the active simulator session manually.")
        spec = self.queue.sessions[index]
        observed = self._checked_active(spec, now_s)
        if observed is None:
            return self._record_handling(
                "manual_action_required",
                "The exact AI/recorder session is not verified. Stop the car and save or close the game manually.")
        try:
            self.queue.launcher.finalize(spec, now_s, observed)
        except Exception as exc:
            return self._record_handling(
                "manual_action_required",
                f"Guarded save/shutdown was unavailable ({type(exc).__name__}). Stop and review the game manually.")
        deadline = self.clock_s() + spec.finalize_timeout_s
        max_polls = math.ceil(spec.finalize_timeout_s / self.poll_s) + 2
        for _ in range(max_polls):
            self.sleep(self.poll_s)
            now_s += self.poll_s
            try:
                closed = self.queue.launcher.observe(spec, now_s)
            except Exception:
                closed = None
            if (closed is not None and
                    closed.physical_session_id == observed.physical_session_id and
                    closed.recorder_saved is True and
                    closed.recorder_active is False and
                    closed.session_closed is True and
                    self.process_running() is False):
                return self._record_handling(
                    "guarded_save_shutdown_verified",
                    "The failed session saved and the simulator exited; no later session was launched.")
            if self.clock_s() >= deadline:
                break
        return self._record_handling(
            "manual_action_required",
            "A guarded save/shutdown was requested but not verified. Check the game manually.")

    def run(self) -> dict[str, Any]:
        """Run only after an explicit CLI command and a fresh local preflight."""
        if self.queue.state["phase"] == "complete":
            return dict(self.queue.state)
        if self.queue.state["phase"] == "stopped":
            return self._handle_stop(0.0)
        if self.queue.state["phase"] != "pending":
            raise QueueError("active or stopped checkpoint cannot be resumed implicitly")
        if self.process_running() is not False:
            raise QueueError("simulator process must be verified stopped before first launch")
        start = self.clock_s()
        max_ticks = math.ceil(self.max_total_s / self.poll_s) + len(self.queue.sessions) * 10
        for _ in range(max_ticks):
            now_s = self.clock_s() - start
            if now_s > self.max_total_s:
                self.queue._fail("runner_total_deadline_exceeded", now_s)
                return self._handle_stop(now_s)
            if (self.queue.state["phase"] == "recording" and
                    self.process_running() is not True):
                self.queue._fail("simulator_process_lost_during_recording", now_s)
                return self._handle_stop(now_s)
            state = self.queue.tick(now_s)
            if state["phase"] == "complete":
                return state
            if state["phase"] == "stopped":
                return self._handle_stop(now_s)
            self.sleep(self.poll_s)
        now_s = self.clock_s() - start
        self.queue._fail("runner_poll_bound_exceeded", now_s)
        return self._handle_stop(now_s)


def _required_path(record: Mapping[str, Any], key: str) -> Path:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise QueueError(f"live runner config missing {key}")
    path = Path(value)
    if not path.is_absolute():
        raise QueueError(f"live runner {key} must be an absolute path")
    return path


def _path_map(record: Mapping[str, Any], key: str) -> dict[str, Path]:
    value = record.get(key)
    if not isinstance(value, dict) or not value:
        raise QueueError(f"live runner config needs {key} mapping")
    result = {}
    for item_id, path_text in value.items():
        if (not isinstance(item_id, str) or not item_id or
                not isinstance(path_text, str) or not Path(path_text).is_absolute()):
            raise QueueError(f"live runner {key} entries need IDs and absolute paths")
        result[item_id] = Path(path_text)
    return result


def checkpoint_from_config(config_path: str | Path) -> Path:
    """Locate the lock/checkpoint without constructing or mutating a queue."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise QueueError("unsupported live runner config schema")
    bridge_root = _required_path(config, "bridge_root")
    value = config.get("checkpoint_path")
    if value is None:
        return bridge_root.parent / "overnight.checkpoint.json"
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise QueueError("checkpoint_path must be an absolute path")
    return Path(value)


@contextmanager
def runner_lock(checkpoint: Path, *, timeout_s: float = 0.0):
    """Hold an OS file lock before queue construction; a second worker cannot stop it."""
    if not math.isfinite(timeout_s) or timeout_s < 0 or timeout_s > 10:
        raise QueueError("runner lock wait must be 0–10 seconds")
    lock_path = checkpoint.with_name(checkpoint.name + ".runner.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise QueueError("another overnight runner owns this checkpoint") from exc
                time.sleep(0.1)
        try:
            yield lock_path
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def prevent_idle_sleep():
    """Keep Windows awake only while the local runner is active."""
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel = ctypes.windll.kernel32
    kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    kernel.SetThreadExecutionState.restype = ctypes.c_uint
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    if kernel.SetThreadExecutionState(es_continuous | es_system_required) == 0:
        raise QueueError("Windows idle-sleep prevention unavailable")
    try:
        yield
    finally:
        kernel.SetThreadExecutionState(es_continuous)


def build_live_runner(config_path: str | Path) -> LiveOvernightRunner:
    """Load an explicit local config; all requested files are checked again on launch."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise QueueError("unsupported live runner config schema")
    queue_path = _required_path(config, "queue_path")
    queue_record = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(queue_record, dict) or queue_record.get("launch_mode") != "verified_local":
        raise QueueError("live runner queue must declare verified_local launch mode")
    preflight_path = _required_path(config, "preflight_path")
    cm_executable = _required_path(config, "cm_executable")
    race_ini_path = _required_path(config, "race_ini_path")
    bridge_root = _required_path(config, "bridge_root")
    checkpoint = (Path(config["checkpoint_path"]) if
                  isinstance(config.get("checkpoint_path"), str) else
                  bridge_root.parent / "overnight.checkpoint.json")
    if not checkpoint.is_absolute():
        raise QueueError("checkpoint_path must be absolute")
    presets = _path_map(config, "preset_files")
    variants = _path_map(config, "variant_manifest_files")
    _, _, sessions = load_queue(queue_path, allow_verified_local=True)
    if any(MODE_VARIANTS.get(spec.expected["mode_id"]) != spec.expected["variant_id"]
           for spec in sessions):
        raise QueueError("live queue is limited to the paired WIMNA AI modes")
    if any(spec.expected["preset_id"] not in presets or
           spec.expected["variant_id"] not in variants for spec in sessions):
        raise QueueError("live runner config lacks a queued preset or variant")
    observer = BridgeObservationProvider(bridge_root)
    writer = BridgeFinalizeRequestWriter(bridge_root)
    launcher = ContentManagerUriLauncher(
        queue_path=queue_path, preflight_path=preflight_path,
        cm_executable=cm_executable, race_ini_path=race_ini_path,
        preset_files=presets, variant_manifest_files=variants,
        observation_provider=observer, session_finalizer=writer)
    replay_archiver = ReplayArchiver(
        replay_temp=race_ini_path.parent.parent / "replay" / "temp",
        output=checkpoint.parent / "replays",
        replay_ini=race_ini_path.parent / "replay.ini")
    queue = OvernightQueue(queue_path, checkpoint, launcher,
                           replay_archiver=replay_archiver)
    return LiveOvernightRunner(queue, process_running=assetto_corsa_process_running)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--activate-local-live", action="store_true",
                        help="explicitly start the guarded local CM queue")
    args = parser.parse_args(argv)
    if not args.activate_local_live:
        parser.error("--activate-local-live is required; importing or configuring does not launch")
    try:
        # Queue construction mutates an interrupted active checkpoint to
        # stopped. The lock must therefore be acquired before construction.
        with runner_lock(checkpoint_from_config(args.config), timeout_s=5.0):
            runner = build_live_runner(args.config)
            with prevent_idle_sleep():
                try:
                    state = runner.run()
                except (Exception, KeyboardInterrupt) as exc:
                    if runner.queue.state.get("phase") not in {"stopped", "complete"}:
                        runner.queue._fail(f"runner_unhandled:{type(exc).__name__}", None)
                    state = runner._handle_stop(0.0)
    except (OSError, ValueError, QueueError) as exc:
        print(json.dumps({"phase": "not_started", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"phase": state["phase"], "completed": len(state["completed"]),
                      "failure_reason": state.get("failure_reason"),
                      "failure_handling": state.get("failure_handling"),
                      "checkpoint": str(runner.queue.checkpoint_path)}, sort_keys=True))
    return 0 if state["phase"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
