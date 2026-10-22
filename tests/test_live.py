"""Bridge/director handoff checks without a game installation or Tk display."""

import json
import tempfile
import unittest
from pathlib import Path

from wimna.analysis import analyze_store
from wimna.config import load_campaign
from wimna.gui import InstanceLock, _filled_config, _parse_band, main
from wimna.live import CAMPAIGN_ID, LiveDirector
from wimna.scheduler import CampaignScheduler
from wimna.store import Store


RUN1 = "20261020170000-00000001"
RUN2 = "20261020180000-00000002"
REF = "20261020160000-00000000"


def envelope(run_id, seq, event_type, **data):
    return {"schema": 1, "run_id": run_id, "seq": seq, "sim_time_s": float(seq),
            "wall_time_utc": "2026-10-20T17:00:00Z", "kind": "event",
            "data": {"type": event_type, **data}}


def write_run(bridge, run_id, events, *, track="monza", layout=None, app_version=None):
    folder = bridge / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1, "run_id": run_id, "physical_session_id": run_id,
        "car_id": "rss_formula_hybrid_2026", "track_id": track,
        "layout_id": layout, "selection": "full"}
    if app_version is not None:
        manifest["app_version"] = app_version
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "000001.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def current_state(bridge, run_id, track="monza", layout=None):
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "current_state.json").write_text(json.dumps({
        "schema": 1, "run_id": run_id, "write_ok": True,
        "car_id": "rss_formula_hybrid_2026", "track_id": track, "layout_id": layout,
        "status": "active"}), encoding="utf-8")


def configured_campaign():
    """Legacy Monza-first fixture for existing campaign compatibility checks."""
    config = load_campaign()
    config["track_order"] = ["monza_gp", "imola", "silverstone_gp"]
    config["tracks"]["monza_gp"]["track_id"] = "monza"
    config["tracks"]["monza_gp"]["layout_id"] = None
    config["policy"].update(car_id="rss_formula_hybrid_2026", reference_fuel_l=35.0,
                            reference_compound="Soft", baseline_map="base",
                            deployment_map="push", recovery_map="recover",
                            target_soc_band_j=[3000000, 4000000])
    return config


def spa_campaign():
    """Current four-track campaign with Spa/default first."""
    config = load_campaign()
    config["policy"].update(car_id="rss_formula_hybrid_2026", reference_fuel_l=35.0,
                            reference_compound="C4", baseline_map="base",
                            deployment_map="push", recovery_map="recover",
                            target_soc_band_j=[3000000, 4000000])
    return config


def seed_block(data, config, cursor):
    scheduler = CampaignScheduler(config)
    snapshot = scheduler.to_dict()
    snapshot["state"]["phase"] = "prepare_run"
    snapshot["state"]["block_cursor"] = cursor
    with Store(data / "store") as store:
        store.save_campaign(CAMPAIGN_ID, {"schema": 1, "scheduler": snapshot,
                                         "processed": {}, "pending_learning": [],
                                         "campaign_started_wall_s": None,
                                         "live_run_id": None, "session_indices": {}, "messages": []})


def seed_reference_for_management(data, config, track_key, lap_points):
    cursor = config["track_order"].index(track_key) * 6 + 4
    block_prefix = {"monza_gp": "MZ", "imola": "IM", "silverstone_gp": "SI"}[track_key]
    scheduler = CampaignScheduler(config)
    snapshot = scheduler.to_dict()
    snapshot["state"]["phase"] = "prepare_run"
    snapshot["state"]["block_cursor"] = cursor
    snapshot["state"]["block_results"] = [{"track_key": track_key, "kind": "reference",
                                           "status": "complete", "run_id": REF,
                                           "block_id": f"{block_prefix} R"}]
    with Store(data / "store") as store:
        store.save_campaign(CAMPAIGN_ID, {"schema": 1, "scheduler": snapshot,
                                         "processed": {}, "pending_learning": [],
                                         "campaign_started_wall_s": None,
                                         "live_run_id": None, "session_indices": {}, "messages": []})
        store.start_run(REF, "MEASURED", {"physical_session_id": REF})
        sequence = 1
        for lap_index, points in enumerate(lap_points, 1):
            for distance, speed, brake, throttle in points:
                fields = {"track_distance_m": (distance, "m"), "speed_kmh": (speed, "km/h"),
                          "brake": (brake, "fraction"), "throttle": (throttle, "fraction")}
                data_fields = {key: {"value": value, "unit": unit, "source": "CSP_STATE",
                                     "quality": "available"}
                               for key, (value, unit) in fields.items()}
                store.add_record({"schema": 1, "run_id": REF, "seq": sequence,
                                  "sim_time_s": float(sequence), "kind": "sample",
                                  "data": data_fields})
                sequence += 1
            event = envelope(REF, sequence, "lap_complete", lap_index=lap_index,
                             lap_time_s=90.0, valid_native=True, flags=[], session_index=0)
            store.add_record(event)
            store.annotate_lap(REF, sequence, "program", True, [], "accepted reference")
            sequence += 1


def braking_trace(offset=0):
    return [(0 + offset, 220, 0, 1), (100 + offset, 220, 0, 0.8),
            (120 + offset, 215, 0.4, 0), (140 + offset, 190, 0.7, 0),
            (160 + offset, 155, 0.6, 0), (190 + offset, 140, 0, 0.5),
            (500 + offset, 215, 0, 1), (900 + offset, 230, 0, 1),
            (980 + offset, 230, 0, 0.8), (1000 + offset, 225, 0.5, 0),
            (1020 + offset, 180, 0.7, 0), (1040 + offset, 145, 0.6, 0),
            (1080 + offset, 130, 0, 0.5), (1300 + offset, 210, 0, 1)]


def throttle_trace(offset=0):
    return [(0 + offset, 180, 0, 1), (300 + offset, 95, 0.3, 0.1),
            (400 + offset, 100, 0, 0.1), (420 + offset, 105, 0, 0.4),
            (440 + offset, 110, 0, 0.8), (460 + offset, 125, 0, 1),
            (800 + offset, 210, 0, 1), (1500 + offset, 90, 0.3, 0.1),
            (1600 + offset, 100, 0, 0.1), (1620 + offset, 105, 0, 0.4),
            (1640 + offset, 115, 0, 0.8), (1660 + offset, 130, 0, 1),
            (1900 + offset, 220, 0, 1)]


class LiveDirectorTests(unittest.TestCase):
    def test_spa_first_full_run_assigns_a_new_physical_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1, track="spa")
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"),
                                     envelope(RUN1, 3, "ui_confirm")],
                      track="spa", layout="default")
            (bridge / "current_state.json").write_text(json.dumps({
                "schema": 1, "run_id": RUN1, "write_ok": True,
                "car_id": "rss_formula_hybrid_2026", "track_id": "spa",
                "layout_id": "default", "status": "active"}), encoding="utf-8")
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.config["track_order"],
                                 ["spa", "silverstone_gp", "imola", "monza_gp"])
                self.assertEqual(director.scheduler.state["phase"], "verify_settings")
                self.assertEqual(director.live_run_id, RUN1)
                self.assertEqual(status["block_id"], "SP C")
                self.assertFalse(status["diagnostic_only"])
                assignment = director.store.get_run_assignment(RUN1)
                self.assertEqual((assignment["track"], assignment["block"]), ("spa", "SP C"))

    def test_spa_non_gp_layout_remains_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1, track="spa", layout="short")
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full")],
                      track="spa", layout="short")
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertTrue(status["diagnostic_only"])
                self.assertEqual(director.scheduler.state["phase"], "idle")
                self.assertIsNone(director.store.get_run_assignment(RUN1))

    def test_idle_legacy_migration_retains_old_spa_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "lap_complete", lap_index=1,
                                              lap_time_s=118.9, valid_native=True,
                                              flags=[], session_index=0),
                                     envelope(RUN1, 3, "ui_end")],
                      track="spa", layout="default")
            current_state(bridge, RUN1, track="spa")
            legacy = CampaignScheduler(configured_campaign())
            with Store(data / "store") as store:
                store.save_campaign(CAMPAIGN_ID, {"schema": 1, "scheduler": legacy.to_dict(),
                                                 "processed": {}, "pending_learning": [],
                                                 "campaign_started_wall_s": None,
                                                 "live_run_id": None, "session_indices": {},
                                                 "messages": []})
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.state["phase"], "idle")
                self.assertEqual(director.scheduler.config["track_order"][0], "spa")
                self.assertIsNone(director.store.get_run_assignment(RUN1))
                self.assertEqual(list(director.store.iter_lap_annotations(RUN1)), [])
                self.assertEqual(len(list(director.store.iter_records(RUN1, "event"))), 3)
                self.assertTrue(status["diagnostic_only"])
                self.assertIsNone(director.campaign_started_wall_s)
            with LiveDirector(bridge, data, config=spa_campaign()) as restarted:
                restarted.poll()
                self.assertIn(RUN1, restarted.legacy_diagnostic_runs)
                self.assertEqual(restarted.scheduler.state["phase"], "idle")
                current_state(bridge, RUN2, track="spa")
                write_run(bridge, RUN2, [envelope(RUN2, 1, "ui_start_full")],
                          track="spa", layout="default")
                (bridge / "current_state.json").write_text(json.dumps({
                    "schema": 1, "run_id": RUN2, "write_ok": True,
                    "car_id": "rss_formula_hybrid_2026", "track_id": "spa",
                    "layout_id": "default", "status": "active"}), encoding="utf-8")
                restarted.poll()
                self.assertEqual(restarted.scheduler.state["phase"], "preflight")
                self.assertEqual(restarted.live_run_id, RUN2)
                self.assertIsNone(restarted.store.get_run_assignment(RUN1))

    def test_idle_migration_allows_fresh_versioned_spa_run_already_on_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full")],
                      track="spa", layout="default", app_version="0.1.0")
            write_run(bridge, RUN2, [envelope(RUN2, 1, "ui_start_full")],
                      track="spa", layout="default", app_version="0.2.0")
            current_state(bridge, RUN2, track="spa", layout="default")
            with Store(data / "store") as store:
                store.save_campaign(CAMPAIGN_ID, {
                    "schema": 1, "scheduler": CampaignScheduler(configured_campaign()).to_dict(),
                    "processed": {}, "pending_learning": [],
                    "campaign_started_wall_s": None, "live_run_id": None,
                    "session_indices": {}, "messages": []})
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertIn(RUN1, director.legacy_diagnostic_runs)
                self.assertNotIn(RUN2, director.legacy_diagnostic_runs)
                self.assertEqual(director.scheduler.state["phase"], "preflight")
                self.assertEqual(director.live_run_id, RUN2)
                self.assertEqual(status["block_id"], "SP C")
                self.assertIsNone(director.store.get_run_assignment(RUN1))

    def test_active_legacy_campaign_keeps_order_and_displays_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            seed_block(data, configured_campaign(), 0)
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.config["track_order"][0], "monza_gp")
                self.assertIn("original track order", status["campaign_notice"])

    def test_partial_and_cut_spa_learning_laps_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1, track="spa")
            (bridge / "current_state.json").write_text(json.dumps({
                "schema": 1, "run_id": RUN1, "write_ok": True,
                "car_id": "rss_formula_hybrid_2026", "track_id": "spa",
                "layout_id": "default", "status": "active"}), encoding="utf-8")
            write_run(bridge, RUN1, [
                envelope(RUN1, 1, "ui_start_full"),
                envelope(RUN1, 2, "ui_confirm"),
                envelope(RUN1, 3, "ui_confirm"),
                envelope(RUN1, 4, "ui_confirm"),
                envelope(RUN1, 5, "ui_confirm"),
                envelope(RUN1, 6, "lap_complete", lap_index=1, lap_time_s=150,
                         valid_native=True, flags=["partial_run_lap"], session_index=0),
                envelope(RUN1, 7, "ui_confirm_learning"),
                envelope(RUN1, 8, "lap_complete", lap_index=2, lap_time_s=150,
                         valid_native=True, last_lap_cuts_count=1, flags=[], session_index=0),
                envelope(RUN1, 9, "ui_confirm_learning"),
            ], track="spa", layout="default")
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "calibration")
                self.assertEqual(director.scheduler.state["accepted_laps"], 0)
                annotations = list(director.store.iter_lap_annotations(RUN1))
                self.assertEqual(len(annotations), 2)
                self.assertFalse(any(item["eligible"] for item in annotations))
                self.assertIn("partial_run_lap", annotations[0]["quality_flags"])
                self.assertIn("missing_required_data", annotations[0]["quality_flags"])
                self.assertIn("off_track", annotations[1]["quality_flags"])

    def test_unconfigured_track_diagnostic_does_not_start_campaign_clock(self):
        for choice in ("ui_start_full", "ui_individual"):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                bridge, data = base / "bridge", base / "data"
                current_state(bridge, RUN1, track="laguna")
                write_run(bridge, RUN1, [envelope(RUN1, 1, choice, block_id="C"),
                                         envelope(RUN1, 2, "ui_confirm"),
                                         envelope(RUN1, 3, "ui_end")], track="laguna")
                with LiveDirector(bridge, data, config=spa_campaign()) as director:
                    result = director.poll()
                    self.assertEqual(director.scheduler.state["phase"], "idle")
                    self.assertIsNone(director.campaign_started_wall_s)
                    self.assertIsNone(director.live_run_id)
                    self.assertEqual(result["director"]["run_id"], RUN1)
                    self.assertEqual(result["director"]["state"], "diagnostic_recording")
                    self.assertTrue(result["director"]["diagnostic_only"])
                    self.assertIsNone(result["director"]["block_id"])
                    self.assertEqual(result["director"]["lap_progress"], "—")
                    self.assertEqual(result["director"]["target"], "No campaign lap target")
                    self.assertIn("laguna", result["director"]["instruction"])
                    self.assertEqual(len(list(director.store.iter_records(RUN1, "event"))), 3)
                with LiveDirector(bridge, data, config=spa_campaign()) as restarted:
                    restarted.poll()
                    self.assertEqual(restarted.scheduler.state["phase"], "idle")
                    self.assertIsNone(restarted.campaign_started_wall_s)
                    current_state(bridge, RUN2, track="spa", layout="default")
                    write_run(bridge, RUN2, [envelope(RUN2, 1, "ui_start_full")],
                              track="spa", layout="default")
                    restarted.poll()
                    self.assertEqual(restarted.scheduler.state["phase"], "preflight")
                    self.assertIsNotNone(restarted.campaign_started_wall_s)

    def test_unconfigured_track_status_is_run_bound_and_diagnostic_before_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1, track="laguna")
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full")], track="laguna")
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                published = json.loads((bridge / "director_state.json").read_text(encoding="utf-8"))
                self.assertEqual(status, published)
                self.assertEqual(status["run_id"], RUN1)
                self.assertEqual(status["state"], "diagnostic_recording")
                self.assertTrue(status["diagnostic_only"])
                self.assertEqual(status["recording_status"], "recording")
                self.assertIsNone(status["block_id"])
                self.assertEqual(status["lap_progress"], "—")
                self.assertEqual(director.scheduler.state["phase"], "idle")

    def test_ended_diagnostic_run_shows_next_campaign_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1, track="laguna")
            state_path = bridge / "current_state.json"
            current = json.loads(state_path.read_text(encoding="utf-8"))
            current["status"] = "ended"
            state_path.write_text(json.dumps(current), encoding="utf-8")
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_end")], track="laguna")
            with LiveDirector(bridge, data, config=spa_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.state["phase"], "idle")
                self.assertEqual(status["state"], "idle")
                self.assertEqual(status["block_id"], "SP C")
                self.assertFalse(status["diagnostic_only"])
                self.assertIsNone(status["run_id"])
                self.assertEqual(status, json.loads(
                    (bridge / "director_state.json").read_text(encoding="utf-8")))

    def test_skip_requires_matching_block_and_driver_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            records = [envelope(RUN1, 1, "ui_start_full")]
            records.extend(envelope(RUN1, seq, "ui_confirm") for seq in range(2, 6))
            records.extend([
                envelope(RUN1, 6, "lap_complete", lap_index=1, lap_time_s=101,
                         valid_native=True, flags=[], session_index=0),
                envelope(RUN1, 7, "ui_confirm_learning"),
                envelope(RUN1, 8, "ui_skip", block_id="MZ C", reason="  "),
                envelope(RUN1, 9, "ui_skip", block_id="IM C", reason="Cannot finish"),
            ])
            write_run(bridge, RUN1, records)
            with LiveDirector(bridge, data, config=configured_campaign()) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "calibration")
                self.assertEqual(director.scheduler.state["accepted_laps"], 1)
                self.assertEqual(director.scheduler.state["block_results"], [])
                folder = bridge / "runs" / RUN1
                (folder / "000002.jsonl").write_text("".join(json.dumps(event) + "\n" for event in [
                    envelope(RUN1, 10, "ui_skip", block_id="MZ C", reason="RSS learning would not accept further laps"),
                    envelope(RUN1, 11, "ui_end"),
                ]), encoding="utf-8")
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "next_block")
                result = director.scheduler.state["block_results"][0]
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["accepted_laps"], 1)
                self.assertEqual(result["reason"], "RSS learning would not accept further laps")

    def test_spa_events_do_not_interrupt_an_active_monza_campaign(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm")])
            with LiveDirector(bridge, data, config=configured_campaign()) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "prepare_run")
                current_state(bridge, RUN2, track="spa")
                write_run(bridge, RUN2, [envelope(RUN2, 1, "ui_start_full"),
                                         envelope(RUN2, 2, "ui_confirm"),
                                         envelope(RUN2, 3, "session_changed"),
                                         envelope(RUN2, 4, "ui_end")], track="spa")
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.state["phase"], "prepare_run")
                self.assertEqual(director.live_run_id, RUN1)
                self.assertEqual(status["run_id"], RUN2)
                self.assertTrue(status["diagnostic_only"])
                self.assertIsNone(status["block_id"])
                self.assertEqual(status["lap_progress"], "—")
                self.assertEqual([event["action"] for event in director.scheduler.events],
                                 ["start", "confirm_preflight"])
                self.assertEqual(len(list(director.store.iter_records(RUN2, "event"))), 4)
                current_state(bridge, RUN1)
                restored = director.poll()["director"]
                self.assertEqual(restored["run_id"], RUN1)
                self.assertFalse(restored["diagnostic_only"])
                self.assertEqual(restored["block_id"], "MZ C")

    def test_validation_skip_requires_explicit_unavoidable_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            config = configured_campaign()
            seed_block(data, config, 5)  # MZ V
            current_state(bridge, RUN1)
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_skip", block_id="MZ V",
                                              reason="Live restriction unavailable")])
            with LiveDirector(bridge, data) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "prepare_run")
                self.assertEqual(director.scheduler.state["block_results"], [])
                folder = bridge / "runs" / RUN1
                (folder / "000002.jsonl").write_text(json.dumps(
                    envelope(RUN1, 3, "ui_skip", block_id="MZ V",
                             reason="Live restriction unavailable", unavoidable=True)) + "\n",
                    encoding="utf-8")
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "next_block")
                result = director.scheduler.state["block_results"][0]
                self.assertEqual(result["block_id"], "MZ V")
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["reason"], "Live restriction unavailable")

    def test_monza_management_cues_use_accepted_reference_brake_distances(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            config = configured_campaign()
            seed_reference_for_management(data, config, "monza_gp",
                                          [braking_trace(), braking_trace(5)])
            current_state(bridge, RUN1)
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"),
                                     envelope(RUN1, 3, "ui_confirm"),
                                     envelope(RUN1, 4, "ui_confirm")])
            with LiveDirector(bridge, data) as director:
                result = director.poll()
                assignment = director.store.get_run_assignment(RUN1)["details"]
                cues = assignment["management_cues"]
                self.assertEqual(cues["status"], "ready")
                self.assertEqual(cues["source_run_id"], REF)
                self.assertEqual(len(cues["cues"]), 2)
                self.assertAlmostEqual(cues["cues"][0]["distance_m"], 87.5)
                self.assertAlmostEqual(cues["cues"][1]["distance_m"], 967.5)
                self.assertEqual(cues["intervention_distance_evidence"], "ASSUMED")
                self.assertIn("87.5 m", result["director"]["instruction"])
                self.assertIn("967.5 m", result["director"]["instruction"])

    def test_silverstone_management_uses_throttle_proxy_when_slip_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            config = configured_campaign()
            seed_reference_for_management(data, config, "silverstone_gp",
                                          [throttle_trace(), throttle_trace(4)])
            current_state(bridge, RUN1, track="ks_silverstone", layout="gp")
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"),
                                     envelope(RUN1, 3, "ui_confirm"),
                                     envelope(RUN1, 4, "ui_confirm")],
                      track="ks_silverstone", layout="gp")
            with LiveDirector(bridge, data) as director:
                director.poll()
                cues = director.store.get_run_assignment(RUN1)["details"]["management_cues"]
                self.assertEqual(cues["status"], "ready")
                self.assertEqual([cue["distance_m"] for cue in cues["cues"]], [422, 1622])
                self.assertTrue(all(cue["proxy"] == "reference throttle ramp" for cue in cues["cues"]))
                self.assertIn("unverified", cues["limitations"][0])

    def test_management_without_reference_cues_excludes_lap(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            config = configured_campaign()
            seed_reference_for_management(data, config, "monza_gp", [])
            current_state(bridge, RUN1)
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"),
                                     envelope(RUN1, 3, "ui_confirm"),
                                     envelope(RUN1, 4, "ui_confirm"),
                                     envelope(RUN1, 5, "lap_complete", lap_index=1,
                                              lap_time_s=95, valid_native=True,
                                              flags=[], session_index=0)])
            with LiveDirector(bridge, data) as director:
                director.poll()
                cues = director.store.get_run_assignment(RUN1)["details"]["management_cues"]
                self.assertEqual(cues["status"], "unavailable")
                lap = list(director.store.iter_lap_annotations(RUN1))[0]
                self.assertFalse(lap["eligible"])
                self.assertIn("management_cues_unavailable", lap["quality_flags"])

    def test_native_fuel_and_soc_units_are_preserved_without_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            config = configured_campaign()
            config["policy"]["target_soc_band_j"] = None
            config["policy"]["target_soc_band_fraction"] = [0.45, 0.55]
            sample = {"schema": 1, "run_id": RUN1, "seq": 3, "sim_time_s": 3.0,
                      "wall_time_utc": "2026-10-20T17:00:00Z", "kind": "sample",
                      "data": {"fuel": {"value": 35.0, "unit": "L_native_display",
                                        "source": "CSP_STATE", "quality": "available"},
                               "battery_soc_native": {"value": 0.50, "unit": "fraction_native",
                                                      "source": "CSP_STATE", "quality": "available"}}}
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"), sample,
                                     envelope(RUN1, 4, "ui_confirm"),
                                     envelope(RUN1, 5, "ui_confirm")])
            with LiveDirector(bridge, data, config=config) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "warmup")
                assignment = director.store.get_run_assignment(RUN1)["details"]
                observed = assignment["observed_initial_state"]
                self.assertIsNone(observed["fuel_l"])
                self.assertEqual(observed["fuel_native"]["unit"], "L_native_display")
                self.assertIsNone(observed["battery_j"])
                self.assertEqual(observed["battery_soc_native"]["unit"], "fraction_native")
                self.assertEqual(assignment["requested_initial_state"]["target_soc_band_fraction"],
                                 [0.45, 0.55])
                settings = director.scheduler.state["active_settings_check"]
                self.assertIsNone(settings["observed"]["fuel_l"])
                self.assertEqual(settings["requested"]["target_soc_band_fraction"], [0.45, 0.55])

    def test_calibration_manual_profile_and_idempotent_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            records = [
                envelope(RUN1, 1, "ui_start_full"),
                envelope(RUN1, 2, "ui_confirm"),  # preflight
                envelope(RUN1, 3, "ui_confirm"),  # prepare
                envelope(RUN1, 4, "ui_confirm"),  # manual setting confirmation
                envelope(RUN1, 5, "ui_confirm"),  # warmup complete
            ]
            seq = 6
            for lap in range(1, 4):
                records.append(envelope(RUN1, seq, "lap_complete", lap_index=lap,
                                        lap_time_s=100.0 + lap, valid_native=True,
                                        flags=[], session_index=0))
                seq += 1
                records.append(envelope(RUN1, seq, "ui_confirm_learning"))
                seq += 1
            records.append(envelope(RUN1, seq, "ui_confirm_profile", mode="FRZ_AUTO"))
            records.append(envelope(RUN1, seq + 1, "ui_confirm"))  # box and analyze
            write_run(bridge, RUN1, records)
            with LiveDirector(bridge, data, config=configured_campaign(), clock=lambda: 1_792_515_600) as director:
                first = director.poll()
                self.assertEqual(director.scheduler.state["phase"], "next_block")
                self.assertEqual(director.scheduler.state["block_results"][0]["accepted_laps"], 3)
                self.assertEqual(director.scheduler.state["track_profiles"]["monza_gp"]["verification"],
                                 "manual_confirmation")
                self.assertTrue(all(result["control_class"] == "manually_confirmed"
                                    for result in director.scheduler.state["settings_checks"][0]["outcomes"].values()))
                annotations = list(director.store.iter_lap_annotations(RUN1))
                self.assertEqual(len(annotations), 3)
                self.assertTrue(all(item["phase"] == "program" and item["eligible"] for item in annotations))
                written = json.loads((bridge / "director_state.json").read_text(encoding="utf-8"))
                self.assertEqual(written["run_id"], RUN1)
                self.assertEqual(first["director"]["state"], "next_block")
                before = len(director.scheduler.events)
                director.poll()
                self.assertEqual(len(director.scheduler.events), before)
            with LiveDirector(bridge, data, config=configured_campaign(), clock=lambda: 1_792_515_600) as restarted:
                restarted.poll()
                self.assertEqual(len(restarted.scheduler.events), before)
                self.assertEqual(len(list(restarted.store.iter_lap_annotations(RUN1))), 3)

    def test_fresh_physical_run_for_next_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            records = [envelope(RUN1, 1, "ui_start_full"), envelope(RUN1, 2, "ui_confirm"),
                       envelope(RUN1, 3, "ui_confirm"), envelope(RUN1, 4, "ui_confirm"),
                       envelope(RUN1, 5, "ui_confirm")]
            seq = 6
            for lap in range(1, 4):
                records += [envelope(RUN1, seq, "lap_complete", lap_index=lap, lap_time_s=100,
                                     valid_native=True, flags=[], session_index=0),
                            envelope(RUN1, seq + 1, "ui_confirm_learning")]
                seq += 2
            records += [envelope(RUN1, seq, "ui_confirm_profile", mode="MANUAL_APPLIED",
                                 profile_name="Tyler baseline"),
                        envelope(RUN1, seq + 1, "ui_confirm")]
            write_run(bridge, RUN1, records)
            with LiveDirector(bridge, data, config=configured_campaign()) as director:
                director.poll()
                current_state(bridge, RUN2)
                second = [envelope(RUN2, 1, "ui_start_full"), envelope(RUN2, 2, "ui_confirm"),
                          envelope(RUN2, 3, "ui_confirm"), envelope(RUN2, 4, "ui_confirm"),
                          envelope(RUN2, 5, "lap_complete", lap_index=1, lap_time_s=91.1,
                                    valid_native=True, flags=[], session_index=0)]
                write_run(bridge, RUN2, second)
                status = director.poll()["director"]
                self.assertEqual(director.scheduler.state["phase"], "record_block")
                self.assertEqual(director.scheduler.state["active_run_id"], RUN2)
                self.assertEqual(director.scheduler.state["run_laps"], 1)
                self.assertEqual(status["lap_progress"], "1/5 accepted; 1 attempted")
                self.assertEqual(status["target"], "5 accepted flying laps")
                self.assertEqual(director.scheduler.state["track_profiles"]["monza_gp"]["profile_id"],
                                 "Tyler baseline")
                self.assertEqual(list(director.store.iter_lap_annotations(RUN2))[0]["eligible"], True)
                status = json.loads((bridge / "director_state.json").read_text(encoding="utf-8"))
                self.assertEqual(status["run_id"], RUN2)
                self.assertEqual(status["block_id"], "MZ R")
                forecasts, _, _ = analyze_store(director.store, director.scheduler.config)
                reference = forecasts["tracks"]["monza_gp"]["reference"]
                self.assertEqual(reference["evidence"], ["MEASURED"])
                self.assertAlmostEqual(reference["pace_mean_s"], 91.1)

    def test_missing_lap_and_session_break_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            records = [envelope(RUN1, 1, "ui_start_full"), envelope(RUN1, 2, "ui_confirm"),
                       envelope(RUN1, 3, "ui_confirm"), envelope(RUN1, 4, "ui_confirm"),
                       envelope(RUN1, 5, "lap_complete", lap_index=0, lap_time_s=105,
                                valid_native=True, flags=[], session_index=0),  # warmup
                       envelope(RUN1, 6, "ui_confirm"),
                       envelope(RUN1, 7, "lap_complete", lap_index=1, lap_time_s=None,
                                valid_native=None, flags=["sim_paused"], session_index=0),
                       envelope(RUN1, 8, "ui_confirm_learning"),
                       envelope(RUN1, 9, "session_changed", **{"from": 0, "to": 1}),
                       envelope(RUN1, 10, "lap_complete", lap_index=1, lap_time_s=99,
                                valid_native=True, flags=[], session_index=1)]
            write_run(bridge, RUN1, records)
            with LiveDirector(bridge, data, config=configured_campaign()) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "interrupted")
                self.assertEqual(director.scheduler.state["accepted_laps"], 0)
                annotations = list(director.store.iter_lap_annotations(RUN1))
                self.assertEqual(len(annotations), 3)
                self.assertEqual([item["eligible"] for item in annotations], [False, False, False])
                self.assertEqual(annotations[0]["phase"], "warmup")
                self.assertIn("paused", annotations[1]["quality_flags"])
                self.assertEqual(annotations[2]["phase"], "unknown")

    def test_energy_transition_requires_driver_confirmation_not_readback_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            seed_block(data, configured_campaign(), 3)  # MZ E
            write_run(bridge, RUN1, [
                envelope(RUN1, 1, "ui_start_full"),
                envelope(RUN1, 2, "ui_confirm"),  # prepare
                envelope(RUN1, 3, "ui_confirm"),  # settings
                envelope(RUN1, 4, "ui_confirm"),  # warmup done
                envelope(RUN1, 5, "lap_complete", lap_index=1, lap_time_s=91,
                         valid_native=True, flags=[], session_index=0),
                envelope(RUN1, 6, "lap_complete", lap_index=2, lap_time_s=91,
                         valid_native=True, flags=[], session_index=0),
                envelope(RUN1, 7, "ui_confirm"),  # change to recovery map
            ])
            with LiveDirector(bridge, data) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "record_block")
                self.assertEqual(director.scheduler.state["run_laps"], 2)
                check = director.scheduler.state["active_settings_check"]["outcomes"]["energy_map"]
                self.assertEqual(check["requested"], "recover")
                self.assertIsNone(check["observed"])
                self.assertEqual(check["status"], "manually_confirmed")

    def test_validation_prediction_is_frozen_before_run_and_nulls_stay_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            seed_block(data, configured_campaign(), 5)  # MZ V, no fit data
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm")])
            with LiveDirector(bridge, data) as director:
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "verify_settings")
                prediction = director.scheduler.state["predictions"]["MZ V"]
                self.assertTrue(prediction["frozen_before_run"])
                self.assertIsNone(prediction["predicted"]["mean_lap_time_s"])
                self.assertEqual(prediction["condition"]["thermal_intervention"], "analysis_only_unverified")
                stored = list(director.store.iter_predictions())
                self.assertEqual(stored[0]["validation_run_id"], RUN1)

    def test_unconfigured_target_stays_visible_and_uses_analysis_only_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bridge, data = base / "bridge", base / "data"
            current_state(bridge, RUN1)
            config = configured_campaign()
            config["policy"]["reference_fuel_l"] = None
            write_run(bridge, RUN1, [envelope(RUN1, 1, "ui_start_full"),
                                     envelope(RUN1, 2, "ui_confirm"),
                                     envelope(RUN1, 3, "ui_confirm"),
                                     envelope(RUN1, 4, "ui_confirm")])
            with LiveDirector(bridge, data, config=config) as director:
                result = director.poll()
                self.assertEqual(director.scheduler.state["phase"], "unavailable_capability")
                self.assertIn("reference_fuel_l", result["director"]["unconfigured_targets"])
                self.assertIn("analysis-only", result["director"]["next_action"])
                self.assertEqual(director.scheduler.state["active_settings_check"]["outcomes"]
                                 ["missing:reference_fuel_l"]["status"], "unsupported")
                extra = envelope(RUN1, 5, "ui_confirm")
                folder = bridge / "runs" / RUN1
                (folder / "000002.jsonl").write_text(json.dumps(extra) + "\n", encoding="utf-8")
                director.poll()
                self.assertEqual(director.scheduler.state["phase"], "warmup")
                self.assertEqual(director.scheduler.state["mode"], "ANALYSIS_ONLY")


class GuiImportTests(unittest.TestCase):
    def test_soc_fraction_band_bounds(self):
        self.assertEqual(_parse_band("0.45, 0.55", "SOC", fraction=True), [0.45, 0.55])
        with self.assertRaises(ValueError):
            _parse_band("0.8, 1.1", "SOC", fraction=True)

    def test_setup_persists_soc_fraction_without_inventing_joules(self):
        class Field:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        values = {
            "car_id": "rss_formula_hybrid_2026",
            "track_spa": "spa :: default",
            "track_monza_gp": "monza :: ", "track_imola": "imola :: ",
            "track_silverstone_gp": "silverstone :: ",
            "reference_compound": "Soft", "reference_fuel_l": "35",
            "contrasting_fuel_l": "45", "target_soc_band_fraction": "0.45,0.55",
            "target_soc_band_j": "", "baseline_map": "base", "deployment_map": "push",
            "recovery_map": "recover", "setup_hash": "", "fuel_start_tolerance_l": "0.5",
            "battery_start_tolerance_j": "", "battery_start_tolerance_fraction": "0.03",
            "tire_start_tolerance": "", "environment_tolerance_c": "",
            "management_lift_distance_m": "35",
            "management_throttle_ramp_distance_m": "50",
        }
        entries = {key: Field(value) for key, value in values.items()}
        config = _filled_config(load_campaign(), entries)
        self.assertEqual(config["policy"]["target_soc_band_fraction"], [0.45, 0.55])
        self.assertIsNone(config["policy"]["target_soc_band_j"])
        self.assertEqual(config["policy"]["battery_start_tolerance_fraction"], 0.03)
        entries["track_monza_gp"].value = "ks_monza66 :: road"
        with self.assertRaisesRegex(ValueError, "Modern Monza"):
            _filled_config(load_campaign(), entries)
        entries["track_monza_gp"].value = "ks_monza :: gp"
        entries["track_silverstone_gp"].value = "ks_silverstone1967 :: gp"
        with self.assertRaisesRegex(ValueError, "Silverstone GP"):
            _filled_config(load_campaign(), entries)

    def test_import_only_self_test_and_singleton_lock(self):
        self.assertEqual(main(["--self-test"]), 0)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "companion.lock"
            first, second = InstanceLock(path), InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
