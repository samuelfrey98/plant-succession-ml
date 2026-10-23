import base64
import csv
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
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
    read_csv,
    suggest_batch_metadata,
    write_csv,
)
from plant_succession.google_sheets import GoogleSheetsSyncError  # noqa: E402
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


class FakeMachineReviewer:
    def configuration(self) -> dict[str, object]:
        return {
            "available": True,
            "provider": "test_provider",
            "model": "test_vision_v1",
            "prompt_version": "test_prompt_v1",
            "reason": "",
        }

    def review(
        self,
        *,
        site_id: str,
        observation_date: str,
        photos: list[dict[str, str]],
    ) -> dict[str, object]:
        return {
            "provider": "test_provider",
            "model": "test_vision_v1",
            "prompt_version": "test_prompt_v1",
            "provider_response_id": "response_test_01",
            "draft": {
                "values": {
                    "estimated_total_vegetation_cover_pct": 62,
                    "bare_ground_pct": 28,
                    "dominant_species": "Artemisia sp.",
                    "dominant_taxon_rank": "genus",
                    "dominant_species_confidence": "likely",
                    "dominant_flowering": False,
                    "successional_stage": "developing_herb_layer",
                    "notes": f"Drafted from {len(photos)} verified photo for Site {site_id}.",
                },
                "assessments": [
                    {
                        "field_name": "estimated_total_vegetation_cover_pct",
                        "confidence": "medium",
                        "evidence": "The wide view shows discontinuous live cover.",
                    }
                ],
                "overall_summary": f"Machine draft for {observation_date}.",
                "review_priority_fields": ["dominant_species"],
            },
        }


class FakeSheetsPublisher:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def configuration(self) -> dict[str, object]:
        return {
            "available": True,
            "destination": "google_sheets",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/test-sheet",
            "reason": "",
        }

    def sync(self, public_dir: Path) -> dict[str, object]:
        self.calls += 1
        if self.fail:
            raise GoogleSheetsSyncError("Test destination unavailable.")
        return {
            "destination": "google_sheets",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/test-sheet",
            "row_counts": {
                "observations.csv": len(csv_rows(public_dir / "observations.csv")),
            },
        }


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
        self.assertEqual(batch["status"], "observation_review")
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

        with self.assertRaisesRegex(IngestionError, "observation review"):
            self.service.publish_batch(batch["batch_id"])
        review_values = {
            "estimated_total_vegetation_cover_pct": 65,
            "bare_ground_pct": 25,
            "gravel_rock_pct": 10,
            "dominant_species": "Artemisia sp.",
            "dominant_taxon_rank": "genus",
            "dominant_species_confidence": "likely",
            "dominant_flowering": "false",
            "new_species_detected": "false",
            "successional_stage": "developing_herb_layer",
            "notes": "Reviewed from the staged photo set.",
            "completion_status": "reviewed",
        }
        batch = self.service.save_observation_review(
            batch["batch_id"], review_values, finalize=False
        )
        self.assertEqual(batch["status"], "observation_review")
        self.assertEqual(batch["observation_review_events"][-1]["action"], "draft_saved")
        batch = self.service.save_observation_review(
            batch["batch_id"], review_values, finalize=True
        )
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["observation_review"]["reviewed_by"], "project_owner")
        self.assertEqual(batch["observation_review_events"][-1]["action"], "finalized")

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
        self.assertEqual(observation["review_status"], "reviewed")
        self.assertEqual(observation["estimated_total_vegetation_cover_pct"], "65")
        self.assertEqual(observation["dominant_species"], "Artemisia sp.")
        self.assertEqual(
            validate_public_photo_data(self.public_dir, ROOT / "data" / "schema"), []
        )

        review_values["estimated_total_vegetation_cover_pct"] = 70
        review_values["notes"] = "Corrected after a second review."
        batch = self.service.save_observation_review(
            batch["batch_id"], review_values, finalize=True, actor_id="reviewer_two"
        )
        self.assertEqual(batch["status"], "ready")
        batch = self.service.publish_batch(batch["batch_id"])
        self.assertEqual(batch["status"], "published")
        self.assertEqual(
            len(
                [
                    row
                    for row in csv_rows(self.public_dir / "photo_manifest.csv")
                    if row["observation_date"] == "2026-10-15"
                ]
            ),
            2,
        )
        corrected = next(
            row
            for row in csv_rows(self.public_dir / "observations.csv")
            if row["observation_id"] == "site_01_2026_10_15"
        )
        self.assertEqual(corrected["estimated_total_vegetation_cover_pct"], "70")
        self.assertIn("Corrected after a second review.", corrected["notes"])

    def test_publish_rejects_unresolved_photo_without_mutating_public_data(self):
        before = (self.public_dir / "photo_manifest.csv").read_bytes()
        batch = self.service.create_batch(
            "2",
            "2026-10-15",
            "",
            [("IMG_5001.jpg", image_bytes((30, 30, 30)))],
        )
        with self.assertRaisesRegex(IngestionError, "Photo roles"):
            self.service.publish_batch(batch["batch_id"])
        self.assertEqual(before, (self.public_dir / "photo_manifest.csv").read_bytes())

    def test_publication_run_and_sheet_sync_are_visible(self):
        publisher = FakeSheetsPublisher()
        self.service.sheets_publisher = publisher
        batch = self.service.create_batch(
            "2", "2026-10-17", "", [("wide.jpg", image_bytes((30, 80, 40)))]
        )
        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        batch = self.service.save_observation_review(
            batch["batch_id"],
            {"notes": "Visible herb cover.", "completion_status": "reviewed"},
            finalize=True,
        )

        batch = self.service.publish_batch(batch["batch_id"])
        published = self.service.published_data()

        self.assertEqual(batch["status"], "published")
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(published["summary"]["publication_run_count"], 1)
        run = published["publication_runs"][0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["validation_status"], "passed")
        self.assertEqual(run["latest_sync"]["status"], "succeeded")
        self.assertEqual(run["photo_rows_written"], 1)
        self.assertIn("observations.csv", run["output_sha256"])

    def test_sheet_sync_failure_does_not_rollback_local_publication(self):
        publisher = FakeSheetsPublisher(fail=True)
        self.service.sheets_publisher = publisher
        batch = self.service.create_batch(
            "2", "2026-10-18", "", [("wide.jpg", image_bytes((30, 80, 40)))]
        )
        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        batch = self.service.save_observation_review(
            batch["batch_id"],
            {"notes": "Visible herb cover.", "completion_status": "reviewed"},
            finalize=True,
        )

        batch = self.service.publish_batch(batch["batch_id"])
        run = self.service.published_data()["publication_runs"][0]

        self.assertEqual(batch["status"], "published")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["latest_sync"]["status"], "failed")
        self.assertIn("unavailable", run["latest_sync"]["error_message"])

    def test_observation_review_validates_ranges_and_partial_completion(self):
        batch = self.service.create_batch(
            "4",
            "2026-10-15",
            "",
            [("wide.jpg", image_bytes((50, 90, 50)))],
        )
        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        self.assertEqual(batch["status"], "observation_review")

        with self.assertRaisesRegex(IngestionError, "between 0 and 100"):
            self.service.save_observation_review(
                batch["batch_id"],
                {"estimated_total_vegetation_cover_pct": 101},
                finalize=False,
            )
        with self.assertRaisesRegex(IngestionError, "what remains unavailable"):
            self.service.save_observation_review(
                batch["batch_id"],
                {"notes": "Some visible vegetation.", "completion_status": "partially_reviewed"},
                finalize=True,
            )

        batch = self.service.save_observation_review(
            batch["batch_id"],
            {
                "notes": "Some visible vegetation.",
                "completion_status": "partially_reviewed",
                "completeness_notes": "Cover could not be estimated from this framing.",
            },
            finalize=True,
            actor_id="reviewer_two",
        )
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["observation_review"]["reviewed_by"], "reviewer_two")

    def test_machine_review_is_a_suggestion_until_a_human_saves_it(self):
        self.service.machine_reviewer = FakeMachineReviewer()
        batch = self.service.create_batch(
            "4",
            "2026-10-16",
            "",
            [("wide.jpg", image_bytes((50, 90, 50)))],
        )

        with self.assertRaisesRegex(IngestionError, "photo-role review"):
            self.service.generate_machine_observation_review(batch["batch_id"])

        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        suggestion = self.service.generate_machine_observation_review(batch["batch_id"])
        self.assertEqual(suggestion["draft"]["values"]["bare_ground_pct"], 28)

        batch = self.service.get_batch(batch["batch_id"])
        self.assertIsNone(batch["observation_review"])
        self.assertEqual(batch["latest_machine_review"]["model"], "test_vision_v1")
        self.assertEqual(batch["observation_review_events"][-1]["actor_type"], "model")
        self.assertEqual(batch["observation_review_events"][-1]["action"], "suggested")

        edited = dict(suggestion["draft"]["values"])
        edited["estimated_total_vegetation_cover_pct"] = 65
        edited["completion_status"] = "reviewed"
        batch = self.service.save_observation_review(batch["batch_id"], edited)
        self.assertEqual(
            batch["observation_review"]["estimated_total_vegetation_cover_pct"], 65
        )
        self.assertEqual(batch["observation_review_events"][-1]["actor_type"], "human")
        machine_values = batch["latest_machine_review"]["draft"]["values"]
        self.assertEqual(machine_values["estimated_total_vegetation_cover_pct"], 62)

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

    def test_canonical_store_is_versioned_and_exports_over_changed_csvs(self):
        status = self.service.canonical_store_status()
        self.assertEqual(status["schema_version"], 4)
        self.assertEqual(status["expected_schema_version"], 4)
        self.assertEqual(
            {migration["version"] for migration in status["migrations"]}, {1, 2, 3, 4}
        )
        self.assertIn(
            "observations.csv",
            {dataset["filename"] for dataset in status["datasets"]},
        )
        self.assertNotIn(
            "analysis_ready_observations.csv",
            {dataset["filename"] for dataset in status["datasets"]},
        )

        path = self.public_dir / "observations.csv"
        fields, rows = read_csv(path)
        canonical_note = rows[0]["notes"]
        rows[0]["notes"] = "This manual CSV edit is not canonical."
        write_csv(path, fields, rows)

        _, stored_rows = self.service.read_canonical_dataset("observations.csv")
        self.assertEqual(stored_rows[0]["notes"], canonical_note)

        result = self.service.export_public_data()
        self.assertEqual(result["row_counts"]["observations.csv"], len(rows))
        self.assertEqual(result["row_counts"]["analysis_ready_observations.csv"], len(rows))
        self.assertEqual(csv_rows(path)[0]["notes"], canonical_note)

        reloaded = IngestionService(self.service.paths)
        self.assertEqual(len(reloaded.canonical_store_status()["migrations"]), 4)

    def test_explicit_import_and_online_backup(self):
        source = self.root / "replacement"
        shutil.copytree(self.public_dir, source)
        observations_path = source / "observations.csv"
        fields, rows = read_csv(observations_path)
        rows[0]["notes"] = "Explicitly imported replacement."
        write_csv(observations_path, fields, rows)

        with self.assertRaisesRegex(IngestionError, "already initialized"):
            self.service.import_public_exports(source)
        result = self.service.import_public_exports(source, replace=True)
        self.assertEqual(result["dataset_count"], len(list(source.glob("*.csv"))) - 1)
        _, stored_rows = self.service.read_canonical_dataset("observations.csv")
        self.assertEqual(stored_rows[0]["notes"], "Explicitly imported replacement.")

        backup = self.service.backup_database(self.root / "backups")
        backup_path = Path(backup["path"])
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup["schema_versions"], [1, 2, 3, 4])
        with closing(sqlite3.connect(backup_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        manifest = json.loads(Path(backup["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["sha256"], backup["sha256"])

    def test_basic_auth_validation(self):
        valid = "Basic " + base64.b64encode(b"samuel:correct horse").decode("ascii")
        invalid = "Basic " + base64.b64encode(b"samuel:wrong").decode("ascii")
        self.assertTrue(valid_basic_auth(valid, "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth(invalid, "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth("Basic not-base64", "samuel", "correct horse"))
        self.assertFalse(valid_basic_auth(None, "samuel", "correct horse"))

    def test_dashboard_reports_quality_matrix_and_longitudinal_data(self):
        batch = self.service.create_batch(
            "3",
            "2026-10-22",
            "Dashboard test batch.",
            [("wide.jpg", image_bytes((40, 90, 50)))],
        )
        batch = self.service.accept_explicit_suggestions(batch["batch_id"])
        batch = self.service.save_observation_review(
            batch["batch_id"],
            {
                "estimated_total_vegetation_cover_pct": 42,
                "completion_status": "partially_reviewed",
                "completeness_notes": "Dominant taxon still needs review.",
            },
            finalize=True,
        )
        self.service.publish_batch(batch["batch_id"])

        dashboard = self.service.dashboard()

        self.assertEqual(dashboard["summary"]["batch_count"], 1)
        self.assertEqual(dashboard["summary"]["photo_count"], 1)
        self.assertEqual(dashboard["summary"]["verified_photo_count"], 1)
        self.assertEqual(dashboard["review_status_counts"], {"partially_reviewed": 1})
        self.assertEqual(dashboard["review_provenance_counts"], {"human": 1})
        self.assertEqual(dashboard["quality"]["missing_dominant_taxon"], 1)
        self.assertEqual(dashboard["quality"].get("missing_vegetation_cover", 0), 0)
        self.assertEqual(dashboard["site_matrix"]["dates"], ["2026-10-22"])
        site = next(
            row for row in dashboard["site_matrix"]["rows"] if row["site_id"] == "3"
        )
        self.assertEqual(site["cells"][0]["batch_id"], batch["batch_id"])
        series = next(
            row for row in dashboard["longitudinal"]["sites"] if row["site_id"] == "3"
        )
        self.assertEqual(series["points"][0]["vegetation_cover_pct"], 42)
        self.assertEqual(dashboard["issues"][0]["severity"], "follow_up")
        self.assertIn("Dominant taxon is missing", dashboard["issues"][0]["reasons"])

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
