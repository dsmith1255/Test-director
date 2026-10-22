"""Campaign configuration with explicit missing-value handling."""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_BLOCK_KINDS = ("calibration", "reference", "fuel", "energy", "management", "validation")


def template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "campaign.template.json"


def load_campaign(path: str | Path | None = None) -> dict:
    target = Path(path) if path else template_path()
    config = json.loads(target.read_text(encoding="utf-8"))
    validate_campaign(config)
    return config


def validate_campaign(config: dict) -> None:
    if config.get("schema_version") != 1:
        raise ValueError("unsupported campaign schema")
    schedule = config.get("schedule", [])
    if sum(item["minutes"] for item in schedule) != config.get("time_budget_minutes"):
        raise ValueError("schedule does not equal campaign time budget")
    order = config.get("track_order", [])
    if order != ["spa", "silverstone_gp", "imola", "monza_gp"]:
        raise ValueError("campaign track order must be Spa, Silverstone, Imola, Monza")
    scheduled_tracks = [item["activity"] for item in schedule if item["activity"] in config.get("tracks", {})]
    if scheduled_tracks != order:
        raise ValueError("schedule track order does not match campaign track order")
    for track in order:
        blocks = config["tracks"][track]["blocks"]
        if tuple(block["kind"] for block in blocks) != REQUIRED_BLOCK_KINDS:
            raise ValueError(f"missing or out-of-order block for {track}")
        program_laps = sum(block.get("laps", block.get("laps_min", 0)) for block in blocks)
        if program_laps != 20:
            raise ValueError(f"expected 20 minimum program laps for {track}, got {program_laps}")
        if blocks[0].get("laps_max") != 5:
            raise ValueError("learning lap cap must be five")


def unresolved_live_inputs(config: dict) -> list[str]:
    missing: list[str] = []
    for track in config["track_order"]:
        for field in ("track_id", "layout_id"):
            if config["tracks"][track].get(field) is None:
                missing.append(f"tracks.{track}.{field}")
    for field, value in config["policy"].items():
        if value is None:
            missing.append(f"policy.{field}")
    return missing
