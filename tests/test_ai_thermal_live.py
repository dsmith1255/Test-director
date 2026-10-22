"""AI thermal diagnostics must remain separate from the lap campaign."""

import json
import tempfile
import unittest
from pathlib import Path

from wimna.config import load_campaign
from wimna.live import LiveDirector
from wimna.store import Store


RUN_ID = "20261021120000-00000001"


def _write_bridge(bridge: Path, selection: str, track: str = "spa") -> None:
    run_dir = bridge / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": 1, "run_id": RUN_ID, "physical_session_id": RUN_ID,
        "car_id": "rss_formula_hybrid_2026", "track_id": track,
        "layout_id": "default", "selection": selection,
    }), encoding="utf-8")
    events = [
        {"type": "ui_start_full"},
        {"type": "ui_confirm"},
        {"type": "lap_complete", "lap_index": 1, "lap_time_s": 120.0,
         "valid_native": True, "flags": [], "session_index": 0},
        {"type": "ui_end"},
    ]
    (run_dir / "000001.jsonl").write_text("".join(json.dumps({
        "schema": 1, "run_id": RUN_ID, "seq": seq, "sim_time_s": float(seq),
        "wall_time_utc": "2026-10-21T12:00:00Z", "kind": "event", "data": data,
    }) + "\n" for seq, data in enumerate(events, 1)), encoding="utf-8")
    (bridge / "current_state.json").write_text(json.dumps({
        "schema": 1, "run_id": RUN_ID, "status": "active", "write_ok": True,
        "car_id": "rss_formula_hybrid_2026", "track_id": track,
        "layout_id": "default", "mode": selection,
    }), encoding="utf-8")


class AIThermalLiveTests(unittest.TestCase):
    def test_ai_thermal_on_campaign_track_is_diagnostic_and_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, data = root / "bridge", root / "data"
            _write_bridge(bridge, "ai_thermal")
            for _ in range(2):
                with LiveDirector(bridge, data, config=load_campaign()) as director:
                    status = director.poll()["director"]
                    self.assertEqual(director.scheduler.state["phase"], "idle")
                    self.assertIsNone(director.campaign_started_wall_s)
                    self.assertIsNone(director.live_run_id)
                    self.assertIsNone(director.store.get_run_assignment(RUN_ID))
                    self.assertEqual(list(director.store.iter_lap_annotations(RUN_ID)), [])
                    self.assertEqual(status["state"], "diagnostic_ai_thermal_recording")
                    self.assertTrue(status["diagnostic_only"])
                    self.assertEqual(status["mode"], "AI_THERMAL_DIAGNOSTIC")
                    self.assertEqual(status["recording_status"], "recording")
                    self.assertIsNone(status["block_id"])
                    self.assertEqual(status["lap_progress"], "—")
                    self.assertEqual(status["ai_control_status"], "unavailable")
                    self.assertIn("do not label this trace AI-driven", status["target"])
                    self.assertIn("AI thermal", status["instruction"])
                    self.assertIn("No campaign laps", status["instruction"])
                    self.assertEqual(status, json.loads(
                        (bridge / "director_state.json").read_text(encoding="utf-8")))

    def test_native_ai_control_readback_changes_diagnostic_target(self):
        for observed, expected, target_text in (
            (True, "observed_on", "observed ON"),
            (False, "observed_off", "observed OFF"),
            (None, "unavailable", "readback unavailable"),
        ):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bridge, data = root / "bridge", root / "data"
                _write_bridge(bridge, "ai_thermal")
                state_path = bridge / "current_state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["is_ai_controlled"] = observed
                state_path.write_text(json.dumps(state), encoding="utf-8")
                with LiveDirector(bridge, data, config=load_campaign()) as director:
                    status = director.poll()["director"]
                    self.assertEqual(status["ai_control_status"], expected)
                    self.assertIn(target_text, status["target"])
                    self.assertEqual(director.scheduler.state["phase"], "idle")
                    self.assertIsNone(director.store.get_run_assignment(RUN_ID))

    def test_ai_stop_pending_has_distinct_instruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, data = root / "bridge", root / "data"
            _write_bridge(bridge, "ai_thermal")
            state_path = bridge / "current_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update(status="ending_ai", is_ai_controlled=True)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with LiveDirector(bridge, data, config=load_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(status["state"], "diagnostic_ai_thermal_stopping")
                self.assertEqual(status["ai_control_status"], "observed_on")
                self.assertIn("verify native readback OFF", status["instruction"])
                self.assertIn("Toggle AI Driver", status["next_action"])
                self.assertEqual(director.scheduler.state["phase"], "idle")

    def test_regular_unassigned_run_keeps_generic_diagnostic_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, data = root / "bridge", root / "data"
            _write_bridge(bridge, "full", track="laguna")
            with LiveDirector(bridge, data, config=load_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(status["state"], "diagnostic_recording")
                self.assertEqual(status["mode"], "DIAGNOSTIC")
                self.assertNotIn("AI thermal", status["instruction"])
                self.assertEqual(director.scheduler.state["phase"], "idle")

    def test_immutable_stored_selection_protects_replay_without_bridge_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, data = root / "bridge", root / "data"
            bridge.mkdir()
            (bridge / "current_state.json").write_text(json.dumps({
                "schema": 1, "run_id": RUN_ID, "status": "active", "write_ok": True,
                "track_id": "spa", "layout_id": "default",
            }), encoding="utf-8")
            with Store(data / "store") as store:
                store.start_run(RUN_ID, "MEASURED", {
                    "physical_session_id": RUN_ID, "track_id": "spa",
                    "layout_id": "default", "selection": "ai_thermal",
                })
                store.add_record({
                    "schema": 1, "run_id": RUN_ID, "seq": 1, "sim_time_s": 1.0,
                    "kind": "event", "data": {"type": "ui_start_full"},
                })
            with LiveDirector(bridge, data, config=load_campaign()) as director:
                status = director.poll()["director"]
                self.assertEqual(status["state"], "diagnostic_ai_thermal_recording")
                self.assertEqual(director.scheduler.state["phase"], "idle")
                self.assertIsNone(director.live_run_id)


if __name__ == "__main__":
    unittest.main()
