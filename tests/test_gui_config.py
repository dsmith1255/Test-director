"""A saved three-track setup must retain driver values when Spa is added."""

from __future__ import annotations

import copy
import unittest

from wimna.config import load_campaign, validate_campaign
from wimna.gui import _upgrade_legacy_campaign


class LegacyConfigUpgradeTests(unittest.TestCase):
    def test_spa_is_added_without_losing_saved_driver_setup(self):
        template = load_campaign()
        saved = copy.deepcopy(template)
        saved["track_order"] = ["monza_gp", "imola", "silverstone_gp"]
        saved["tracks"].pop("spa")
        saved["schedule"] = [
            {"activity": "preflight", "minutes": 15},
            {"activity": "monza_gp", "minutes": 60},
            {"activity": "break", "minutes": 10},
            {"activity": "imola", "minutes": 55},
            {"activity": "break", "minutes": 10},
            {"activity": "silverstone_gp", "minutes": 55},
            {"activity": "reserve", "minutes": 20},
            {"activity": "review_export", "minutes": 15},
        ]
        saved["time_budget_minutes"] = 240
        saved["policy"]["reference_fuel_l"] = 42.0
        saved["tracks"]["monza_gp"]["track_id"] = "monza"

        upgraded = _upgrade_legacy_campaign(saved, template)
        validate_campaign(upgraded)
        self.assertEqual(upgraded["track_order"],
                         ["spa", "silverstone_gp", "imola", "monza_gp"])
        self.assertEqual((upgraded["tracks"]["spa"]["track_id"],
                          upgraded["tracks"]["spa"]["layout_id"]),
                         ("spa", "default"))
        self.assertEqual(upgraded["policy"]["reference_fuel_l"], 42.0)
        self.assertEqual(upgraded["tracks"]["monza_gp"]["track_id"], "monza")
        self.assertEqual(saved["track_order"], ["monza_gp", "imola", "silverstone_gp"])


if __name__ == "__main__":
    unittest.main()
