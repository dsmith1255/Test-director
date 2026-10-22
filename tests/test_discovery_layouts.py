"""Guard AC's root-level models_<layout>.ini layout convention."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wimna.discovery import _track_inventory


class LayoutDiscoveryTests(unittest.TestCase):
    def test_default_named_and_historic_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracks = root / "content" / "tracks"
            monza = tracks / "monza"
            monza.mkdir(parents=True)
            (monza / "models.ini").write_text("[MODEL_0]\n", encoding="utf-8")
            spa = tracks / "spa"
            spa.mkdir()
            (spa / "models.ini").write_text("[MODEL_0]\n", encoding="utf-8")
            historic_spa = tracks / "spa_1966"
            historic_spa.mkdir()
            (historic_spa / "models.ini").write_text("[MODEL_0]\n", encoding="utf-8")
            silverstone = tracks / "ks_silverstone"
            (silverstone / "gp").mkdir(parents=True)
            (silverstone / "models_gp.ini").write_text("[MODEL_0]\n", encoding="utf-8")
            historic = tracks / "ks_monza66"
            historic.mkdir()
            (historic / "models.ini").write_text("[MODEL_0]\n", encoding="utf-8")
            found = _track_inventory(root)
            self.assertIn(("spa", "default", False),
                          {(item["track_id"], item["layout_id"], item["excluded_historic"])
                           for item in found["spa"]})
            self.assertIn(("spa_1966", "default", True),
                          {(item["track_id"], item["layout_id"], item["excluded_historic"])
                           for item in found["spa"]})
            self.assertIn(("monza", "default", False),
                          {(item["track_id"], item["layout_id"], item["excluded_historic"])
                           for item in found["monza_gp"]})
            self.assertIn(("ks_monza66", "default", True),
                          {(item["track_id"], item["layout_id"], item["excluded_historic"])
                           for item in found["monza_gp"]})
            self.assertEqual([(item["track_id"], item["layout_id"])
                              for item in found["silverstone_gp"]], [("ks_silverstone", "gp")])


if __name__ == "__main__":
    unittest.main()
