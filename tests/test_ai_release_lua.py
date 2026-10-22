"""Execute the CSP Lua control path with a mocked car and native readback.

Install the optional test interpreter with ``python -m pip install lupa``.
The production app has no Python/Lupa dependency.
"""

from pathlib import Path
import unittest

from wimna.store import _compact_chunk_records

try:
    from lupa import LuaRuntime
except ImportError:  # pragma: no cover - optional Lua interpreter
    LuaRuntime = None


SOURCE = (Path(__file__).resolve().parents[1] / "ac_app" / "apps" / "lua"
          / "WimnaTestDirector" / "WimnaTestDirector.lua").read_text(encoding="utf-8")


@unittest.skipUnless(LuaRuntime, "install optional lupa to execute Lua safety tests")
class AiReleaseLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("""
            script = {}
            car = { isAIControlled = true, speedKmh = 186,
              isInPitlane = false, isInPit = false }
            sim = { systemTime = 1 }
            commands = {}
            clock = 1
            os.preciseClock = function() return clock end
            ac = {
              FolderID = { Documents = 1, CurrentTrack = 2,
                CurrentTrackLayout = 3 },
              getSim = function() return sim end,
              getCar = function() return car end,
              getFolder = function() return 'test' end,
              getTrackID = function() return 'spa' end,
              getCarID = function() return 'rss_formula_hybrid_2026' end,
              getTrackLayout = function() return 'default' end
            }
            physics = {
              allowed = function() return true end,
              setCarAutopilot = function(desired)
                table.insert(commands, desired)
                return true
              end
            }
            JSON = { stringify = function(value)
              return string.format('%q', tostring(value))
            end }
            saved = {}
            io.save = function(path, payload)
              saved[path] = payload
              return true
            end
            selectedButton = nil
            ui = {
              text = function() end,
              textWrapped = function() end,
              sameLine = function() end,
              button = function(label) return label == selectedButton end,
              inputText = function(_, value) return value end
            }
        """)
        self.app = self.lua.execute(SOURCE + "\nreturn { state = state, "
            "endAiRun = endAiRun, requestAiControl = requestAiControl, "
            "pollAiControl = pollAiControl, finishRun = finishRun, startRun = startRun, "
            "endCollectionStint = endCollectionStint, queue = queue, flush = flush, "
            "envelope = envelope, channel = channel }")
        self.state = self.app.state
        self.car = self.lua.globals().car
        self.commands = self.lua.globals().commands
        self.state.runId = "test-run"
        self.state.status = "active"
        self.state.mode = "collection"
        self.state.writeOk = False
        self.state.aiObserved = True

    def events(self):
        return [record["data"]["type"] for record in self.state.buffer.values()
                if record["kind"] == "event"]

    def test_end_at_speed_closes_recorder_and_keeps_ai_on(self):
        self.app.endAiRun("driver_requested")
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)
        self.assertTrue(self.car.isAIControlled)
        self.assertTrue(self.state.aiStopUnverified)
        self.assertIn("ai_control_retained_after_end", self.events())
        self.assertIn("ui_end", self.events())

    def test_direct_stop_at_speed_is_blocked(self):
        self.assertFalse(self.app.requestAiControl(False, "driver_button_at_rest"))
        self.assertEqual(len(self.commands), 0)
        self.assertIn("ai_release_blocked_for_safety", self.events())

    def test_stop_button_is_not_rendered_while_moving(self):
        self.lua.globals().selectedButton = "Stop AI Driver at Rest"
        self.lua.globals().script.windowMain()
        self.assertEqual(len(self.commands), 0)
        self.car.speedKmh = 0
        self.lua.globals().script.windowMain()
        self.assertEqual(len(self.commands), 1)

    def test_end_at_rest_also_keeps_ai_on_until_separate_stop(self):
        self.car.speedKmh = 0
        self.app.endAiRun("driver_requested")
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)
        self.assertTrue(self.state.aiStopUnverified)

    def test_separate_stop_at_rest_waits_for_native_off_readback(self):
        self.car.speedKmh = 0
        self.assertTrue(self.app.requestAiControl(False, "driver_button_at_rest"))
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(self.commands[1])
        self.assertEqual(self.state.aiRequest.desired, False)
        self.car.isAIControlled = False
        self.app.pollAiControl()
        self.assertIn("ai_control_effect_verified", self.events())

    def test_end_stint_does_not_change_ai_control(self):
        self.state.collectionStintId = "test-stint"
        self.state.collectionStintMeta = self.lua.table_from({"started_sim_time_s": 0})
        self.app.endCollectionStint("driver_boxed_or_requested")
        self.assertEqual(len(self.commands), 0)
        self.assertEqual(self.state.status, "active")

    def test_pit_handoff_button_only_marks_data(self):
        self.state.collectionStintId = "test-stint"
        self.state.collectionStintMeta = self.lua.table_from({"started_sim_time_s": 0})
        self.lua.globals().selectedButton = "Mark pit / control handoff"
        self.lua.globals().script.windowMain()
        self.assertEqual(len(self.commands), 0)
        self.assertIn("collection_handoff_requested", self.events())

    def test_end_collection_button_does_not_send_ai_off(self):
        self.lua.globals().selectedButton = "End Collection Run"
        self.lua.globals().script.windowMain()
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)

    def test_unknown_speed_blocks_direct_stop(self):
        self.car.speedKmh = None
        self.assertFalse(self.app.requestAiControl(False, "driver_button_at_rest"))
        self.assertEqual(len(self.commands), 0)

    def test_unknown_ai_readback_ends_without_off_command(self):
        self.car.isAIControlled = None
        self.car.speedKmh = 0
        self.app.endAiRun("driver_requested")
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)
        self.assertTrue(self.state.aiStopUnverified)

    def test_pending_enable_does_not_become_a_blind_toggle(self):
        self.car.isAIControlled = False
        self.state.aiRequest = self.lua.table_from({"desired": True,
            "requestedAt": 1, "method": "physics.setCarAutopilot"})
        self.app.endAiRun("driver_requested")
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)
        self.assertTrue(self.state.aiStopUnverified)
        self.assertIsNotNone(self.state.aiRequest)
        self.app.startRun("full", None, False)
        self.assertEqual(self.state.runId, "test-run")
        self.assertEqual(self.state.status, "ended")
        self.app.pollAiControl()
        self.assertTrue(self.state.aiStopUnverified)
        self.assertIsNotNone(self.state.aiRequest)
        self.assertNotIn("ai_stop_observed_after_run", self.events())
        self.car.isAIControlled = True
        self.app.pollAiControl()
        self.assertIsNone(self.state.aiRequest)
        self.app.startRun("full", None, False)
        self.assertEqual(self.state.runId, "test-run")
        self.assertEqual(self.state.status, "ended")

    def test_pending_enable_timeout_is_not_mislabeled_as_verified_stop(self):
        self.car.isAIControlled = False
        self.state.aiObserved = False
        self.state.aiRequest = self.lua.table_from({"desired": True,
            "requestedAt": 1, "method": "physics.setCarAutopilot"})
        self.app.endAiRun("driver_requested")
        self.lua.globals().clock = 5
        self.app.pollAiControl()
        self.assertIsNone(self.state.aiRequest)
        self.assertFalse(self.state.aiStopUnverified)
        self.assertIn("ai_enable_unverified_after_end", self.events())
        self.assertNotIn("ai_stop_observed_after_run", self.events())

    def test_post_run_stop_is_allowed_only_at_rest_and_verified(self):
        self.app.endAiRun("driver_requested")
        self.assertFalse(self.app.requestAiControl(False, "post_run_safe_stop"))
        self.assertEqual(len(self.commands), 0)
        self.car.speedKmh = 0
        self.assertTrue(self.app.requestAiControl(False, "post_run_safe_stop"))
        self.assertEqual(len(self.commands), 1)
        self.car.isAIControlled = False
        self.app.pollAiControl()
        self.assertFalse(self.state.aiStopUnverified)
        self.assertIn("ai_stop_observed_after_run", self.events())

    def test_generic_end_does_not_request_ai_off_at_speed(self):
        self.state.mode = "full"
        self.app.finishRun("driver_requested", None)
        self.assertEqual(self.state.status, "ended")
        self.assertEqual(len(self.commands), 0)
        self.assertTrue(self.state.aiStopUnverified)

    def test_lua_compact_chunk_roundtrips_null_zero_and_event(self):
        self.state.writeOk = True
        self.state.simTimeS = 1.25
        channels = self.lua.table_from({
            "battery_energy_j": self.app.channel(None, "J", "RSS_UNVERIFIED"),
            "fuel_l": self.app.channel(0, "L", "CSP_STATE"),
        })
        self.assertTrue(self.app.queue(self.app.envelope("sample", channels), True))
        self.assertTrue(self.app.queue(self.app.envelope("event",
            self.lua.table_from({"type": "marker"})), False))
        self.app.flush()
        payloads = list(self.lua.globals().saved.values())
        self.assertEqual(len(payloads), 1)
        records = [record for _, record in _compact_chunk_records(payloads[0].encode("utf-8"))]
        self.assertEqual(len(records), 2)
        self.assertIsNone(records[0]["data"]["battery_energy_j"]["value"])
        self.assertEqual(records[0]["data"]["battery_energy_j"]["quality"], "unavailable")
        self.assertEqual(records[0]["data"]["fuel_l"]["value"], 0)
        self.assertEqual(records[0]["data"]["fuel_l"]["quality"], "available")
        self.assertEqual(records[1]["data"]["type"], "marker")


if __name__ == "__main__":
    unittest.main()
