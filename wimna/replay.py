"""Small deterministic full-campaign fixture, visibly synthetic at every layer."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_campaign
from .analysis import analyze_store
from .models import (ThermalSample, TireConfig, TireSample,
                     TireState, EnergySample, StintConfig, run_thermal_scenario,
                     case_scenario_presets, project_stint, thermal_assumption_profiles)
from .report import export_bundle
from .scheduler import CampaignScheduler
from .store import Store

BASE_PACE = {"spa": 130.0, "silverstone_gp": 108.0, "imola": 101.0, "monza_gp": 91.0}
PREFIX = {"spa": "sp", "silverstone_gp": "si", "imola": "im", "monza_gp": "mz"}
FIXTURE_DISTANCE_M = {"spa": 7004, "silverstone_gp": 5901, "imola": 4909, "monza_gp": 5793}
FIXED_START = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def _channel(value, unit: str, *, quality: str = "synthetic") -> dict:
    return {"value": value, "unit": unit, "source": "deterministic_replay_fixture", "quality": quality,
            "evidence": "SYNTHETIC"}


def _record(run_id: str, seq: int, sim_time_s: float, kind: str, data: dict, wall_offset_s: float) -> dict:
    wall = (FIXED_START + timedelta(seconds=wall_offset_s)).isoformat().replace("+00:00", "Z")
    return {"schema": 1, "run_id": run_id, "seq": seq, "sim_time_s": round(sim_time_s, 4),
            "wall_time_utc": wall, "kind": kind, "data": data}


def replay_campaign(destination: str | Path, *, config_path: str | Path | None = None) -> dict:
    """Drive every configured block through the same scheduler used live."""
    output = Path(destination).resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = load_campaign(config_path)
    scheduler = CampaignScheduler(config)
    elapsed = 0.0
    scheduler.dispatch("start", mode="SYNTHETIC", elapsed_min=elapsed)
    elapsed += 15
    scheduler.dispatch("confirm_preflight", elapsed_min=elapsed)
    reference_runs = {}
    synthetic_times: dict[str, list[float]] = {key: [] for key in config["track_order"]}
    with Store(output / "store") as store:
        for track_index, track_key in enumerate(config["track_order"]):
            if track_index:
                elapsed += 10
            for block in config["tracks"][track_key]["blocks"]:
                block_id = block["id"]
                code = block_id.split()[-1].lower()
                run_id = f"sim_{PREFIX[track_key]}_{code}"
                if block["kind"] == "validation":
                    ref_mean = sum(synthetic_times[track_key]) / len(synthetic_times[track_key])
                    predicted = {"mean_lap_time_s": round(ref_mean + 0.12, 4),
                                 "fuel_l_per_lap": 2.0,
                                 "battery_delta_j_per_lap": 0.0}
                    prediction_id = f"fixture_{PREFIX[track_key]}_v1"
                    scheduler.dispatch("freeze_prediction", prediction_id=prediction_id,
                                       model_id="synthetic_reference_mean_v1", predicted=predicted,
                                       condition={"block": block_id, "mode": "SYNTHETIC"},
                                       calibration_run_ids=[reference_runs[track_key]], elapsed_min=elapsed)
                    store.register_prediction(prediction_id, [reference_runs[track_key]],
                                              {"model_id": "synthetic_reference_mean_v1", "evidence": "SYNTHETIC"},
                                              predicted, registered_wall_s=FIXED_START.timestamp() + elapsed * 60)
                scheduler.dispatch("prepare", run_id=run_id, native_session_id=run_id,
                                   physical_stint_id=run_id, elapsed_min=elapsed)
                store.start_run(run_id, "SYNTHETIC", {
                    "physical_session_id": run_id, "started_wall_utc": FIXED_START.isoformat().replace("+00:00", "Z"),
                    "track": track_key, "block": block_id, "car_id": "synthetic_fh26_fixture",
                    "track_id": f"synthetic_{track_key}", "layout_id": "synthetic_gp",
                    "profile_hash": "synthetic-fixture-only", "setup_hash": "synthetic-fixed-setup",
                    "tire_preparation_hash": "synthetic-matched-fresh-tires",
                    "requested_initial_state": {"fuel_l": 35 if code != "f" else 45, "battery_j": 4_000_000},
                    "observed_initial_state": {"fuel_l": 35 if code != "f" else 45, "battery_j": 4_000_000},
                    "source": "deterministic_replay_fixture", "source_mode": "SYNTHETIC",
                    "program_version": config["program_version"]})
                scheduler.dispatch("verify_settings", requested={"profile": "fixture-baseline"},
                                   observed={"profile": "fixture-baseline"},
                                   control_classes={"profile": "automatic_with_readback"}, elapsed_min=elapsed)
                seq = 0
                sim_time = 0.0
                wall_offset = elapsed * 60
                # The warmup lap is retained but excluded from comparable summaries.
                warmup_time = BASE_PACE[track_key] + 9
                store.add_record(_record(run_id, seq, sim_time + warmup_time, "event", {
                    "type": "lap_complete", "lap_index": 0, "phase": "warmup", "lap_time_s": warmup_time,
                    "valid_native": True, "flags": ["warmup"], "fuel_used_l": 2.1,
                    "battery_delta_j": 0, "evidence": "SYNTHETIC", "eligible_for_comparison": False}, wall_offset + warmup_time))
                seq += 1
                sim_time += warmup_time
                elapsed += warmup_time / 60
                scheduler.dispatch("warmup_done", elapsed_min=elapsed)
                lap_count = block.get("laps", block.get("laps_min"))
                for lap in range(1, lap_count + 1):
                    base = BASE_PACE[track_key]
                    variance = ((lap % 3) - 1) * 0.08
                    effect = (0.3 if code == "c" else 0.0 if code == "r" else 0.15 if code == "f" else
                              -0.35 if code == "e" and lap <= 2 else 0.25 if code == "e" else
                              0.22 if code == "m" else 0.12)
                    lap_time = round(base + variance + effect, 3)
                    fuel_used = 1.9 if code == "m" else 2.0
                    battery_delta = (-500_000 if code == "e" and lap <= 2 else 200_000 if code == "e" else 0)
                    for index in range(25):
                        fraction = (index + 1) / 25
                        sample_time = sim_time + lap_time * fraction
                        speed = 58 + 12 * (index % 3)
                        data = {
                            "distance_m": _channel(FIXTURE_DISTANCE_M[track_key] * fraction, "m"),
                            "speed_m_s": _channel(speed, "m/s"),
                            "throttle": _channel(0.65 if index % 4 else 0.0, "fraction"),
                            "brake": _channel(0.2 if index % 4 == 0 else 0.0, "fraction"),
                            "fuel_l": _channel((45 if code == "f" else 35) - (lap - 1 + fraction) * fuel_used, "L"),
                            "battery_j": _channel(4_000_000 + (lap - 1 + fraction) * battery_delta, "J"),
                            "tire_wear_fl": _channel(0.002 * (lap - 1 + fraction), "fraction"),
                            "tire_temp_fl_c": _channel(83 + fraction, "deg C"),
                            "motor_temp_c": _channel(None, "deg C", quality="unavailable"),
                        }
                        store.add_record(_record(run_id, seq, sample_time, "sample", data,
                                                 wall_offset + sample_time))
                        seq += 1
                    sim_time += lap_time
                    elapsed += lap_time / 60
                    lap_event = {
                        "type": "lap_complete", "lap_index": lap, "phase": "program",
                        "lap_time_s": lap_time, "valid_native": True, "flags": [], "eligible_for_comparison": True,
                        "fuel_used_l": fuel_used, "battery_delta_j": battery_delta,
                        "tire_state": {wheel: {"wear_fraction": round(0.002 * lap, 5)} for wheel in ("fl", "fr", "rl", "rr")},
                        "evidence": "SYNTHETIC"}
                    store.add_record(_record(run_id, seq, sim_time, "event", lap_event, wall_offset + sim_time))
                    seq += 1
                    scheduler.dispatch("lap", lap_uid=f"{run_id}:{lap}", native_session_id=run_id,
                                       lap_time_s=lap_time, quality_flags=[], accepted=True,
                                       elapsed_min=elapsed)
                    if code == "e" and lap == 2:
                        scheduler.dispatch("verify_map_transition", requested={"energy_map": "fixture-recovery"},
                                           observed={"energy_map": "fixture-recovery"},
                                           control_classes={"energy_map": "automatic_with_readback"},
                                           elapsed_min=elapsed)
                    if code == "r":
                        synthetic_times[track_key].append(lap_time)
                if code == "c":
                    scheduler.dispatch("confirm_profile", mode="FRZ_AUTO", observed_mode="FRZ_AUTO",
                                       profile_hash="synthetic-fixture-only", elapsed_min=elapsed)
                if code == "r":
                    reference_runs[track_key] = run_id
                if code == "v":
                    store.attach_validation(prediction_id, run_id)
                scheduler.dispatch("box", elapsed_min=elapsed)
                scheduler.dispatch("analyze", elapsed_min=elapsed)
                store.save_campaign("synthetic-full", scheduler.to_dict())
                elapsed += 1.0
                if scheduler.state["phase"] == "next_block":
                    scheduler.dispatch("next_block", elapsed_min=elapsed)
        scheduler.dispatch("export_complete", elapsed_min=elapsed)
        store.save_campaign("synthetic-full", scheduler.to_dict())
        forecasts, parameters = _synthetic_forecasts()
        validation = _synthetic_validation(store, scheduler)
        _write_json = lambda path, item: Path(path).write_text(__import__("json").dumps(item, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        _write_json(output / "scheduler_events.json", scheduler.events)
        _write_json(output / "scheduler_state.json", scheduler.state)
        _write_json(output / "forecasts.json", forecasts)
        _write_json(output / "validation.json", validation)
        # The portable sample follows the same analysis path as a live export.
        # Top-level forecasts.json remains a separate illustrative model fixture.
        analyzed, checked, analyzed_parameters = analyze_store(store, config, assumptions={
            "ambient_by_scenario_c": {"reference": 25.0, "elevated": 28.0, "severe": 31.0},
            "fuel_density_kg_per_l": 0.75,
            "plan_c_airflow_multiplier": 1.10,
            "plan_c_airflow_range": [0.95, 1.20],
            "traffic_airflow_multiplier": 0.75,
            "higher_deployment_workload_multiplier": 1.08,
        })
        exported = export_bundle(store, output / "results", config=config,
                                 forecasts=analyzed, validation=checked, parameters=analyzed_parameters)
        return {"elapsed_minutes": round(elapsed, 3), "scheduler_phase": scheduler.state["phase"],
                "block_results": len(scheduler.state["block_results"]), "counts": store.counts(),
                "results": exported, "mode": "SYNTHETIC"}


def _synthetic_forecasts() -> tuple[dict, dict]:
    """Illustrative assumptions only; never used as measured car parameters."""
    profiles = thermal_assumption_profiles()
    trace = [ThermalSample(5.0, 0.65 + 0.1 * (index % 3), 42 + 8 * (index % 4), 25.0)
             for index in range(20)]
    grid = []
    for stress, profile in profiles.items():
        ambient_by_scenario = {"reference_R4": 25.0, "elevated_R8": 28.0, "severe_R5": 31.0}
        for scenario in case_scenario_presets():
            scenario = replace(scenario, ambient_c=ambient_by_scenario[scenario.name],
                               evidence=("ASSUMED", "SYNTHETIC"))
            for package in ("baseline", "Plan C"):
                for energy in ("baseline", "higher deployment"):
                    for exposure, exposure_factor in (("clear air", 1.0), ("traffic assumption", 0.75)):
                        airflow = exposure_factor * (1.10 if package == "Plan C" else 1.0)
                        workload = 1.08 if energy == "higher deployment" else 1.0
                        adjusted = [replace(sample, workload_fraction=sample.workload_fraction * workload)
                                    for sample in trace]
                        result = run_thermal_scenario(adjusted, profile,
                                                      replace(scenario, airflow_factor=airflow), repeats=10)
                        grid.append({"thermal_stress_profile": stress, "scenario": scenario.name,
                                     "package": package, "energy_strategy": energy, "exposure": exposure,
                                     "package_airflow_multiplier": 1.10 if package == "Plan C" else 1.0,
                                     "package_multiplier_range": [0.95, 1.20] if package == "Plan C" else [1.0, 1.0],
                                     "result": asdict(result), "evidence": ["ASSUMED", "PHYSICS_DERIVED", "SYNTHETIC"]})
    tire_model = TireConfig(1e-7, 0.0002, 0.05, 1.0, 0.4,
                            ((0.0, 0.0), (0.1, 0.15), (0.5, 1.2), (0.8, 4.0), (1.0, 8.0)),
                            ((30.0, 1.0), (80.0, 0.0), (95.0, 0.0), (120.0, 1.5)))
    tires = {wheel: TireState(0.01, 83.0) for wheel in ("fl", "fr", "rl", "rr")}
    tire_samples = {wheel: [TireSample(5.0, 55.0, 1.0, 0.02, 25.0, 29.0) for _ in range(20)]
                    for wheel in tires}
    stint = project_stint(StintConfig(
        lap_count=20, base_lap_time_s=91.0, starting_fuel_l=45.0,
        fuel_burn_l_per_lap=2.0, fuel_density_kg_per_l=0.75,
        fuel_seconds_per_kg=0.02, initial_tires=tires,
        tire_samples_per_lap=tire_samples, tire_model=tire_model,
        starting_battery_j=4_000_000, usable_battery_j=5_000_000,
        energy_samples_per_lap=[EnergySample(10.0, 20_000), EnergySample(10.0, -20_000)],
        energy_pace_delta_s_per_lap=0.0, thermal_model=profiles["base"],
        thermal_samples_per_lap=trace, derate_seconds_at_full_reduction=2.0,
        restriction_speed_response=0.2, restriction_workload_response=0.2,
        restriction_energy_response=0.2, evidence=("SYNTHETIC", "ASSUMED")))
    forecasts = {"status": "computed", "model_version": "fixture_assumptions_v1",
                 "calculation_mode": "scenario_projection",
                 "physical_effect_status": "analysis_only", "thermal_grid": grid,
                 "stint_20_laps": asdict(stint),
                 "limitations": ["Synthetic fixture cannot calibrate the installed FH26 car.",
                                 "Plan C airflow factor is an explicit assumption, not a measured package gain.",
                                 "Traffic cooling factor is unmeasured.",
                                 "No full race or failure probability is inferred."]}
    parameters = {"model_version": "fixture_assumptions_v1", "evidence": ["ASSUMED", "SYNTHETIC"],
                  "thermal_profiles": {key: {name: asdict(value) for name, value in profile.parameter_records.items()}
                                       for key, profile in profiles.items()},
                  "other_assumptions": {"ambient_c_by_scenario": {"reference_R4": 25.0, "elevated_R8": 28.0,
                                                                   "severe_R5": 31.0}, "Plan C airflow multiplier": 1.10,
                                        "Plan C plausible airflow multiplier": [0.95, 1.20],
                                        "traffic airflow multiplier": 0.75,
                                        "fuel density kg/L": 0.75,
                                        "fuel sensitivity s/kg": 0.02}}
    return forecasts, parameters


def _synthetic_validation(store: Store, scheduler: CampaignScheduler) -> dict:
    items = []
    for block_id, frozen in scheduler.state["predictions"].items():
        result = next((value for value in scheduler.state["block_results"] if value["block_id"] == block_id), None)
        if result is None:
            continue
        lap_times = [record["data"].get("lap_time_s") for record in store.iter_records(result["run_id"], "event")
                     if record["data"].get("type") == "lap_complete" and record["data"].get("phase") == "program"]
        actual = sum(lap_times) / len(lap_times) if lap_times else None
        prediction = frozen["predicted"]["mean_lap_time_s"]
        items.append({"block_id": block_id, "fit_run_ids": frozen["calibration_run_ids"],
                      "validation_run_id": result["run_id"], "predicted_mean_lap_time_s": prediction,
                      "observed_mean_lap_time_s": actual,
                      "residual_s": None if actual is None else round(actual - prediction, 6),
                      "evidence": "SYNTHETIC", "held_out": True})
    return {"status": "computed", "results": items, "evidence": "SYNTHETIC"}
