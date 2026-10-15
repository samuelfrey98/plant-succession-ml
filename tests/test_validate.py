import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.validate import (
    validate_disturbance_observation_references,
    validate_disturbance_windows,
)


class DisturbanceValidationTests(unittest.TestCase):
    def test_rejects_reversed_disturbance_window(self):
        issues = validate_disturbance_windows(
            [
                {
                    "disturbance_window_start": "2026-06-03",
                    "disturbance_window_end": "2026-06-01",
                }
            ]
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].column, "disturbance_window_start")

    def test_rejects_unknown_related_observation(self):
        issues = validate_disturbance_observation_references(
            [{"related_observation_id": "site_06_2026_06_04"}],
            [{"observation_id": "site_06_2026_06_03"}],
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].column, "related_observation_id")

    def test_allows_blank_or_known_related_observation(self):
        issues = validate_disturbance_observation_references(
            [
                {"related_observation_id": ""},
                {"related_observation_id": "site_06_2026_06_03"},
            ],
            [{"observation_id": "site_06_2026_06_03"}],
        )

        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
