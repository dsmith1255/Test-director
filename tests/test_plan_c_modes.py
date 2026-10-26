"""Exercise the CSP mode handshake and bound the experimental force proxy."""

import hashlib
import json
from pathlib import Path
import unittest

try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional test interpreter
    LuaRuntime = None


MODES = (Path(__file__).resolve().parents[1] / "ac_app" / "extension"
         / "lua" / "new-modes")


@unittest.skipUnless(LuaRuntime, "install optional lupa to execute Lua mode tests")
class PlanCModesTests(unittest.TestCase):
    def mode(self, name, allowed=True, worker_fails=False):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            script = {}
            saved = {}
            calls = 0
            ac = {
              store = function(key, value) saved[key] = value end,
              isLuaAppRunning = function() return false end,
              setAppOpen = function() end
            }
            physics = {
              allowed = function() return permission end,
              startPhysicsWorker = function(name, parameters, callback)
                if failWorker then error('worker rejected') end
                calls = calls + 1
                workerName = name
                workerParameters = parameters
                if callbackError then callback('worker load rejected') end
              end
            }
        """)
        lua.globals().permission = allowed
        lua.globals().failWorker = worker_fails is True
        lua.globals().callbackError = worker_fails == "callback"
        source = (MODES / name / "mode.lua").read_text(encoding="utf-8")
        lua.execute(source)
        lua.globals().script.update(0.003)
        return lua

    def test_paired_modes_stamp_distinct_hashed_parameters(self):
        for name, variant in (("wimna-ai-baseline", "baseline"),
                              ("wimna-ai-plan-c", "plan_c")):
            lua = self.mode(name)
            saved = lua.globals().saved
            self.assertEqual(saved["wimna.session_mode.v1"], name)
            self.assertEqual(saved["wimna.prototype.variant.v1"], variant)
            parameters = saved["wimna.prototype.parameters.v1"]
            self.assertEqual(
                saved["wimna.prototype.parameter_hash.v1"],
                hashlib.sha256(parameters.encode("utf-8")).hexdigest())
            self.assertEqual(saved["wimna.prototype.permission.v1"], 1)
            self.assertEqual(json.loads(parameters)["version"],
                             "wimna-prototype-v1")
            self.assertEqual(json.loads(parameters)["variant"], variant)
            self.assertEqual(saved["wimna.prototype.native_invalid_expected.v1"],
                             1 if variant == "plan_c" else 0)
            self.assertEqual(saved["wimna.prototype.effect.v1"],
                             "worker_requested_effect_unverified" if variant == "plan_c"
                             else "none")
            self.assertEqual(saved["wimna.prototype.force_call_pairs.v1"], 0)
            self.assertEqual(saved["wimna.prototype.worker_telemetry_status.v1"],
                             "pending" if variant == "plan_c" else "not_applicable")
        self.assertEqual(self.mode("wimna-ai-baseline").globals().calls, 0)
        self.assertEqual(self.mode("wimna-ai-plan-c").globals().calls, 1)

    def test_pair_uses_same_native_practice_and_extended_physics_rules(self):
        def settings(name):
            text = (MODES / name / "manifest.ini").read_text(encoding="utf-8")
            return {line.strip() for line in text.splitlines()
                    if line.startswith(("BASE_MODE", "START_TYPE",
                                        "ALLOW_PHYSICS_ALTERATIONS",
                                        "FORCE_EXTENDED_PHYSICS", "IMMEDIATE_START",
                                        "ASSOCIATED_APPS"))}
        self.assertEqual(settings("wimna-ai-baseline"),
                         settings("wimna-ai-plan-c"))
        self.assertIn("IMMEDIATE_START = 1", settings("wimna-ai-baseline"))

    def test_permission_rejection_does_not_start_force_worker(self):
        lua = self.mode("wimna-ai-plan-c", allowed=False)
        self.assertEqual(lua.globals().calls, 0)
        saved = lua.globals().saved
        self.assertEqual(saved["wimna.prototype.effect.v1"],
                         "permission_unavailable")
        self.assertEqual(saved["wimna.prototype.permission.v1"], 0)

    def test_worker_rejection_is_not_reported_as_effect(self):
        lua = self.mode("wimna-ai-plan-c", worker_fails=True)
        self.assertEqual(lua.globals().calls, 0)
        self.assertEqual(lua.globals().saved["wimna.prototype.effect.v1"],
                         "worker_start_failed")
        callback_rejection = self.mode("wimna-ai-plan-c",
                                       worker_fails="callback")
        self.assertEqual(callback_rejection.globals().saved[
            "wimna.prototype.effect.v1"], "worker_start_failed")

    def test_worker_force_direction_bounds_and_invalid_timestep(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            script = {}
            forces = {}
            saved = {}
            velocity = {x = 0, y = 0, z = -80}
            sim = {ambientTemperature = 20}
            vec3 = function() return {x = 0, y = 0, z = 0} end
            ac = {getSim = function() return sim end,
                  store = function(key, value) saved[key] = value end}
            physics = {
              getPointVelocity = function() return velocity end,
              addForce = function(carIndex, origin, posLocal, force, forceLocal)
                table.insert(forces, {carIndex = carIndex,
                  x = force.x, y = force.y, z = force.z,
                  forceLocal = forceLocal})
              end
            }
        """)
        source = (MODES / "wimna-ai-plan-c" / "mode.lua").read_text(
            encoding="utf-8")
        params_text = source.split("local parameters = [[", 1)[1].split("]]", 1)[0]
        params = json.loads(params_text)
        lua.globals()["__input"] = lua.table_from({
            "deltaCdA": params["delta_cd_a_m2"],
            "deltaDownforceArea": params["delta_downforce_area_m2"],
            "pressurePa": params["air_pressure_pa_assumed"],
            "ambientFallbackC": params["ambient_fallback_c"],
            "maxDragN": params["max_drag_n"],
            "maxDownforceN": params["max_downforce_n"],
            "maxDtS": params["max_dt_s"],
            "maxSpeedMS": params["max_speed_m_s"],
            "nominalStepS": params["nominal_step_s"],
        })
        lua.execute((MODES / "wimna-ai-plan-c" / "aero_worker.lua")
                    .read_text(encoding="utf-8"))
        lua.globals().script.update(0.003)
        forces = lua.globals().forces
        self.assertEqual(len(forces), 2)
        self.assertGreater(forces[1]["z"], 0)  # opposes forward velocity
        self.assertLess(forces[2]["y"], 0)     # added downforce
        self.assertLessEqual(forces[1]["z"], params["max_drag_n"])
        self.assertLessEqual(-forces[2]["y"], params["max_downforce_n"])
        lua.globals().script.update(0.050)
        self.assertEqual(len(forces), 2)       # no catch-up impulse
        lua.globals().velocity.z = -400
        lua.globals().script.update(0.003)
        self.assertLessEqual(forces[3]["z"], params["max_drag_n"])
        self.assertLessEqual(-forces[4]["y"], params["max_downforce_n"])
        self.assertIsNone(lua.globals().saved["wimna.prototype.force_call_pairs.v1"])
        lua.globals().velocity.z = -80
        for _ in range(334):
            lua.globals().script.update(0.003)
        saved = lua.globals().saved
        self.assertGreaterEqual(saved["wimna.prototype.force_call_pairs.v1"], 334)
        self.assertGreater(saved["wimna.prototype.last_drag_n.v1"], 0)
        self.assertGreater(saved["wimna.prototype.last_downforce_n.v1"], 0)
        self.assertAlmostEqual(saved["wimna.prototype.last_speed_m_s.v1"], 80.0)
        self.assertEqual(saved["wimna.prototype.worker_telemetry_status.v1"],
                         "force_calls_issued_effect_unverified")


if __name__ == "__main__":
    unittest.main()
