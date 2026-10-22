"""Command-line interface for diagnostics, replay and offline export."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import __version__
from .case_import import import_update6
from .config import load_campaign, unresolved_live_inputs
from .discovery import discover
from .live import documents_root
from .replay import replay_campaign
from .report import export_bundle
from .store import Store, ingest_bridge
from .vehicle_data import inspect_readable_car


def data_directory() -> Path:
    value = os.environ.get("WIMNA_DATA_DIR")
    return Path(value).expanduser().resolve() if value else (documents_root() / "WIMNA Test Director")


def _save(path: str | Path, obj: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wimna", description="WIMNA Test Director offline core")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("discover", help="read-only local AC/CM inventory")
    inventory.add_argument("--ac-root")
    inventory.add_argument("--cm-exe")
    inventory.add_argument("--output")
    imported = commands.add_parser("import-case", help="strict Update6 workbook import")
    imported.add_argument("workbook")
    imported.add_argument("--output", required=True)
    vehicle = commands.add_parser("inspect-car", help="inventory readable installed vehicle definitions")
    vehicle.add_argument("car_folder")
    vehicle.add_argument("--output", required=True)
    replay = commands.add_parser("replay", help="deterministic synthetic full campaign")
    replay.add_argument("--output", required=True)
    replay.add_argument("--config")
    bridge = commands.add_parser("ingest", help="idempotent CSP bridge import")
    bridge.add_argument("--bridge", default=str(data_directory() / "bridge"))
    bridge.add_argument("--store", default=str(data_directory() / "data" / "store"))
    report = commands.add_parser("report", help="export raw data and evidence-labeled results")
    report.add_argument("--store", default=str(data_directory() / "data" / "store"))
    report.add_argument("--config")
    report.add_argument("--case-import")
    report.add_argument("--assumptions")
    report.add_argument("--output", required=True)
    commands.add_parser("gui", help="open the Windows driver companion")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "discover":
        report = discover(args.ac_root, args.cm_exe)
        report["unresolved_campaign_inputs"] = unresolved_live_inputs(load_campaign())
        if args.output:
            _save(args.output, report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.command == "import-case":
        report = import_update6(args.workbook)
        _save(args.output, report)
        print(f"Imported Update6 with SHA-256 {report['source_sha256']} to {args.output}")
    elif args.command == "inspect-car":
        report = inspect_readable_car(args.car_folder)
        _save(args.output, report)
        print(f"Inventoried {len(report['files'])} readable data files; model_ready={report['model_ready']}")
    elif args.command == "replay":
        print(json.dumps(replay_campaign(args.output, config_path=args.config), indent=2))
    elif args.command == "ingest":
        with Store(args.store) as store:
            print(json.dumps(ingest_bridge(args.bridge, store), indent=2))
    elif args.command == "report":
        config = load_campaign(args.config)
        case = json.loads(Path(args.case_import).read_text(encoding="utf-8")) if args.case_import else None
        assumptions = json.loads(Path(args.assumptions).read_text(encoding="utf-8")) if args.assumptions else None
        with Store(args.store) as store:
            from .analysis import analyze_store
            forecasts, validation, parameters = analyze_store(store, config, case_data=case, assumptions=assumptions)
            print(json.dumps(export_bundle(store, args.output, config=config, case_data=case,
                                           forecasts=forecasts, validation=validation,
                                           parameters=parameters), indent=2))
    elif args.command == "gui":
        from .gui import main as gui_main
        return int(gui_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
