"""Explicit detached start and read-only health check for a local live queue.

The hourly Codex monitor may call status, but the local runner owns all timing.
Preparation, import, and status never start Content Manager or Assetto Corsa.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from .bridge_observation import assetto_corsa_process_running
from .overnight import QueueError, _write_checkpoint
from .overnight_live import (build_live_runner, checkpoint_from_config,
                             runner_lock)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _background_path(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name + ".background.json")


def _worker_command(config: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        entry = [sys.executable]
    else:
        entry = [sys.executable, str(Path(__file__).resolve().parents[1] / "launch_gui.py")]
    return [*entry, "--overnight-runner", "--config", str(config),
            "--activate-local-live"]


def _spawn_worker(command: list[str], cwd: Path) -> subprocess.Popen:
    options: dict[str, Any] = {
        "cwd": str(cwd), "shell": False, "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": {**os.environ, "WIMNA_FORCE_COMMAND_LOG": "1"},
    }
    if os.name == "nt":
        options["creationflags"] = (subprocess.DETACHED_PROCESS |
                                    subprocess.CREATE_NEW_PROCESS_GROUP)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    return subprocess.Popen(command, **options)


def _pid_alive(pid: int) -> bool | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_uint)]
            kernel.GetExitCodeProcess.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_uint()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def start_background(config_path: str | Path, *,
                     runner_builder: Callable[[Path], Any] = build_live_runner,
                     process_running: Callable[[], bool | None] = assetto_corsa_process_running,
                     spawner: Callable[[list[str], Path], Any] = _spawn_worker) -> dict[str, Any]:
    """Validate a prepared eight-session queue, then spawn exactly one worker."""
    config = Path(config_path).resolve(strict=True)
    checkpoint = checkpoint_from_config(config)
    background = _background_path(checkpoint)
    with runner_lock(checkpoint):
        if background.exists():
            raise QueueError("background start already attempted for this checkpoint; review status")
        if checkpoint.exists():
            existing = json.loads(checkpoint.read_text(encoding="utf-8"))
            if existing.get("phase") != "pending":
                raise QueueError("only a pending checkpoint can start in background")
        if process_running() is not False:
            raise QueueError("simulator must be verified stopped before background start")
        # This rechecks fresh preflight, exact queue, CM version and installed
        # file paths. It can create a pending checkpoint but never launches AC.
        runner = runner_builder(config)
        if runner.queue.state.get("phase") != "pending":
            raise QueueError("live queue is not pending")
        if (runner.queue.queue_id != "four-track-plan-c-paired-overnight" or
                len(runner.queue.sessions) != 8):
            raise QueueError("background start requires the prepared eight-session campaign")
        record = {
            "schema_version": 1,
            "status": "spawn_intent",
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "config_path": str(config),
            "config_sha256": _sha256(config),
            "queue_sha256": runner.queue.queue_hash,
            "checkpoint_path": str(checkpoint),
            "worker_pid": None,
            "session_count": len(runner.queue.sessions),
        }
        _write_checkpoint(background, record)
        try:
            worker = spawner(_worker_command(config), config.parent)
            if not isinstance(worker.pid, int) or worker.pid <= 0:
                raise QueueError("background worker returned no process ID")
        except Exception as exc:
            record["status"] = "spawn_failed"
            record["failure"] = type(exc).__name__
            _write_checkpoint(background, record)
            raise QueueError("background worker did not start") from exc
        record["status"] = "spawned_unverified"
        record["worker_pid"] = worker.pid
        _write_checkpoint(background, record)
        return record


def status(config_path: str | Path, *,
           pid_alive: Callable[[int], bool | None] = _pid_alive,
           now_utc: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Read local process and checkpoint state; never launch or repair."""
    config = Path(config_path).resolve(strict=True)
    config_record = json.loads(config.read_text(encoding="utf-8"))
    queue_value = config_record.get("queue_path") if isinstance(config_record, dict) else None
    if not isinstance(queue_value, str) or not Path(queue_value).is_absolute():
        raise QueueError("status needs an absolute queue_path in runner config")
    queue_path = Path(queue_value).resolve(strict=True)
    queue_hash = _sha256(queue_path)
    queue_record = json.loads(queue_path.read_text(encoding="utf-8"))
    checkpoint = checkpoint_from_config(config)
    background = _background_path(checkpoint)
    launch = json.loads(background.read_text(encoding="utf-8")) if background.exists() else None
    state = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else None
    phase = state.get("phase") if isinstance(state, dict) else None
    pid = launch.get("worker_pid") if isinstance(launch, dict) else None
    alive = pid_alive(pid) if isinstance(pid, int) else False
    clock = now_utc or (lambda: datetime.now(timezone.utc))
    age_s = None
    if checkpoint.exists():
        age_s = max(0.0, clock().timestamp() - checkpoint.stat().st_mtime)
    current = None
    if isinstance(state, dict) and isinstance(queue_record, dict):
        sessions = queue_record.get("sessions")
        index = state.get("index")
        if (isinstance(sessions, list) and isinstance(index, int) and
                not isinstance(index, bool) and 0 <= index < len(sessions) and
                isinstance(sessions[index], dict)):
            current = sessions[index]
    expected = current.get("expected") if current else None
    if not isinstance(expected, dict):
        expected = {}
    reason = None
    if isinstance(state, dict) and state.get("queue_sha256") != queue_hash:
        reason = "checkpoint_queue_mismatch"
    elif isinstance(launch, dict) and launch.get("config_sha256") != _sha256(config):
        reason = "config_changed_since_start"
    elif isinstance(launch, dict) and isinstance(state, dict) and \
            launch.get("queue_sha256") != state.get("queue_sha256"):
        reason = "queue_identity_changed"
    elif isinstance(launch, dict) and launch.get("status") == "spawn_failed":
        reason = "worker_spawn_failed"
    elif phase == "stopped":
        reason = state.get("failure_reason") or "queue_stopped"
    elif phase in {"verifying", "recording", "finalizing"}:
        if alive is not True:
            reason = "active_queue_worker_missing_or_unknown"
        elif age_s is None or age_s > 180:
            reason = "active_queue_checkpoint_stale"
    elif phase == "pending" and isinstance(launch, dict):
        if alive is not True:
            reason = "worker_missing_before_launch"
        elif age_s is not None and age_s > 180:
            reason = "pending_queue_not_progressing"
    elif phase not in {"pending", "complete"}:
        reason = "checkpoint_missing_or_invalid"
    return {
        "phase": phase, "completed": len(state.get("completed", [])) if isinstance(state, dict) else 0,
        "current_session_id": current.get("session_id") if current else None,
        "current_track_id": expected.get("track_id"),
        "current_variant_id": expected.get("variant_id"),
        "current_run_id": state.get("run_id") if isinstance(state, dict) else None,
        "lap_crossings": (state.get("last_laps") - state.get("start_laps")
                          if isinstance(state, dict) and
                          isinstance(state.get("last_laps"), int) and
                          isinstance(state.get("start_laps"), int) else None),
        "dropped_samples": state.get("dropped_samples") if isinstance(state, dict) else None,
        "worker_pid": pid, "worker_alive": alive,
        "checkpoint_age_s": round(age_s, 1) if age_s is not None else None,
        "attention_required": reason is not None, "reason": reason,
        "checkpoint": str(checkpoint), "background_record": str(background),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--activate-local-live", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "start" and not args.activate_local_live:
        parser.error("--activate-local-live is required to start the local worker")
    try:
        result = (start_background(args.config) if args.command == "start"
                  else status(args.config))
    except (OSError, ValueError, QueueError) as exc:
        print(json.dumps({"phase": "not_started", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if args.command == "status" and result["attention_required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
