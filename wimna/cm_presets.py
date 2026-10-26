"""Build matched Content Manager Quick Drive presets without editing user data.

The serialized fields and custom-mode URI follow Content Manager's QuickDrive
source.  Generated presets still need a live load/readback check on the exact
installed CM/CSP build before they can be treated as an automatic launch route.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


CUSTOM_MODE_PREFIX = "/Pages/Drive/QuickDrive_Custom.xaml?Id="
MODES = {"baseline": "wimna-ai-baseline", "plan_c": "wimna-ai-plan-c"}
CAR_ID = "rss_formula_hybrid_2026"
TRACK_ID = "spa"


class PresetError(ValueError):
    """Source preset or destination does not satisfy the known CM schema."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (float, int))
            and math.isfinite(value))


def _read_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise PresetError(f"{label} must be a serialized JSON object")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PresetError(f"{label} is invalid JSON") from exc
    if not isinstance(result, dict):
        raise PresetError(f"{label} must be a serialized JSON object")
    return result


def _serialized(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def build_spa_pair(source_preset: str | Path, output_directory: str | Path,
                   ac_root: str | Path, *, ambient_air_c: float = 26.0,
                   road_c: float = 29.0, weather_id: str = "3_clear",
                   overwrite: bool = False) -> dict[str, Any]:
    """Write two new local presets and a hash manifest; never edit the source.

    Tire, fuel, battery and RSS profile are deliberately outside this builder:
    the CM Quick Drive preset schema does not encode their requested state.
    """
    source = Path(source_preset).resolve(strict=True)
    output = Path(output_directory).resolve()
    root = Path(ac_root).resolve(strict=True)
    if output == source.parent or output in source.parents or source in output.parents:
        raise PresetError("output must not be the user preset location")
    if not _finite_number(ambient_air_c) or not -20 <= ambient_air_c <= 60:
        raise PresetError("ambient_air_c must be within -20 to 60 C")
    if not _finite_number(road_c) or not -20 <= road_c <= 80:
        raise PresetError("road_c must be within -20 to 80 C")
    if not weather_id or not weather_id.replace("_", "").isalnum():
        raise PresetError("weather_id must be an installed identifier")
    required_paths = (
        root / "content" / "cars" / CAR_ID,
        root / "content" / "tracks" / TRACK_ID,
        root / "content" / "weather" / weather_id,
    )
    if not all(path.is_dir() for path in required_paths):
        raise PresetError("selected car, Spa track or weather is not installed")
    try:
        template = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PresetError("source is not a CM JSON preset") from exc
    if not isinstance(template, dict):
        raise PresetError("source CM preset must be a JSON object")
    if not (isinstance(template.get("Mode"), str)
            and template["Mode"].startswith("/Pages/Drive/QuickDrive_")):
        raise PresetError("source is not a recognized Quick Drive preset")
    mode_data = _read_json_object(template.get("ModeData"), "ModeData")
    assists = _read_json_object(template.get("AssistsData"), "AssistsData")
    track_state = _read_json_object(template.get("TrackPropertiesData"),
                                    "TrackPropertiesData")
    race_grid = mode_data.get("RaceGridSerialized")
    _read_json_object(race_grid, "ModeData.RaceGridSerialized")
    for field in ("TyreWear", "FuelConsumption"):
        if not _finite_number(assists.get(field)) or assists[field] != 1.0:
            raise PresetError(f"source AssistsData.{field} must equal 1.0")
    if not _finite_number(track_state.get("s")):
        raise PresetError("source track-state grip is missing")

    # A fresh, explicit Quick Drive record prevents personal preset paths and
    # the old Track Day mode from carrying into either generated file.
    common = {
        "CarId": CAR_ID,
        "TrackId": TRACK_ID,
        "WeatherId": weather_id,
        "RealConditions": False,
        "Temperature": ambient_air_c,
        "Time": 12 * 60 * 60,
        "TimeMultipler": 1,  # CM's actual serialized spelling.
        "udt": False,
        "tpc": True,
        "TrackPropertiesData": _serialized(track_state),
        "asc": True,
        "AssistsData": _serialized(assists),
        "ico": False,
        "wsf": 0.0,
        "wst": 0.0,
        "wd": 0.0,
        "rws": False,
        "rwd": False,
        "rcTimezones": True,
        "rcManTime": False,
        "rcManWind": False,
        "rcLw": False,
        "rte": False,
        "rti": False,
        "crt": True,
        "crtv": road_c,
    }
    generated: list[tuple[Path, bytes, str]] = []
    for label, mode_id in MODES.items():
        preset = {
            **common,
            "Mode": CUSTOM_MODE_PREFIX + mode_id,
            "ModeData": _serialized({"Version": 2, "Penalties": True,
                                     "JumpStartPenalty": 0, "LapsNumber": 2,
                                     "RaceGridSerialized": race_grid}),
        }
        filename = output / f"wimna-spa-{label}.cmpreset"
        payload = (_serialized(preset) + "\n").encode("utf-8")
        generated.append((filename, payload, mode_id))
    manifest_path = output / "spa-preset-pair.json"
    planned = [item[0] for item in generated] + [manifest_path]
    if not overwrite and any(path.exists() for path in planned):
        raise PresetError("generated preset already exists; use overwrite deliberately")
    manifest = {
        "schema_version": 1,
        "source_preset_sha256": _sha256(source),
        "cm_preset_schema": "Quick Drive, observed locally and checked against actools source",
        "status": "GENERATED_NOT_LIVE_VERIFIED",
        "requested": {"car_id": CAR_ID, "track_id": TRACK_ID,
                      "weather_id": weather_id, "ambient_air_c": ambient_air_c,
                      "road_c": road_c},
        "presets": [
            {"preset_id": path.stem, "mode_id": mode_id,
             "sha256": hashlib.sha256(payload).hexdigest(), "file": path.name}
            for path, payload, mode_id in generated
        ],
        "uncontrolled_by_preset": ["tire_compound", "fuel_start_l",
                                   "battery_state", "rss_energy_profile"],
    }
    output.mkdir(parents=True, exist_ok=True)
    for path, payload, _ in generated:
        path.write_bytes(payload)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_preset", type=Path)
    parser.add_argument("ac_root", type=Path)
    parser.add_argument("--output", type=Path,
                        default=Path("build") / "cm-presets")
    parser.add_argument("--ambient-c", type=float, default=26.0)
    parser.add_argument("--road-c", type=float, default=29.0)
    parser.add_argument("--weather-id", default="3_clear")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    result = build_spa_pair(args.source_preset, args.output, args.ac_root,
                            ambient_air_c=args.ambient_c, road_c=args.road_c,
                            weather_id=args.weather_id, overwrite=args.overwrite)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
