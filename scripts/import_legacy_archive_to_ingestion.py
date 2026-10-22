#!/usr/bin/env python
"""Register the historical Drive archive in the local ingestion database.

The public CSVs remain the source for reviewed metadata. Raw image bytes are
hard-linked into ingestion batch folders so the review app can display the
historical records without duplicating several gigabytes of photos.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.ingestion import (  # noqa: E402
    OBSERVATION_PERCENT_FIELDS,
    OBSERVATION_REVIEW_FIELDS,
    IngestionPaths,
    IngestionService,
    infer_photo_type,
)


DEFAULT_PUBLIC_DIR = ROOT / "data" / "public"
DEFAULT_SCHEMA_DIR = ROOT / "data" / "schema"
DEFAULT_STATE_DIR = ROOT / "data" / "raw" / "ingestion"
DEFAULT_ARCHIVE_DIR = ROOT / "photos"
DEFAULT_SITE_ANCHORS = ROOT / "data" / "raw" / "site_anchors.csv"
REVIEWER_ID = "codex_archive_review"
COMPLETENESS_NOTE = (
    "Historical photo-backed fields remain incomplete and open to human review."
)


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_rows(
    path: Path,
    fields: list[str],
    rows: list[dict[str, str]],
    *,
    lineterminator: str = "\n",
) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            lineterminator=lineterminator,
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_review(observation: dict[str, str]) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in OBSERVATION_REVIEW_FIELDS:
        value = observation.get(field, "").strip()
        if field in OBSERVATION_PERCENT_FIELDS:
            values[field] = int(value) if value else None
        else:
            values[field] = value
    values["completion_status"] = "partially_reviewed"
    values["completeness_notes"] = COMPLETENESS_NOTE
    return values


def normalize_public_statuses(public_dir: Path) -> int:
    changed = 0
    for filename in ("observations.csv", "analysis_ready_observations.csv"):
        path = public_dir / filename
        lineterminator = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
        fields, rows = read_rows(path)
        if "review_status" not in fields:
            raise ValueError(f"{path} has no review_status column")
        for row in rows:
            if row["review_status"] != "partially_reviewed":
                row["review_status"] = "partially_reviewed"
                changed += 1
        write_rows(path, fields, rows, lineterminator=lineterminator)
    return changed


def ensure_hard_link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != source.stat().st_size:
            raise ValueError(f"Existing staged file has the wrong size: {destination}")
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def import_archive(service: IngestionService) -> dict[str, int]:
    _, observations = read_rows(service.paths.public_dir / "observations.csv")
    _, manifest = read_rows(service.paths.public_dir / "photo_manifest.csv")
    observations_by_id = {row["observation_id"]: row for row in observations}
    historical = {
        key: row
        for key, row in observations_by_id.items()
        if row.get("record_origin") == "photo_archive_backfill"
    }
    photos_by_observation: dict[str, list[dict[str, str]]] = {}
    for row in manifest:
        if row["observation_id"] in historical:
            photos_by_observation.setdefault(row["observation_id"], []).append(row)

    missing_batches = sorted(set(historical) - set(photos_by_observation))
    if missing_batches:
        raise ValueError(
            "Historical observations without photos: " + ", ".join(missing_batches)
        )

    imported_batches = 0
    imported_photos = 0
    imported_published_reviews = 0
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with service._connection() as connection:
        existing_photos = {
            row["photo_id"]: dict(row)
            for row in connection.execute("SELECT * FROM photos")
        }
        known_hashes = {
            row["sha256"]: row["photo_id"] for row in existing_photos.values()
        }

    for observation_id in sorted(
        historical,
        key=lambda key: (
            int(historical[key]["site_id"]),
            historical[key]["observation_date"],
        ),
    ):
        observation = historical[observation_id]
        batch_id = f"legacy_{observation_id}"
        batch_root = service.paths.batch_dir / batch_id
        original_dir = batch_root / "original"
        preview_dir = batch_root / "previews"
        original_dir.mkdir(parents=True, exist_ok=True)
        preview_dir.mkdir(parents=True, exist_ok=True)
        created_at = f"{observation['observation_date']}T00:00:00+00:00"
        batch_photos = photos_by_observation[observation_id]

        with service._connection() as connection:
            existed = connection.execute(
                "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO batches (
                    batch_id, site_id, observation_date, observation_notes,
                    status, photo_count, duplicate_count, created_at, published_at
                ) VALUES (?, ?, ?, ?, 'published', ?, 0, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    observation_notes = excluded.observation_notes,
                    status = 'published',
                    photo_count = excluded.photo_count,
                    published_at = COALESCE(batches.published_at, excluded.published_at)
                """,
                (
                    batch_id,
                    observation["site_id"],
                    observation["observation_date"],
                    observation.get("notes", ""),
                    len(batch_photos),
                    created_at,
                    now,
                ),
            )
        imported_batches += int(existed is None)

        for photo in batch_photos:
            source = ROOT / photo["relative_path"]
            if not source.is_file():
                raise FileNotFoundError(f"Missing mirrored photo: {source}")
            existing_photo = existing_photos.get(photo["photo_id"])
            digest = str(existing_photo["sha256"]) if existing_photo else sha256(source)
            other_photo_id = known_hashes.get(digest)
            if other_photo_id not in {None, photo["photo_id"]}:
                raise ValueError(
                    f"Duplicate image bytes for {photo['photo_id']} and {other_photo_id}"
                )
            known_hashes[digest] = photo["photo_id"]
            stored_filename = source.name
            staged = original_dir / stored_filename
            ensure_hard_link(source, staged)
            existing_preview = (
                preview_dir / str(existing_photo.get("preview_filename") or "")
                if existing_photo
                else None
            )
            if existing_preview is not None and existing_preview.is_file():
                width = existing_photo["width_px"]
                height = existing_photo["height_px"]
                captured_at = existing_photo["captured_at"]
                preview_filename = existing_photo["preview_filename"]
            else:
                width, height, captured_at, preview_filename = service._create_preview(
                    staged, preview_dir, photo["photo_id"]
                )
            candidate = infer_photo_type(photo["source_filename"])
            reviewed_type = photo.get("photo_type", "") or "unclassified"
            with service._connection() as connection:
                existed = connection.execute(
                    "SELECT 1 FROM photos WHERE photo_id = ?", (photo["photo_id"],)
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO photos (
                        photo_id, batch_id, source_filename, stored_filename, sha256,
                        file_size_bytes, width_px, height_px, captured_at, date_warning,
                        preview_filename, candidate_type, candidate_confidence,
                        candidate_reason, label_model_version, reviewed_type,
                        review_status, review_notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'verified', ?, ?)
                    ON CONFLICT(photo_id) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        source_filename = excluded.source_filename,
                        stored_filename = excluded.stored_filename,
                        sha256 = excluded.sha256,
                        file_size_bytes = excluded.file_size_bytes,
                        width_px = excluded.width_px,
                        height_px = excluded.height_px,
                        captured_at = excluded.captured_at,
                        date_warning = excluded.date_warning,
                        preview_filename = excluded.preview_filename,
                        reviewed_type = excluded.reviewed_type,
                        review_status = 'verified',
                        review_notes = excluded.review_notes
                    """,
                    (
                        photo["photo_id"],
                        batch_id,
                        photo["source_filename"],
                        stored_filename,
                        digest,
                        int(photo["file_size_bytes"]),
                        width,
                        height,
                        captured_at,
                        int(
                            bool(
                                captured_at
                                and captured_at[:10]
                                != observation["observation_date"]
                            )
                        ),
                        preview_filename,
                        candidate.photo_type,
                        candidate.confidence,
                        candidate.reason,
                        candidate.model_version,
                        reviewed_type,
                        photo.get("notes", ""),
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO review_events (
                        review_event_id, photo_id, label_field, label_value,
                        actor_type, actor_id, actor_version, action,
                        confidence, notes, created_at
                    ) VALUES (?, ?, 'photo_type', ?, 'rule', 'filename_rules', ?,
                              'suggested', ?, ?, ?)
                    """,
                    (
                        f"legacy_candidate_{photo['photo_id']}",
                        photo["photo_id"],
                        candidate.photo_type,
                        candidate.model_version,
                        candidate.confidence,
                        candidate.reason,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO review_events (
                        review_event_id, photo_id, label_field, label_value,
                        actor_type, actor_id, action, notes, created_at
                    ) VALUES (?, ?, 'photo_type', ?, 'ai_assistant', ?, 'verified', ?, ?)
                    """,
                    (
                        f"legacy_verified_{photo['photo_id']}",
                        photo["photo_id"],
                        reviewed_type,
                        REVIEWER_ID,
                        photo.get("notes", ""),
                        now,
                    ),
                )
            imported_photos += int(existed is None)

        review = normalized_review(observation)
        columns = ", ".join(OBSERVATION_REVIEW_FIELDS)
        placeholders = ", ".join("?" for _ in OBSERVATION_REVIEW_FIELDS)
        updates = ", ".join(
            f"{field} = excluded.{field}" for field in OBSERVATION_REVIEW_FIELDS
        )
        with service._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO observation_reviews (
                    batch_id, {columns}, is_final, reviewed_by, updated_at
                ) VALUES (?, {placeholders}, 1, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    {updates}, is_final = 1, reviewed_by = excluded.reviewed_by,
                    updated_at = excluded.updated_at
                """,
                (
                    batch_id,
                    *(review[field] for field in OBSERVATION_REVIEW_FIELDS),
                    REVIEWER_ID,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO observation_review_events (
                    review_event_id, batch_id, actor_type, actor_id,
                    action, payload_json, created_at
                ) VALUES (?, ?, 'ai_assistant', ?, 'finalized', ?, ?)
                """,
                (
                    f"legacy_observation_review_{batch_id}",
                    batch_id,
                    REVIEWER_ID,
                    json.dumps(review, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO publication_runs (
                    run_id, batch_id, observation_id, status, validation_status,
                    photo_rows_written, started_at, completed_at
                ) VALUES (?, ?, ?, 'legacy', 'not_recorded', ?, ?, ?)
                """,
                (
                    f"legacy_{batch_id}",
                    batch_id,
                    observation_id,
                    len(batch_photos),
                    now,
                    now,
                ),
            )

    with service._connection() as connection:
        connection.execute(
            "UPDATE observation_reviews SET completion_status = 'partially_reviewed'"
        )

        missing_reviews = connection.execute(
            """
            SELECT batch_id, site_id, observation_date, published_at, created_at
            FROM batches
            WHERE status = 'published'
              AND NOT EXISTS (
                  SELECT 1 FROM observation_reviews review
                  WHERE review.batch_id = batches.batch_id
              )
            ORDER BY site_id, observation_date
            """
        ).fetchall()
        observations_by_key = {
            (row["site_id"], row["observation_date"]): row for row in observations
        }
        for batch in missing_reviews:
            observation = observations_by_key.get(
                (str(batch["site_id"]), str(batch["observation_date"]))
            )
            if observation is None:
                raise ValueError(
                    f"Published batch {batch['batch_id']} has no public observation row"
                )
            review = normalized_review(observation)
            columns = ", ".join(OBSERVATION_REVIEW_FIELDS)
            placeholders = ", ".join("?" for _ in OBSERVATION_REVIEW_FIELDS)
            timestamp = str(batch["published_at"] or batch["created_at"] or now)
            connection.execute(
                f"""
                INSERT INTO observation_reviews (
                    batch_id, {columns}, is_final, reviewed_by, updated_at
                ) VALUES (?, {placeholders}, 1, 'legacy_publication', ?)
                """,
                (
                    batch["batch_id"],
                    *(review[field] for field in OBSERVATION_REVIEW_FIELDS),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO observation_review_events (
                    review_event_id, batch_id, actor_type, actor_id,
                    action, payload_json, created_at
                ) VALUES (?, ?, 'legacy_unknown', 'pre_tracking_publication',
                          'finalized', ?, ?)
                """,
                (
                    f"legacy_observation_review_{batch['batch_id']}",
                    batch["batch_id"],
                    json.dumps(review, sort_keys=True, separators=(",", ":")),
                    timestamp,
                ),
            )
            imported_published_reviews += 1

    return {
        "historical_batches": len(historical),
        "historical_photos": sum(len(rows) for rows in photos_by_observation.values()),
        "new_batches_registered": imported_batches,
        "new_photos_registered": imported_photos,
        "published_reviews_registered": imported_published_reviews,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC_DIR)
    parser.add_argument("--schema-dir", type=Path, default=DEFAULT_SCHEMA_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    args = parser.parse_args()

    changed = normalize_public_statuses(args.public_dir)
    service = IngestionService(
        IngestionPaths(
            public_dir=args.public_dir,
            schema_dir=args.schema_dir,
            state_dir=args.state_dir,
            archive_dir=args.archive_dir,
            site_anchor_file=DEFAULT_SITE_ANCHORS if DEFAULT_SITE_ANCHORS.exists() else None,
        )
    )
    result = import_archive(service)
    print(
        f"Normalized {changed} public review-status cells; "
        f"registered {result['new_batches_registered']} new historical batches and "
        f"{result['new_photos_registered']} new historical photos "
        f"({result['historical_batches']} batches / {result['historical_photos']} photos total); "
        f"backfilled {result['published_reviews_registered']} pre-tracking observation reviews."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
