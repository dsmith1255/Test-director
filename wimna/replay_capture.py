"""Archive AC's own autosaved replay after a verified offline session exit.

This copies a native ``.acreplay`` file. It cannot trim a lap, prove that a
complete lap survived AC's replay size cap, or encode a TV camera view.
"""
from __future__ import annotations

import configparser
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from .overnight import Observation, SessionSpec


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ReplayArchiver:
    """Copy exactly one fresh car/track autosave before the next CM launch."""

    def __init__(self, replay_temp: str | Path, output: str | Path,
                 replay_ini: str | Path, *,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_attempts: int = 40, poll_interval_s: float = 0.25):
        if (not isinstance(poll_attempts, int) or isinstance(poll_attempts, bool)
                or not 2 <= poll_attempts <= 120 or not 0 <= poll_interval_s <= 1):
            raise ValueError("bounded replay polling settings required")
        self.replay_temp = Path(replay_temp)
        self.output = Path(output)
        self.replay_ini = Path(replay_ini)
        self.sleep = sleep
        self.poll_attempts = poll_attempts
        self.poll_interval_s = poll_interval_s
        self._prepared_session_id: str | None = None
        self._before: dict[str, tuple[int, int]] = {}
        self._autosave_config: dict[str, Any] = {}

    def _config(self) -> dict[str, Any]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            if not parser.read(self.replay_ini, encoding="utf-8-sig"):
                return {"status": "unavailable"}
            return {
                "status": "read",
                "enabled": parser.getboolean("AUTOSAVE", "ENABLED", fallback=None),
                "other_sessions": parser.getint("AUTOSAVE", "OTHERS", fallback=None),
                "minimum_time_s": parser.getint("AUTOSAVE", "MIN_TIME_SECONDS", fallback=None),
                "max_size_mb": parser.getint("REPLAY", "MAX_SIZE_MB", fallback=None),
            }
        except (OSError, ValueError, configparser.Error):
            return {"status": "unavailable"}

    @staticmethod
    def _suffixes(spec: SessionSpec) -> tuple[str, ...]:
        car = str(spec.expected["car_id"])
        track = str(spec.expected["track_id"])
        layout = str(spec.expected["layout_id"])
        if any(not SAFE_NAME.fullmatch(value) for value in (car, track, layout)):
            return ()
        suffix = f"_{car}_{track}_"
        return (suffix, suffix + "default") if layout == "default" else (suffix + layout,)

    def _matching_files(self, spec: SessionSpec) -> dict[Path, tuple[int, int]]:
        suffixes = tuple(value.lower() for value in self._suffixes(spec))
        if not suffixes or not self.replay_temp.is_dir():
            return {}
        found = {}
        for path in self.replay_temp.glob("*.acreplay"):
            if not path.stem.lower().startswith("ac_") or not path.stem.lower().endswith(suffixes):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if path.is_file() and info.st_size > 0:
                found[path] = (info.st_size, info.st_mtime_ns)
        return found

    def prepare(self, spec: SessionSpec, now_s: float) -> None:
        """Snapshot matching autosaves before any external launch side effect."""
        self._prepared_session_id = spec.session_id
        self._before = {path.name: signature
                        for path, signature in self._matching_files(spec).items()}
        self._autosave_config = self._config()

    def _copy(self, source: Path, signature: tuple[int, int],
              spec: SessionSpec, obs: Observation) -> dict[str, Any]:
        run_id = obs.values.get("run_id")
        if not isinstance(run_id, str) or not SAFE_NAME.fullmatch(run_id):
            return {"status": "unavailable", "reason": "run_id_missing_or_unsafe"}
        label = f"{run_id}_{spec.expected['track_id']}_{spec.expected['layout_id']}"
        if not SAFE_NAME.fullmatch(label):
            return {"status": "unavailable", "reason": "output_label_unsafe"}
        self.output.mkdir(parents=True, exist_ok=True)
        destination = self.output / (label + ".acreplay")
        temporary = self.output / (label + ".acreplay.tmp")
        if destination.exists():
            return {"status": "unavailable", "reason": "archive_destination_already_exists"}
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as readable, temporary.open("xb") as writable:
                while block := readable.read(1024 * 1024):
                    writable.write(block)
                    digest.update(block)
                    size += len(block)
                writable.flush()
                os.fsync(writable.fileno())
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != signature or size != signature[0]:
                return {"status": "unavailable", "reason": "source_changed_during_copy"}
            os.replace(temporary, destination)
        except OSError as exc:
            return {"status": "unavailable", "reason": "copy_failed:" + type(exc).__name__}
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "status": "archived_content_unverified",
            "evidence": "MEASURED",
            "run_id": run_id,
            "physical_session_id": obs.physical_session_id,
            "track_id": spec.expected["track_id"],
            "layout_id": spec.expected["layout_id"],
            "car_id": spec.expected["car_id"],
            "mode_id": spec.expected["mode_id"],
            "variant_id": spec.expected["variant_id"],
            "source_replay_file": source.name,
            "archive_file": destination.name,
            "bytes": size,
            "sha256": digest.hexdigest(),
            "autosave_config": self._autosave_config,
            "full_lap_verified": False,
            "tv_camera_view_embedded_verified": False,
            "tv_camera_playback": "select_track_camera_in_replay_player",
            "warning": "Native replay file archived; playback, full-lap coverage and TV camera view require a live check",
        }

    def archive(self, spec: SessionSpec, obs: Observation, now_s: float) -> dict[str, Any]:
        """Use only a new, settled replay after save and process-exit readbacks."""
        if (self._prepared_session_id != spec.session_id or
                obs.recorder_saved is not True or obs.session_closed is not True or
                obs.recorder_active is not False):
            return {"status": "unavailable", "reason": "session_not_prepared_saved_and_closed"}
        previous: tuple[Path, tuple[int, int]] | None = None
        for attempt in range(self.poll_attempts):
            fresh = [(path, signature) for path, signature in self._matching_files(spec).items()
                     if self._before.get(path.name) != signature]
            if len(fresh) > 1:
                return {"status": "unavailable", "reason": "multiple_fresh_matching_replays"}
            if len(fresh) == 1:
                if previous == fresh[0]:
                    return self._copy(fresh[0][0], fresh[0][1], spec, obs)
                previous = fresh[0]
            else:
                previous = None
            if attempt + 1 < self.poll_attempts:
                self.sleep(self.poll_interval_s)
        return {"status": "unavailable", "reason": "fresh_matching_replay_not_observed",
                "autosave_config": self._autosave_config}
