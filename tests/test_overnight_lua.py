"""Exercise overnight CSP mode readback and the recorder's guarded auto-start."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import unittest

try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional Lua interpreter
    LuaRuntime = None


SOURCE = (Path(__file__).resolve().parents[1] / "ac_app" / "apps" / "lua"
          / "WimnaTestDirector" / "WimnaTestDirector.lua").read_text(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HASHES = {
    "baseline": "1b0fd209d1c46087bdbf3f8ff0ac6fe30472ae7938b29d662371772e0496bb12",
    "plan_c": "4cafb3a4cf76afe426126b660996fd29777a58f031322c4ccc9a2ef974697cd9",
}


def mode_parameters(variant: str) -> str:
    mode_id = "wimna-ai-baseline" if variant == "baseline" else "wimna-ai-plan-c"
    source = (ROOT / "ac_app" / "extension" / "lua" / "new-modes"
              / mode_id / "mode.lua").read_text(encoding="utf-8")
    match = re.search(r"local parameters = \[\[(.*?)\]\]", source)
    if match is None:
        raise AssertionError(f"{mode_id} did not publish canonical parameters")
    return match.group(1)


@unittest.skipUnless(LuaRuntime, "install optional lupa to execute Lua integration tests")
class OvernightLuaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().python_sha256 = (
            lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest())
        self.lua.execute("""
            script = {}
            car = { isAIControlled = false, extendedPhysics = true,
              physicsAvailable = true, speedKmh = 0, lapCount = 0,
              resetCounter = 0, restrictor = 0, aeroLiftFront = -1.2,
              aeroLiftRear = -1.5, aeroDrag = 0.9 }
            car.tyresName = function() return 'C4' end
            sim = { systemTime = 1, currentSessionIndex = 0,
              isReplayActive = false, isLive = true,
              ambientTemperature = 25, roadTemperature = 29 }
            commands = {}
            shared = {}
            saved = {}
            physicsPermission = true
            ac = {
              FolderID = { Documents = 1, CurrentTrack = 2,
                CurrentTrackLayout = 3, ContentCars = 4,
                CurrentTrackLayoutUI = 5 },
              getSim = function() return sim end,
              getCar = function() return car end,
              getFolder = function(id)
                if id == 1 then return 'documents' end
                if id == 4 then return 'cars' end
                if id == 5 then return 'track_ui' end
                return 'track'
              end,
              getTrackID = function() return 'spa' end,
              getCarID = function() return 'rss_formula_hybrid_2026' end,
              getTrackLayout = function() return 'default' end,
              getSessionName = function() return 'Practice' end,
              getPatchVersionCode = function() return 3465 end,
              getGameDeltaT = function() return 0.05 end,
              load = function(key) return shared[key] end,
              checksumSHA256 = function(value) return python_sha256(value) end
            }
            physics = {
              allowed = function() return physicsPermission end,
              setCarAutopilot = function(desired)
                table.insert(commands, desired)
                return true
              end
            }
            JSON = {
              stringify = function(value) return string.format('%q', tostring(value)) end,
              parse = function(value)
                return { version = value:match('"version"%s*:%s*"([^"]+)"'),
                  variant = value:match('"variant"%s*:%s*"([^"]+)"') }
              end
            }
            io.createDir = function() return true end
            io.save = function(path, payload)
              saved[path] = payload
              return true
            end
            io.load = function(path) return saved[path] end
            saved['cars/rss_formula_hybrid_2026/ui/ui_car.json'] = '{"version":"2"}'
            saved['track_ui/ui_track.json'] = '{"version":"1"}'
            os.preciseClock = function() return 1.25 end
            ui = {
              text = function() end,
              textWrapped = function() end,
              sameLine = function() end,
              button = function() return false end,
              inputText = function(_, value) return value end
            }
        """)
        self.app = self.lua.execute(SOURCE + "\nreturn { state = state, "
            "startRun = startRun, captureSample = captureSample, "
            "prototypeReadback = prototypeReadback }")
        self.state = self.app.state
        self.shared = self.lua.globals().shared
        self.car = self.lua.globals().car
        self.commands = self.lua.globals().commands
        self.saved = self.lua.globals().saved

    def mode(self, variant: str, effect: str = "none", permission: int = 1) -> None:
        self.shared["wimna.session_mode.v1"] = (
            "wimna-ai-baseline" if variant == "baseline" else "wimna-ai-plan-c")
        self.shared["wimna.prototype.variant.v1"] = variant
        self.shared["wimna.prototype.parameter_hash.v1"] = EXPECTED_HASHES[variant]
        self.shared["wimna.prototype.parameters.v1"] = mode_parameters(variant)
        self.shared["wimna.prototype.effect.v1"] = effect
        self.shared["wimna.prototype.permission.v1"] = permission
        self.shared["wimna.prototype.native_invalid_expected.v1"] = (
            0 if variant == "baseline" else 1)

    def manifest(self) -> dict:
        key = f"documents/WIMNA Test Director/bridge/runs/{self.state.runId}/manifest.json"
        return json.loads(self.saved[key])

    def test_baseline_mode_autostarts_collection_and_requests_ai(self) -> None:
        self.mode("baseline")
        self.lua.globals().script.update(0.5)
        self.assertEqual(self.state.mode, "collection")
        self.assertEqual(self.state.collectionConditionLabel, "baseline")
        self.assertTrue(self.state.overnightAutoStartAttempted)
        self.assertEqual(len(self.commands), 1)
        manifest = self.manifest()
        self.assertEqual(manifest["start_trigger"], "overnight_mode_auto")
        self.assertEqual(manifest["car_version"], "2")
        self.assertEqual(manifest["track_version"], "1")
        self.assertEqual(manifest["prototype"]["variant_id"], "baseline")
        self.assertEqual(manifest["prototype"]["parameter_hash_reported"], EXPECTED_HASHES["baseline"])
        self.assertTrue(manifest["prototype"]["parameter_hash_verified"])
        self.assertFalse(manifest["prototype"]["force_native_lap_invalid_expected"])
        self.assertFalse(manifest["prototype"]["physical_effect_verified"])

    def test_plan_c_waits_for_worker_then_records_requested_status(self) -> None:
        self.mode("plan_c", effect="pending")
        self.lua.globals().script.update(0.5)
        self.assertIsNone(self.state.runId)
        self.assertEqual(len(self.commands), 0)
        self.shared["wimna.prototype.effect.v1"] = "worker_requested_effect_unverified"
        self.lua.globals().script.update(0.5)
        self.assertEqual(self.state.mode, "collection")
        self.assertEqual(self.state.collectionConditionLabel, "plan_c")
        self.assertTrue(self.manifest()["prototype"]["force_native_lap_invalid_expected"])
        self.assertEqual(self.manifest()["prototype"]["effect_status"],
                         "worker_requested_effect_unverified")

    def test_missing_native_permission_preserves_manual_start(self) -> None:
        self.mode("baseline", permission=0)
        self.lua.globals().script.update(0.5)
        self.assertIsNone(self.state.runId)
        self.assertEqual(len(self.commands), 0)
        self.app.startRun("collection", None, False)
        self.assertEqual(self.state.mode, "collection")
        self.assertEqual(len(self.commands), 0)
        self.assertEqual(self.manifest()["start_trigger"], "driver_button")

    def test_non_overnight_mode_never_autostarts(self) -> None:
        self.mode("baseline")
        self.shared["wimna.session_mode.v1"] = "wimna-ai-thermal"
        self.lua.globals().script.update(1)
        self.assertIsNone(self.state.runId)

    def test_changed_parameter_content_blocks_automatic_start(self) -> None:
        self.mode("plan_c", effect="worker_requested_effect_unverified")
        self.shared["wimna.prototype.parameters.v1"] = mode_parameters("plan_c").replace(
            '"delta_cd_a_m2":0.012', '"delta_cd_a_m2":0.020')
        self.lua.globals().script.update(0.5)
        self.assertIsNone(self.state.runId)
        self.assertEqual(len(self.commands), 0)
        self.assertFalse(self.app.prototypeReadback()["parameter_hash_verified"])

    def test_wrong_lap_validity_policy_blocks_automatic_start(self) -> None:
        self.mode("plan_c", effect="worker_requested_effect_unverified")
        self.shared["wimna.prototype.native_invalid_expected.v1"] = 0
        self.lua.globals().script.update(0.5)
        self.assertIsNone(self.state.runId)
        self.assertEqual(len(self.commands), 0)

    def test_native_and_prototype_channels_keep_false_and_missing_distinct(self) -> None:
        self.mode("baseline")
        self.app.startRun("collection", None, False)
        self.app.captureSample()
        sample = [item for item in self.state.buffer.values()
                  if item["kind"] == "sample"][-1]["data"]
        self.assertEqual(sample["restrictor_native"]["value"], 0)
        self.assertEqual(sample["aero_drag_native"]["value"], 0.9)
        self.assertFalse(sample["force_native_lap_invalid_expected"]["value"])
        self.assertEqual(sample["prototype_effect_status"]["value"], "none")
        self.shared["wimna.prototype.effect.v1"] = None
        self.car.aeroDrag = None
        self.app.captureSample()
        sample = [item for item in self.state.buffer.values()
                  if item["kind"] == "sample"][-1]["data"]
        self.assertEqual(sample["aero_drag_native"]["quality"], "unavailable")
        self.assertEqual(sample["prototype_effect_status"]["quality"], "unavailable")


if __name__ == "__main__":
    unittest.main()
