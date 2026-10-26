"""Read-only bridge observation checks with synthetic saved files."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wimna.bridge_observation import BridgeObservationProvider, PROTOTYPE_HASHES
from wimna.overnight import SessionSpec


NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "fixture-run"
NATIVE_ID = "fixture-run-s0"
PARAMETERS = ('{"aero_force":null,"cooling_model_multiplier":1.0,'
              '"effect":"none","variant":"baseline","version":"wimna-prototype-v1"}')


def _stamp(delta_s: int = 0) -> str:
    return (NOW + timedelta(seconds=delta_s)).isoformat().replace("+00:00", "Z")


def _record(seq: int, kind: str, data: dict, *, stamp_s: int = -1,
            native_id: str = NATIVE_ID) -> dict:
    return {"schema": 1, "run_id": RUN_ID, "physical_session_id": native_id,
            "seq": seq, "sim_time_s": float(seq), "wall_time_utc": _stamp(stamp_s),
            "kind": kind, "data": data}


def _channel(value, unit: str = "native") -> dict:
    return {"value": value, "unit": unit, "source": "CSP_STATE", "quality": "available"}


class BridgeObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bridge = Path(temporary.name) / "bridge"
        self.run_dir = self.bridge / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.prototype = {
            "variant_id": "baseline", "mode_id": "wimna-ai-baseline",
            "parameters_variant_id": "baseline", "parameter_hash_verified": True,
            "parameters_json_requested": PARAMETERS,
            "parameter_hash_reported": PROTOTYPE_HASHES["baseline"],
            "parameter_hash_computed": PROTOTYPE_HASHES["baseline"],
            "effect_status": "none", "physics_permission_reported": 1,
            "force_native_lap_invalid_expected": False,
        }
        self.manifest = {
            "schema": 1, "run_id": RUN_ID, "physical_session_id": NATIVE_ID,
            "started_utc": _stamp(),
            "car_id": "rss_formula_hybrid_2026", "car_version": "v2a",
            "track_id": "spa", "layout_id": "default",
            "session_mode_id": "wimna-ai-baseline", "prototype": self.prototype,
            "selection": "collection", "start_trigger": "overnight_mode_auto",
        }
        self.state = {
            "schema": 1, "run_id": RUN_ID, "physical_session_id": NATIVE_ID,
            "updated_utc": _stamp(), "status": "active", "write_ok": True,
            "car_id": "rss_formula_hybrid_2026", "car_version": "v2a",
            "track_id": "spa", "layout_id": "default",
            "session_mode_id": "wimna-ai-baseline", "prototype": self.prototype,
            "mode": "collection", "extended_physics_observed": True,
            "observed_tyre_compound": "C4", "is_ai_controlled": True,
            "samples_attempted": 2, "recorded_lap_crossings": 1,
            "dropped_samples": 0,
        }
        self.records = [
            _record(1, "event", {"type": "ui_start"}, stamp_s=-5),
            _record(2, "sample", {
                "fuel": _channel(35.0, "L"),
                "ambient_air_c": _channel(20.0, "degC"),
                "track_surface_c": _channel(25.0, "degC"),
                "is_ai_controlled": _channel(True, "boolean"),
            }, stamp_s=-3),
            _record(3, "event", {"type": "lap_complete"}),
            _record(4, "sample", {
                "fuel": _channel(33.0, "L"),
                "ambient_air_c": _channel(20.0, "degC"),
                "track_surface_c": _channel(25.0, "degC"),
                "is_ai_controlled": _channel(True, "boolean"),
            }),
        ]
        self.spec = SessionSpec("fixture", {"mode_id": "wimna-ai-baseline"}, {},
                                1, 100, 120, 10, 10, 5, 0)
        self._save()

    def _save(self):
        (self.bridge / "current_state.json").write_text(json.dumps(self.state), encoding="utf-8")
        (self.run_dir / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.run_dir / "000001.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in self.records), encoding="utf-8")

    def _provider(self, running=True):
        provider = BridgeObservationProvider(self.bridge, clock_utc=lambda: NOW,
                                             process_running=lambda: running)
        state_path = self.bridge / "current_state.json"
        held_path = self.bridge / "current_state.before_launch.json"
        state_path.rename(held_path)
        try:
            provider.mark_launch_intent(NOW)
        finally:
            held_path.rename(state_path)
        return provider

    def _save_ack(self):
        identity = {"schema_version": 1, "command": "save_and_shutdown",
                    "run_id": RUN_ID, "physical_session_id": NATIVE_ID,
                    "session_mode_id": "wimna-ai-baseline", "nonce": "a" * 32}
        (self.bridge / "overnight_finalize_request.json").write_text(
            json.dumps({**identity, "min_recorded_lap_crossings": 1}), encoding="utf-8")
        (self.bridge / "overnight_finalize_ack.json").write_text(
            json.dumps({**identity, "status": "save_complete_shutdown_requested",
                        "write_ok": True, "chunk_count": 1,
                        "recorded_lap_crossings": 1}), encoding="utf-8")

    def test_observer_requires_launch_intent_and_rejects_preexisting_run(self):
        provider = BridgeObservationProvider(self.bridge, clock_utc=lambda: NOW,
                                             process_running=lambda: True)
        self.assertIsNone(provider(self.spec, 100.0))
        provider.mark_launch_intent(NOW)
        self.assertIsNone(provider(self.spec, 100.0))

    def test_active_readbacks_and_lap_boundary(self):
        observed = self._provider()(self.spec, 100.0)
        self.assertIsNotNone(observed)
        self.assertEqual(observed.physical_session_id, NATIVE_ID)
        self.assertEqual(observed.values["prototype_parameter_sha256"], PROTOTYPE_HASHES["baseline"])
        self.assertEqual(observed.values["fuel_start_l"], 35.0)
        self.assertEqual((observed.sample_seq, observed.lap_crossings,
                          observed.last_lap_at_s), (4, 1, 99.0))
        self.assertTrue(observed.ai_controlled)
        self.assertTrue(observed.recorder_active)
        self.assertFalse(observed.session_closed)
        for unavailable in ("preset_id", "preset_sha256", "variant_sha256",
                            "energy_profile_id"):
            self.assertNotIn(unavailable, observed.values)

    def test_live_capability_record_is_part_of_sequence(self):
        self.records[0] = _record(1, "capability", {"recording": {"status": "runtime_probe_passed"}},
                                  stamp_s=-5)
        self._save()
        observed = self._provider()(self.spec, 100.0)
        self.assertIsNotNone(observed)
        self.assertEqual((observed.sample_seq, observed.lap_crossings), (4, 1))

    def test_stale_or_missing_physical_readback_fails_closed(self):
        self.state["updated_utc"] = _stamp(-20)
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))
        self.state["updated_utc"] = _stamp()
        self.state["physical_session_id"] = "different-native-session"
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))

    def test_tampered_hash_and_incomplete_chunk_fail_closed(self):
        self.state["prototype"] = dict(self.prototype, parameter_hash_reported="0" * 64)
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))
        self.state["prototype"] = self.prototype
        self._save()
        (self.run_dir / "000001.jsonl").write_text(
            json.dumps({"$": "WIMNA_COMPACT_JSONL_V1", "schema": 1,
                        "run_id": RUN_ID, "channels": [["fuel", "L", "CSP_STATE"]]}) + "\n",
            encoding="utf-8")
        self.assertIsNone(self._provider()(self.spec, 100.0))

    def test_plan_c_requires_worker_requested_status_not_physical_claim(self):
        source = (Path(__file__).resolve().parents[1] / "ac_app" / "extension" /
                  "lua" / "new-modes" / "wimna-ai-plan-c" / "mode.lua").read_text(encoding="utf-8")
        parameters = re.search(r"local parameters = \[\[(.*?)\]\]", source).group(1)
        self.prototype.update({
            "variant_id": "plan_c", "mode_id": "wimna-ai-plan-c",
            "parameters_variant_id": "plan_c", "parameters_json_requested": parameters,
            "parameter_hash_reported": PROTOTYPE_HASHES["plan_c"],
            "parameter_hash_computed": PROTOTYPE_HASHES["plan_c"],
            "effect_status": "worker_requested_effect_unverified",
            "force_native_lap_invalid_expected": True,
            "worker_force_call_pairs_reported": 1,
        })
        self.state["session_mode_id"] = "wimna-ai-plan-c"
        self.manifest["session_mode_id"] = "wimna-ai-plan-c"
        self._save()
        observed = self._provider()(self.spec, 100.0)
        self.assertIsNotNone(observed)
        self.assertEqual(observed.values["prototype_parameter_sha256"],
                         PROTOTYPE_HASHES["plan_c"])
        self.assertNotIn("physical_effect_verified", observed.values)
        self.prototype["effect_status"] = "pending"
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))

    def test_end_requires_saved_event_and_process_exit_for_closed(self):
        self.records.append(_record(5, "event", {"type": "ui_end",
                                                 "reason": "overnight_switch"}))
        self.state["status"] = "ended"
        self._save()
        self._save_ack()
        still_running = self._provider()(self.spec, 100.0)
        self.assertTrue(still_running.recorder_saved)
        self.assertFalse(still_running.recorder_active)
        self.assertFalse(still_running.session_closed)
        self.assertTrue(self._provider(running=False)(self.spec, 100.0).session_closed)
        self.assertIsNone(self._provider(running=None)(self.spec, 100.0).session_closed)

    def test_missing_or_wrong_ack_does_not_claim_saved(self):
        self.records.append(_record(5, "event", {"type": "ui_end",
                                                 "reason": "overnight_switch"}))
        self.state["status"] = "ended"
        self._save()
        self.assertIsNone(self._provider(running=False)(self.spec, 100.0).recorder_saved)
        self._save_ack()
        ack_path = self.bridge / "overnight_finalize_ack.json"
        ack = json.loads(ack_path.read_text(encoding="utf-8"))
        ack["nonce"] = "b" * 32
        ack_path.write_text(json.dumps(ack), encoding="utf-8")
        self.assertIsNone(self._provider(running=False)(self.spec, 100.0).session_closed)

    def test_native_lap_count_and_drop_gaps_cannot_be_invented(self):
        self.state["recorded_lap_crossings"] = 2
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))
        self.state["recorded_lap_crossings"] = 1
        self.records[3]["seq"] = 6
        self._save()
        self.assertIsNone(self._provider()(self.spec, 100.0))


if __name__ == "__main__":
    unittest.main()
