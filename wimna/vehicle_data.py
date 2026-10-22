"""Read accessible car definitions without unpacking protected data archives."""
from __future__ import annotations

import re
from pathlib import Path

from .discovery import sha256_file

KEY_VALUE = re.compile(r"^\s*([^=;#\[\]]+?)\s*=\s*(.*?)\s*$")
SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _parse_ini(path: Path) -> list[dict]:
    section = None
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        heading = SECTION.match(line)
        if heading:
            section = heading.group(1).strip()
            continue
        match = KEY_VALUE.match(line)
        if match:
            raw_value = match.group(2).split(";", 1)[0].strip()
            rows.append({"section": section, "key": match.group(1).strip(), "raw_value": raw_value,
                         "line": line_number, "unit": None, "value": None,
                         "reason": "native meaning and unit require installed SDK/manual verification"})
    return rows


def _parse_lut(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        content = line.split(";", 1)[0].strip()
        if not content or "|" not in content:
            continue
        x, y = content.split("|", 1)
        try:
            rows.append({"x": float(x.strip()), "y": float(y.strip()), "line": line_number})
        except ValueError:
            rows.append({"raw": content, "line": line_number, "parse_error": True})
    return rows


def inspect_readable_car(car_folder: str | Path, *, max_files: int = 300) -> dict:
    """Inventory visible files and lookup tables; do not infer curves' active role."""
    car = Path(car_folder).resolve()
    if not car.is_dir():
        raise FileNotFoundError(car)
    data = car / "data"
    if not data.is_dir():
        return {"car_id": car.name, "readable_data": False,
                "protected_archive_present": (car / "data.acd").is_file(),
                "files": [], "model_ready": False,
                "reason": "readable data directory unavailable; protected archives are not bypassed"}
    files = sorted(path for path in data.rglob("*") if path.is_file() and path.suffix.casefold() in (".ini", ".lut"))
    if len(files) > max_files:
        raise ValueError("readable data inventory exceeds configured safety limit")
    details = []
    for path in files:
        kind = path.suffix.casefold()
        details.append({"path": path.relative_to(car).as_posix(), "sha256": sha256_file(path),
                        "kind": kind, "entries": _parse_ini(path) if kind == ".ini" else _parse_lut(path),
                        "active_status": "unverified", "units_verified": False})
    scripts = sorted(path for path in (car / "extension").rglob("*.lua") if path.is_file()) if (car / "extension").is_dir() else []
    return {"car_id": car.name, "readable_data": True,
            "protected_archive_present": (car / "data.acd").is_file(),
            "files": details, "scripts": [{"path": path.relative_to(car).as_posix(),
                                            "sha256": sha256_file(path), "role": "unverified"} for path in scripts],
            "model_ready": False,
            "reason": "curve units, active compound/script and semantics must be verified before calibration"}
