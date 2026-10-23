"""Small packaged Windows companion for live setup, status and export."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .analysis import DEFAULT_ASSUMPTIONS, analyze_store
from .case_import import import_update6
from .config import load_campaign, validate_campaign
from .discovery import discover, find_ac_roots
from .live import LiveDirector, _atomic_json, default_bridge_root, default_data_root
from .report import export_bundle


def _template_config() -> dict:
    if getattr(sys, "frozen", False):
        return load_campaign(Path(sys._MEIPASS) / "campaign.template.json")  # type: ignore[attr-defined]
    return load_campaign()


class InstanceLock:
    """One companion process per data folder, including repeated CSP launches."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def _float_or_none(text: str) -> float | None:
    value = text.strip()
    if not value:
        return None
    number = float(value)
    if not __import__("math").isfinite(number):
        raise ValueError("numeric setup values must be finite")
    return number


def _track_option(track_id: str | None, layout_id: str | None) -> str:
    return f"{track_id or ''} :: {layout_id or ''}"


def _parse_track_option(text: str) -> tuple[str | None, str | None]:
    if "::" not in text:
        raise ValueError("enter track and layout as 'track_id :: layout_id'; layout may be blank")
    track_id, layout_id = (part.strip() for part in text.split("::", 1))
    return track_id or None, layout_id or None


def _parse_band(text: str, name: str, *, fraction: bool = False) -> list[float] | None:
    if not text.strip():
        return None
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != 2:
        raise ValueError(f"{name} must contain minimum,maximum")
    values = [_float_or_none(part) for part in parts]
    if any(value is None for value in values):
        raise ValueError(f"{name} must contain two numbers")
    lower, upper = values
    if lower < 0 or upper < lower or (fraction and upper > 1):
        raise ValueError(f"{name} bounds are invalid")
    return [lower, upper]


def _filled_config(config: dict, entries: dict[str, Any]) -> dict:
    updated = copy.deepcopy(config)
    updated["policy"]["car_id"] = entries["car_id"].get().strip() or None
    for track_key in updated["track_order"]:
        track_id, layout_id = _parse_track_option(entries[f"track_{track_key}"].get())
        if track_id:
            candidate = re.sub(r"[^a-z0-9]", "", f"{track_id} {layout_id or ''}".casefold())
            if track_key == "monza_gp" and any(token in candidate for token in
                                                ("monza66", "1966", "historic", "bank", "oval")):
                raise ValueError("Modern Monza GP cannot use historic or banking layouts")
            if track_key == "silverstone_gp" and any(token in candidate for token in
                                                      ("silverstone67", "1967", "historic", "vintage")):
                raise ValueError("Silverstone GP cannot use the 1967 or historic layout")
            if track_key == "spa" and any(token in candidate for token in
                                          ("spa1966", "spa1967", "historic", "classic")):
                raise ValueError("Spa GP cannot use a historic layout")
        updated["tracks"][track_key]["track_id"] = track_id
        updated["tracks"][track_key]["layout_id"] = layout_id
    text_fields = ("reference_compound", "baseline_map", "deployment_map", "recovery_map", "setup_hash")
    numeric_fields = ("reference_fuel_l", "contrasting_fuel_l", "fuel_start_tolerance_l",
                      "battery_start_tolerance_j", "battery_start_tolerance_fraction",
                      "tire_start_tolerance", "environment_tolerance_c",
                      "management_lift_distance_m", "management_throttle_ramp_distance_m")
    for name in text_fields:
        updated["policy"][name] = entries[name].get().strip() or None
    for name in numeric_fields:
        updated["policy"][name] = _float_or_none(entries[name].get())
    updated["policy"]["target_soc_band_j"] = _parse_band(
        entries["target_soc_band_j"].get(), "battery target band (J)")
    updated["policy"]["target_soc_band_fraction"] = _parse_band(
        entries["target_soc_band_fraction"].get(), "SOC target band (fraction)", fraction=True)
    tolerance = updated["policy"]["battery_start_tolerance_fraction"]
    if tolerance is not None and not 0 <= tolerance <= 1:
        raise ValueError("SOC start tolerance must be a fraction from 0 to 1")
    for name in ("management_lift_distance_m", "management_throttle_ramp_distance_m"):
        distance = updated["policy"][name]
        if distance is not None and not 5 <= distance <= 150:
            raise ValueError(f"{name} must be between 5 and 150 m")
    validate_campaign(updated)
    return updated


def _safe_read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _upgrade_legacy_campaign(saved: dict | None, template: dict) -> dict:
    """Expose the new Spa program without discarding a driver's saved setup.

    LiveDirector decides separately whether its persisted scheduler is idle enough
    to adopt this new configuration. Existing diagnostic runs are never promoted.
    """
    if saved is None:
        return template
    old_order = ["monza_gp", "imola", "silverstone_gp"]
    if saved.get("track_order") != old_order or template.get("track_order") != [
        "spa", "silverstone_gp", "imola", "monza_gp"
    ]:
        return saved
    updated = copy.deepcopy(saved)
    updated["program_version"] = template["program_version"]
    updated["time_budget_minutes"] = template["time_budget_minutes"]
    updated["schedule"] = copy.deepcopy(template["schedule"])
    updated["track_order"] = copy.deepcopy(template["track_order"])
    updated["tracks"] = {"spa": copy.deepcopy(template["tracks"]["spa"]),
                         **{key: copy.deepcopy(saved["tracks"][key]) for key in old_order}}
    updated.setdefault("policy", {})
    for key, value in template["policy"].items():
        updated["policy"].setdefault(key, copy.deepcopy(value))
    validate_campaign(updated)
    return updated


class DirectorWindow:
    def __init__(self, root: Any, director: LiveDirector, data_root: Path):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.root, self.director, self.data_root = root, director, data_root
        self.entries: dict[str, Any] = {}
        self.config = director.scheduler.config
        self.assumptions = {**DEFAULT_ASSUMPTIONS,
                            **(_safe_read_json(self.data_root / "model.assumptions.json") or {})}
        root.title("WIMNA Test Director")
        root.geometry("900x750")
        root.minsize(700, 480)
        canvas = tk.Canvas(root, highlightthickness=0)
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=12)
        canvas_window = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))
        frame.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(frame, text="WIMNA Test Director", font=("Segoe UI", 16, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Label(frame, text="Collect labeled tire stints first; export raw data and condition-aware summaries at any time. Campaign targets below are optional defaults.",
                  wraplength=840, justify="left").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Label(frame, text="In game: label tire and conditions → Start Recording or Start AI Recording → drive → end stint in pits → swap tires → start next stint. Stop AI only with a driver ready to steer.",
                  wraplength=840, justify="left").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.status = tk.StringVar(value="Waiting for bridge")
        ttk.Label(frame, textvariable=self.status, wraplength=840, justify="left").grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        row += 1
        self.root_path = tk.StringVar(value="")
        roots = find_ac_roots()
        if roots:
            self.root_path.set(str(roots[0]))
        ttk.Label(frame, text="Assetto Corsa root").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.root_path).grid(row=row, column=1, sticky="ew")
        ttk.Button(frame, text="Browse", command=self._browse_root).grid(row=row, column=2, sticky="e")
        row += 1
        ttk.Button(frame, text="Detect car and track IDs", command=self._detect).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 8))
        row += 1
        self.entries["car_id"] = tk.StringVar(value=self.config["policy"].get("car_id") or "")
        row = self._entry(frame, row, "FH26 car folder ID", "car_id")
        self.track_combos = {}
        for key in self.config["track_order"]:
            label = self.config["tracks"][key]["display_name"]
            name = f"track_{key}"
            self.entries[name] = tk.StringVar(value=_track_option(self.config["tracks"][key].get("track_id"),
                                                                   self.config["tracks"][key].get("layout_id")))
            ttk.Label(frame, text=label + " exact ID/layout").grid(row=row, column=0, sticky="w")
            combo = ttk.Combobox(frame, textvariable=self.entries[name], state="normal")
            combo.grid(row=row, column=1, columnspan=2, sticky="ew")
            self.track_combos[key] = combo
            row += 1
        defaults = self.config["policy"]
        labels = (
            ("reference_compound", "Reference tire compound"),
            ("reference_fuel_l", "Reference fuel (L)"),
            ("contrasting_fuel_l", "Contrast fuel (L)"),
            ("target_soc_band_fraction", "SOC target band (0..1 min,max)"),
            ("target_soc_band_j", "Battery target band (J min,max)"),
            ("baseline_map", "Baseline map label"),
            ("deployment_map", "Deployment map label"),
            ("recovery_map", "Recovery map label"),
            ("setup_hash", "Setup/profile hash"),
            ("fuel_start_tolerance_l", "Fuel start tolerance (L)"),
            ("battery_start_tolerance_j", "Battery start tolerance (J)"),
            ("battery_start_tolerance_fraction", "SOC start tolerance (0..1)"),
            ("tire_start_tolerance", "Tire state tolerance"),
            ("environment_tolerance_c", "Environment tolerance (C)"),
            ("management_lift_distance_m", "M lift distance (m; assumed)"),
            ("management_throttle_ramp_distance_m", "M throttle ramp distance (m; assumed)"),
        )
        for key, label in labels:
            value = defaults.get(key)
            if isinstance(value, list):
                value = ",".join(str(item) for item in value)
            self.entries[key] = tk.StringVar(value="" if value is None else str(value))
            row = self._entry(frame, row, label, key)
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        ttk.Button(buttons, text="Save campaign defaults", command=self._save_setup).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Import Update6 workbook", command=self._import_case).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Model assumptions", command=self._assumption_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Export Results", command=self._export).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Refresh", command=self._poll).pack(side="left")
        row += 1
        ttk.Label(frame, text="Live status and recent transitions").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.log = tk.Text(frame, height=11, wrap="word", state="disabled")
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(row, weight=1)
        self._detect(silent=True)
        self._poll()

    def _entry(self, frame: Any, row: int, label: str, key: str) -> int:
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
        self.ttk.Entry(frame, textvariable=self.entries[key]).grid(row=row, column=1, columnspan=2, sticky="ew")
        return row + 1

    def _browse_root(self) -> None:
        chosen = self.filedialog.askdirectory(title="Select Assetto Corsa root")
        if chosen:
            self.root_path.set(chosen)
            self._detect()

    def _detect(self, silent: bool = False) -> None:
        try:
            inventory = discover(self.root_path.get().strip() or None)
            if not inventory["installations"]:
                if not silent:
                    self.messagebox.showinfo("Installation", "No Assetto Corsa install was detected. Choose its root folder.")
                return
            installation = inventory["installations"][0]
            cars = installation["cars"]
            if len(cars) == 1 and not self.entries["car_id"].get().strip():
                self.entries["car_id"].set(cars[0]["car_id"])
            for key, combo in self.track_combos.items():
                options = [_track_option(item["track_id"], item["layout_id"])
                           for item in installation["tracks"][key] if not item.get("excluded_historic")]
                combo["values"] = options
                if len(options) == 1 and not self.entries[f"track_{key}"].get().split("::")[0].strip():
                    self.entries[f"track_{key}"].set(options[0])
            self.status.set(f"Detected {installation['root']}; select the exact modern GP layouts and save setup.")
        except (OSError, ValueError) as exc:
            if not silent:
                self.messagebox.showerror("Discovery", str(exc))

    def _save_setup(self) -> None:
        try:
            config = _filled_config(self.config, self.entries)
            self.director.configure(config)
            _atomic_json(self.data_root / "campaign.live.json", config)
            self.config = config
            self.status.set("Campaign defaults saved. For tire data, choose Collect Data in the game and export when ready.")
        except (ValueError, OSError) as exc:
            self.messagebox.showerror("Setup", str(exc))

    def _import_case(self) -> None:
        path = self.filedialog.askopenfilename(title="Select authoritative Update6 workbook",
                                               filetypes=[("Excel workbook", "*.xlsx")])
        if not path:
            return
        try:
            case_data = import_update6(path)
            _atomic_json(self.data_root / "case_import.json", case_data)
            self.status.set("Update6 workbook imported with cell references and source hash.")
        except (OSError, ValueError, KeyError, IndexError) as exc:
            self.messagebox.showerror("Update6 import", str(exc))

    def _assumption_dialog(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("WIMNA model assumptions")
        window.geometry("720x540")
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        self.ttk.Label(frame, text="Engineering assumptions; these are not measured FH26 parameters.",
                       wraplength=680).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        values = self.assumptions
        fields = (
            ("ambient_c", "Reference ambient air (C); independent of track temp"),
            ("ambient_reference", "R4/reference ambient air (C)"),
            ("ambient_elevated", "R8/elevated ambient air (C)"),
            ("ambient_severe", "R5/severe ambient air (C)"),
            ("fuel_density_kg_per_l", "Fuel density assumption (kg/L)"),
            ("plan_c_airflow_multiplier", "Plan C airflow multiplier (assumed)"),
            ("plan_c_airflow_low", "Plan C airflow plausible low"),
            ("plan_c_airflow_high", "Plan C airflow plausible high"),
            ("traffic_airflow_multiplier", "Traffic cooling multiplier (unmeasured)"),
            ("higher_deployment_workload_multiplier", "Higher deployment workload multiplier"),
            ("thermal_repeats", "Thermal trace repetitions"),
        )
        scenario_ambient = values.get("ambient_by_scenario_c") or {}
        airflow_range = values.get("plan_c_airflow_range") or [None, None]
        initial = {**values, "ambient_reference": scenario_ambient.get("reference"),
                   "ambient_elevated": scenario_ambient.get("elevated"),
                   "ambient_severe": scenario_ambient.get("severe"),
                   "plan_c_airflow_low": airflow_range[0],
                   "plan_c_airflow_high": airflow_range[1]}
        entries = {}
        for row, (key, label) in enumerate(fields, 1):
            self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            value = initial.get(key)
            variable = self.tk.StringVar(value="" if value is None else str(value))
            entries[key] = variable
            self.ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew")
        help_row = len(fields) + 1
        self.ttk.Label(frame, text="Optional JSON can supply track-specific stint_inputs and other declared model inputs.",
                       wraplength=680).grid(row=help_row, column=0, columnspan=3, sticky="w", pady=(8, 4))

        def import_json() -> None:
            path = self.filedialog.askopenfilename(title="Select model assumptions JSON",
                                                   filetypes=[("JSON", "*.json")])
            if not path:
                return
            try:
                selected = _safe_read_json(Path(path))
                if selected is None:
                    raise ValueError("assumptions file must contain a JSON object")
                self.assumptions = {**DEFAULT_ASSUMPTIONS, **selected}
                _atomic_json(self.data_root / "model.assumptions.json", self.assumptions)
                window.destroy()
                self.status.set("Model assumptions imported and saved locally.")
            except (OSError, ValueError) as exc:
                self.messagebox.showerror("Assumptions", str(exc), parent=window)

        def save() -> None:
            try:
                numeric = {key: _float_or_none(variable.get()) for key, variable in entries.items()
                           if key != "thermal_repeats"}
                repeats_text = entries["thermal_repeats"].get().strip()
                repeats = int(repeats_text) if repeats_text else int(self.assumptions.get("thermal_repeats") or 3)
                if repeats < 1 or repeats > 60:
                    raise ValueError("thermal repetitions must be positive")
                for key in ("plan_c_airflow_multiplier", "traffic_airflow_multiplier",
                            "higher_deployment_workload_multiplier"):
                    value = numeric[key]
                    if value is None or value <= 0:
                        raise ValueError(f"{key} must be positive")
                for key in ("fuel_density_kg_per_l", "plan_c_airflow_low", "plan_c_airflow_high"):
                    value = numeric[key]
                    if value is not None and value <= 0:
                        raise ValueError(f"{key} must be positive when supplied")
                low, high = numeric["plan_c_airflow_low"], numeric["plan_c_airflow_high"]
                if low is not None and high is not None and low > high:
                    raise ValueError("Plan C airflow low exceeds high")
                if low is not None and high is not None and numeric["plan_c_airflow_multiplier"] is not None:
                    if not low <= numeric["plan_c_airflow_multiplier"] <= high:
                        raise ValueError("Plan C airflow base must be inside its plausible range")
                selected = dict(self.assumptions)
                selected.update({key: numeric[key] for key in (
                    "ambient_c", "fuel_density_kg_per_l", "plan_c_airflow_multiplier",
                    "traffic_airflow_multiplier", "higher_deployment_workload_multiplier")})
                selected["ambient_by_scenario_c"] = {
                    "reference": numeric["ambient_reference"],
                    "elevated": numeric["ambient_elevated"],
                    "severe": numeric["ambient_severe"],
                }
                selected["plan_c_airflow_range"] = [low, high]
                selected["thermal_repeats"] = repeats
                _atomic_json(self.data_root / "model.assumptions.json", selected)
                self.assumptions = selected
                self.status.set("Model assumptions saved; thermal results remain analysis-only.")
                window.destroy()
            except (ValueError, OSError) as exc:
                self.messagebox.showerror("Assumptions", str(exc), parent=window)

        button_row = help_row + 1
        self.ttk.Button(frame, text="Import assumptions JSON", command=import_json).grid(row=button_row, column=0, sticky="w", pady=(8, 0))
        self.ttk.Button(frame, text="Save assumptions", command=save).grid(row=button_row, column=1, sticky="w", pady=(8, 0))

    def _export(self) -> None:
        target = self.filedialog.askdirectory(title="Select parent folder for portable results")
        if not target:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path(target) / f"WIMNA-results-{stamp}"
        try:
            self.director.poll()
            case_data = _safe_read_json(self.data_root / "case_import.json")
            forecasts, validation, parameters = analyze_store(
                self.director.store, self.director.scheduler.config,
                case_data=case_data, assumptions=self.assumptions,
            )
            details = export_bundle(self.director.store, output,
                                    config=self.director.scheduler.config,
                                    case_data=case_data,
                                    forecasts=forecasts, validation=validation,
                                    parameters=parameters)
            self.status.set(f"Exported portable results to {output}")
            self.messagebox.showinfo("Results exported", str(output))
            if self.director.scheduler.state["phase"] == "review_export":
                self.director.scheduler.dispatch("export_complete", elapsed_min=self.director.scheduler.state["elapsed_min"])
                self.director._save()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.messagebox.showerror("Export", str(exc))

    def _poll(self) -> None:
        try:
            result = self.director.poll()
            state = result["director"]
            missing = state.get("unconfigured_targets") or []
            warning = f" | missing targets: {', '.join(missing)}" if missing else ""
            self.status.set(f"{state['state']} | {state.get('block_id') or 'campaign'} | {state['lap_progress']} | "
                            f"recorder: {result['ingest']['new_records']} new records | {state['next_action']}{warning}")
            lines = [f"Instruction: {state['instruction']}", f"Target: {state['target']}",
                     f"Run: {state['run_id'] or 'none'}", "Pause Test pauses the experiment; the simulator may continue.",
                     "", *result["messages"]]
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(lines))
            self.log.configure(state="disabled")
        except Exception as exc:  # Keep a visible diagnostic rather than losing the companion loop.
            self.status.set(f"Director error: {exc}")
        self.root.after(1000, self._poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WIMNA live companion")
    parser.add_argument("--bridge", type=Path, default=default_bridge_root())
    parser.add_argument("--data", type=Path, default=default_data_root())
    parser.add_argument("--self-test", action="store_true", help="check imports without opening Tk")
    args = parser.parse_args(argv)
    if args.self_test:
        _template_config()
        return 0
    lock = InstanceLock(args.data / "companion.lock")
    if not lock.acquire():
        return 0  # CSP may request another launch while this window already owns the bridge.
    import tkinter as tk

    config_path = args.data / "campaign.live.json"
    config = _upgrade_legacy_campaign(_safe_read_json(config_path), _template_config())
    director = LiveDirector(args.bridge, args.data, config=config)
    try:
        root = tk.Tk()
        DirectorWindow(root, director, args.data)
        root.mainloop()
        return 0
    finally:
        director.close()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
