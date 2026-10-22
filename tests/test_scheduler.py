"""Tests for the deterministic campaign state machine."""

import json
import unittest

from wimna.config import load_campaign
from wimna.scheduler import CampaignScheduler, SchedulerError, default_campaign_config


def begin_block(scheduler, session_id="session-1", elapsed=1.0):
    scheduler.dispatch("prepare", native_session_id=session_id, elapsed_min=elapsed)
    scheduler.dispatch(
        "verify_settings",
        requested={"energy_map": "baseline", "fuel_l": 30},
        observed={"energy_map": "baseline", "fuel_l": 30.1},
        control_classes={"energy_map": "automatic_with_readback", "fuel_l": "pit_session_boundary_only"},
        tolerances={"fuel_l": 0.2},
        command_acknowledgements={"energy_map": True},
        elapsed_min=elapsed,
    )
    scheduler.dispatch("warmup_done", warmup_lap_uids=[f"{session_id}:out"], elapsed_min=elapsed)


class SchedulerTests(unittest.TestCase):
    def test_default_campaign_has_spa_first_and_twenty_to_twenty_two_laps_per_track(self):
        config = default_campaign_config()
        scheduler = CampaignScheduler(config)
        self.assertEqual(config["track_order"], ["spa", "silverstone_gp", "imola", "monza_gp"])
        self.assertEqual(len(scheduler.program), 24)
        self.assertEqual([config["tracks"][key]["minutes"] for key in config["track_order"]], [65, 60, 55, 55])
        self.assertEqual(config["time_budget_minutes"], 315)
        self.assertEqual(sum(item["minutes"] for item in config["schedule"]), 315)
        self.assertEqual((config["tracks"]["spa"]["track_id"], config["tracks"]["spa"]["layout_id"]),
                         ("spa", "default"))
        self.assertEqual([block["id"] for block in scheduler.program[:6]],
                         ["SP C", "SP R", "SP F", "SP E", "SP M", "SP V"])
        for track in config["tracks"].values():
            blocks = track["blocks"]
            self.assertEqual([block["kind"] for block in blocks],
                             ["calibration", "reference", "fuel", "energy", "management", "validation"])
            minimum = sum(block.get("laps", block.get("laps_min")) for block in blocks)
            maximum = sum(block.get("laps", block.get("laps_max")) for block in blocks)
            self.assertEqual((minimum, maximum), (20, 22))

    def test_template_and_scheduler_defaults_agree_on_spa_campaign(self):
        template = load_campaign()
        default = default_campaign_config()
        self.assertEqual(template["track_order"], default["track_order"])
        self.assertEqual(template["schedule"], default["schedule"])
        self.assertEqual(template["time_budget_minutes"], default["time_budget_minutes"])
        self.assertEqual([(track, [block["id"] for block in template["tracks"][track]["blocks"]])
                          for track in template["track_order"]],
                         [(track, [block["id"] for block in default["tracks"][track]["blocks"]])
                          for track in default["track_order"]])

    def test_calibration_needs_three_accepted_laps_and_slow_lap_is_eligible(self):
        s = CampaignScheduler()
        s.dispatch("start", mode="SYNTHETIC")
        s.dispatch("confirm_preflight")
        begin_block(s)
        self.assertEqual(s.state["phase"], "calibration")
        s.dispatch("lap", lap_uid="s1:1", native_session_id="session-1", lap_time_s=100, quality_flags=["slow"], accepted=True)
        self.assertEqual(s.state["accepted_laps"], 1)
        s.dispatch("lap", lap_uid="s1:2", native_session_id="session-1", lap_time_s=98, accepted=True)
        s.dispatch("lap", lap_uid="s1:3", native_session_id="session-1", lap_time_s=97, accepted=True)
        self.assertEqual(s.state["phase"], "box_save")
        with self.assertRaises(SchedulerError):
            s.dispatch("box")
        s.dispatch("confirm_profile", mode="FRZ_AUTO", manual_confirmation=True)
        s.dispatch("box")
        s.dispatch("analyze")
        self.assertEqual(s.state["block_results"][0]["status"], "complete")
        self.assertEqual(s.state["track_profiles"]["spa"]["mode"], "FRZ_AUTO")
        self.assertEqual(s.state["block_results"][0]["attempted_laps"], 3)
        self.assertEqual(s.current_block["id"], "SP R")

    def test_duplicate_lap_and_fresh_session_do_not_continue_physical_stint(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s, "session-a")
        s.dispatch("lap", native_session_id="session-a", native_lap_index=1, lap_time_s=100)
        duplicate = s.dispatch("lap", native_session_id="session-a", native_lap_index=1, lap_time_s=100)
        self.assertEqual(duplicate["action"], "duplicate_lap_ignored")
        self.assertEqual(s.state["run_laps"], 1)
        s.dispatch("interrupt", reason="game restart")
        s2 = CampaignScheduler.from_dict(json.loads(json.dumps(s.to_dict())))
        s2.dispatch("resume_interrupted", native_session_id="session-b")
        self.assertEqual(s2.state["phase"], "prepare_run")
        self.assertEqual(s2.state["run_laps"], 0)
        self.assertEqual(s2.state["partial_runs"][0]["native_session_id"], "session-a")
        begin_block(s2, "session-b", elapsed=2)
        self.assertNotEqual(s2.state["physical_stint_id"], "session-a")
        self.assertEqual(s2.state["run_serial"], 2)

    def test_command_ack_is_not_setting_effect_and_manual_confirmation_is_separate(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        s.dispatch("prepare", native_session_id="s")
        event = s.dispatch(
            "verify_settings", requested={"map": "high"}, observed={"map": "low"},
            control_classes={"map": "automatic_with_readback"},
            command_acknowledgements={"map": True},
        )
        self.assertEqual(event["to_phase"], "driver_confirmation")
        self.assertEqual(s.state["active_settings_check"]["outcomes"]["map"]["status"], "mismatch_or_missing")
        with self.assertRaises(SchedulerError):
            s.dispatch("confirm_settings", manual_confirmations={"map": True})
        s.dispatch("verify_settings", requested={"map": "high"}, observed={"map": "high"},
                   control_classes={"map": "automatic_with_readback"})
        self.assertEqual(s.state["phase"], "warmup")

    def test_unsupported_control_requires_explicit_fallback(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        s.dispatch("prepare", native_session_id="s")
        s.dispatch("verify_settings", requested={"thermal_limit": 0.8}, observed={},
                   control_classes={"thermal_limit": "unsupported"})
        self.assertEqual(s.state["phase"], "unavailable_capability")
        s.dispatch("resolve_capability", fallback="analysis_only")
        self.assertEqual(s.state["phase"], "warmup")
        self.assertEqual(s.state["mode"], "ANALYSIS_ONLY")

    def test_quality_carryover_and_restoration(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        s.dispatch("lap", lap_uid="1", native_session_id="session-1", quality_flags=["major_spin"])
        event = s.dispatch("lap", lap_uid="2", native_session_id="session-1")
        self.assertIn("carryover_unrestored", event["data"]["quality_flags"])
        self.assertFalse(event["data"]["eligible_for_comparison"])
        s.dispatch("restore_state", manual_confirmation=True)
        event = s.dispatch("lap", lap_uid="3", native_session_id="session-1", lap_time_s=101, accepted=True)
        self.assertTrue(event["data"]["eligible_for_comparison"])

    def test_learning_acceptance_and_manual_profile_are_explicit(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        event = s.dispatch("lap", lap_uid="unconfirmed", native_session_id="session-1", lap_time_s=101)
        self.assertEqual(s.state["accepted_laps"], 0)
        self.assertIn("learning_acceptance_unconfirmed", event["data"]["quality_flags"])
        for index in range(3):
            s.dispatch("lap", lap_uid=f"accepted-{index}", native_session_id="session-1", lap_time_s=100,
                       accepted=True)
        with self.assertRaises(SchedulerError):
            s.dispatch("confirm_profile", mode="MANUAL_APPLIED", manual_confirmation=True)
        s.dispatch("confirm_profile", mode="MANUAL_APPLIED", profile_id="profile-1", applied=True,
                   manual_confirmation=True)
        self.assertEqual(s.state["track_profiles"]["spa"]["verification"], "manual_confirmation")

    def test_spa_all_six_blocks_advance_on_accepted_laps_before_silverstone(self):
        s = CampaignScheduler()
        s.dispatch("start", mode="SYNTHETIC")
        s.dispatch("confirm_preflight")
        expected = (("SP C", 3), ("SP R", 5), ("SP F", 2),
                    ("SP E", 4), ("SP M", 3), ("SP V", 3))
        for block_id, laps in expected:
            with self.subTest(block=block_id):
                self.assertEqual(s.current_block["id"], block_id)
                if block_id == "SP V":
                    s.dispatch("freeze_prediction", model_id="spa-held-out-v1",
                               predicted={"lap_time_s": 130.0})
                native_session = block_id.replace(" ", "-")
                begin_block(s, native_session)
                for lap in range(1, laps + 1):
                    s.dispatch("lap", lap_uid=f"{native_session}:{lap}",
                               native_session_id=native_session, lap_time_s=130.0,
                               accepted=True)
                    if block_id == "SP E" and lap == 2:
                        self.assertEqual(s.state["phase"], "map_transition")
                        s.dispatch("verify_map_transition", requested={"energy_map": "recovery"},
                                   observed={"energy_map": "recovery"},
                                   control_classes={"energy_map": "automatic_with_readback"})
                self.assertEqual(s.state["phase"], "box_save")
                if block_id == "SP C":
                    s.dispatch("confirm_profile", mode="FRZ_AUTO", observed_mode="FRZ_AUTO")
                s.dispatch("box")
                s.dispatch("analyze")
        self.assertEqual([item["block_id"] for item in s.state["block_results"]],
                         [block_id for block_id, _ in expected])
        self.assertEqual(sum(item["accepted_laps"] for item in s.state["block_results"]), 20)
        self.assertEqual(s.current_block["id"], "SI C")
        self.assertEqual(s.state["phase"], "next_block")

    def test_missing_timing_is_not_comparable(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        event = s.dispatch("lap", lap_uid="missing-time", native_session_id="session-1", accepted=True)
        self.assertFalse(event["data"]["eligible_for_comparison"])
        self.assertIn("missing_required_data", event["data"]["quality_flags"])

    def test_learning_attempts_above_plan_need_explicit_skip(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        for index in range(5):
            s.dispatch("lap", lap_uid=f"learning-{index}", native_session_id="session-1", lap_time_s=100)
        self.assertEqual(s.state["phase"], "calibration")
        s.dispatch("lap", lap_uid="learning-5", native_session_id="session-1", lap_time_s=100)
        self.assertEqual(s.state["phase"], "calibration")
        s.dispatch("skip", reason="RSS learning did not accept three laps")
        self.assertEqual(s.state["block_results"][0]["status"], "partial")
        self.assertEqual(s.state["block_results"][0]["accepted_laps"], 0)
        self.assertEqual(s.state["block_results"][0]["attempted_laps"], 6)

    def test_individual_test_stops_after_selected_block(self):
        s = CampaignScheduler()
        s.dispatch("start", selection="individual", block_id="IM R")
        self.assertEqual(s.current_block["id"], "IM R")
        s.dispatch("confirm_preflight")
        begin_block(s, "imola-session")
        for index in range(5):
            s.dispatch("lap", lap_uid=f"ir-{index}", native_session_id="imola-session", lap_time_s=100)
        s.dispatch("box")
        s.dispatch("analyze")
        self.assertEqual(s.state["phase"], "review_export")
        self.assertEqual(len(s.state["block_results"]), 1)

    def test_timed_blocks_require_accepted_laps_even_after_planned_attempts(self):
        for index in (1, 2, 4, 5):  # R, F, M, V share timed-lap acceptance.
            with self.subTest(block_index=index):
                config = default_campaign_config()
                config["track_order"] = ["monza_gp"]
                block = config["tracks"]["monza_gp"]["blocks"][index]
                config["tracks"]["monza_gp"]["blocks"] = [block]
                s = CampaignScheduler(config)
                s.dispatch("start")
                s.dispatch("confirm_preflight")
                if block["kind"] == "validation":
                    s.dispatch("freeze_prediction", model_id="model-1",
                               predicted={"lap_time_s": 95.0})
                begin_block(s)
                for lap in range(block["laps"] - 1):
                    s.dispatch("lap", lap_uid=f"valid-{lap}", native_session_id="session-1",
                               lap_time_s=100)
                s.dispatch("lap", lap_uid="invalid", native_session_id="session-1",
                           lap_time_s=101, quality_flags=["off_track"])
                self.assertEqual(s.state["phase"], "record_block")
                self.assertEqual(s.state["run_laps"], block["laps"])
                self.assertEqual(s.state["accepted_laps"], block["laps"] - 1)
                self.assertGreater(s.forecast_remaining_minutes()["estimated_program_minutes"], 0)
                s.dispatch("lap", lap_uid="slow-but-valid", native_session_id="session-1",
                           lap_time_s=120, quality_flags=["slow"])
                self.assertEqual(s.state["phase"], "box_save")
                s.dispatch("box")
                s.dispatch("analyze")
                self.assertEqual(s.state["block_results"][0]["status"], "complete")
                self.assertEqual(s.state["block_results"][0]["attempted_laps"], block["laps"] + 1)
                self.assertEqual(s.state["block_results"][0]["accepted_laps"], block["laps"])

    def test_energy_recovery_map_change_is_verified_between_laps_two_and_three(self):
        config = default_campaign_config()
        config["track_order"] = ["monza_gp"]
        config["tracks"]["monza_gp"]["blocks"] = [config["tracks"]["monza_gp"]["blocks"][3]]
        config["policy"]["recovery_map"] = "recovery"
        s = CampaignScheduler(config)
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        s.dispatch("lap", lap_uid="e1", native_session_id="session-1", lap_time_s=100)
        s.dispatch("lap", lap_uid="e2-invalid", native_session_id="session-1", lap_time_s=100,
                   quality_flags=["off_track"])
        self.assertEqual(s.state["phase"], "record_block")
        self.assertEqual(s.view()["target"]["map_phase"], "deployment")
        s.dispatch("lap", lap_uid="e2", native_session_id="session-1", lap_time_s=100)
        self.assertEqual(s.state["phase"], "map_transition")
        with self.assertRaises(SchedulerError):
            s.dispatch("lap", lap_uid="e3", native_session_id="session-1", lap_time_s=100)
        s.dispatch("verify_map_transition", requested={"energy_map": "recovery"},
                   observed={"energy_map": "deployment"},
                   control_classes={"energy_map": "automatic_with_readback"},
                   command_acknowledgements={"energy_map": True})
        self.assertEqual(s.state["phase"], "driver_confirmation")
        s.dispatch("verify_map_transition", requested={"energy_map": "recovery"},
                   observed={"energy_map": "recovery"},
                   control_classes={"energy_map": "automatic_with_readback"})
        self.assertEqual(s.state["phase"], "record_block")
        s.dispatch("lap", lap_uid="e3", native_session_id="session-1", lap_time_s=101)
        s.dispatch("lap", lap_uid="e4", native_session_id="session-1", lap_time_s=102)
        self.assertEqual(s.state["phase"], "box_save")

    def test_pause_is_experiment_only_and_state_is_restorable(self):
        s = CampaignScheduler()
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        begin_block(s)
        pause = s.dispatch("pause")
        self.assertIn("simulator may continue", pause["reason"])
        restored = CampaignScheduler.from_dict(json.loads(json.dumps(s.to_dict())))
        restored.dispatch("resume", native_session_id="session-1")
        self.assertEqual(restored.state["phase"], "calibration")

    def test_prediction_must_be_frozen_before_validation_and_cannot_be_revised(self):
        config = default_campaign_config()
        config["track_order"] = ["monza_gp"]
        config["tracks"]["monza_gp"]["blocks"] = [config["tracks"]["monza_gp"]["blocks"][-1]]
        s = CampaignScheduler(config)
        s.dispatch("start")
        s.dispatch("confirm_preflight")
        with self.assertRaises(SchedulerError):
            s.dispatch("prepare", native_session_id="v")
        s.dispatch("freeze_prediction", model_id="model-1", predicted={"lap_time_s": 95.0}, condition={"mode": "warm_start"})
        with self.assertRaises(SchedulerError):
            s.dispatch("freeze_prediction", model_id="model-2", predicted={"lap_time_s": 94.0})
        s.dispatch("prepare", native_session_id="v")
        self.assertEqual(s.state["predictions"]["MZ V"]["model_id"], "model-1")

    def test_time_budget_is_advisory_and_repeat_remains_bounded(self):
        s = CampaignScheduler()
        budget = s.config["time_budget_minutes"]
        s.dispatch("start", elapsed_min=0)
        s.dispatch("confirm_preflight", elapsed_min=15)
        with self.assertRaises(SchedulerError):
            s.dispatch("request_repeat", laps=3, question="test", decision_impact="strategy")
        repeat = s.dispatch("request_repeat", laps=2, question="test", decision_impact="strategy", elapsed_min=budget - 10)
        self.assertEqual(repeat["action"], "request_repeat")
        self.assertTrue(s.forecast_remaining_minutes()["at_risk"])
        event = s.dispatch("prepare", native_session_id="session-1", elapsed_min=budget)
        self.assertEqual(event["action"], "prepare")
        self.assertEqual(s.state["phase"], "verify_settings")
        s.dispatch("verify_settings", requested={}, elapsed_min=budget + 1)
        s.dispatch("warmup_done", elapsed_min=budget + 2)
        self.assertEqual(s.state["phase"], "calibration")
        for index in range(3):
            s.dispatch("lap", lap_uid=f"late-{index}", native_session_id="session-1",
                       lap_time_s=100, accepted=True, elapsed_min=budget + 10 + index)
        self.assertEqual(s.state["phase"], "box_save")
        with self.assertRaises(SchedulerError):
            s.dispatch("box", elapsed_min=budget + 13)
        s.dispatch("confirm_profile", mode="FRZ_AUTO", manual_confirmation=True, elapsed_min=budget + 13)
        s.dispatch("box", elapsed_min=budget + 13)
        s.dispatch("analyze", elapsed_min=budget + 14)
        self.assertEqual(s.state["phase"], "next_block")

    def test_replay_is_deterministic_with_explicit_inputs(self):
        config = default_campaign_config()
        commands = [
            {"action": "start", "elapsed_min": 0, "mode": "SYNTHETIC"},
            {"action": "confirm_preflight", "elapsed_min": 15},
            {"action": "prepare", "elapsed_min": 16, "native_session_id": "fixture-s1"},
            {"action": "verify_settings", "elapsed_min": 16, "requested": {}},
            {"action": "warmup_done", "elapsed_min": 18},
            {"action": "lap", "elapsed_min": 20, "lap_uid": "fixture-s1:1", "native_session_id": "fixture-s1", "lap_time_s": 99.0, "accepted": True},
        ]
        a = CampaignScheduler.replay(config, commands).to_dict()
        b = CampaignScheduler.replay(config, commands).to_dict()
        self.assertEqual(a, b)
        self.assertEqual(a["events"][-1]["data"]["eligible_for_comparison"], True)


if __name__ == "__main__":
    unittest.main()
