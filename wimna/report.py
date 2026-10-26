"""Reproducible offline exports with evidence and physical-effect labels."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import statistics
from pathlib import Path

from .store import Store


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_cell(value):
    """Keep null empty and neutralize spreadsheet formula prefixes in labels."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _write_collection_tables(store: Store, output: Path, collection: dict) -> None:
    """Export an easy-to-read wide table while retaining full raw JSONL evidence."""
    context = ("run_id", "evidence_mode", "physical_session_id", "collection_id",
               "collection_stint_id", "seq", "sim_time_s", "controller_observed",
               "car_id", "track_id", "layout_id", "planned_tire_label",
               "requested_tyre_compound", "observed_tyre_compound", "condition_label",
               "setup_label_requested", "setup_label_source",
               "session_mode_observed", "extended_physics_observed",
               "in_pitlane", "in_pit")
    channels: set[str] = set()
    channel_metadata: dict[str, dict] = {}
    for item in store.iter_collection_samples():
        for name, raw in (item["record"].get("data") or {}).items():
            if not isinstance(raw, dict) or "value" not in raw:
                continue
            channels.add(name)
            meta = channel_metadata.setdefault(name, {"units": set(), "sources": set(),
                                                       "quality_counts": {}, "non_null_count": 0})
            if raw.get("unit") is not None:
                meta["units"].add(raw["unit"])
            if raw.get("source") is not None:
                meta["sources"].add(raw["source"])
            quality = str(raw.get("quality") or "unlabeled")
            meta["quality_counts"][quality] = meta["quality_counts"].get(quality, 0) + 1
            if raw.get("value") is not None:
                meta["non_null_count"] += 1
    ordered_channels = sorted(channels)
    with (output / "collection_samples.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([*context, *ordered_channels])
        for item in store.iter_collection_samples():
            raw = item["record"].get("data") or {}
            values = [item.get(name) for name in context]
            values += [value.get("value") if isinstance(value := raw.get(name), dict)
                       and value.get("quality") == "available" else None
                       for name in ordered_channels]
            writer.writerow([_csv_cell(value) for value in values])
    _write_json(output / "collection_channels.json", {
        "description": "Wide CSV includes only available-quality values. This file aggregates units, sources and quality counts; gzip JSONL retains per-sample metadata and is authoritative for modeling.",
        "channels": {name: {"units": sorted(meta["units"]),
                            "sources": sorted(meta["sources"]),
                            "quality_counts": meta["quality_counts"],
                            "non_null_count": meta["non_null_count"]}
                     for name, meta in sorted(channel_metadata.items())},
        "missing_is_zero": False,
        "wide_csv_available_quality_only": True,
    })
    columns = ("run_id", "physical_session_id", "collection_stint_id", "track_id",
               "layout_id", "compound_requested", "compound_observed", "condition_label",
               "setup_label_requested", "setup_label_source", "controller_observed",
               "evidence", "lap_index", "lap_time_s",
               "sim_start_s", "sim_end_s", "native_valid", "native_cuts",
               "comparable_for_pace", "usable_for_exploratory_pace", "pace_use",
               "telemetry_sample_count", "sample_coverage", "fuel_l", "battery_state_native",
               "tire_state_by_wheel", "quality_reasons", "quality_caveats", "flags")
    with (output / "collection_laps.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for stint in collection.get("stints") or []:
            for lap in stint.get("laps") or []:
                row = {**stint, **lap}
                writer.writerow([_csv_cell(row.get(name)) for name in columns])


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _spread(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else None


def _display(value, unit: str = "", digits: int = 3) -> str:
    return "unavailable" if value is None else f"{value:.{digits}f}{unit}"


def _usable_lap(data: dict, annotation: dict | None = None) -> tuple[bool, list[str]]:
    reasons = list(data.get("flags") or [])
    phase = annotation["phase"] if annotation else data.get("phase")
    if phase != "program":
        reasons.append(phase or "unclassified_phase")
    if annotation:
        reasons += annotation["quality_flags"]
        if not annotation["eligible"]:
            reasons.append(annotation["reason"] or "ineligible")
    elif data.get("eligible_for_comparison") is not True:
        reasons.append("eligibility_unverified")
    if data.get("valid_native") is False:
        reasons.append("native_invalid")
    if data.get("lap_time_s") is None:
        reasons.append("lap_time_unavailable")
    return not reasons, sorted(set(reasons))


def summarize_run(store: Store, run: dict) -> dict:
    run_id = run["run_id"]
    assignment = store.get_run_assignment(run_id) or {}
    manifest = run["manifest"]
    ai_thermal = manifest.get("selection") == "ai_thermal"
    collection = manifest.get("selection") == "collection"
    track = assignment.get("track") or manifest.get("track") or manifest.get("track_id")
    track_source = ("director_assignment" if assignment.get("track") else
                    "manifest.track" if manifest.get("track") else
                    "manifest.track_id" if manifest.get("track_id") else "unavailable")
    annotations = {item["lap_event_seq"]: item for item in store.iter_lap_annotations(run_id)}
    laps = []
    missing_channels: set[str] = set()
    sample_times = []
    sequence_gaps = 0
    previous_seq = None
    for record in store.iter_records(run_id):
        if previous_seq is not None and record["seq"] > previous_seq + 1:
            sequence_gaps += record["seq"] - previous_seq - 1
        previous_seq = record["seq"]
        if record["kind"] == "sample":
            if record.get("sim_time_s") is not None:
                sample_times.append(record["sim_time_s"])
            for name, value in record["data"].items():
                if isinstance(value, dict) and value.get("value") is None:
                    missing_channels.add(name)
        if record["kind"] == "event" and record["data"].get("type") == "lap_complete":
            data = record["data"]
            annotation = annotations.get(record["seq"])
            included, reasons = _usable_lap(data, annotation)
            if ai_thermal or collection:
                included = False
                reasons = sorted(set([*reasons, "collection_not_campaign_pace" if collection
                                      else "supplementary_ai_thermal_not_campaign_pace"]))
            laps.append({"lap_index": data.get("lap_index"), "phase": annotation["phase"] if annotation else data.get("phase"),
                         "lap_time_s": data.get("lap_time_s"), "included": included,
                         "exclusion_reasons": reasons,
                         "fuel_used_l": data.get("fuel_used_l"),
                         "battery_delta_j": data.get("battery_delta_j"),
                         "tire_state": data.get("tire_state")})
    included = [lap for lap in laps if lap["included"]]
    pace = [lap["lap_time_s"] for lap in included if isinstance(lap["lap_time_s"], (int, float))]
    fuel = [lap["fuel_used_l"] for lap in included if isinstance(lap["fuel_used_l"], (int, float))]
    battery = [lap["battery_delta_j"] for lap in included if isinstance(lap["battery_delta_j"], (int, float))]
    positive_dt = [b - a for a, b in zip(sample_times, sample_times[1:]) if b > a]
    effective_rate_hz = 1 / statistics.median(positive_dt) if positive_dt else None
    return {"run_id": run_id, "track": track, "track_source": track_source,
            "track_id": manifest.get("track_id"), "layout_id": manifest.get("layout_id"),
            "selection": manifest.get("selection"), "campaign_counted": not (ai_thermal or collection),
            "block": assignment.get("block") or manifest.get("block"), "evidence": run["mode"],
            "calculation_mode": "observed_summary", "physical_effect_status": "observed_trace" if run["mode"] == "MEASURED" else "synthetic_only",
            "laps_total": len(laps), "laps_included": len(included), "laps_excluded": len(laps) - len(included),
            "pace_mean_s": _mean(pace), "pace_spread_s": _spread(pace),
            "fuel_mean_l_per_lap": _mean(fuel), "battery_mean_delta_j_per_lap": _mean(battery),
            "effective_sample_rate_hz": effective_rate_hz, "sequence_gaps": sequence_gaps,
            "missing_channels": sorted(missing_channels), "laps": laps}


def export_bundle(store: Store, destination: str | Path, *, config: dict,
                  case_data: dict | None = None, forecasts: dict | None = None,
                  validation: dict | None = None, parameters: dict | None = None) -> dict:
    output = Path(destination).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"results destination is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    runs = list(store.iter_runs())
    summaries = [summarize_run(store, run) for run in runs]
    track_order = list(config.get("track_order") or [])
    track_positions = {track: index for index, track in enumerate(track_order)}
    block_positions = {
        track: {block["id"]: index for index, block in enumerate(
            (config.get("tracks", {}).get(track) or {}).get("blocks", []))}
        for track in track_order
    }
    for summary in summaries:
        track = summary["track"]
        summary["campaign_block"] = (summary["campaign_counted"] and track in block_positions
                                     and summary["block"] in block_positions[track])
        summary["track_display_name"] = (
            (config.get("tracks", {}).get(track) or {}).get("display_name") or track
            if summary["campaign_block"] else track)
    summaries.sort(key=lambda summary: (
        0 if summary["campaign_block"] else 1,
        track_positions.get(summary["track"], len(track_order)) if summary["campaign_block"] else len(track_order),
        block_positions.get(summary["track"], {}).get(summary["block"], 99),
        summary["run_id"],
    ))
    _write_json(output / "campaign.json", config)
    _write_json(output / "case_import.json", case_data or {"authority": "unavailable", "evidence": "CASE", "reason": "Update6 workbook not imported"})
    _write_json(output / "parameters.json", parameters or {"status": "unavailable", "parameters": [], "missing_is_zero": False})
    _write_json(output / "forecasts.json", forecasts or {"status": "unavailable", "results": [], "calculation_mode": "analysis_only"})
    _write_json(output / "collection_stints.json", (forecasts or {}).get("collection") or
                {"status": "unavailable", "stints": [], "tire_comparisons": [], "human_pace_anchors": []})
    _write_json(output / "validation.json", validation or {"status": "unavailable", "results": []})
    _write_json(output / "frozen_predictions.json", list(store.iter_predictions()))
    _write_json(output / "campaign_states.json", list(store.iter_campaign_states()))
    _write_json(output / "lap_annotations.json", list(store.iter_lap_annotations()))
    _write_json(output / "run_assignments.json", list(store.iter_run_assignments()))
    _write_json(output / "run_summaries.json", summaries)
    _write_collection_tables(store, output, (forecasts or {}).get("collection") or {})
    for run in runs:
        run_id = run["run_id"]
        folder = output / "runs" / run_id
        manifest = dict(run["manifest"])
        manifest.update({"run_id": run_id, "evidence": run["mode"]})
        manifest["director_assignment"] = store.get_run_assignment(run_id)
        _write_json(folder / "manifest.json", manifest)
        sample_stream = None
        raw_stream = None
        chunk_index = 0
        sample_count = 0
        events = []
        capabilities = []
        try:
            for record in store.iter_records(run_id):
                if record["kind"] == "sample":
                    if sample_count % 1000 == 0:
                        if sample_stream is not None:
                            sample_stream.close()
                            raw_stream.close()
                        chunk_index += 1
                        name = f"telemetry-{chunk_index:06d}.jsonl.gz"
                        raw_stream = (folder / name).open("wb")
                        compressed = gzip.GzipFile(fileobj=raw_stream, mode="wb", filename="", mtime=0)
                        sample_stream = io.TextIOWrapper(compressed, encoding="utf-8", newline="\n")
                    sample_stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
                    sample_count += 1
                elif record["kind"] == "event":
                    events.append(record)
                else:
                    capabilities.append(record)
        finally:
            if sample_stream is not None:
                sample_stream.close()
                raw_stream.close()
        with (folder / "events.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for record in events:
                stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
        _write_json(folder / "capabilities.json", capabilities)
    lines = ["# WIMNA data collection and modeling", "", "Evidence key: CASE, MEASURED, PHYSICS_DERIVED, ASSUMED, SYNTHETIC.",
             "Calculation mode and physical-effect status are separate fields in the JSON exports.", ""]
    if track_order:
        names = [(config.get("tracks", {}).get(track) or {}).get("display_name") or track
                 for track in track_order]
        lines += ["Campaign order: " + " → ".join(names) + ".", ""]
    if not runs:
        lines.append("No run data are available. No measurement or calibration is claimed.")
    for summary in summaries:
        label = summary["evidence"]
        display_track = summary["track_display_name"] or "Unknown track"
        if summary["track_source"] == "manifest.track_id" and summary["layout_id"]:
            display_track += f" / {summary['layout_id']}"
        pace = f"{summary['pace_mean_s']:.3f} s" if summary["pace_mean_s"] is not None else "unavailable"
        spread = f"{summary['pace_spread_s']:.3f} s" if summary["pace_spread_s"] is not None else "unavailable"
        fuel = f"{summary['fuel_mean_l_per_lap']:.3f} L/lap" if summary["fuel_mean_l_per_lap"] is not None else "unavailable"
        lines += [f"## {display_track} — {summary['block'] or summary['run_id']} [{label}]", "",
                  "Legacy AI thermal run: data appear in the physical-stint inventory; no campaign laps counted." if summary["selection"] == "ai_thermal" else
                  "Collection run: AI and human samples share one physical-stint inventory; no campaign laps counted." if summary["selection"] == "collection" else
                  "Campaign run; block eligibility is shown below.",
                  f"Campaign-accepted laps: {summary['laps_included']}/{summary['laps_total']}; block pace {pace}; lap spread {spread}; fuel {fuel}.",
                  f"Effective recorded rate: {summary['effective_sample_rate_hz'] or 'unavailable'} Hz; sequence gaps: {summary['sequence_gaps']}.",
                  f"Unavailable channels: {', '.join(summary['missing_channels']) or 'none recorded as null'}.", ""]
    collection = (forecasts or {}).get("collection") or {}
    lines += ["## Physical stint data", "",
              "Every recorded stint is retained, including AI and human control in the same run. Controller identity is a covariate. Tire labels are requests until native compound readback is available. `collection_samples.csv` and `collection_laps.csv` provide convenience tables; `collection_channels.json` lists channel units, sources and quality counts. The raw `telemetry-*.jsonl.gz` files retain per-sample metadata and are authoritative for modeling. Empty CSV cells do not mean zero.", ""]
    if not collection.get("stints"):
        lines += ["No collection stints have been indexed yet.", ""]
    else:
        lines += ["| Run / stint | Tire requested → observed | Controller samples AI / human / unknown | Ambient / track median | Pace laps: clean AI / exploratory AI / clean human | Thermal model |",
                  "|---|---|---:|---:|---:|---|",]
        for stint in collection["stints"]:
            counts = stint.get("controller_sample_counts") or {}
            laps = stint.get("laps") or []
            ai_laps = sum(lap.get("comparable_for_pace") and lap.get("controller_observed") == "AI" for lap in laps)
            exploratory_ai = sum(lap.get("pace_use") == "exploratory_ai_native_invalid" for lap in laps)
            human_laps = sum(lap.get("comparable_for_pace") and lap.get("controller_observed") == "HUMAN" for lap in laps)
            air = (stint.get("ambient_air_c") or {}).get("median")
            track = (stint.get("track_surface_c") or {}).get("median")
            lines.append(
                f"| `{stint['run_id']}` / `{stint['collection_stint_id']}` [{stint.get('evidence')}] "
                f"| {stint.get('compound_requested') or 'unavailable'} → {stint.get('compound_observed') or 'unavailable'} "
                f"| {counts.get('AI', 0)} / {counts.get('HUMAN', 0)} / {counts.get('UNKNOWN', 0)} "
                f"| {_display(air, ' °C', 1)} / {_display(track, ' °C', 1)} "
                f"| {ai_laps} / {exploratory_ai} / {human_laps} | {(stint.get('thermal') or {}).get('status', 'unavailable')} |")
        lines += ["", "The full per-wheel native wear, temperature, pressure, load and slip summaries, fuel, energy and lap quality records are in `collection_stints.json`. Native wear is not assumed to mean remaining tire life. An AI lap with native invalidity and zero reported cuts is exploratory only; the Plan C force would invalidate its native lap when applied, while other causes require separate review. A native-valid lap with no recorded flags is not independently verified contact- or spin-free because those fault channels are unavailable. Custom thermal temperatures are analysis-only.", ""]
        prototype = collection.get("package_prototype_analysis") or {}
        lines += ["### Plan C simulator prototype pairs", ""]
        if prototype.get("pairs"):
            lines += ["| Track / layout | Baseline AI median | Plan C AI median | Plan C − baseline | Evidence |",
                      "|---|---:|---:|---:|---|",]
            for pair in prototype["pairs"]:
                lines.append(
                    f"| {pair.get('track_id')} / {pair.get('layout_id')} "
                    f"| {_display((pair.get('baseline') or {}).get('pace', {}).get('median_s'), ' s')} "
                    f"| {_display((pair.get('plan_c') or {}).get('pace', {}).get('median_s'), ' s')} "
                    f"| {_display(pair.get('delta_plan_c_minus_baseline_s'), ' s')} "
                    f"| exploratory simulated intervention; physical effect unverified |")
            lines.append("")
        else:
            lines += ["No matched baseline and Plan C AI stints have passed the recorded identity, lap, and starting-condition checks.", ""]
        lines += ["The prototype requests bounded aerodynamic forces; its cooling change is analysis-only. Native-invalid, zero-cut Plan C crossings remain exploratory simulator observations. A worker request does not prove the force affected FH26. The full lap inventory, rejected pairs, parameter hashes, and conditional human transfers are in `collection_stints.json`. No case-packet gain is added to the observed delta.", ""]
        comparisons = collection.get("tire_comparisons") or []
        lines += ["### Tire stint contrasts", ""]
        if comparisons:
            lines += ["| Controller | Tire pair | Observed median lap delta, right − left | Match status | Open issues |",
                      "|---|---|---:|---|---|",]
            for item in comparisons:
                lines.append(f"| {item['controller_observed']} | {item['left_compound']} → {item['right_compound']} "
                             f"| {_display(item.get('observed_median_delta_right_minus_left_s'), ' s')} "
                             f"| {item['status']} | {'; '.join(item.get('match_issues') or []) or 'none under declared exploratory tolerances'} |")
            lines += ["", "These are observed contrasts between stints, not causal tire gains or a race-time ranking. The calculation treats laps within a stint as repeated observations, not independent stints.", ""]
        else:
            lines += ["No pair of tire stints has usable pace laps under the same observed controller yet.", ""]
        anchors = collection.get("human_pace_anchors") or []
        lines += ["### Human competitive pace anchors", ""]
        if anchors:
            for item in anchors:
                lines.append(f"- {item.get('track_id') or 'unknown'} / {item.get('layout_id') or 'unknown'}, "
                             f"{item.get('compound_observed')}: best {_display(item.get('best_clean_lap_s'), ' s')}, "
                             f"median {_display(item.get('median_clean_lap_s'), ' s')} "
                             f"over {item.get('clean_lap_count')} clean observed laps; runs {', '.join(item.get('run_ids') or [])}.")
            lines.append("")
        else:
            lines += ["No clean human lap with a verified tire compound is available yet.", ""]
        scenarios = collection.get("anchored_tire_scenarios") or []
        lines += ["### Conditional human tire scenarios [MEASURED, ASSUMED, PHYSICS_DERIVED]", ""]
        if scenarios:
            for item in scenarios:
                lines.append(
                    f"- {item['baseline_compound']} → {item['candidate_compound']}: observed AI median delta "
                    f"{_display(item.get('ai_observed_relative_delta_s'), ' s')}; human baseline median "
                    f"{_display(item.get('human_baseline_median_s'), ' s')}; candidate human median "
                    f"{_display(item.get('candidate_human_median_s'), ' s')}. "
                    f"Validation: {item['validation_status']}. Open controls: "
                    f"{'; '.join(item.get('unverified_controls') or []) or 'none recorded'}."
                )
            lines.append("")
        else:
            lines += ["No AI tire contrast can yet be applied to a human baseline under recorded matching conditions.", ""]
        lines += ["The AI-to-human tire response transfer is a model assumption, never a measured human lap. A separately registered, held-out human stint is required for validation.", ""]
    case_verified = case_data is not None and case_data.get("authority") == "Update6 workbook"
    if not case_verified:
        lines += ["## Case evidence", "", "Update6 workbook is unavailable; case ranking and Plan C screening are withheld.", ""]
    else:
        case_summary = case_data.get("summary") or {}
        screen = config.get("case_screening") or {}
        race_laps = screen.get("race_laps_assumed")
        introduction = screen.get("plan_c_introduction_round")
        remaining = screen.get("rounds_remaining")
        upgraded_rounds = max(0, 17 - introduction) if isinstance(introduction, int) else None
        upgraded_laps = upgraded_rounds * race_laps if isinstance(race_laps, int) and upgraded_rounds is not None else None
        lines += ["## Case evidence [CASE]", "",
                  f"Imported source SHA-256: `{case_data.get('source_sha256', 'unavailable')}`.",
                  f"AVR: {case_summary.get('position', 'unavailable')} and {case_summary.get('points', 'unavailable')} points; historical mean race-pace deficit {_display(case_summary.get('mean_race_pace_deficit_s_per_lap'), ' s/lap', 5)}; three reported DNFs are observations, not a calibrated reliability curve.",
                  f"Plan C packet estimate: {(case_summary.get('options') or {}).get('C', {}).get('expected_gain_raw', 'unavailable')} s/lap, introduction {(case_summary.get('options') or {}).get('C', {}).get('introduction_raw', 'unavailable')}. This is a case screening range.",
                  f"At the editable {race_laps or 'unavailable'}-lap assumption and introduction round {introduction or 'unavailable'}, potential upgraded laps: {upgraded_laps if upgraded_laps is not None else 'unavailable'} of {(remaining * race_laps) if isinstance(remaining, int) and isinstance(race_laps, int) else 'unavailable'} remaining. This is exposure, not a measured gain.",
                  f"Plan C remaining development funding reduction: {_display(screen.get('plan_c_funding_reduction_fraction'), ' fraction', 2)} from the supplied case brief context; no cost or probability has been inferred.",
                  "Case expected gains are screening inputs, not Tyler's measured before/after performance.", ""]
    if forecasts is None:
        lines += ["## Longer-stint and thermal projections", "", "Unavailable until required inputs or explicit assumption profiles are selected.", ""]
    else:
        lines += ["## Calibrated metrics and projections", "",
                  f"Model: `{forecasts.get('model_version', 'unavailable')}`; calculation mode: `{forecasts.get('calculation_mode', 'unavailable')}`; physical effect: `{forecasts.get('physical_effect_status', 'unavailable')}`.", ""]
        forecast_tracks = forecasts.get("tracks", {})
        ordered_forecast_tracks = list(dict.fromkeys([*track_order, *forecast_tracks]))
        for track in ordered_forecast_tracks:
            if track not in forecast_tracks:
                continue
            result = forecast_tracks[track]
            reference = result.get("reference") or {}
            contrast = result.get("fuel_contrast") or {}
            energy = result.get("energy_cycle") or {}
            tires = result.get("tires") or {}
            stint = result.get("longer_stint") or {}
            evidence = ", ".join(reference.get("evidence") or []) or "unavailable"
            tire_parts = [f"{wheel} wear change {_display(state.get('wear_change_fraction'), ' fraction', 5)}"
                          for wheel, state in (tires.get("per_tire") or {}).items()]
            tire_text = ", ".join(tire_parts) if tire_parts else "state unavailable"
            race = result.get("race_time") or {}
            race_text = (f"{_display(race.get('total_time_s'), ' s')} for {race.get('race_laps')} laps, "
                         f"{race.get('pit_stops')} full stops and {_display(race.get('full_pit_loss_s'), ' s/stop')} pit loss"
                         if race.get("status") == "computed" else
                         "unavailable (" + ", ".join(race.get("missing_inputs") or ["explicit race and reserve rules or modeled stint coverage missing"]) + ")")
            display_name = (config.get("tracks", {}).get(track) or {}).get("display_name") or track
            lines += [f"### {display_name} [{evidence}]", "",
                      f"Reference: {reference.get('lap_count', 0)} comparable laps; pace {_display(reference.get('pace_mean_s'), ' s')}; spread {_display(reference.get('pace_spread_s'), ' s')}; fuel {_display(reference.get('fuel_mean_l_per_lap'), ' L/lap')}.",
                      f"Fuel contrast: `{contrast.get('status', 'unavailable')}`; local sensitivity {_display(contrast.get('seconds_per_litre'), ' s/L')}; exploratory two-lap check only.",
                      f"Energy cycle: `{energy.get('status', 'unavailable')}`; start {_display(energy.get('starting_battery_j'), ' J', 0)}, end {_display(energy.get('ending_battery_j'), ' J', 0)}, unresolved debt {_display(energy.get('remaining_debt_j'), ' J', 0)}. Any time benefit with unresolved debt is conditional.",
                      f"Tires: {tire_text}.",
                      f"Longer stint: `{stint.get('status', 'unavailable')}`; total {_display((stint.get('result') or {}).get('total_time_s'), ' s')}. Projection outside the short-run validation domain.",
                      f"Conditional race time: {race_text}. Analysis only; no race-time winner claimed.", ""]
        fixture_stint = forecasts.get("stint_20_laps")
        if fixture_stint:
            lines += [f"Illustrative 20-lap stint [SYNTHETIC, ASSUMED, PHYSICS_DERIVED]: {_display(fixture_stint.get('total_time_s'), ' s')}; analysis only.", ""]
        thermal = forecasts.get("thermal_grid") or []
        if thermal:
            thermal_evidence = "CASE, ASSUMED, PHYSICS_DERIVED" if case_verified else "ASSUMED, PHYSICS_DERIVED"
            lines += [f"### Thermal scenario sweep [{thermal_evidence}]", "",
                      "Track temperatures are case-informed anchors; ambient air is independent. Peaks are custom modeled temperatures. Restrictions have analysis-only physical status unless separately verified.", "",
                      "| Track | Scenario | Package | Peak electrical node | Above derate | Requested derating | Physical effect |",
                      "|---|---|---|---:|---:|---:|---|"]
            shown = 0
            for item in thermal:
                exposure = item.get("cooling_exposure", item.get("exposure"))
                if (item.get("thermal_stress_profile") != "base" or item.get("energy_strategy") != "baseline" or
                        exposure not in ("clear air", "clear_air") or
                        (item.get("package") == "Plan C" and item.get("package_assumption") != "base")):
                    continue
                result = item.get("result") or {}
                peaks = result.get("peak_temperatures_c") or {}
                lines.append("| {track} | {scenario} | {package} | {peak} | {above} | {duration} | {effect} |".format(
                    track=item.get("track", "synthetic fixture"),
                    scenario=item.get("scenario", "?"), package=item.get("package", "?"),
                    peak=_display(peaks.get("electrical_lumped"), " °C", 2),
                    above=_display(result.get("time_above_derate_s"), " s", 1),
                    duration=_display(result.get("derating_duration_s"), " s", 1),
                    effect=item.get("physical_effect_status", result.get("physical_effect_status", "unavailable"))))
                shown += 1
            if not shown:
                lines.append("| — | Input incomplete | — | unavailable | unavailable | unavailable | analysis_only |")
            lines += ["", f"Full grid: {len(thermal)} combinations in forecasts.json. Plan C airflow and traffic factors are assumptions, not measured gains.", ""]
        ai_checks = (forecasts.get("ai_thermal") or {}).get("runs") or []
        if ai_checks:
            lines += ["### Supplementary AI thermal checks", "",
                      "AI-controlled intervals can supply a measured driving workload. Native AI lap invalidity does not discard the trace. Custom electrical and cooling nodes, thresholds and modeled derating remain analysis-only. These runs do not enter Tyler's pace, validation or campaign totals.", ""]
            for check in ai_checks:
                trace = check.get("observed_trace") or {}
                environment = check.get("scenario_inputs") or {}
                lines += [f"#### Run `{check.get('run_id')}` — {check.get('track_id') or 'unknown track'} / {check.get('layout_id') or 'unknown layout'}", "",
                          f"Status: `{check.get('status')}`; AI control readback: `{check.get('ai_control_readback')}`; requested origin: `{check.get('ai_control_origin_requested') or 'unavailable'}`; physical effect: `analysis_only`.",
                          f"Observed AI trace: {trace.get('ai_controlled_sample_count', 0)} controlled samples, {trace.get('valid_interval_count', 0)} usable intervals across {trace.get('segment_count', 0)} segments, {_display(trace.get('observed_duration_s'), ' s', 1)}, mean speed {_display(trace.get('mean_speed_m_s'), ' m/s', 2)}, workload proxy {_display(trace.get('mean_workload_fraction'), ' fraction', 3)}.",
                          f"Ambient air {_display(environment.get('ambient_air_c'), ' °C', 2)} [{environment.get('ambient_evidence', 'UNAVAILABLE')}]; track surface {_display(environment.get('track_surface_c'), ' °C', 2)} [{environment.get('track_evidence', 'UNAVAILABLE')}]. Constant environment uses the median of observed AI-controlled samples where available. Modeled outputs combine MEASURED workload, ASSUMED heat/cooling and PHYSICS_DERIVED calculations.",
                          "Workload = 0.8 throttle + 0.2 brake. It is a driving proxy, not measured electrical power. Each recording segment starts a separate custom thermal state.", ""]
                native = trace.get("native_component_observations") or {}
                for channel, observed in native.items():
                    if observed.get("unit") == "degC":
                        span = f"{_display(observed.get('minimum_c'), ' °C', 2)} to {_display(observed.get('maximum_c'), ' °C', 2)}"
                    else:
                        span = (f"{_display(observed.get('minimum_native'), '', 2)} to "
                                f"{_display(observed.get('maximum_native'), '', 2)} native units")
                    lines.append(f"- Native {channel}: {span} [{observed.get('evidence', 'MEASURED')}]. {observed.get('limitation', '')}")
                if not any(item.get("unit") == "degC" for item in native.values()):
                    lines.append("Native component temperatures with verified °C units: unavailable. Custom node peaks below are not native measurements.")
                lines.append("")
                profiles = check.get("modeled_profiles") or {}
                if profiles:
                    lines += ["| Thermal stress assumption | Peak custom electrical node | Peak custom cooling node | Above warning | Above derate | Modeled derating duration |",
                              "|---|---:|---:|---:|---:|---:|"]
                    for name in ("low", "base", "high"):
                        result = profiles.get(name)
                        if not result:
                            continue
                        peaks = result.get("peak_temperatures_c") or {}
                        lines.append(f"| {name} | {_display(peaks.get('electrical_lumped'), ' °C', 2)} | {_display(peaks.get('cooling_lumped'), ' °C', 2)} | {_display(result.get('time_above_warning_s'), ' s', 1)} | {_display(result.get('time_above_derate_s'), ' s', 1)} | {_display(result.get('derating_duration_s'), ' s', 1)} |")
                    lines += ["", "Low/base/high are engineering assumption profiles, not calibrated probabilities or native temperatures.", ""]
                for warning in check.get("warnings") or []:
                    lines.append(f"- {warning}")
                lines.append("")
        comparison = forecasts.get("strategy_comparison") or {}
        if comparison:
            lines += ["### Plan C assumption sensitivity [ASSUMED, PHYSICS_DERIVED]", "",
                      f"Modeled thermal comparison: `{comparison.get('status', 'unavailable')}`; ranking sensitivity: `{comparison.get('assumption_sensitive')}`. This compares custom electrical-node peaks, not race time or reliability.",
                      f"Reasons: {'; '.join(comparison.get('reasons') or []) or 'none identified within selected ranges'}.",
                      "No race-time winner is inferred from the thermal sweep.", ""]
        for limitation in forecasts.get("limitations") or []:
            lines.append(f"- {limitation}")
        lines.append("")
    if validation is None:
        lines += ["## Held-out validation", "", "No validation result is available.", ""]
    else:
        lines += ["## Held-out validation", ""]
        results = validation.get("results") or []
        if not results:
            lines += ["No held-out result is available.", ""]
        else:
            for item in results:
                residuals = item.get("residuals")
                if residuals:
                    pace = residuals.get("mean_lap_time_s") or {}
                    lines.append(f"- {item.get('block_id', 'V')}: fit runs {item.get('fit_run_ids', [])}; validation run {item.get('validation_run_id')}; pace residual {_display(pace.get('residual_observed_minus_predicted'), ' s')}. Evidence: {item.get('evidence', 'unavailable')}.")
                else:
                    lines.append(f"- {item.get('block_id', 'V')}: residual {_display(item.get('residual_s'), ' s')}; evidence {item.get('evidence', 'unavailable')}.")
            lines.append("")
    lines += ["## Race screening", "", "Race-time values above require selected race length, full pit loss, fuel and battery reserves, and exact modeled stint coverage. Relative case pit deltas cannot supply full pit loss. No race-time winner is claimed.", ""]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "bundle_manifest.json")
    bundle = {"schema_version": 1, "files": [{"path": path.relative_to(output).as_posix(),
                                              "sha256": _hash(path), "bytes": path.stat().st_size} for path in files],
              "evidence_modes": sorted({run["mode"] for run in runs}),
              "synthetic_counts_excluded_from_measured": True}
    _write_json(output / "bundle_manifest.json", bundle)
    return {"output": str(output), "files": len(files) + 1, "runs": len(runs), "counts": store.counts()}
