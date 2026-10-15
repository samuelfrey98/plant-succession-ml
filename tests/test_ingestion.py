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
    infer_photo_type,
)
from plant_succession.app import initialize_public_data, valid_basic_auth  # noqa: E402
from plant_succession.validate import validate_public_photo_data  # noqa: E402


def image_bytes(color: tuple[int, int, int]) -> io.BytesIO:
    output = io.BytesIO()
    Image.new("RGB", (80, 60), color).save(output, "JPEG")
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


if __name__ == "__main__":
    unittest.main()
