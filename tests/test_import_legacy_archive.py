from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "import_legacy_archive_to_ingestion.py"
)
SPEC = importlib.util.spec_from_file_location("import_legacy_archive", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ImportLegacyArchiveTests(unittest.TestCase):
    def test_normalize_public_statuses_preserves_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            public_dir = Path(directory)
            for filename in ("observations.csv", "analysis_ready_observations.csv"):
                with (public_dir / filename).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=["observation_id", "review_status", "notes"],
                    )
                    writer.writeheader()
                    writer.writerow(
                        {
                            "observation_id": "site_01_2026_05_25",
                            "review_status": "reviewed",
                            "notes": "Keep this value.",
                        }
                    )

            self.assertEqual(MODULE.normalize_public_statuses(public_dir), 2)
            self.assertEqual(MODULE.normalize_public_statuses(public_dir), 0)

            for filename in ("observations.csv", "analysis_ready_observations.csv"):
                with (public_dir / filename).open(newline="", encoding="utf-8") as handle:
                    row = next(csv.DictReader(handle))
                self.assertEqual(row["review_status"], "partially_reviewed")
                self.assertEqual(row["notes"], "Keep this value.")


if __name__ == "__main__":
    unittest.main()
