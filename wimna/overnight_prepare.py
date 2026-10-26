"""Prepare local paired AI queues without launching or approving them."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Callable

from .cm_presets import CAR_ID, CUSTOM_MODE_PREFIX, TRACKS
from .overnight import _windows_file_version


MODES = {"baseline": "wimna-ai-baseline", "plan_c": "wimna-ai-plan-c"}
CAMPAIGN_ORDER = (
    ("spa", "baseline"),
    ("monza", "plan_c"),
    ("imola", "baseline"),
    ("ks_silverstone/gp", "plan_c"),
    ("spa", "plan_c"),
    ("monza", "baseline"),
    ("imola", "plan_c"),
    ("ks_silverstone/gp", "baseline"),
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_pair(*, ac_root: Path, cm_exe: Path, preset_dir: Path,
                 bridge_root: Path, race_ini: Path, car_version: str,
                 output: Path) -> dict:
    """Hash installed inputs; leave every live authorization flag false."""
    ac_root = ac_root.resolve(strict=True)
    cm_exe = cm_exe.resolve(strict=True)
    preset_dir = preset_dir.resolve(strict=True)
    bridge_root = bridge_root.resolve(strict=True)
    race_ini = race_ini.resolve(strict=True)
    output = output.resolve()
    if not car_version:
        raise ValueError("exact observed car version is required")
    if output.exists() and any(output.iterdir()):
        raise ValueError("pilot output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    car_ui = ac_root / "content" / "cars" / CAR_ID / "ui" / "ui_car.json"
    app_dir = ac_root / "apps" / "lua" / "WimnaTestDirector"
    common_files = [car_ui, app_dir / "manifest.ini", app_dir / "WimnaTestDirector.lua"]
    expected = []
    preset_files = {}
    variant_files = {}
    for variant_id, mode_id in MODES.items():
        mode_dir = ac_root / "extension" / "lua" / "new-modes" / mode_id
        installed = [*common_files, mode_dir / "manifest.ini", mode_dir / "mode.lua"]
        if variant_id == "plan_c":
            installed.append(mode_dir / "aero_worker.lua")
        files = [{"path": str(path.resolve(strict=True)), "sha256": _hash(path)}
                 for path in installed]
        variant_manifest = output / f"{variant_id}.variant.json"
        _save(variant_manifest, {
            "schema_version": 1, "variant_id": variant_id,
            "car_id": CAR_ID, "car_version": car_version,
            "files": files,
        })
        variant_files[variant_id] = str(variant_manifest)
        preset = preset_dir / f"wimna-spa-{variant_id}.cmpreset"
        preset = preset.resolve(strict=True)
        preset_record = json.loads(preset.read_text(encoding="utf-8-sig"))
        if (preset_record.get("Mode") !=
                "/Pages/Drive/QuickDrive_Custom.xaml?Id=" + mode_id or
                preset_record.get("CarId") != CAR_ID or
                preset_record.get("TrackId") != "spa"):
            raise ValueError(f"Spa preset identity mismatch: {preset.name}")
        preset_files[preset.stem] = str(preset)
        expected.append({
            "session_id": f"spa-{variant_id}",
            "expected": {
                "preset_id": preset.stem, "preset_sha256": _hash(preset),
                "car_id": CAR_ID, "car_version": car_version,
                "track_id": "spa", "layout_id": "default",
                "variant_id": variant_id, "variant_sha256": _hash(variant_manifest),
                "mode_id": mode_id, "tire_compound": "C4",
                "energy_profile_id": "unverified-readback",
                "ambient_air_c": float(preset_record["Temperature"]),
                "track_surface_c": float(preset_record["crtv"]),
                "fuel_start_l": 50.0,
            },
        })
    queue = output / "spa-two-session.queue.json"
    _save(queue, {
        "schema_version": 1, "queue_id": "spa-paired-supervised-pilot",
        "launch_mode": "verified_local",
        "defaults": {
            "min_lap_crossings": 2, "planned_switch_after_s": 10,
            "max_session_s": 420, "start_timeout_s": 90,
            "finalize_timeout_s": 60, "watchdog_s": 12,
            "max_dropped_samples": 0,
            "tolerances": {"ambient_air_c": 0.5,
                           "track_surface_c": 0.5, "fuel_start_l": 1.0},
        },
        "sessions": expected,
    })
    preflight = output / "preflight.json"
    _save(preflight, {
        "schema_version": 1, "status": "unverified",
        "verified_at_utc": None,
        "queue_sha256": _hash(queue),
        "cm_executable_path": str(cm_exe),
        "cm_executable_sha256": _hash(cm_exe),
        "cm_file_version": _windows_file_version(cm_exe),
        "uri_launch_smoke_tested": False,
        "two_stage_load_go_smoke_tested": False,
        "recording_save_smoke_tested": False,
        "session_close_smoke_tested": False,
        "readbacks_smoke_tested": False,
    })
    config = output / "runner.json"
    _save(config, {
        "schema_version": 1, "queue_path": str(queue),
        "preflight_path": str(preflight), "cm_executable": str(cm_exe),
        "race_ini_path": str(race_ini), "bridge_root": str(bridge_root),
        "checkpoint_path": str(bridge_root.parent / "overnight.checkpoint.json"),
        "preset_files": preset_files,
        "variant_manifest_files": variant_files,
    })
    return {"queue": str(queue), "preflight": str(preflight),
            "config": str(config), "variant_manifests": variant_files,
            "status": "prepared_not_approved_or_launched"}


def _track_files(ac_root: Path, cm_track_id: str) -> list[Path]:
    """Resolve the exact installed layout and the model files it loads."""
    folder_id, layout_id, expected_name, ui_relative, model_relative = TRACKS[cm_track_id]
    folder = (ac_root / "content" / "tracks" / folder_id).resolve(strict=True)
    ui_file = (folder / ui_relative).resolve(strict=True)
    model_file = (folder / model_relative).resolve(strict=True)
    ui = json.loads(ui_file.read_text(encoding="utf-8-sig"))
    if not isinstance(ui, dict) or ui.get("name") != expected_name:
        raise ValueError(f"installed track identity differs: {cm_track_id}")
    data_folder = folder / ("data" if layout_id == "default" else layout_id + "/data")
    surfaces = (data_folder / "surfaces.ini").resolve(strict=True)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(model_file, encoding="utf-8-sig")
    files = [ui_file, model_file, surfaces]
    for section in parser.sections():
        if not section.upper().startswith("MODEL_"):
            continue
        model_name = parser.get(section, "FILE", fallback="").strip()
        if not model_name.lower().endswith(".kn5"):
            raise ValueError(f"installed model reference is missing: {cm_track_id}/{section}")
        model = (folder / model_name).resolve(strict=True)
        if folder not in model.parents:
            raise ValueError(f"installed model reference escapes track: {cm_track_id}")
        files.append(model)
    if len(files) == 3:
        raise ValueError(f"installed layout has no model references: {cm_track_id}")
    return files


def _observed_car_version(car_ui: Path) -> str | None:
    ui = json.loads(car_ui.read_text(encoding="utf-8-sig"))
    if not isinstance(ui, dict):
        raise ValueError("installed car UI is not a JSON object")
    explicit = ui.get("version")
    if isinstance(explicit, (str, int)) and str(explicit).strip():
        return str(explicit).strip()
    description = ui.get("description")
    if isinstance(description, str):
        match = re.search(r"\bVersion:\s*([A-Za-z0-9._-]+)", description)
        if match:
            return match.group(1)
    return None


def _campaign_presets(preset_dir: Path, ac_root: Path) -> tuple[dict, dict]:
    """Reject stale or edited generated presets before writing a queue."""
    manifest_file = (preset_dir / "four-track-preset-pairs.json").resolve(strict=True)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or
            manifest.get("status") != "GENERATED_NOT_LIVE_VERIFIED"):
        raise ValueError("four-track preset manifest is unavailable or unsupported")
    requested = manifest.get("requested")
    if (not isinstance(requested, dict) or requested.get("car_id") != CAR_ID or
            requested.get("track_ids") != list(TRACKS)):
        raise ValueError("four-track preset manifest has different car or track order")
    weather_id = requested.get("weather_id")
    if (not isinstance(weather_id, str) or not weather_id or
            not weather_id.replace("_", "").isalnum()):
        raise ValueError("four-track preset manifest has invalid weather ID")
    track_records = manifest.get("installed_track_identities")
    if not isinstance(track_records, list) or len(track_records) != len(TRACKS):
        raise ValueError("four-track preset manifest lacks installed track identities")
    for row, cm_track_id in zip(track_records, TRACKS):
        if not isinstance(row, dict) or row.get("content_manager_track_id") != cm_track_id:
            raise ValueError("four-track preset manifest track identity differs")
        folder_id, layout_id, _, ui_relative, model_relative = TRACKS[cm_track_id]
        folder = ac_root / "content" / "tracks" / folder_id
        if (row.get("track_id") != folder_id or row.get("layout_id") != layout_id or
                row.get("ui_track_sha256") != _hash(folder / ui_relative) or
                row.get("models_ini_sha256") != _hash(folder / model_relative)):
            raise ValueError(f"installed track file changed since preset generation: {cm_track_id}")
    rows = manifest.get("presets")
    if not isinstance(rows, list) or len(rows) != 8:
        raise ValueError("four-track preset manifest must contain eight presets")
    records = {}
    common = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("four-track preset record is malformed")
        cm_track_id = row.get("content_manager_track_id")
        mode_id = row.get("mode_id")
        variant_id = next((key for key, value in MODES.items() if value == mode_id), None)
        if cm_track_id not in TRACKS or variant_id is None:
            raise ValueError("four-track preset has unsupported track or mode")
        slug = cm_track_id.replace("/", "-")
        expected_name = f"wimna-{slug}-{variant_id}"
        if (row.get("preset_id") != expected_name or
                row.get("file") != expected_name + ".cmpreset"):
            raise ValueError("four-track preset ID or filename differs")
        preset = (preset_dir / row["file"]).resolve(strict=True)
        if preset.parent != preset_dir or row.get("sha256") != _hash(preset):
            raise ValueError(f"generated preset changed since its manifest: {expected_name}")
        data = json.loads(preset.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or data.get("CarId") != CAR_ID or
                data.get("TrackId") != cm_track_id or
                data.get("Mode") != CUSTOM_MODE_PREFIX + mode_id or
                data.get("RealConditions") is not False or
                data.get("WeatherId") != weather_id):
            raise ValueError(f"generated preset identity differs: {expected_name}")
        for key in ("Temperature", "crtv"):
            value = data.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value)):
                raise ValueError(f"generated preset lacks numeric {key}: {expected_name}")
        if (data["Temperature"] != requested.get("ambient_air_c") or
                data["crtv"] != requested.get("road_c")):
            raise ValueError(f"generated preset environment differs: {expected_name}")
        mode_data = json.loads(data.get("ModeData", "null"))
        if (not isinstance(mode_data, dict) or
                any(key in mode_data for key in ("LapsNumber", "RaceGridSerialized"))):
            raise ValueError(f"generated preset has an unexpected lap/grid control: {expected_name}")
        conditions = {key: value for key, value in data.items()
                      if key not in {"TrackId", "Mode"}}
        if common is None:
            common = conditions
        elif conditions != common:
            raise ValueError("generated presets have unmatched requested conditions")
        pair = (cm_track_id, variant_id)
        if pair in records:
            raise ValueError(f"duplicate generated preset: {expected_name}")
        records[pair] = {"path": preset, "sha256": row["sha256"], "data": data}
    if set(records) != {(track, variant) for track in TRACKS for variant in MODES}:
        raise ValueError("generated presets do not cover all four matched pairs")
    return manifest, records


def prepare_campaign(*, ac_root: Path, cm_exe: Path, preset_dir: Path,
                     bridge_root: Path, race_ini: Path, car_version: str,
                     output: Path, tire_compound: str = "C4",
                     energy_profile_id: str = "unverified-readback",
                     fuel_start_l: float = 50.0,
                     max_dropped_samples: int = 100,
                     version_reader: Callable[[Path], str] = _windows_file_version) -> dict:
    """Prepare the eight-session four-track queue; never dispatch CM or AC.

    Fuel, tire and RSS energy values are requested matching targets. The CM
    preset cannot apply or read them, so the live queue records any mismatches
    as comparison limits while retaining raw simulator telemetry.
    """
    ac_root = ac_root.resolve(strict=True)
    cm_exe = cm_exe.resolve(strict=True)
    preset_dir = preset_dir.resolve(strict=True)
    bridge_root = bridge_root.resolve(strict=True)
    race_ini = race_ini.resolve(strict=True)
    output = output.resolve()
    if not car_version or not car_version.strip():
        raise ValueError("exact observed car version is required")
    if not tire_compound or not tire_compound.strip():
        raise ValueError("requested tire compound is required")
    if not energy_profile_id or not energy_profile_id.strip():
        raise ValueError("requested energy profile label is required")
    if (isinstance(fuel_start_l, bool) or not isinstance(fuel_start_l, (int, float))
            or not math.isfinite(fuel_start_l) or fuel_start_l <= 0):
        raise ValueError("requested fuel must be a positive number of liters")
    if (isinstance(max_dropped_samples, bool) or
            not isinstance(max_dropped_samples, int) or
            not 1 <= max_dropped_samples <= 1000):
        raise ValueError("drop threshold must be an integer from 1 to 1000")
    if output.exists() and any(output.iterdir()):
        raise ValueError("campaign output directory must be new or empty")
    # Each campaign owns its own checkpoint. A prior Spa pilot can remain in
    # Documents without being overwritten or blocking a new four-track run.
    checkpoint = output / "overnight.checkpoint.json"
    car_ui = ac_root / "content" / "cars" / CAR_ID / "ui" / "ui_car.json"
    observed_version = _observed_car_version(car_ui)
    if observed_version is None or observed_version != car_version:
        raise ValueError("requested car version differs from installed car UI")
    car_data = ac_root / "content" / "cars" / CAR_ID / "data.acd"
    if not car_data.is_file():
        raise ValueError("installed FH26 physics archive is unavailable")
    manifest, presets = _campaign_presets(preset_dir, ac_root)
    app_dir = ac_root / "apps" / "lua" / "WimnaTestDirector"
    common_files = [car_ui, car_data, app_dir / "manifest.ini",
                    app_dir / "WimnaTestDirector.lua"]
    weather = str(manifest["requested"]["weather_id"])
    weather_file = ac_root / "content" / "weather" / weather / "weather.ini"
    common_files.append(weather_file)
    for track in TRACKS:
        common_files.extend(_track_files(ac_root, track))
    variant_paths = {}
    for variant_id, mode_id in MODES.items():
        mode_dir = ac_root / "extension" / "lua" / "new-modes" / mode_id
        files = [*common_files, mode_dir / "manifest.ini", mode_dir / "mode.lua"]
        if variant_id == "plan_c":
            files.append(mode_dir / "aero_worker.lua")
        variant_paths[variant_id] = files
    # Stream each installed file only once, including the selected KN5 models.
    hashes = {_path: _hash(_path) for paths in variant_paths.values()
              for path in paths for _path in [path.resolve(strict=True)]}
    output.mkdir(parents=True, exist_ok=True)
    variant_files = {}
    for variant_id, files in variant_paths.items():
        variant_file = output / f"{variant_id}.variant.json"
        _save(variant_file, {
            "schema_version": 1, "variant_id": variant_id,
            "car_id": CAR_ID, "car_version": car_version,
            "files": [{"path": str(path.resolve(strict=True)),
                       "sha256": hashes[path.resolve(strict=True)]}
                      for path in files],
        })
        variant_files[variant_id] = str(variant_file)
    sessions = []
    preset_files = {}
    for index, (cm_track_id, variant_id) in enumerate(CAMPAIGN_ORDER, 1):
        folder_id, layout_id, *_ = TRACKS[cm_track_id]
        preset = presets[(cm_track_id, variant_id)]
        preset_files[preset["path"].stem] = str(preset["path"])
        sessions.append({
            "session_id": f"{index:02d}-{cm_track_id.replace('/', '-')}-{variant_id}",
            "expected": {
                "preset_id": preset["path"].stem,
                "preset_sha256": preset["sha256"],
                "car_id": CAR_ID, "car_version": car_version,
                "track_id": folder_id, "layout_id": layout_id,
                "variant_id": variant_id,
                "variant_sha256": _hash(Path(variant_files[variant_id])),
                "mode_id": MODES[variant_id],
                "tire_compound": tire_compound,
                "energy_profile_id": energy_profile_id,
                "ambient_air_c": float(preset["data"]["Temperature"]),
                "track_surface_c": float(preset["data"]["crtv"]),
                "fuel_start_l": float(fuel_start_l),
            },
        })
    queue = output / "four-track-eight-session.queue.json"
    _save(queue, {
        "schema_version": 1, "queue_id": "four-track-plan-c-paired-overnight",
        "launch_mode": "verified_local",
        "preset_manifest_sha256": _hash(preset_dir / "four-track-preset-pairs.json"),
        "requested_controls_not_applied_by_preset": [
            "tire_compound", "fuel_start_l", "battery_state", "rss_energy_profile"],
        "defaults": {
            "min_lap_crossings": 2,
            "planned_switch_after_s": 3600,
            "max_session_s": 3960,
            "start_timeout_s": 120,
            "finalize_timeout_s": 90,
            "watchdog_s": 12,
            "max_dropped_samples": max_dropped_samples,
            "tolerances": {"ambient_air_c": 0.5,
                           "track_surface_c": 0.5, "fuel_start_l": 1.0},
        },
        "sessions": sessions,
    })
    preflight = output / "preflight.json"
    _save(preflight, {
        "schema_version": 1, "status": "unverified",
        "verified_at_utc": None,
        "queue_sha256": _hash(queue),
        "cm_executable_path": str(cm_exe),
        "cm_executable_sha256": _hash(cm_exe),
        "cm_file_version": version_reader(cm_exe),
        "uri_launch_smoke_tested": False,
        "two_stage_load_go_smoke_tested": False,
        "recording_save_smoke_tested": False,
        "session_close_smoke_tested": False,
        "readbacks_smoke_tested": False,
    })
    config = output / "runner.json"
    _save(config, {
        "schema_version": 1, "queue_path": str(queue),
        "preflight_path": str(preflight), "cm_executable": str(cm_exe),
        "race_ini_path": str(race_ini), "bridge_root": str(bridge_root),
        "checkpoint_path": str(checkpoint),
        "preset_files": preset_files,
        "variant_manifest_files": variant_files,
    })
    return {"queue": str(queue), "preflight": str(preflight),
            "config": str(config), "variant_manifests": variant_files,
            "sessions": len(sessions), "status": "prepared_not_approved_or_launched"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="store_true",
                        help="prepare all four tracks and eight hourly sessions")
    for name in ("ac-root", "cm-exe", "preset-dir", "bridge-root",
                 "race-ini", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--car-version", required=True)
    parser.add_argument("--expected-tire", default="C4")
    parser.add_argument("--expected-energy-profile", default="unverified-readback")
    parser.add_argument("--expected-fuel-l", type=float, default=50.0)
    parser.add_argument("--max-dropped-samples", type=int, default=100,
                        help="per-session drop stop threshold (1–1000; default 100)")
    args = parser.parse_args(argv)
    prepare = prepare_campaign if args.campaign else prepare_pair
    extra = ({"tire_compound": args.expected_tire,
              "energy_profile_id": args.expected_energy_profile,
              "fuel_start_l": args.expected_fuel_l,
              "max_dropped_samples": args.max_dropped_samples}
             if args.campaign else {})
    result = prepare(
        ac_root=args.ac_root, cm_exe=args.cm_exe,
        preset_dir=args.preset_dir, bridge_root=args.bridge_root,
        race_ini=args.race_ini, car_version=args.car_version,
        output=args.output, **extra)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
