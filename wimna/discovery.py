"""Read-only local Assetto Corsa and Content Manager inventory."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Iterable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(path: Path) -> str | None:
    if sys.platform != "win32" or not path.is_file():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        numbers = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
        return f"{numbers[2] >> 16}.{numbers[2] & 65535}.{numbers[3] >> 16}.{numbers[3] & 65535}"
    except (AttributeError, OSError, ValueError):
        return None


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        try:
            import winreg

            for hive, key_name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
                except OSError:
                    pass
        except ImportError:
            pass
    for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        if os.environ.get(env_name):
            roots.append(Path(os.environ[env_name]) / "Steam")
    return roots


def _library_paths(steam_root: Path) -> Iterable[Path]:
    yield steam_root
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    if library_file.is_file():
        raw = library_file.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r'"path"\s*"([^"]+)"', raw, re.IGNORECASE):
            yield Path(value.replace("\\\\", "\\"))


def find_ac_roots(explicit: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("WIMNA_AC_ROOT"):
        candidates.append(Path(os.environ["WIMNA_AC_ROOT"]))
    for steam_root in _steam_roots():
        for library in _library_paths(steam_root):
            candidates.append(library / "steamapps" / "common" / "assettocorsa")
    unique: dict[str, Path] = {}
    for path in candidates:
        resolved = path.expanduser().resolve()
        if (resolved / "acs.exe").is_file():
            unique[str(resolved).casefold()] = resolved
    return list(unique.values())


def _content_manager(explicit: str | Path | None = None) -> dict:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for key in ("LOCALAPPDATA", "APPDATA"):
        if os.environ.get(key):
            base = Path(os.environ[key])
            candidates.extend((base / "AcTools Content Manager" / "Content Manager.exe",
                               base / "Content Manager" / "Content Manager.exe"))
    matches = []
    for path in candidates:
        if path.is_file():
            matches.append({"path": str(path.resolve()), "version": _version(path), "sha256": sha256_file(path)})
    return {"installations": matches, "status": "detected" if matches else "unavailable"}


def _car_inventory(root: Path) -> list[dict]:
    cars_root = root / "content" / "cars"
    if not cars_root.is_dir():
        return []
    results = []
    for folder in cars_root.iterdir():
        if not folder.is_dir() or "formula_hybrid_2026" not in folder.name.casefold():
            continue
        ui = folder / "ui" / "ui_car.json"
        details: dict = {"car_id": folder.name, "path": str(folder), "ui_version": None,
                         "ui_sha256": sha256_file(ui) if ui.is_file() else None,
                         "readable_data": (folder / "data").is_dir(),
                         "protected_data_archive": (folder / "data.acd").is_file()}
        if ui.is_file():
            try:
                details["ui_version"] = json.loads(ui.read_text(encoding="utf-8-sig" )).get("version")
            except (ValueError, UnicodeError):
                details["ui_parse_error"] = True
        results.append(details)
    return results


def _track_inventory(root: Path) -> dict[str, list[dict]]:
    result = {"spa": [], "silverstone_gp": [], "imola": [], "monza_gp": []}
    tracks = root / "content" / "tracks"
    if not tracks.is_dir():
        return result
    for folder in tracks.iterdir():
        if not folder.is_dir():
            continue
        name = folder.name.casefold()
        match = ("spa" if re.search(r"(^|[_-])spa($|[_-])", name) else
                 "silverstone_gp" if "silverstone" in name else
                 "imola" if "imola" in name else
                 "monza_gp" if "monza" in name else None)
        if match is None:
            continue
        excluded = match == "monza_gp" and any(token in name for token in ("1966", "monza66", "historic", "bank", "oval"))
        excluded = excluded or (match == "spa" and any(token in name for token in ("1966", "1967", "historic", "classic")))
        layouts: dict[str, tuple[Path, Path]] = {}
        default_models = folder / "models.ini"
        if default_models.is_file():
            layouts["default"] = (folder, default_models)
        for model_file in folder.glob("models_*.ini"):
            layout_id = model_file.stem[len("models_"):]
            layouts[layout_id] = (folder / layout_id if (folder / layout_id).is_dir() else folder, model_file)
        for child in folder.iterdir():
            if child.is_dir() and (child / "models.ini").is_file():
                layouts[child.name] = (child, child / "models.ini")
        if not layouts:
            continue
        for layout_id, (layout, model_file) in sorted(layouts.items()):
            lname = layout_id.casefold()
            old = excluded or (match == "monza_gp" and any(token in lname for token in ("1966", "monza66", "historic", "bank", "oval")))
            old = old or (match == "spa" and any(token in lname for token in ("1966", "1967", "historic", "classic")))
            old = old or (match == "silverstone_gp" and any(token in f"{name} {lname}" for token in ("1967", "historic")))
            ui_candidates = [folder / "ui" / layout_id / "ui_track.json", layout / "ui" / "ui_track.json",
                             folder / "ui" / "ui_track.json"]
            ui = next((p for p in ui_candidates if p.is_file()), None)
            result[match].append({"track_id": folder.name,
                                  "layout_id": layout_id,
                                  "path": str(layout), "excluded_historic": old,
                                  "model_sha256": sha256_file(model_file) if model_file.is_file() else None,
                                  "ui_sha256": sha256_file(ui) if ui else None})
    return result


def discover(ac_root: str | Path | None = None, cm_exe: str | Path | None = None) -> dict:
    roots = find_ac_roots(ac_root)
    installations = []
    for root in roots:
        exe = root / "acs.exe"
        csp_binary = root / "dwrite.dll"
        sdk = root / "extension" / "internal" / "lua-sdk"
        pure_candidates = [root / "extension" / "weather-controllers" / "pure",
                           root / "extension" / "weather-controllers" / "pure.lua",
                           root / "content" / "weather" / "pure"]
        pure = [{"path": str(path), "kind": "directory" if path.is_dir() else "file",
                 "sha256": sha256_file(path) if path.is_file() else None,
                 "version": None, "version_status": "unverified"}
                for path in pure_candidates if path.exists()]
        installations.append({"root": str(root), "ac_executable_version": _version(exe),
                              "ac_executable_sha256": sha256_file(exe),
                              "csp_binary_version": _version(csp_binary),
                              "csp_binary_sha256": sha256_file(csp_binary) if csp_binary.is_file() else None,
                              "csp_sdk_path": str(sdk) if sdk.is_dir() else None,
                              "csp_sdk_status": "detected" if sdk.is_dir() else "unavailable",
                              "pure_candidates": pure,
                              "cars": _car_inventory(root), "tracks": _track_inventory(root)})
    return {"schema_version": 1, "host_os": platform.platform(), "windows_build": platform.version(),
            "installations": installations, "content_manager": _content_manager(cm_exe),
            "live_capability_status": "unverified", "folder_picker_required": not roots}
