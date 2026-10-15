import importlib.util
import unittest
from pathlib import Path

import pandas as pd


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_longitudinal_outputs.py"
SPEC = importlib.util.spec_from_file_location("build_longitudinal_outputs", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuildLongitudinalOutputsTests(unittest.TestCase):
    def test_build_analysis_table_keeps_missing_rows_and_derives_counts(self):
        observations = pd.DataFrame(
            [
                {
                    "observation_id": "site_01_2026_05_25",
                    "site_id": "1",
                    "observation_date": "2026-05-25",
                    "record_origin": "photo_archive_backfill",
                    "review_status": "partially_reviewed",
                    "estimated_total_vegetation_cover_pct": "60",
                    "bare_ground_pct": "25",
                    "gravel_rock_pct": "5",
                    "litter_mulch_woodchip_pct": "10",
                    "standing_dead_pct": "",
                    "source_photo_count": 2,
                    "notes": "Reviewed.",
                },
                {
                    "observation_id": "site_01_2026_06_01",
                    "site_id": "1",
                    "observation_date": "2026-06-01",
                    "record_origin": "photo_archive_backfill",
                    "review_status": "pending_visual_review",
                    "estimated_total_vegetation_cover_pct": "",
                    "bare_ground_pct": "",
                    "gravel_rock_pct": "",
                    "source_photo_count": 1,
                    "notes": "Pending.",
                },
            ]
        )
        sites = pd.DataFrame(
            [{"site_id": "1", "site_name": "Test Site", "habitat_context": "mixed_edge"}]
        )
        photos = pd.DataFrame(
            [
                {
                    "photo_id": "one",
                    "observation_id": "site_01_2026_05_25",
                    "site_id": "1",
                    "photo_type": "wide",
                    "classification_status": "verified",
                },
                {
                    "photo_id": "two",
                    "observation_id": "site_01_2026_05_25",
                    "site_id": "1",
                    "photo_type": "context_left",
                    "classification_status": "verified",
                },
                {
                    "photo_id": "three",
                    "observation_id": "site_01_2026_06_01",
                    "site_id": "1",
                    "photo_type": "unclassified",
                    "classification_status": "pending_visual_review",
                },
            ]
        )

        result = MODULE.build_analysis_table(observations, sites, photos)

        self.assertEqual(len(result), 2)
        self.assertTrue(result.iloc[0]["has_cover_estimate"])
        self.assertFalse(result.iloc[1]["has_cover_estimate"])
        self.assertEqual(result.iloc[0]["surface_cover_sum_pct"], 100)
        self.assertEqual(result.iloc[0]["litter_mulch_woodchip_pct"], 10)
        self.assertEqual(result.iloc[0]["context_photo_count"], 1)
        self.assertEqual(result.iloc[1]["unclassified_photo_count"], 1)
        self.assertEqual(result.iloc[1]["days_since_previous_observation"], 7)


if __name__ == "__main__":
    unittest.main()
