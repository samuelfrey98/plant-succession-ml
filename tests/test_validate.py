import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.validate import (
    validate_disturbance_lineage,
    validate_disturbance_observation_references,
    validate_disturbance_windows,
    validate_field_note_site_references,
    validate_site_check_references,
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

    def test_rejects_site_check_outside_source_note_scope(self):
        issues = validate_site_check_references(
            [
                {
                    "field_note_id": "note_1",
                    "site_id": "3",
                    "check_date": "2026-06-13",
                    "related_observation_id": "",
                }
            ],
            [
                {
                    "field_note_id": "note_1",
                    "note_date": "2026-06-13",
                    "note_type": "disturbance",
                    "related_site_ids": "1;2",
                }
            ],
            [],
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].column, "site_id")

    def test_rejects_phenology_note_as_disturbance_check(self):
        issues = validate_site_check_references(
            [
                {
                    "field_note_id": "note_1",
                    "site_id": "1",
                    "check_date": "2026-07-20",
                    "related_observation_id": "",
                }
            ],
            [
                {
                    "field_note_id": "note_1",
                    "note_date": "2026-07-20",
                    "note_type": "phenology",
                    "related_site_ids": "1",
                }
            ],
            [],
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].column, "field_note_id")

    def test_explicit_negative_check_does_not_create_event(self):
        issues = validate_disturbance_lineage(
            [],
            [
                {
                    "site_check_id": "check_3",
                    "disturbance_assessment": "none_seen",
                }
            ],
            [],
        )

        self.assertEqual(issues, [])

    def test_rejects_event_linked_to_explicit_negative_check(self):
        issues = validate_disturbance_lineage(
            [
                {
                    "site_id": "3",
                    "disturbance_event_type": "mowing",
                    "disturbance_window_end": "2026-06-13",
                    "source_field_note_id": "note_3",
                    "site_check_id": "check_3",
                }
            ],
            [
                {
                    "site_check_id": "check_3",
                    "field_note_id": "note_3",
                    "site_id": "3",
                    "check_date": "2026-06-13",
                    "disturbance_assessment": "none_seen",
                    "disturbance_type_assessed": "mowing",
                }
            ],
            [{"field_note_id": "note_3"}],
        )

        self.assertTrue(any("must have assessment" in issue.message for issue in issues))
        self.assertTrue(any("must not link" in issue.message for issue in issues))

    def test_rejects_invalid_field_note_scope(self):
        issues = validate_field_note_site_references(
            [
                {
                    "site_scope": "site_specific",
                    "related_site_ids": "1;2",
                }
            ],
            [{"site_id": "1"}, {"site_id": "2"}],
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].column, "related_site_ids")


if __name__ == "__main__":
    unittest.main()
