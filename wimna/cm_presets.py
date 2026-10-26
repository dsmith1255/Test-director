"""Build matched Content Manager Quick Drive presets without editing user data.

The serialized fields and custom-mode URI follow Content Manager's QuickDrive
source.  CM can silently launch its default Race/Practice mode if a custom mode
is still loading or absent from its cached mode list.  Stage the preset in
Quick Drive, confirm its custom mode, and verify the launched native session;
generated files alone are not an automatic launch route.
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
TRACKS = {
    # Content Manager serializes the root layout as the track ID alone.
    "spa": ("spa", "default", "Spa", "ui/ui_track.json", "models.ini"),
    "monza": ("monza", "default", "Monza", "ui/ui_track.json", "models.ini"),
    "imola": ("imola", "default", "Imola", "ui/ui_track.json", "models.ini"),
    "ks_silverstone/gp": ("ks_silverstone", "gp", "Silverstone GP",
                          "ui/gp/ui_track.json", "models_gp.ini"),
}


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


def _build_pairs(source_preset: str | Path, output_directory: str | Path,
                 ac_root: str | Path, track_ids: tuple[str, ...], *,
                 ambient_air_c: float, road_c: float, weather_id: str,
                 overwrite: bool) -> dict[str, Any]:
    """Write matched local presets and a hash manifest; never edit the source.

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
    if not (root / "content" / "cars" / CAR_ID).is_dir():
        raise PresetError("selected car is not installed")
    if not (root / "content" / "weather" / weather_id).is_dir():
        raise PresetError("selected weather is not installed")
    installed_tracks = []
    for cm_track_id in track_ids:
        if cm_track_id not in TRACKS:
            raise PresetError(f"unsupported exact track/layout: {cm_track_id}")
        folder_id, layout_id, expected_name, ui_relative, model_relative = TRACKS[cm_track_id]
        folder = root / "content" / "tracks" / folder_id
        ui_file = folder / ui_relative
        model_file = folder / model_relative
        if not ui_file.is_file() or not model_file.is_file():
            raise PresetError(f"selected track/layout is not installed: {cm_track_id}")
        try:
            ui = json.loads(ui_file.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise PresetError(f"track UI file is invalid: {cm_track_id}") from exc
        if not isinstance(ui, dict) or ui.get("name") != expected_name:
            raise PresetError(f"track/layout UI identity differs: {cm_track_id}")
        installed_tracks.append({
            "content_manager_track_id": cm_track_id,
            "track_id": folder_id,
            "layout_id": layout_id,
            "ui_track_sha256": _sha256(ui_file),
            "models_ini_sha256": _sha256(model_file),
        })
    try:
        template = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PresetError("source is not a CM JSON preset") from exc
    if not isinstance(template, dict):
        raise PresetError("source CM preset must be a JSON object")
    if not (isinstance(template.get("Mode"), str)
            and template["Mode"].startswith("/Pages/Drive/QuickDrive_")):
        raise PresetError("source is not a recognized Quick Drive preset")
    _read_json_object(template.get("ModeData"), "ModeData")
    assists = _read_json_object(template.get("AssistsData"), "AssistsData")
    track_state = _read_json_object(template.get("TrackPropertiesData"),
                                    "TrackPropertiesData")
    for field in ("TyreWear", "FuelConsumption"):
        if not _finite_number(assists.get(field)) or assists[field] != 1.0:
            raise PresetError(f"source AssistsData.{field} must equal 1.0")
    if not _finite_number(track_state.get("s")):
        raise PresetError("source track-state grip is missing")

    # A fresh, explicit Quick Drive record prevents personal preset paths and
    # the old Track Day mode from carrying into either generated file.
    common = {
        "CarId": CAR_ID,
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
    generated: list[tuple[Path, bytes, str, str]] = []
    for track in installed_tracks:
        cm_track_id = track["content_manager_track_id"]
        slug = cm_track_id.replace("/", "-")
        for label, mode_id in MODES.items():
            preset = {
                **common,
                "TrackId": cm_track_id,
                "Mode": CUSTOM_MODE_PREFIX + mode_id,
                # Inherited race fields are irrelevant for BASE_MODE=PRACTICE.
                # PracticeProperties has no lap limit; hourly limits belong to
                # the separate queue and have not been verified live.
                # No source race grid: this is a single-car Practice mode.
                # A stale CM custom-mode cache can otherwise fall back to
                # Quick Race with the template's old opponents.
                "ModeData": _serialized({"Version": 2, "Penalties": True,
                                         "JumpStartPenalty": 0}),
            }
            filename = output / f"wimna-{slug}-{label}.cmpreset"
            payload = (_serialized(preset) + "\n").encode("utf-8")
            generated.append((filename, payload, mode_id, cm_track_id))
    manifest_path = output / ("spa-preset-pair.json" if track_ids == ("spa",)
                              else "four-track-preset-pairs.json")
    planned = [item[0] for item in generated] + [manifest_path]
    if not overwrite and any(path.exists() for path in planned):
        raise PresetError("generated preset already exists; use overwrite deliberately")
    manifest = {
        "schema_version": 1,
        "source_preset_sha256": _sha256(source),
        "cm_preset_schema": "Quick Drive, observed locally and checked against actools source",
        "status": "GENERATED_NOT_LIVE_VERIFIED",
        "requested": {"car_id": CAR_ID, "track_ids": list(track_ids),
                      "weather_id": weather_id, "ambient_air_c": ambient_air_c,
                      "road_c": road_c},
        "installed_track_identities": installed_tracks,
        "presets": [
            {"preset_id": path.stem, "mode_id": mode_id,
             "content_manager_track_id": cm_track_id,
             "sha256": hashlib.sha256(payload).hexdigest(), "file": path.name}
            for path, payload, mode_id, cm_track_id in generated
        ],
        "duration_status": "PRACTICE_PRESET_HAS_NO_HOURLY_END_CONTROL",
        "launch_status": "LOAD_PRESET_THEN_VERIFY_CUSTOM_MODE_BEFORE_GO",
        "uncontrolled_by_preset": ["tire_compound", "fuel_start_l",
                                   "battery_state", "rss_energy_profile"],
    }
    output.mkdir(parents=True, exist_ok=True)
    for path, payload, _, _ in generated:
        path.write_bytes(payload)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return manifest


def build_spa_pair(source_preset: str | Path, output_directory: str | Path,
                   ac_root: str | Path, *, ambient_air_c: float = 26.0,
                   road_c: float = 29.0, weather_id: str = "3_clear",
                   overwrite: bool = False) -> dict[str, Any]:
    """Retained two-preset builder for the first Spa smoke test."""
    return _build_pairs(source_preset, output_directory, ac_root, ("spa",),
                        ambient_air_c=ambient_air_c, road_c=road_c,
                        weather_id=weather_id, overwrite=overwrite)


def build_campaign_pairs(source_preset: str | Path, output_directory: str | Path,
                         ac_root: str | Path, *, ambient_air_c: float = 26.0,
                         road_c: float = 29.0, weather_id: str = "3_clear",
                         overwrite: bool = False) -> dict[str, Any]:
    """Build eight exact Spa/Monza/Imola/Silverstone GP A/B presets."""
    return _build_pairs(source_preset, output_directory, ac_root,
                        tuple(TRACKS), ambient_air_c=ambient_air_c,
                        road_c=road_c, weather_id=weather_id,
                        overwrite=overwrite)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_preset", type=Path)
    parser.add_argument("ac_root", type=Path)
    parser.add_argument("--output", type=Path,
                        default=Path("build") / "cm-presets")
    parser.add_argument("--ambient-c", type=float, default=26.0)
    parser.add_argument("--road-c", type=float, default=29.0)
    parser.add_argument("--weather-id", default="3_clear")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = build_campaign_pairs(args.source_preset, args.output, args.ac_root,
                                  ambient_air_c=args.ambient_c, road_c=args.road_c,
                                  weather_id=args.weather_id, overwrite=args.overwrite)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
