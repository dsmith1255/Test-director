"""Read-only, fail-closed observations from the CSP recorder bridge.

This adapter reads native/app readbacks. It cannot verify Content Manager's
applied preset, an RSS energy profile, or a variant manifest file hash; those
fields remain absent and limit matched comparisons.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .overnight import Observation, SessionSpec
from .store import _compact_chunk_records


PROTOTYPE_HASHES = {
    "baseline": "1b0fd209d1c46087bdbf3f8ff0ac6fe30472ae7938b29d662371772e0496bb12",
    "plan_c": "4cafb3a4cf76afe426126b660996fd29777a58f031322c4ccc9a2ef974697cd9",
}
MODE_VARIANTS = {"wimna-ai-baseline": "baseline", "wimna-ai-plan-c": "plan_c"}


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _observed_channel(sample: Mapping[str, Any] | None, key: str) -> Any:
    if not isinstance(sample, Mapping):
        return None
    entry = sample.get(key)
    if not isinstance(entry, Mapping) or entry.get("quality") != "available":
        return None
    return entry.get("value")


def assetto_corsa_process_running() -> bool | None:
    """Read the local Windows process table; unknown is not an exit readback."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
            text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    names = set()
    for line in result.stdout.splitlines():
        if not line.startswith('"'):
            continue
        names.add(line.split('"', 2)[1].lower())
    return bool(names.intersection({"acs.exe", "acs_x86.exe"}))


class BridgeObservationProvider:
    """Observe a single fresh run without launching or controlling the game.

    The run manifest and current state must identify the same run and session.
    All chunk files are checked for a complete footer and monotonic sequence.
    A cached scan avoids reparsing old chunks on each poll. Any missing or
    malformed evidence returns ``None`` or an Observation with absent fields.
    """

    def __init__(self, bridge_root: str | Path, *,
                 clock_utc: Callable[[], datetime] | None = None,
                 process_running: Callable[[], bool | None] = assetto_corsa_process_running,
                 max_state_age_s: float = 10.0):
        self.bridge_root = Path(bridge_root)
        self.clock_utc = clock_utc or (lambda: datetime.now(timezone.utc))
        self.process_running = process_running
        self.max_state_age_s = max_state_age_s
        self._armed_at_utc: datetime | None = None
        self._preexisting_run_id: str | None = None
        self._run_id: str | None = None
        self._chunk_signatures: list[tuple[str, int, int]] = []
        self._last_seq = 0
        self._last_sample_seq: int | None = None
        self._first_sample: Mapping[str, Any] | None = None
        self._last_sample: Mapping[str, Any] | None = None
        self._lap_count = 0
        self._last_lap_utc: datetime | None = None
        self._ui_end_seen = False
        self._ui_end_reason: str | None = None
        self._session_ids: set[str] = set()
        self._sequence_gaps = 0

    def mark_launch_intent(self, at_utc: datetime) -> None:
        """Bind observations to a new run before dispatching a launch command."""
        if at_utc.tzinfo is None:
            raise ValueError("launch intent timestamp must have a UTC offset")
        self._armed_at_utc = at_utc.astimezone(timezone.utc)
        self._preexisting_run_id = None
        try:
            previous = json.loads((self.bridge_root / "current_state.json").read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("run_id"), str):
                self._preexisting_run_id = previous["run_id"]
        except (OSError, UnicodeError, ValueError):
            pass
        self._reset("")

    def _reset(self, run_id: str) -> None:
        self._run_id = run_id
        self._chunk_signatures = []
        self._last_seq = 0
        self._last_sample_seq = None
        self._first_sample = None
        self._last_sample = None
        self._lap_count = 0
        self._last_lap_utc = None
        self._ui_end_seen = False
        self._ui_end_reason = None
        self._session_ids = set()
        self._sequence_gaps = 0

    def _scan(self, run_dir: Path, run_id: str) -> bool:
        def invalid() -> bool:
            self._reset(run_id)
            return False

        paths = sorted(run_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9].jsonl"))
        if not paths:
            return invalid()
        signatures = [(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                      for path in paths]
        if any(path.name != f"{index:06d}.jsonl"
               for index, path in enumerate(paths, 1)):
            return invalid()
        if signatures[:len(self._chunk_signatures)] != self._chunk_signatures:
            self._reset(run_id)
        for path, signature in zip(paths[len(self._chunk_signatures):],
                                   signatures[len(self._chunk_signatures):]):
            try:
                records = _compact_chunk_records(path.read_bytes())
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                return invalid()
            if not records:
                return invalid()
            for _, record in records:
                seq = record.get("seq")
                if (record.get("run_id") != run_id or not _nonnegative_int(seq)
                        or seq <= self._last_seq):
                    return invalid()
                self._sequence_gaps += seq - self._last_seq - 1
                self._last_seq = seq
                physical_id = record.get("physical_session_id")
                if not isinstance(physical_id, str) or not physical_id:
                    return invalid()
                self._session_ids.add(physical_id)
                if record.get("kind") == "sample":
                    data = record.get("data")
                    if not isinstance(data, dict):
                        return invalid()
                    self._first_sample = self._first_sample or data
                    self._last_sample = data
                    self._last_sample_seq = seq
                elif record.get("kind") == "event":
                    data = record.get("data")
                    if not isinstance(data, dict):
                        return invalid()
                    if data.get("type") == "lap_complete":
                        stamp = _utc(record.get("wall_time_utc"))
                        if stamp is None:
                            return invalid()
                        self._lap_count += 1
                        self._last_lap_utc = stamp
                    elif data.get("type") == "ui_end":
                        self._ui_end_seen = True
                        self._ui_end_reason = data.get("reason")
                elif record.get("kind") == "capability":
                    if not isinstance(record.get("data"), dict):
                        return invalid()
                else:
                    return invalid()
            self._chunk_signatures.append(signature)
        return True

    def __call__(self, spec: SessionSpec, now_s: float) -> Observation | None:
        if self._armed_at_utc is None:
            return None
        try:
            state = json.loads((self.bridge_root / "current_state.json").read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema") != 1:
                return None
            run_id = state.get("run_id")
            native_id = state.get("physical_session_id")
            if (not isinstance(run_id, str) or not run_id or
                    not isinstance(native_id, str) or not native_id):
                return None
            if run_id == self._preexisting_run_id:
                return None
            now_utc = self.clock_utc()
            if now_utc.tzinfo is None:
                return None
            updated = _utc(state.get("updated_utc"))
            if updated is None:
                return None
            age_s = (now_utc - updated).total_seconds()
            allowed_age = (max(self.max_state_age_s, spec.finalize_timeout_s)
                           if state.get("status") == "ended" else self.max_state_age_s)
            if not math.isfinite(age_s) or not 0 <= age_s <= allowed_age:
                return None
            run_dir = self.bridge_root / "runs" / run_id
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
                return None
            started = _utc(manifest.get("started_utc"))
            # Lua UTC timestamps have second resolution. The run ID check above
            # rejects the preexisting run even when both fall in the same second.
            if (started is None or
                    started < self._armed_at_utc.replace(microsecond=0)):
                return None
            for key in ("car_id", "car_version", "track_id", "layout_id", "session_mode_id"):
                if manifest.get(key) != state.get(key):
                    return None
            if (manifest.get("selection") != "collection" or
                    manifest.get("start_trigger") != "overnight_mode_auto" or
                    state.get("mode") != "collection" or
                    state.get("extended_physics_observed") is not True):
                return None
            if self._run_id != run_id:
                self._reset(run_id)
            if not self._scan(run_dir, run_id):
                return None
            if (native_id not in self._session_ids or
                    not _nonnegative_int(state.get("recorded_lap_crossings")) or
                    state["recorded_lap_crossings"] != self._lap_count or
                    not _nonnegative_int(state.get("dropped_samples")) or
                    self._sequence_gaps > state["dropped_samples"] or
                    not _nonnegative_int(state.get("samples_attempted")) or
                    state["samples_attempted"] < 1):
                return None
            prototype = state.get("prototype")
            variant = MODE_VARIANTS.get(state.get("session_mode_id"))
            if variant is None or not isinstance(prototype, dict):
                return None
            if (prototype.get("variant_id") != variant or
                    prototype.get("mode_id") != state["session_mode_id"] or
                    prototype.get("parameters_variant_id") != variant or
                    prototype.get("parameter_hash_verified") is not True or
                    not isinstance(prototype.get("parameters_json_requested"), str)):
                return None
            permission = prototype.get("physics_permission_reported")
            if (not _nonnegative_int(permission) or permission != 1 or
                    prototype.get("force_native_lap_invalid_expected") is not
                    (variant == "plan_c") or
                    prototype.get("effect_status") !=
                    ("none" if variant == "baseline" else
                     "worker_requested_effect_unverified")):
                return None
            parameter_text = prototype["parameters_json_requested"]
            digest = hashlib.sha256(parameter_text.encode("utf-8")).hexdigest()
            if (digest != PROTOTYPE_HASHES[variant] or
                    prototype.get("parameter_hash_reported") != digest or
                    prototype.get("parameter_hash_computed") != digest):
                return None
            manifest_prototype = manifest.get("prototype")
            if (not isinstance(manifest_prototype, dict) or
                    manifest_prototype.get("variant_id") != variant or
                    manifest_prototype.get("parameter_hash_reported") != digest):
                return None
            try:
                parameters = json.loads(parameter_text)
            except ValueError:
                return None
            if not isinstance(parameters, dict) or parameters.get("variant") != variant:
                return None
            values: dict[str, Any] = {
                "run_id": run_id,
                "prototype_parameter_sha256": digest,
                "car_id": state.get("car_id"),
                "car_version": state.get("car_version"),
                "track_id": state.get("track_id"),
                "layout_id": state.get("layout_id"),
                "mode_id": state.get("session_mode_id"),
                "variant_id": variant,
                "tire_compound": state.get("observed_tyre_compound"),
                "ambient_air_c": _observed_channel(self._last_sample, "ambient_air_c"),
                "track_surface_c": _observed_channel(self._last_sample, "track_surface_c"),
                "fuel_start_l": _observed_channel(self._first_sample, "fuel"),
            }
            # A process table readback is required to call a session closed.
            running = self.process_running()
            if running not in (True, False):
                running = None
            status = state.get("status")
            write_ok = state.get("write_ok") is True
            saved = (status == "ended" and write_ok and self._ui_end_seen and
                     self._ui_end_reason == "overnight_switch" and
                     self._finalize_ack_verified(run_id, native_id,
                                                 state["session_mode_id"]))
            active = status == "active" and write_ok and self._last_sample_seq is not None
            ai = state.get("is_ai_controlled")
            if not isinstance(ai, bool):
                ai = None
            last_sample_ai = _observed_channel(self._last_sample, "is_ai_controlled")
            if isinstance(last_sample_ai, bool) and ai is not None and last_sample_ai != ai:
                ai = None
            last_lap_at = None
            if self._last_lap_utc is not None:
                lap_age = (now_utc - self._last_lap_utc).total_seconds()
                if lap_age >= 0:
                    last_lap_at = now_s - lap_age
            closed = (not running) if running is not None and saved else (
                False if running is True and active else None)
            observation_at = now_s if saved and running is False else now_s - age_s
            return Observation(observation_at, native_id, values, ai,
                               active if active else (False if saved else None),
                               saved if saved else None, closed,
                               self._last_sample_seq, self._lap_count,
                               last_lap_at, state["dropped_samples"])
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _finalize_ack_verified(self, run_id: str, native_id: str, mode_id: str) -> bool:
        """Match the app's save ack to the exact external request and chunks."""
        try:
            request = json.loads((self.bridge_root / "overnight_finalize_request.json")
                                 .read_text(encoding="utf-8"))
            ack = json.loads((self.bridge_root / "overnight_finalize_ack.json")
                             .read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return False
        if not isinstance(request, dict) or not isinstance(ack, dict):
            return False
        nonce = request.get("nonce")
        if (not isinstance(nonce, str) or len(nonce) != 32 or
                any(letter not in "0123456789abcdef" for letter in nonce)):
            return False
        for record in (request, ack):
            if (record.get("schema_version") != 1 or
                    record.get("command") != "save_and_shutdown" or
                    record.get("run_id") != run_id or
                    record.get("physical_session_id") != native_id or
                    record.get("session_mode_id") != mode_id or
                    record.get("nonce") != nonce):
                return False
        minimum = request.get("min_recorded_lap_crossings")
        chunk_count = ack.get("chunk_count")
        crossings = ack.get("recorded_lap_crossings")
        return (ack.get("status") == "save_complete_shutdown_requested" and
                ack.get("write_ok") is True and
                _nonnegative_int(minimum) and minimum >= 1 and
                minimum <= self._lap_count and
                _nonnegative_int(crossings) and crossings == self._lap_count and
                _nonnegative_int(chunk_count) and
                chunk_count == len(self._chunk_signatures))
