"""Checkpointed overnight session queue with explicit comparison limits.

The queue never assumes that a Content Manager command launched the requested
session. Fresh car, track, mode, AI and recorder readbacks gate unattended
collection. Missing preset, tire, energy-profile or condition readbacks are
recorded as limits on matched comparisons, without discarding raw telemetry.
The live launcher still checks requested preset and variant files before each
dispatch; those file checks are not in-game effect readbacks.

An hourly GPT message can supervise this queue, but cannot act as its clock or
session controller.  The local process waits for a lap boundary after each
planned switch time and stops if a watchdog or hard session limit is reached.
"""
from __future__ import annotations

import argparse
import configparser
import ctypes
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode, quote


HASH = re.compile(r"^[0-9a-f]{64}$")
RUN_CRITICAL_KEYS = ("car_id", "car_version", "track_id", "layout_id",
                     "variant_id", "mode_id")
COMPARISON_KEYS = ("preset_id", "preset_sha256", "variant_sha256",
                   "tire_compound", "energy_profile_id")
IDENTITY_KEYS = (*COMPARISON_KEYS, *RUN_CRITICAL_KEYS)
INITIAL_KEYS = ("ambient_air_c", "track_surface_c", "fuel_start_l")
ONGOING_KEYS = ("ambient_air_c", "track_surface_c")


class QueueError(ValueError):
    """Invalid queue or unsafe queue state."""


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    expected: Mapping[str, Any]
    tolerances: Mapping[str, float]
    min_lap_crossings: int
    planned_switch_after_s: float
    max_session_s: float
    start_timeout_s: float
    finalize_timeout_s: float
    watchdog_s: float
    max_dropped_samples: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SessionSpec":
        expected = raw.get("expected")
        if not isinstance(expected, dict):
            raise QueueError("each session needs an exact expected manifest")
        for key in (*IDENTITY_KEYS, *INITIAL_KEYS):
            if expected.get(key) is None or expected.get(key) == "":
                raise QueueError(f"missing required expected readback: {key}")
        for key in IDENTITY_KEYS:
            if not isinstance(expected[key], str) or not expected[key].strip():
                raise QueueError(f"{key} must be a nonempty string")
        for key in ("preset_sha256", "variant_sha256"):
            if not isinstance(expected[key], str) or not HASH.fullmatch(expected[key]):
                raise QueueError(f"{key} must be a SHA-256 hex digest")
        for key in INITIAL_KEYS:
            value = expected[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise QueueError(f"{key} must be numeric")
        tolerances = raw.get("tolerances", {})
        if not isinstance(tolerances, dict):
            raise QueueError("tolerances must be an object")
        for key in INITIAL_KEYS:
            value = tolerances.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise QueueError(f"nonnegative tolerance required for {key}")
        sid = raw.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise QueueError("each session needs a nonempty session_id")
        laps = raw.get("min_lap_crossings")
        drops = raw.get("max_dropped_samples")
        if not isinstance(laps, int) or isinstance(laps, bool) or laps < 1:
            raise QueueError("min_lap_crossings must be positive")
        if not isinstance(drops, int) or isinstance(drops, bool) or drops < 0:
            raise QueueError("max_dropped_samples must be nonnegative")
        limits = {}
        for key in ("planned_switch_after_s", "max_session_s", "start_timeout_s",
                    "finalize_timeout_s", "watchdog_s"):
            value = raw.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise QueueError(f"{key} must be positive")
            limits[key] = float(value)
        if limits["planned_switch_after_s"] >= limits["max_session_s"]:
            raise QueueError("hard session limit must exceed planned switch time")
        return cls(sid, dict(expected), dict(tolerances), laps,
                   limits["planned_switch_after_s"], limits["max_session_s"],
                   limits["start_timeout_s"], limits["finalize_timeout_s"],
                   limits["watchdog_s"], drops)


@dataclass(frozen=True)
class Observation:
    """Readbacks produced by a launcher/recorder adapter, never a command ack."""

    observed_at_s: float
    physical_session_id: str | None
    values: Mapping[str, Any]
    ai_controlled: bool | None
    recorder_active: bool | None
    recorder_saved: bool | None
    session_closed: bool | None
    sample_seq: int | None
    lap_crossings: int | None
    last_lap_at_s: float | None
    dropped_samples: int | None


class SessionLauncher(Protocol):
    """Unverified launch/finalize transport boundary for target integration."""

    def launch(self, spec: SessionSpec, now_s: float) -> None: ...
    def observe(self, spec: SessionSpec, now_s: float) -> Observation | None: ...
    def finalize(self, spec: SessionSpec, now_s: float, observed: Observation) -> None: ...


def load_queue(path: str | Path, *, allow_verified_local: bool = False
               ) -> tuple[str, str, tuple[SessionSpec, ...]]:
    """Load an exact queue; live mode requires the guarded adapter and preflight."""
    raw_bytes = Path(path).read_bytes()
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise QueueError("unsupported overnight queue schema")
    launch_mode = raw.get("launch_mode")
    if launch_mode != "dry_run" and not (allow_verified_local and launch_mode == "verified_local"):
        raise QueueError("live Content Manager launcher is unverified without preflight")
    entries = raw.get("sessions")
    defaults = raw.get("defaults", {})
    if not isinstance(entries, list) or not entries or not isinstance(defaults, dict):
        raise QueueError("queue needs defaults and at least one session")
    sessions = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise QueueError("session must be an object")
        merged = {**defaults, **entry}
        merged["expected"] = {**defaults.get("expected", {}), **entry.get("expected", {})}
        merged["tolerances"] = {**defaults.get("tolerances", {}), **entry.get("tolerances", {})}
        sessions.append(SessionSpec.from_mapping(merged))
    ids = [item.session_id for item in sessions]
    if len(set(ids)) != len(ids):
        raise QueueError("session IDs must be unique")
    return str(raw.get("queue_id") or "unnamed"), source_hash, tuple(sessions)


def _same(requested: Any, observed: Any, tolerance: float | None) -> bool:
    if requested is None or observed is None:
        return False
    if tolerance is None:
        return requested == observed
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False
    return abs(float(requested) - float(observed)) <= tolerance


def _counter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _fresh(obs: Observation, now_s: float, watchdog_s: float) -> bool:
    return (math.isfinite(obs.observed_at_s)
            and now_s - watchdog_s <= obs.observed_at_s <= now_s)


def _write_checkpoint(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    # Windows scanners can briefly hold the destination open. Keep the old
    # checkpoint intact and retry the atomic replacement for a bounded period.
    for attempt in range(10):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _windows_file_version(path: Path) -> str:
    """Read PE fixed-file version; an unavailable version fails preflight."""
    if os.name != "nt":
        raise QueueError("Content Manager version probe requires Windows")

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            "signature", "struct_version", "file_version_ms", "file_version_ls",
            "product_version_ms", "product_version_ls", "flags_mask", "flags",
            "os", "file_type", "subtype", "date_ms", "date_ls")]

    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise QueueError("Content Manager executable has no readable file version")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise QueueError("Content Manager file version read failed")
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise QueueError("Content Manager fixed file version is unavailable")
    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    return ".".join(str(part) for part in (
        info.file_version_ms >> 16, info.file_version_ms & 0xffff,
        info.file_version_ls >> 16, info.file_version_ls & 0xffff))


class BridgeFinalizeRequestWriter:
    """Write one exact-session save/shutdown request; never claim it succeeded.

    The in-game app is responsible for ending the run, saving its final chunk
    and asking CSP to close AC. A separate observer must verify complete raw
    chunks and the simulator process exit before another CM URI is dispatched.
    """

    def __init__(self, bridge_root: str | Path, *,
                 clock_utc: Callable[[], datetime] | None = None,
                 nonce_factory: Callable[[], str] | None = None,
                 max_state_age_s: float = 5.0):
        self.bridge_root = Path(bridge_root)
        self.clock_utc = clock_utc or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        if not math.isfinite(max_state_age_s) or max_state_age_s <= 0:
            raise ValueError("max_state_age_s must be positive")
        self.max_state_age_s = max_state_age_s

    def __call__(self, spec: SessionSpec, now_s: float,
                 observed: Observation) -> dict[str, Any]:
        if (not _fresh(observed, now_s, min(spec.watchdog_s, self.max_state_age_s)) or
                observed.ai_controlled is not True or
                observed.recorder_active is not True or
                observed.recorder_saved is True or
                observed.session_closed is not False or
                not _counter(observed.lap_crossings) or
                not isinstance(observed.physical_session_id, str) or
                not observed.physical_session_id):
            raise QueueError("fresh active AI/recorder readback required for finalization")
        observed_run_id = observed.values.get("run_id")
        if not isinstance(observed_run_id, str) or not observed_run_id:
            raise QueueError("run ID absent from independent observation")
        try:
            state = json.loads((self.bridge_root / "current_state.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise QueueError("current bridge state unavailable") from exc
        if not isinstance(state, dict) or state.get("schema") != 1:
            raise QueueError("current bridge state schema invalid")
        if (state.get("run_id") != observed_run_id or
                state.get("physical_session_id") != observed.physical_session_id or
                state.get("session_mode_id") != spec.expected["mode_id"] or
                state.get("status") != "active" or state.get("write_ok") is not True or
                state.get("is_ai_controlled") is not True or
                not _counter(state.get("recorded_lap_crossings")) or
                state["recorded_lap_crossings"] != observed.lap_crossings):
            raise QueueError("bridge state differs from active observed session")
        prototype = state.get("prototype")
        if (not isinstance(prototype, dict) or
                prototype.get("variant_id") != spec.expected["variant_id"]):
            raise QueueError("bridge prototype variant differs")
        for observed_key, state_key in (("car_id", "car_id"),
                                        ("car_version", "car_version"),
                                        ("track_id", "track_id"),
                                        ("layout_id", "layout_id"),
                                        ("mode_id", "session_mode_id"),
                                        ("variant_id", None)):
            state_value = (prototype.get("variant_id") if state_key is None
                           else state.get(state_key))
            if (state_value != spec.expected[observed_key] or
                    observed.values.get(observed_key) != state_value):
                raise QueueError(f"{observed_key} differs at finalize request")
        now_utc = self.clock_utc()
        if now_utc.tzinfo is None:
            raise QueueError("UTC clock has no offset")
        try:
            updated = datetime.fromisoformat(str(state["updated_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise QueueError("bridge state update timestamp unavailable") from exc
        if updated.tzinfo is None:
            raise QueueError("bridge state update timestamp lacks offset")
        age_s = (now_utc - updated).total_seconds()
        if not math.isfinite(age_s) or not 0 <= age_s <= self.max_state_age_s:
            raise QueueError("bridge state is stale at finalize request")
        nonce = self.nonce_factory()
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise QueueError("finalize request nonce invalid")
        request = {
            "schema_version": 1,
            "command": "save_and_shutdown",
            "run_id": observed_run_id,
            "physical_session_id": observed.physical_session_id,
            "session_mode_id": spec.expected["mode_id"],
            "nonce": nonce,
            "min_recorded_lap_crossings": observed.lap_crossings,
        }
        path = self.bridge_root / "overnight_finalize_request.json"
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise QueueError("previous finalize request is unreadable") from exc
            if (not isinstance(previous, dict) or
                    previous.get("physical_session_id") == observed.physical_session_id):
                raise QueueError("finalize request already exists for this native session")
        _write_checkpoint(path, request)
        if json.loads(path.read_text(encoding="utf-8")) != request:
            raise QueueError("finalize request write could not be read back")
        return request


class ContentManagerUriLauncher:
    """Guarded two-stage CM launch with a native race.ini identity readback.

    The preset is loaded before the separate Go command. CM does not expose a
    reliable custom-mode scan completion readback, so a bounded discovery wait
    precedes Go, and a freshly written race.ini must identify the exact mode,
    car and layout before the bridge observation is passed to the queue.
    """

    def __init__(self, *, queue_path: str | Path, preflight_path: str | Path,
                 cm_executable: str | Path,
                 preset_files: Mapping[str, str | Path],
                 variant_manifest_files: Mapping[str, str | Path],
                 race_ini_path: str | Path,
                 observation_provider: Callable[[SessionSpec, float], Observation | None],
                 session_finalizer: Callable[[SessionSpec, float, Observation], Any],
                 version_reader: Callable[[Path], str] = _windows_file_version,
                 process_spawner: Callable[[list[str], Path], Any] | None = None,
                 clock_utc: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 mode_scan_wait_s: float = 3.0):
        self.queue_path = Path(queue_path).resolve(strict=True)
        self.preflight_path = Path(preflight_path).resolve(strict=True)
        self.cm_executable = Path(cm_executable).resolve(strict=True)
        self.preset_files = {key: Path(value).resolve(strict=True)
                             for key, value in preset_files.items()}
        self.variant_manifest_files = {
            key: Path(value).resolve(strict=True)
            for key, value in variant_manifest_files.items()}
        self.race_ini_path = Path(race_ini_path).resolve()
        self.observation_provider = observation_provider
        self.session_finalizer = session_finalizer
        self.version_reader = version_reader
        self.process_spawner = process_spawner or self._spawn
        self.clock_utc = clock_utc or (lambda: datetime.now(timezone.utc))
        if (not math.isfinite(mode_scan_wait_s) or
                not 2.1 <= mode_scan_wait_s <= 15.0):
            raise QueueError("CM custom-mode discovery wait must be 2.1–15 seconds")
        self.sleep = sleep
        self.mode_scan_wait_s = mode_scan_wait_s
        self._launched_session_id: str | None = None
        self._race_ini_previous_mtime_ns: int | None = None
        self._race_ini_candidate: tuple[int, int] | None = None
        self._race_ini_verified = False
        self._verify_preflight()

    @staticmethod
    def _spawn(command: list[str], cwd: Path) -> None:
        subprocess.Popen(command, cwd=str(cwd), shell=False)

    def _verify_preflight(self) -> None:
        record = json.loads(self.preflight_path.read_text(encoding="utf-8"))
        if record.get("schema_version") != 1 or record.get("status") != "verified_live":
            raise QueueError("live launch preflight not verified")
        for key in ("uri_launch_smoke_tested", "two_stage_load_go_smoke_tested",
                    "recording_save_smoke_tested",
                    "session_close_smoke_tested", "readbacks_smoke_tested"):
            if record.get(key) is not True:
                raise QueueError(f"live launch preflight missing {key}")
        try:
            tested_at = datetime.fromisoformat(str(record["verified_at_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise QueueError("live launch preflight timestamp invalid") from exc
        if tested_at.tzinfo is None:
            raise QueueError("live launch preflight timestamp needs UTC offset")
        age_s = (self.clock_utc() - tested_at).total_seconds()
        if not 0 <= age_s <= 24 * 3600:
            raise QueueError("live launch preflight must be within 24 hours")
        if record.get("queue_sha256") != _file_sha256(self.queue_path):
            raise QueueError("preflight queue hash mismatch")
        if Path(str(record.get("cm_executable_path", ""))).resolve() != self.cm_executable:
            raise QueueError("preflight Content Manager path mismatch")
        if record.get("cm_executable_sha256") != _file_sha256(self.cm_executable):
            raise QueueError("preflight Content Manager binary hash mismatch")
        if record.get("cm_file_version") != self.version_reader(self.cm_executable):
            raise QueueError("preflight Content Manager version mismatch")

    def launch(self, spec: SessionSpec, now_s: float) -> None:
        self._verify_preflight()
        preset = self.preset_files.get(str(spec.expected["preset_id"]))
        variant = self.variant_manifest_files.get(str(spec.expected["variant_id"]))
        if preset is None or variant is None:
            raise QueueError("preset or variant manifest path unavailable")
        if preset.suffix.lower() != ".cmpreset" or preset.stem != spec.expected["preset_id"]:
            raise QueueError("preset path does not match exact requested ID")
        if _file_sha256(preset) != spec.expected["preset_sha256"]:
            raise QueueError("preset hash changed since queue creation")
        try:
            preset_data = json.loads(preset.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise QueueError("Content Manager preset is unreadable") from exc
        expected_track = spec.expected["track_id"]
        if spec.expected["layout_id"] != "default":
            expected_track += "/" + spec.expected["layout_id"]
        if (not isinstance(preset_data, dict) or
                preset_data.get("Mode") !=
                "/Pages/Drive/QuickDrive_Custom.xaml?Id=" + spec.expected["mode_id"] or
                preset_data.get("CarId") != spec.expected["car_id"] or
                preset_data.get("TrackId") != expected_track):
            raise QueueError("preset mode/car/track identity mismatch")
        if _file_sha256(variant) != spec.expected["variant_sha256"]:
            raise QueueError("variant manifest hash changed since queue creation")
        manifest = json.loads(variant.read_text(encoding="utf-8"))
        if (manifest.get("schema_version") != 1 or
                manifest.get("variant_id") != spec.expected["variant_id"] or
                manifest.get("car_id") != spec.expected["car_id"] or
                manifest.get("car_version") != spec.expected["car_version"]):
            raise QueueError("variant manifest identity mismatch")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise QueueError("variant manifest needs installed file hashes")
        for item in files:
            if not isinstance(item, dict) or not HASH.fullmatch(str(item.get("sha256", ""))):
                raise QueueError("variant file hash entry invalid")
            path = Path(str(item.get("path", "")))
            if not path.is_absolute() or not path.is_file() or _file_sha256(path) != item["sha256"]:
                raise QueueError("installed variant file is missing or changed")
        # Bind the observer to a new physical run before dispatch. Otherwise a
        # still-fresh previous current_state could satisfy the next queue item.
        arm = getattr(self.observation_provider, "mark_launch_intent", None)
        if callable(arm):
            arm(self.clock_utc())
        self._launched_session_id = None
        self._race_ini_candidate = None
        self._race_ini_verified = False
        load_uri = "acmanager://race/quick?" + urlencode(
            {"presetFile": str(preset), "loadPreset": "1"}, quote_via=quote)
        self.process_spawner([str(self.cm_executable), load_uri], self.cm_executable.parent)
        # This pause is a bounded discovery allowance, not a mode readback.
        self.sleep(self.mode_scan_wait_s)
        try:
            self._race_ini_previous_mtime_ns = self.race_ini_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._race_ini_previous_mtime_ns = None
        go_uri = "acmanager://race/quick?" + urlencode(
            {"presetFile": str(preset), "go": "1"}, quote_via=quote)
        self.process_spawner([str(self.cm_executable), go_uri], self.cm_executable.parent)
        self._launched_session_id = spec.session_id

    def _verify_fresh_race_ini(self, spec: SessionSpec) -> bool:
        try:
            stat = self.race_ini_path.stat()
        except FileNotFoundError:
            return False
        if (self._race_ini_previous_mtime_ns is not None and
                stat.st_mtime_ns <= self._race_ini_previous_mtime_ns):
            return False
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._race_ini_candidate != signature:
            self._race_ini_candidate = signature
            return False
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            with self.race_ini_path.open("r", encoding="utf-8-sig") as handle:
                parser.read_file(handle)
            mode = parser.get("RACE", "__CM_CUSTOM_MODE", fallback=None)
            car = parser.get("RACE", "MODEL", fallback=None)
            track = parser.get("RACE", "TRACK", fallback=None)
            layout = parser.get("RACE", "CONFIG_TRACK", fallback=None)
        except (OSError, UnicodeError, configparser.Error):
            # A CM write can be observed midway. The queue start timeout bounds
            # retries; never accept an unreadable native launch record.
            self._race_ini_candidate = None
            return False
        try:
            checked = self.race_ini_path.stat()
        except FileNotFoundError:
            self._race_ini_candidate = None
            return False
        if (checked.st_mtime_ns, checked.st_size) != signature:
            self._race_ini_candidate = None
            return False
        if layout == "":
            layout = "default"
        expected = spec.expected
        if (mode != expected["mode_id"] or car != expected["car_id"] or
                track != expected["track_id"] or layout != expected["layout_id"]):
            raise QueueError("fresh race.ini custom mode/car/track/layout mismatch")
        return True

    def observe(self, spec: SessionSpec, now_s: float) -> Observation | None:
        if self._launched_session_id != spec.session_id:
            return None
        if not self._race_ini_verified:
            self._race_ini_verified = self._verify_fresh_race_ini(spec)
            if not self._race_ini_verified:
                return None
        return self.observation_provider(spec, now_s)

    def finalize(self, spec: SessionSpec, now_s: float,
                 observed: Observation) -> None:
        self.session_finalizer(spec, now_s, observed)


class OvernightQueue:
    """One durable transition per tick, with no automatic retry after a failure."""

    def __init__(self, queue_path: str | Path, checkpoint_path: str | Path,
                 launcher: SessionLauncher):
        live_launcher = isinstance(launcher, ContentManagerUriLauncher)
        queue_id, queue_hash, sessions = load_queue(
            queue_path, allow_verified_local=live_launcher)
        self.queue_id = queue_id
        self.queue_hash = queue_hash
        self.sessions = sessions
        self.launcher = launcher
        self.evidence = "MEASURED" if live_launcher else "SYNTHETIC"
        self.checkpoint_path = Path(checkpoint_path)
        if self.checkpoint_path.exists():
            self.state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if self.state.get("queue_sha256") != queue_hash:
                raise QueueError("queue changed since checkpoint; refuse implicit resume")
            if self.state.get("phase") in {"verifying", "recording", "finalizing"}:
                self._fail("runner_restarted_during_active_physical_session", None)
        else:
            self.state: dict[str, Any] = {
                "schema_version": 1, "queue_id": queue_id, "queue_sha256": queue_hash,
                "evidence": self.evidence, "phase": "pending", "index": 0,
                "completed": [], "events": [], "failure_reason": None,
                "last_physical_session_id": None,
            }
            self._save()

    def _save(self) -> None:
        _write_checkpoint(self.checkpoint_path, self.state)

    def _event(self, kind: str, now_s: float | None, **data: Any) -> None:
        self.state["events"].append({"type": kind, "at_s": now_s, **data})
        self._save()

    def _fail(self, reason: str, now_s: float | None) -> None:
        self.state["phase"] = "stopped"
        self.state["failure_reason"] = reason
        self._event("stopped", now_s, reason=reason,
                    session_id=self._current_id())

    def _current_id(self) -> str | None:
        index = self.state["index"]
        return self.sessions[index].session_id if index < len(self.sessions) else None

    def _identity_issue(self, spec: SessionSpec, obs: Observation,
                        *, initial: bool) -> str | None:
        for key in RUN_CRITICAL_KEYS:
            observed = obs.values.get(key)
            if observed is None:
                return f"missing_readback:{key}"
            if not _same(spec.expected[key], observed, None):
                return f"readback_mismatch:{key}"
        return None

    @staticmethod
    def _comparison_limits(spec: SessionSpec, obs: Observation,
                           *, initial: bool) -> list[str]:
        conditions = INITIAL_KEYS if initial else ONGOING_KEYS
        limits = []
        for key in (*COMPARISON_KEYS, *conditions):
            observed = obs.values.get(key)
            if observed is None:
                limits.append(f"missing_readback:{key}")
            elif not _same(spec.expected[key], observed, spec.tolerances.get(key)):
                limits.append(f"readback_mismatch:{key}")
        return limits

    def _record_comparison_limits(self, spec: SessionSpec, obs: Observation,
                                  *, initial: bool) -> None:
        limits = set(self.state.get("comparison_limits", []))
        limits.update(self._comparison_limits(spec, obs, initial=initial))
        self.state["comparison_limits"] = sorted(limits)

    def tick(self, now_s: float) -> dict[str, Any]:
        """Advance locally; missing/contradictory live evidence stops dispatch."""
        if self.state["phase"] in {"stopped", "complete"}:
            return dict(self.state)
        spec = self.sessions[self.state["index"]]
        phase = self.state["phase"]
        if phase == "pending":
            # Persist before the external side effect: a crash cannot relaunch twice.
            self.state.update(phase="verifying", launched_at_s=now_s,
                              last_observed_at_s=None, comparison_limits=[],
                              run_id=None, physical_session_id=None)
            self._event("launch_intent", now_s, session_id=spec.session_id,
                        expected=dict(spec.expected))
            try:
                self.launcher.launch(spec, now_s)
            except Exception as exc:
                self._fail(f"launcher_error:{type(exc).__name__}", now_s)
            return dict(self.state)

        try:
            obs = self.launcher.observe(spec, now_s)
        except Exception as exc:
            self._fail(f"observe_error:{type(exc).__name__}", now_s)
            return dict(self.state)

        if phase == "verifying":
            if now_s - self.state["launched_at_s"] > spec.start_timeout_s:
                self._fail("start_readback_timeout", now_s)
                return dict(self.state)
            if obs is None:
                return dict(self.state)
            if not _fresh(obs, now_s, spec.watchdog_s):
                return dict(self.state)
            issue = self._identity_issue(spec, obs, initial=True)
            if issue is not None:
                if issue.startswith("readback_mismatch:"):
                    self._fail(issue, now_s)
                return dict(self.state)
            if (not obs.physical_session_id or
                    obs.physical_session_id == self.state["last_physical_session_id"]):
                self._fail("missing_or_reused_physical_session_id", now_s)
                return dict(self.state)
            if (obs.ai_controlled is not True or obs.recorder_active is not True
                    or obs.session_closed is not False):
                self._fail("ai_or_recorder_not_verified_on", now_s)
                return dict(self.state)
            if not all(_counter(value) for value in (
                    obs.sample_seq, obs.lap_crossings, obs.dropped_samples)):
                return dict(self.state)
            run_id = obs.values.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return dict(self.state)
            if obs.dropped_samples > spec.max_dropped_samples:
                self._fail("sample_drop_limit_exceeded", now_s)
                return dict(self.state)
            self._record_comparison_limits(spec, obs, initial=True)
            self.state.update(phase="recording", run_id=run_id,
                              physical_session_id=obs.physical_session_id,
                              recording_started_at_s=now_s, start_laps=obs.lap_crossings,
                              last_laps=obs.lap_crossings, last_seq=obs.sample_seq,
                              last_progress_at_s=now_s, dropped_samples=obs.dropped_samples)
            self._event("readbacks_verified", now_s, session_id=spec.session_id,
                        physical_session_id=obs.physical_session_id,
                        comparison_limits=list(self.state["comparison_limits"]))
            return dict(self.state)

        if phase == "recording":
            if now_s - self.state["recording_started_at_s"] > spec.max_session_s:
                self._fail("hard_session_limit_exceeded", now_s)
                return dict(self.state)
            if obs is None or not _fresh(obs, now_s, spec.watchdog_s):
                if now_s - self.state["last_progress_at_s"] > spec.watchdog_s:
                    self._fail("observation_watchdog_expired", now_s)
                return dict(self.state)
            if (obs.physical_session_id != self.state["physical_session_id"] or
                    obs.values.get("run_id") != self.state["run_id"]):
                self._fail("physical_session_changed", now_s)
                return dict(self.state)
            issue = self._identity_issue(spec, obs, initial=False)
            if issue is not None:
                self._fail(issue, now_s)
                return dict(self.state)
            if obs.ai_controlled is not True or obs.recorder_active is not True:
                self._fail("ai_or_recorder_lost", now_s)
                return dict(self.state)
            if not all(_counter(value) for value in (
                    obs.sample_seq, obs.lap_crossings, obs.dropped_samples)):
                self._fail("missing_recorder_progress_readback", now_s)
                return dict(self.state)
            if obs.sample_seq < self.state["last_seq"] or obs.lap_crossings < self.state["last_laps"]:
                self._fail("recorder_or_lap_counter_reset", now_s)
                return dict(self.state)
            if obs.dropped_samples > spec.max_dropped_samples:
                self._fail("sample_drop_limit_exceeded", now_s)
                return dict(self.state)
            self._record_comparison_limits(spec, obs, initial=False)
            if obs.sample_seq > self.state["last_seq"]:
                self.state["last_seq"] = obs.sample_seq
                self.state["last_progress_at_s"] = now_s
            elif now_s - self.state["last_progress_at_s"] > spec.watchdog_s:
                self._fail("sample_progress_watchdog_expired", now_s)
                return dict(self.state)
            self.state["last_laps"] = obs.lap_crossings
            self.state["dropped_samples"] = obs.dropped_samples
            self._save()
            elapsed = now_s - self.state["recording_started_at_s"]
            laps = obs.lap_crossings - self.state["start_laps"]
            # Target time arms a switch; only a subsequently completed lap can trigger it.
            if (elapsed >= spec.planned_switch_after_s and laps >= spec.min_lap_crossings
                    and obs.last_lap_at_s is not None
                    and obs.last_lap_at_s <= now_s
                    and obs.last_lap_at_s >= self.state["recording_started_at_s"]
                    + spec.planned_switch_after_s):
                self.state.update(phase="finalizing", finalize_requested_at_s=now_s)
                self._event("finalize_intent", now_s, session_id=spec.session_id,
                            completed_lap_crossings=laps)
                try:
                    self.launcher.finalize(spec, now_s, obs)
                except Exception as exc:
                    self._fail(f"finalize_error:{type(exc).__name__}", now_s)
            return dict(self.state)

        if phase == "finalizing":
            if now_s - self.state["finalize_requested_at_s"] > spec.finalize_timeout_s:
                self._fail("finalize_readback_timeout", now_s)
                return dict(self.state)
            if obs is None or not _fresh(obs, now_s, spec.watchdog_s):
                return dict(self.state)
            if (obs.physical_session_id != self.state["physical_session_id"] or
                    obs.values.get("run_id") != self.state["run_id"]):
                self._fail("finalize_session_identity_mismatch", now_s)
                return dict(self.state)
            if (obs.recorder_saved is True and obs.session_closed is True
                    and obs.recorder_active is False):
                self.state["completed"].append({
                    "session_id": spec.session_id,
                    "run_id": obs.values.get("run_id"),
                    "physical_session_id": obs.physical_session_id,
                    "lap_crossings": self.state["last_laps"] - self.state["start_laps"],
                    "dropped_samples": self.state["dropped_samples"],
                    "evidence": self.evidence,
                    "comparison_metadata_verified": not self.state["comparison_limits"],
                    "comparison_limits": list(self.state["comparison_limits"]),
                })
                self.state["last_physical_session_id"] = obs.physical_session_id
                self.state["index"] += 1
                self.state["phase"] = "complete" if self.state["index"] == len(self.sessions) else "pending"
                self._event("session_saved_and_closed", now_s, session_id=spec.session_id)
            return dict(self.state)
        raise QueueError(f"unexpected queue phase: {phase}")


class FixtureLauncher:
    """Deterministic dry-run only; it never touches Assetto Corsa or Content Manager."""

    def __init__(self, lap_s: float = 90.0):
        self.lap_s = lap_s
        self.launched: list[str] = []
        self.finalized: list[str] = []
        self.started: dict[str, float] = {}

    def launch(self, spec: SessionSpec, now_s: float) -> None:
        if spec.session_id in self.started:
            raise QueueError("fixture launch repeated")
        self.launched.append(spec.session_id)
        self.started[spec.session_id] = now_s

    def observe(self, spec: SessionSpec, now_s: float) -> Observation | None:
        start = self.started.get(spec.session_id)
        if start is None:
            return None
        elapsed = max(0.0, now_s - start)
        laps = int(elapsed // self.lap_s)
        done = spec.session_id in self.finalized
        return Observation(now_s, "fixture-" + spec.session_id,
                           {**spec.expected, "run_id": "fixture-run-" + spec.session_id},
                           True, not done, done, done, int(elapsed * 20), laps,
                           start + laps * self.lap_s if laps else None, 0)

    def finalize(self, spec: SessionSpec, now_s: float,
                 observed: Observation) -> None:
        self.finalized.append(spec.session_id)


def dry_run(queue_path: str | Path, checkpoint_path: str | Path,
            *, tick_s: float = 5.0, lap_s: float = 90.0) -> dict[str, Any]:
    """Rehearse a new queue with virtual time and synthetic readbacks."""
    launcher = FixtureLauncher(lap_s)
    queue = OvernightQueue(queue_path, checkpoint_path, launcher)
    if queue.state["phase"] not in {"pending", "complete"}:
        return dict(queue.state)
    now = 0.0
    for _ in range(1_000_000):
        state = queue.tick(now)
        if state["phase"] in {"complete", "stopped"}:
            return state
        now += tick_s
    raise QueueError("dry-run fixture exceeded bounded tick count")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rehearse an overnight queue; no game launch")
    parser.add_argument("queue", type=Path)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args(argv)
    state = dry_run(args.queue, args.checkpoint)
    print(json.dumps({"phase": state["phase"], "completed": len(state["completed"]),
                      "failure_reason": state["failure_reason"], "evidence": "SYNTHETIC"},
                     sort_keys=True))
    return 0 if state["phase"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
