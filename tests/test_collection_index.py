"""Collection indexes preserve physical state and controller observations."""
import json
import tempfile
import unittest
from pathlib import Path

from wimna.live import LiveDirector
from wimna.store import Store


def field(value, unit):
    return {"value": value, "unit": unit, "source": "CSP_STATE",
            "quality": "available" if value is not None else "unavailable"}


def record(run, seq, kind, data, session="native-s0"):
    return {"schema": 1, "run_id": run, "seq": seq, "kind": kind,
            "physical_session_id": session, "sim_time_s": float(seq), "data": data}


def sample(run, seq, *, session="native-s0", stint=None, ai=None, human=None,
           tyre=None, air=None, track=None, pit=False):
    return record(run, seq, "sample", {
        "collection_stint_id": field(stint, "identifier"),
        "is_ai_controlled": field(ai, "boolean"),
        "is_user_controlled": field(human, "boolean"),
        "tyre_compound": field(tyre, "name"),
        "ambient_air_c": field(air, "degC"),
        "track_surface_c": field(track, "degC"),
        "in_pitlane": field(pit, "boolean"),
        "in_pit": field(False, "boolean"),
    }, session)


class CollectionIndexTests(unittest.TestCase):
    def test_live_director_exposes_collection_without_campaign_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "bridge"
            folder = bridge / "runs" / "collection_run"
            folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps({
                "physical_session_id": "collection_run-s0", "selection": "collection",
                "collection_enabled": True, "collection_id": "collection_run",
                "track_id": "spa", "layout_id": "default", "app_version": "0.4.0"}),
                encoding="utf-8")
            (folder / "000001.jsonl").write_text(json.dumps(record(
                "collection_run", 1, "event", {"type": "collection_stint_start",
                    "collection_stint_id": "s1", "planned_tire_label": "X"},
                "collection_run-s0")) + "\n", encoding="utf-8")
            (bridge / "current_state.json").write_text(json.dumps({
                "schema": 1, "run_id": "collection_run", "status": "active", "write_ok": True,
                "track_id": "spa", "layout_id": "default", "collection_id": "collection_run",
                "collection_stint_id": "s1", "planned_tire_label": "X",
                "requested_tyre_compound": "C3", "observed_tyre_compound": "C3",
                "is_ai_controlled": True, "is_user_controlled": False}), encoding="utf-8")
            with LiveDirector(bridge, root / "data") as director:
                view = director.poll()["director"]
                self.assertEqual(view["state"], "collection_stint_recording")
                self.assertEqual(view["mode"], "DATA_COLLECTION")
                self.assertEqual(view["ai_control_status"], "observed_on")
                self.assertIn("observed tire C3", view["target"])
                self.assertIsNone(director.store.get_run_assignment("collection_run"))
                self.assertEqual(director.scheduler.state["phase"], "idle")
                state = json.loads((bridge / "current_state.json").read_text(encoding="utf-8"))
                state.update(status="ended", collection_stint_id=None)
                (bridge / "current_state.json").write_text(json.dumps(state), encoding="utf-8")
                ended = director.poll()["director"]
                self.assertEqual(ended["state"], "collection_saved")
                self.assertEqual(ended["recording_status"], "ended")

    def test_two_compounds_and_controller_handoff_share_measured_provenance(self):
        with tempfile.TemporaryDirectory() as path, Store(path) as store:
            store.start_run("native", "MEASURED", {
                "physical_session_id": "native-s0", "collection_enabled": True,
                "collection_id": "native", "selection": "collection",
                "car_id": "fh26", "track_id": "spa", "layout_id": "default",
                "setup_label_requested": "default stable",
                "setup_label_source": "DRIVER_ENTERED_UNVERIFIED", "setup_hash": None})
            inputs = [
                record("native", 1, "event", {"type": "collection_stint_start",
                    "collection_stint_id": "s1", "planned_tire_label": "X",
                    "requested_tyre_compound": "C3", "observed_tyre_compound": "C3",
                    "condition_label": "hot", "setup_label_requested": "high cooling",
                    "setup_label_source": "DRIVER_ENTERED_UNVERIFIED", "setup_hash": None,
                    "stint_session_mode": "wimna-data", "stint_extended_physics": True}),
                sample("native", 2, stint="s1", ai=True, human=False,
                       tyre="C3", air=32, track=36),
                record("native", 3, "event", {"type": "collection_handoff_requested",
                    "collection_stint_id": "s1"}),
                sample("native", 4, stint="s1", ai=False, human=True,
                       tyre="C3", air=32, track=36, pit=True),
                record("native", 5, "event", {"type": "collection_pit_transition",
                    "in_pitlane": True, "in_pit": False}),
                record("native", 6, "event", {"type": "collection_stint_end",
                    "collection_stint_id": "s1", "reason": "tire_change",
                    "session_mode": "wimna-data", "extended_physics_observed": True}),
                record("native", 7, "event", {"type": "collection_stint_start",
                    "collection_stint_id": "s2", "planned_tire_label": "Y",
                    "requested_tyre_compound": "C4", "observed_tyre_compound": "C4",
                    "condition_label": "hot"}),
                sample("native", 8, stint="s2", ai=True, human=False,
                       tyre="C4", air=33, track=36),
                record("native", 9, "event", {"type": "collection_stint_end",
                    "collection_stint_id": "s2", "reason": "done"}),
            ]
            for item in inputs:
                store.add_record(item)
            stints = list(store.iter_collection_stints("native"))
            self.assertEqual([item["collection_stint_id"] for item in stints], ["s1", "s2"])
            self.assertEqual([item["requested_tyre_compound"] for item in stints], ["C3", "C4"])
            self.assertEqual([item["planned_tire_label"] for item in stints], ["X", "Y"])
            self.assertEqual([item["setup_label_requested"] for item in stints],
                             ["high cooling", "default stable"])
            self.assertEqual([item["setup_label_source"] for item in stints],
                             ["DRIVER_ENTERED_UNVERIFIED"] * 2)
            self.assertEqual([item["setup_hash"] for item in stints], [None, None])
            self.assertEqual([item["observed_tyre_compounds"] for item in stints], [["C3"], ["C4"]])
            self.assertEqual([item["status"] for item in stints], ["complete", "complete"])
            self.assertEqual(stints[0]["observed_tyre_compound_at_start"], "C3")
            self.assertEqual(stints[0]["end_reason"], "tire_change")
            self.assertEqual(stints[0]["session_mode_observed"], "wimna-data")
            self.assertTrue(stints[0]["extended_physics_observed"])
            self.assertFalse(stints[0]["session_mode_changed_within_stint"])
            self.assertFalse(stints[0]["extended_physics_changed_within_stint"])
            self.assertEqual((stints[0]["ai_sample_count"], stints[0]["human_sample_count"]), (1, 1))
            self.assertEqual(stints[0]["pit_transition_count"], 1)
            self.assertEqual(stints[0]["handoff_request_count"], 1)
            samples = list(store.iter_collection_samples("native"))
            self.assertEqual([item["controller_observed"] for item in samples], ["AI", "HUMAN", "AI"])
            self.assertEqual([item["evidence_mode"] for item in samples], ["MEASURED"] * 3)
            self.assertEqual([item["ambient_air_c"] for item in samples], [32, 32, 33])
            self.assertEqual(samples[1]["observed_tyre_compound"], "C3")
            self.assertEqual(samples[0]["setup_label_requested"], "high cooling")
            self.assertIsNone(samples[0]["setup_hash"])

    def test_physical_reset_never_continues_same_stint_id(self):
        with tempfile.TemporaryDirectory() as path, Store(path) as store:
            store.start_run("native", "MEASURED", {
                "physical_session_id": "native-s0", "collection_enabled": True})
            store.add_record(record("native", 1, "event", {
                "type": "collection_stint_start", "collection_stint_id": "s1"}))
            store.add_record(sample("native", 2, stint="s1", ai=True, tyre="C3"))
            store.add_record(record("native", 3, "event", {
                "type": "session_changed", "physical_session_id": "native-s1"}, "native-s1"))
            after_reset = sample("native", 4, session="native-s1", stint="s1",
                                 ai=True, tyre="C3")
            after_reset.pop("physical_session_id")  # older adapters may omit it after the reset event
            store.add_record(after_reset)
            stints = list(store.iter_collection_stints("native"))
            self.assertEqual(len(stints), 2)
            self.assertEqual([item["physical_session_id"] for item in stints],
                             ["native-s0", "native-s1"])
            self.assertEqual([item["sample_count"] for item in stints], [1, 1])
            self.assertEqual(stints[0]["status"], "physical_session_changed")
            self.assertEqual(stints[1]["status"], "open_or_interrupted")

    def test_boundary_readback_detects_mode_or_physics_change(self):
        with tempfile.TemporaryDirectory() as path, Store(path) as store:
            store.start_run("native", "MEASURED", {"physical_session_id": "native-s0",
                                                        "collection_enabled": True,
                                                        "session_mode": "mode-at-run-start",
                                                        "extended_physics": True})
            store.add_record(record("native", 1, "event", {
                "type": "collection_stint_start", "collection_stint_id": "s1",
                "stint_session_mode": "mode-a", "stint_extended_physics": True}))
            store.add_record(sample("native", 2, stint="s1", ai=True, tyre="C3"))
            store.add_record(record("native", 3, "event", {
                "type": "collection_stint_end", "collection_stint_id": "s1",
                "session_mode": "mode-b", "extended_physics_observed": False}))
            indexed = next(store.iter_collection_stints("native"))
            self.assertEqual(indexed["session_mode_observed"], "mode-a")
            self.assertEqual(indexed["session_mode_observed_at_end"], "mode-b")
            self.assertTrue(indexed["session_mode_changed_within_stint"])
            self.assertTrue(indexed["extended_physics_changed_within_stint"])
            point = next(store.iter_collection_samples("native"))
            self.assertEqual(point["session_mode_observed"], "mode-a")
            self.assertTrue(point["extended_physics_observed"])

    def test_legacy_ai_run_is_collection_evidence_without_false_compound(self):
        with tempfile.TemporaryDirectory() as path, Store(path) as store:
            store.start_run("old", "MEASURED", {
                "physical_session_id": "old-s0", "selection": "ai_thermal",
                "track_id": "spa", "layout_id": "default"})
            store.add_record(sample("old", 1, session="old-s0", ai=True,
                                    human=False, tyre=None, air=30, track=35))
            stints = list(store.iter_collection_stints("old"))
            self.assertEqual(len(stints), 1)
            self.assertEqual(stints[0]["boundary_source"], "legacy_implicit")
            self.assertEqual(stints[0]["ai_sample_count"], 1)
            self.assertEqual(stints[0]["observed_tyre_compounds"], [])
            point = next(store.iter_collection_samples("old"))
            self.assertEqual(point["controller_observed"], "AI")
            self.assertIsNone(point["observed_tyre_compound"])


if __name__ == "__main__":
    unittest.main()
