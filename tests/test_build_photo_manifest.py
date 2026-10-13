from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_photo_manifest.py"
SPEC = importlib.util.spec_from_file_location("build_photo_manifest", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BuildPhotoManifestTests(unittest.TestCase):
    def test_rebuild_preserves_visual_review_fields(self) -> None:
        inventory = [
            {
                "drive_file_id": "drive-file-1",
                "source_site_folder": "site_1",
                "source_date_folder": "2026-05-25",
                "source_filename": "wide.HEIC",
                "file_size_bytes": "1234",
            }
        ]
        prior = {
            "site_01_2026_05_25_wide": {
                "photo_type": "wide",
                "classification_status": "verified",
                "notes": "Reviewed visually.",
            }
        }

        row = MODULE.build_manifest(inventory, prior)[0]

        self.assertEqual(row["photo_type"], "wide")
        self.assertEqual(row["classification_status"], "verified")
        self.assertEqual(row["notes"], "Reviewed visually.")

    def test_new_photos_remain_pending(self) -> None:
        inventory = [
            {
                "drive_file_id": "drive-file-2",
                "source_site_folder": "site_2",
                "source_date_folder": "2026-06-01",
                "source_filename": "IMG_1001.HEIC",
                "file_size_bytes": "4321",
            }
        ]

        row = MODULE.build_manifest(inventory)[0]

        self.assertEqual(row["photo_type"], "unclassified")
        self.assertEqual(row["classification_status"], "pending_visual_review")
        self.assertEqual(row["notes"], "")


if __name__ == "__main__":
    unittest.main()
