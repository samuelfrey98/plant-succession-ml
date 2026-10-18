import base64
import csv
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.ingestion import (  # noqa: E402
    IngestionError,
    IngestionPaths,
    IngestionService,
    PhotoMetadata,
    SiteAnchor,
    assess_batch_integrity,
    extract_photo_metadata,
    infer_photo_type,
    suggest_batch_metadata,
)
from plant_succession.app import initialize_public_data, valid_basic_auth  # noqa: E402
from plant_succession.validate import validate_public_photo_data  # noqa: E402


def image_bytes(color: tuple[int, int, int]) -> io.BytesIO:
    output = io.BytesIO()
    Image.new("RGB", (80, 60), color).save(output, "JPEG")
    output.seek(0)
    return output


def dated_image_bytes(captured_at: str) -> io.BytesIO:
    output = io.BytesIO()
    exif = Image.Exif()
    exif[306] = captured_at
    Image.new("RGB", (80, 60), (20, 80, 40)).save(output, "JPEG", exif=exif)
    output.seek(0)
    return output


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class IngestionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.public_dir = self.root / "public"
        shutil.copytree(ROOT / "data" / "public", self.public_dir)
        self.service = IngestionService(
            IngestionPaths(
                public_dir=self.public_dir,
                schema_dir=ROOT / "data" / "schema",
                state_dir=self.root / "state",
                archive_dir=self.root / "photos",
            )
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_filename_labeling_is_explicit_and_conservative(self):
        self.assertEqual(infer_photo_type("wide.HEIC").photo_type, "wide")
        self.assertEqual(infer_photo_type("context-left.jpg").photo_type, "context_left")
        candidate = infer_photo_type("IMG_4021.HEIC")
        self.assertEqual(candidate.photo_type, "unclassified")
        self.assertEqual(candidate.confidence, 0.0)

    def test_stage_review_publish_lifecycle(self):
        batch = self.service.create_batch(
            "1",
            "2026-10-15",
            "Test field visit.",
            [
                ("wide.jpg", image_bytes((40, 120, 60))),
                ("IMG_4021.jpg", image_bytes((80, 60, 40))),
            ],
        )
        self.assertEqual(batch["status"], "needs_review")
        self.assertEqual(batch["photo_count"], 2)
        self.assertTrue(all(photo["preview_filename"] for photo in batch["photos"]))

        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        self.assertEqual(
            sum(photo["review_status"] == "verified" for photo in batch["photos"]),
            1,
        )
        unresolved = next(
            photo for photo in batch["photos"] if photo["review_status"] != "verified"
        )
        batch = self.service.review_photo(unresolved["photo_id"], "quadrat")
        self.assertEqual(batch["status"], "ready")
        for photo in batch["photos"]:
            self.assertEqual(photo["review_events"][0]["actor_type"], "rule")
            self.assertIn(photo["review_events"][-1]["actor_type"], {"human"})
        accepted = next(
            photo for photo in batch["photos"] if photo["source_filename"] == "wide.jpg"
        )
        self.assertEqual(accepted["review_events"][-1]["action"], "accepted")
        manually_reviewed = next(
            photo for photo in batch["photos"] if photo["source_filename"] == "IMG_4021.jpg"
        )
        self.assertEqual(manually_reviewed["review_events"][-1]["action"], "verified")

        batch = self.service.publish_batch(batch["batch_id"])
        self.assertEqual(batch["status"], "published")

        manifest = csv_rows(self.public_dir / "photo_manifest.csv")
        published = [row for row in manifest if row["observation_date"] == "2026-10-15"]
        self.assertEqual(len(published), 2)
        self.assertEqual({row["photo_type"] for row in published}, {"wide", "quadrat"})
        observations = csv_rows(self.public_dir / "observations.csv")
        observation = next(
            row for row in observations if row["observation_id"] == "site_01_2026_10_15"
        )
        self.assertEqual(observation["source_photo_count"], "2")
        self.assertEqual(observation["record_origin"], "ingestion_app")
        self.assertEqual(
            validate_public_photo_data(self.public_dir, ROOT / "data" / "schema"), []
        )

    def test_publish_rejects_unresolved_photo_without_mutating_public_data(self):
        before = (self.public_dir / "photo_manifest.csv").read_bytes()
        batch = self.service.create_batch(
            "2",
            "2026-10-15",
            "",
            [("IMG_5001.jpg", image_bytes((30, 30, 30)))],
        )
        with self.assertRaisesRegex(IngestionError, "verified role"):
            self.service.publish_batch(batch["batch_id"])
        self.assertEqual(before, (self.public_dir / "photo_manifest.csv").read_bytes())

    def test_duplicate_upload_is_counted_and_not_staged_twice(self):
        first = image_bytes((10, 20, 30)).getvalue()
        batch = self.service.create_batch(
            "3",
            "2026-10-15",
            "",
            [("first.jpg", io.BytesIO(first)), ("duplicate.jpg", io.BytesIO(first))],
        )
        self.assertEqual(batch["photo_count"], 1)
        self.assertEqual(batch["duplicate_count"], 1)

    def test_persistent_public_data_is_seeded_without_overwriting_changes(self):
        source = self.root / "seed"
        target = self.root / "durable"
        source.mkdir()
        (source / "observations.csv").write_text("version\nrepo\n", encoding="utf-8")
        (source / "sites.csv").write_text("version\nrepo\n", encoding="utf-8")
        target.mkdir()
        (target / "observations.csv").write_text("version\nhosted\n", encoding="utf-8")

        initialize_public_data(source, target)

        self.assertEqual(
            (target / "observations.csv").read_text(encoding="utf-8"),
            "version\nhosted\n",
        )
        self.assertEqual(
            (target / "sites.csv").read_text(encoding="utf-8"),
            "version\nrepo\n",
        )

    def test_basic_auth_validation(self):
        valid = "Basic " + base64.b64encode(b"samuel:correct horse").decode("ascii")
        invalid = "Basic " + base64.b64encode(b"samuel:wrong").decode("ascii")
        self.assertTrue(valid_basic_auth(valid, "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth(invalid, "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth("Basic not-base64", "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth(None, "samuel", "correct horse"))

    def test_metadata_suggestion_votes_for_nearest_site_and_capture_date(self):
        anchors = [
            SiteAnchor("1", 40.00000, -75.00000),
            SiteAnchor("2", 39.99945, -74.99882),
        ]
        photos = [
            PhotoMetadata("wide.heic", "2026-05-25T18:52:13", 40.000016, -74.999992),
            PhotoMetadata("left.heic", "2026-05-25T18:52:16", 40.000010, -75.000000),
            PhotoMetadata("close.heic", "2026-05-25T18:52:20", None, None),
        ]

        suggestion = suggest_batch_metadata(photos, anchors)

        self.assertEqual(suggestion["site_id"], "1")
        self.assertEqual(suggestion["site_confidence"], "high")
        self.assertEqual(suggestion["observation_date"], "2026-05-25")
        self.assertEqual(suggestion["date_confidence"], "high")
        self.assertLess(suggestion["median_distance_meters"], 3)
        self.assertGreater(suggestion["median_runner_up_margin_meters"], 50)

    def test_metadata_suggestion_downgrades_close_anchor_call(self):
        anchors = [
            SiteAnchor("5", 40.00000, -75.00000),
            SiteAnchor("6", 40.00010, -75.00037),
        ]
        photos = [
            PhotoMetadata("wide.heic", "2026-06-03T10:00:00", 40.00005, -75.000185),
        ]

        suggestion = suggest_batch_metadata(photos, anchors)

        self.assertIn(suggestion["site_id"], {"5", "6"})
        self.assertEqual(suggestion["site_confidence"], "low")
        self.assertLess(suggestion["median_runner_up_margin_meters"], 5)
        self.assertTrue(any("close to another" in item for item in suggestion["warnings"]))

    def test_metadata_suggestion_flags_photos_split_across_sites(self):
        anchors = [
            SiteAnchor("1", 40.00000, -75.00000),
            SiteAnchor("2", 39.99945, -74.99882),
        ]
        photos = [
            PhotoMetadata("one.heic", "2026-05-25T10:00:00", 40.00000, -75.00000),
            PhotoMetadata("two.heic", "2026-05-26T10:00:00", 39.99945, -74.99882),
        ]

        suggestion = suggest_batch_metadata(photos, anchors)

        self.assertIsNone(suggestion["site_id"])
        self.assertIsNone(suggestion["observation_date"])
        self.assertTrue(any("multiple sites" in warning for warning in suggestion["warnings"]))
        self.assertTrue(any("multiple capture dates" in warning for warning in suggestion["warnings"]))

    def test_batch_integrity_identifies_per_file_site_and_date_outliers(self):
        files = [
            {
                "filename": "expected.heic",
                "captured_at": "2026-10-17T10:00:00",
                "gps_present": True,
                "nearest_site_id": "7",
                "distance_meters": 3.2,
                "runner_up_margin_meters": 48.0,
            },
            {
                "filename": "wrong-site.heic",
                "captured_at": "2026-10-16T10:00:00",
                "gps_present": True,
                "nearest_site_id": "8",
                "distance_meters": 2.1,
                "runner_up_margin_meters": 46.0,
            },
        ]

        integrity = assess_batch_integrity(files, "7", "2026-10-17")

        self.assertEqual(integrity["status"], "attention_required")
        self.assertEqual(integrity["issue_count"], 1)
        self.assertTrue(integrity["requires_override"])
        self.assertEqual(integrity["files"][0]["site_check"], "match")
        self.assertEqual(integrity["files"][1]["site_check"], "site_mismatch")
        self.assertEqual(integrity["files"][1]["date_check"], "date_mismatch")

    def test_batch_integrity_passes_consistent_files(self):
        files = [
            {
                "filename": "expected.heic",
                "captured_at": "2026-10-17T10:00:00",
                "gps_present": True,
                "nearest_site_id": "7",
                "distance_meters": 3.2,
                "runner_up_margin_meters": 48.0,
            }
        ]

        integrity = assess_batch_integrity(files, "7", "2026-10-17")

        self.assertEqual(integrity["status"], "passed")
        self.assertEqual(integrity["issue_count"], 0)
        self.assertFalse(integrity["requires_override"])

    def test_exif_capture_date_is_extracted(self):
        path = self.root / "dated.jpg"
        path.write_bytes(dated_image_bytes("2026:07:20 14:12:03").getvalue())

        metadata = extract_photo_metadata(path, "dated.jpg")

        self.assertEqual(metadata.captured_at, "2026-07-20T14:12:03")

    def test_prepared_upload_is_staged_without_uploading_files_twice(self):
        prepared = self.service.create_upload_suggestion(
            [("wide.jpg", dated_image_bytes("2026:07:20 14:12:03"))]
        )
        token = str(prepared["upload_token"])
        self.assertEqual(prepared["observation_date"], "2026-07-20")
        self.assertTrue((self.service.paths.suggestion_dir / token).exists())

        with self.assertRaisesRegex(IngestionError, "integrity"):
            self.service.create_batch_from_upload_suggestion(
                token, "1", "2026-07-20", "Prepared upload test."
            )
        batch = self.service.create_batch_from_upload_suggestion(
            token, "1", "2026-07-20", "Prepared upload test.", True
        )

        self.assertEqual(batch["photo_count"], 1)
        self.assertEqual(batch["integrity"]["status"], "attention_required")
        self.assertEqual(batch["integrity"]["override_confirmed"], 1)
        self.assertFalse((self.service.paths.suggestion_dir / token).exists())


if __name__ == "__main__":
    unittest.main()
