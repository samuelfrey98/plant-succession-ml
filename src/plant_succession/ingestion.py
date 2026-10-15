"""Durable staging and publication for field-photo ingestion batches."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from plant_succession.validate import validate_public_photo_data


register_heif_opener()

PHOTO_TYPES = (
    "wide",
    "quadrat",
    "close_up",
    "context_left",
    "context_right",
    "extra",
    "unclassified",
)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
MAX_FILE_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
TOKEN_PATTERN = re.compile(r"[^a-z0-9]+")


class IngestionError(ValueError):
    """A user-correctable ingestion or publication error."""


@dataclass(frozen=True)
class CandidateLabel:
    photo_type: str
    confidence: float
    reason: str
    model_version: str = "filename_rules_v1"


@dataclass(frozen=True)
class IngestionPaths:
    public_dir: Path
    schema_dir: Path
    state_dir: Path
    archive_dir: Path

    @property
    def database(self) -> Path:
        return self.state_dir / "ingestion.sqlite"

    @property
    def batch_dir(self) -> Path:
        return self.state_dir / "batches"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def observation_id(site_id: str, observation_date: str) -> str:
    return f"site_{int(site_id):02d}_{observation_date.replace('-', '_')}"


def sanitize_filename(filename: str) -> str:
    safe_name = Path(filename).name.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise IngestionError("Every upload must have a valid filename.")
    return safe_name


def infer_photo_type(filename: str) -> CandidateLabel:
    """Suggest a role only when the filename carries explicit protocol wording."""

    token = TOKEN_PATTERN.sub("_", Path(filename).stem.casefold()).strip("_")
    padded = f"_{token}_"
    rules = (
        (("context_left", "left_context", "contextleft"), "context_left"),
        (("context_right", "right_context", "contextright"), "context_right"),
        (("close_up", "closeup", "close"), "close_up"),
        (("quadrat", "quad"), "quadrat"),
        (("wide", "overview"), "wide"),
    )
    for terms, photo_type in rules:
        if any(f"_{term}_" in padded for term in terms):
            return CandidateLabel(
                photo_type=photo_type,
                confidence=0.99,
                reason=f"Filename contains explicit '{photo_type}' wording.",
            )
    return CandidateLabel(
        photo_type="unclassified",
        confidence=0.0,
        reason="No explicit role appears in the filename; visual review required.",
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def atomic_write_csv(
    path: Path, fields: list[str], rows: Iterable[dict[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


class IngestionService:
    """Coordinates private staging, human review, and validated publication."""

    def __init__(self, paths: IngestionPaths, max_file_bytes: int = MAX_FILE_BYTES):
        self.paths = paths
        self.max_file_bytes = max_file_bytes
        self._publish_lock = threading.Lock()
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.batch_dir.mkdir(parents=True, exist_ok=True)
        self.paths.archive_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    observation_date TEXT NOT NULL,
                    observation_notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    photo_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS photos (
                    photo_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    source_filename TEXT NOT NULL,
                    stored_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    file_size_bytes INTEGER NOT NULL,
                    width_px INTEGER,
                    height_px INTEGER,
                    captured_at TEXT,
                    date_warning INTEGER NOT NULL DEFAULT 0,
                    preview_filename TEXT,
                    candidate_type TEXT NOT NULL,
                    candidate_confidence REAL NOT NULL,
                    candidate_reason TEXT NOT NULL,
                    label_model_version TEXT NOT NULL,
                    reviewed_type TEXT,
                    review_status TEXT NOT NULL,
                    review_notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS photos_batch_id_idx ON photos(batch_id);
                CREATE INDEX IF NOT EXISTS batches_status_idx ON batches(status);
                """
            )

    def list_sites(self) -> list[dict[str, str]]:
        _, rows = read_csv(self.paths.public_dir / "site_registry.csv")
        return [
            {
                "site_id": row["site_id"],
                "site_name": row.get("site_name", ""),
                "habitat_context": row.get("habitat_context", ""),
            }
            for row in rows
            if row.get("active", "").strip().lower() != "false"
        ]

    def create_batch(
        self,
        site_id: str,
        observation_date: str,
        observation_notes: str,
        uploads: Iterable[tuple[str, BinaryIO]],
    ) -> dict[str, object]:
        site_id = str(site_id).strip()
        valid_sites = {site["site_id"] for site in self.list_sites()}
        if site_id not in valid_sites:
            raise IngestionError(f"Unknown or inactive site_id '{site_id}'.")
        try:
            normalized_date = date.fromisoformat(observation_date.strip()).isoformat()
        except ValueError as exc:
            raise IngestionError("Observation date must use YYYY-MM-DD.") from exc

        batch_id = f"batch_{normalized_date.replace('-', '')}_{uuid.uuid4().hex[:8]}"
        batch_root = self.paths.batch_dir / batch_id
        original_dir = batch_root / "original"
        preview_dir = batch_root / "previews"
        original_dir.mkdir(parents=True)
        preview_dir.mkdir(parents=True)
        created_at = utc_now()

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO batches (
                    batch_id, site_id, observation_date, observation_notes, status, created_at
                ) VALUES (?, ?, ?, ?, 'staged', ?)
                """,
                (batch_id, site_id, normalized_date, observation_notes.strip(), created_at),
            )

        accepted = 0
        duplicates = 0
        try:
            for filename, stream in uploads:
                try:
                    self._stage_photo(
                        batch_id,
                        site_id,
                        normalized_date,
                        filename,
                        stream,
                        original_dir,
                        preview_dir,
                    )
                    accepted += 1
                except IngestionError as exc:
                    if "already staged" in str(exc):
                        duplicates += 1
                        continue
                    raise
            if accepted == 0:
                raise IngestionError("No new supported photos were found in this upload.")
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE batches
                    SET status = 'needs_review', photo_count = ?, duplicate_count = ?
                    WHERE batch_id = ?
                    """,
                    (accepted, duplicates, batch_id),
                )
        except Exception:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE batches SET status = 'failed' WHERE batch_id = ?",
                    (batch_id,),
                )
            raise
        return self.get_batch(batch_id)

    def _stage_photo(
        self,
        batch_id: str,
        site_id: str,
        observation_date: str,
        filename: str,
        stream: BinaryIO,
        original_dir: Path,
        preview_dir: Path,
    ) -> None:
        safe_name = sanitize_filename(filename)
        extension = Path(safe_name).suffix.casefold()
        if extension not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            raise IngestionError(f"Unsupported file '{safe_name}'. Allowed extensions: {allowed}.")

        temporary_path = original_dir / f".upload-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        size = 0
        with temporary_path.open("wb") as destination:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > self.max_file_bytes:
                    destination.close()
                    temporary_path.unlink(missing_ok=True)
                    raise IngestionError(f"'{safe_name}' exceeds the 50 MB upload limit.")
                digest.update(chunk)
                destination.write(chunk)
        sha256 = digest.hexdigest()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT photo_id FROM photos WHERE sha256 = ?", (sha256,)
            ).fetchone()
        if existing is not None:
            temporary_path.unlink(missing_ok=True)
            raise IngestionError(f"'{safe_name}' was already staged as {existing['photo_id']}.")

        photo_id = f"site_{int(site_id):02d}_{observation_date.replace('-', '_')}_{sha256[:12]}"
        stored_filename = f"{photo_id}{extension}"
        stored_path = original_dir / stored_filename
        os.replace(temporary_path, stored_path)
        candidate = infer_photo_type(safe_name)

        width, height, captured_at, preview_filename = self._create_preview(
            stored_path, preview_dir, photo_id
        )
        date_warning = bool(captured_at and captured_at[:10] != observation_date)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO photos (
                    photo_id, batch_id, source_filename, stored_filename, sha256,
                    file_size_bytes, width_px, height_px, captured_at, date_warning,
                    preview_filename, candidate_type, candidate_confidence,
                    candidate_reason, label_model_version, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?)
                """,
                (
                    photo_id,
                    batch_id,
                    safe_name,
                    stored_filename,
                    sha256,
                    size,
                    width,
                    height,
                    captured_at,
                    int(date_warning),
                    preview_filename,
                    candidate.photo_type,
                    candidate.confidence,
                    candidate.reason,
                    candidate.model_version,
                    utc_now(),
                ),
            )

    def _create_preview(
        self, source: Path, preview_dir: Path, photo_id: str
    ) -> tuple[int | None, int | None, str | None, str | None]:
        try:
            with Image.open(source) as image:
                width, height = image.size
                captured_at = self._captured_at(image)
                preview = ImageOps.exif_transpose(image).convert("RGB")
                preview.thumbnail((1400, 1000), Image.Resampling.LANCZOS)
                preview_filename = f"{photo_id}.jpg"
                preview.save(preview_dir / preview_filename, "JPEG", quality=84, optimize=True)
                return width, height, captured_at, preview_filename
        except Exception:
            return None, None, None, None

    @staticmethod
    def _captured_at(image: Image.Image) -> str | None:
        try:
            raw = image.getexif().get(36867) or image.getexif().get(306)
            if not raw:
                return None
            parsed = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
            return parsed.isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return None

    def dashboard(self) -> dict[str, object]:
        with self._connection() as connection:
            status_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM batches GROUP BY status"
                )
            }
            unresolved = connection.execute(
                "SELECT COUNT(*) AS count FROM photos WHERE review_status != 'verified'"
            ).fetchone()["count"]
        return {
            "counts": {
                "needs_review": status_counts.get("needs_review", 0),
                "ready": status_counts.get("ready", 0),
                "published": status_counts.get("published", 0),
                "failed": status_counts.get("failed", 0),
                "unresolved_photos": unresolved,
            },
            "batches": self.list_batches(),
        }

    def list_batches(self) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT b.*,
                       SUM(CASE WHEN p.review_status = 'verified' THEN 1 ELSE 0 END) AS reviewed_count
                FROM batches b
                LEFT JOIN photos p ON p.batch_id = b.batch_id
                GROUP BY b.batch_id
                ORDER BY b.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, object]:
        with self._connection() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise IngestionError(f"Unknown batch '{batch_id}'.")
            photos = connection.execute(
                "SELECT * FROM photos WHERE batch_id = ? ORDER BY source_filename, photo_id",
                (batch_id,),
            ).fetchall()
        result = dict(batch)
        result["photos"] = [dict(photo) for photo in photos]
        return result

    def preview_path(self, photo_id: str) -> Path:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT batch_id, preview_filename FROM photos WHERE photo_id = ?",
                (photo_id,),
            ).fetchone()
        if row is None or not row["preview_filename"]:
            raise IngestionError(f"No preview is available for '{photo_id}'.")
        path = self.paths.batch_dir / row["batch_id"] / "previews" / row["preview_filename"]
        if not path.exists():
            raise IngestionError(f"Preview file is missing for '{photo_id}'.")
        return path

    def review_photo(self, photo_id: str, photo_type: str, notes: str = "") -> dict[str, object]:
        if photo_type not in PHOTO_TYPES or photo_type == "unclassified":
            raise IngestionError("Choose a resolved photo type before marking the photo reviewed.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT batch_id FROM photos WHERE photo_id = ?", (photo_id,)
            ).fetchone()
            if row is None:
                raise IngestionError(f"Unknown photo '{photo_id}'.")
            connection.execute(
                """
                UPDATE photos
                SET reviewed_type = ?, review_status = 'verified', review_notes = ?
                WHERE photo_id = ?
                """,
                (photo_type, notes.strip(), photo_id),
            )
            self._refresh_batch_status(connection, row["batch_id"])
        return self.get_batch(row["batch_id"])

    def accept_explicit_suggestions(self, batch_id: str) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE photos
                SET reviewed_type = candidate_type,
                    review_status = 'verified',
                    review_notes = CASE
                        WHEN review_notes = '' THEN 'Accepted explicit filename suggestion.'
                        ELSE review_notes
                    END
                WHERE batch_id = ?
                  AND review_status != 'verified'
                  AND candidate_confidence >= 0.95
                  AND candidate_type != 'unclassified'
                """,
                (batch_id,),
            )
            self._refresh_batch_status(connection, batch_id)
        return self.get_batch(batch_id)

    @staticmethod
    def _refresh_batch_status(connection: sqlite3.Connection, batch_id: str) -> None:
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END) AS reviewed
            FROM photos WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        status = "ready" if counts["total"] and counts["total"] == counts["reviewed"] else "needs_review"
        connection.execute(
            "UPDATE batches SET status = ? WHERE batch_id = ? AND status != 'published'",
            (status, batch_id),
        )

    def publish_batch(self, batch_id: str) -> dict[str, object]:
        with self._publish_lock:
            batch = self.get_batch(batch_id)
            if batch["status"] == "published":
                return batch
            if batch["status"] != "ready":
                raise IngestionError("Every photo must have a verified role before publication.")

            copied_paths: list[Path] = []
            try:
                manifest_fields, manifest_rows = read_csv(
                    self.paths.public_dir / "photo_manifest.csv"
                )
                observation_fields, observation_rows = read_csv(
                    self.paths.public_dir / "observations.csv"
                )
                summary_fields, summary_rows = read_csv(
                    self.paths.public_dir / "photo_batch_summary.csv"
                )

                known_photo_ids = {row["photo_id"] for row in manifest_rows}
                site_id = str(batch["site_id"])
                observed_on = str(batch["observation_date"])
                obs_id = observation_id(site_id, observed_on)
                new_manifest_rows: list[dict[str, object]] = []
                for photo in batch["photos"]:
                    if photo["photo_id"] in known_photo_ids:
                        raise IngestionError(
                            f"Photo '{photo['photo_id']}' already exists in the public manifest."
                        )
                    extension = Path(str(photo["stored_filename"])).suffix.casefold()
                    archive_name = f"{photo['photo_id']}{extension}"
                    relative_path = (
                        f"photos/site_{int(site_id):02d}/{observed_on}/{archive_name}"
                    )
                    destination = self.paths.archive_dir / f"site_{int(site_id):02d}" / observed_on / archive_name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = (
                        self.paths.batch_dir
                        / batch_id
                        / "original"
                        / str(photo["stored_filename"])
                    )
                    if destination.exists():
                        if self._sha256(destination) != photo["sha256"]:
                            raise IngestionError(f"Archive path collision: {destination}")
                    else:
                        shutil.copy2(source, destination)
                        copied_paths.append(destination)
                    note = "Uploaded and classified through the ingestion service."
                    if photo["review_notes"]:
                        note = f"{note} {photo['review_notes']}"
                    new_manifest_rows.append(
                        {
                            "photo_id": photo["photo_id"],
                            "observation_id": obs_id,
                            "site_id": site_id,
                            "observation_date": observed_on,
                            "photo_type": photo["reviewed_type"],
                            "source_filename": photo["source_filename"],
                            "relative_path": relative_path,
                            "file_size_bytes": photo["file_size_bytes"],
                            "classification_status": "verified",
                            "notes": note,
                        }
                    )

                manifest_rows.extend(new_manifest_rows)
                manifest_rows.sort(
                    key=lambda row: (
                        int(row["site_id"]),
                        row["observation_date"],
                        row["photo_id"],
                    )
                )
                self._upsert_observation(
                    observation_rows,
                    observation_fields,
                    obs_id,
                    site_id,
                    observed_on,
                    str(batch["observation_notes"]),
                    sum(1 for row in manifest_rows if row["observation_id"] == obs_id),
                )
                summary_rows = self._rebuild_summary(manifest_rows, summary_rows)

                with tempfile.TemporaryDirectory(
                    prefix="ingestion-publish-", dir=self.paths.state_dir
                ) as temporary:
                    candidate_dir = Path(temporary)
                    for source in self.paths.public_dir.glob("*.csv"):
                        shutil.copy2(source, candidate_dir / source.name)
                    write_csv(candidate_dir / "photo_manifest.csv", manifest_fields, manifest_rows)
                    write_csv(
                        candidate_dir / "observations.csv", observation_fields, observation_rows
                    )
                    write_csv(
                        candidate_dir / "photo_batch_summary.csv", summary_fields, summary_rows
                    )
                    issues = validate_public_photo_data(candidate_dir, self.paths.schema_dir)
                    if issues:
                        details = "; ".join(issue.format() for issue in issues[:8])
                        raise IngestionError(f"Publication failed validation: {details}")

                atomic_write_csv(
                    self.paths.public_dir / "photo_manifest.csv", manifest_fields, manifest_rows
                )
                atomic_write_csv(
                    self.paths.public_dir / "observations.csv",
                    observation_fields,
                    observation_rows,
                )
                atomic_write_csv(
                    self.paths.public_dir / "photo_batch_summary.csv",
                    summary_fields,
                    summary_rows,
                )
            except Exception:
                for path in copied_paths:
                    path.unlink(missing_ok=True)
                raise

            with self._connection() as connection:
                connection.execute(
                    "UPDATE batches SET status = 'published', published_at = ? WHERE batch_id = ?",
                    (utc_now(), batch_id),
                )
            return self.get_batch(batch_id)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _upsert_observation(
        rows: list[dict[str, str]],
        fields: list[str],
        obs_id: str,
        site_id: str,
        observed_on: str,
        notes: str,
        photo_count: int,
    ) -> None:
        existing = next((row for row in rows if row["observation_id"] == obs_id), None)
        if existing is not None:
            existing["source_photo_count"] = str(photo_count)
            return
        row = {field: "" for field in fields}
        row.update(
            {
                "observation_id": obs_id,
                "site_id": site_id,
                "observation_date": observed_on,
                "record_origin": "ingestion_app",
                "review_status": "partially_reviewed",
                "source_photo_count": str(photo_count),
                "notes": notes.strip()
                or "Observation ingested through the upload service; ecological fields pending review.",
            }
        )
        rows.append(row)
        rows.sort(key=lambda item: (int(item["site_id"]), item["observation_date"]))

    @staticmethod
    def _rebuild_summary(
        manifest_rows: list[dict[str, str]], prior_rows: list[dict[str, str]]
    ) -> list[dict[str, object]]:
        counts: dict[tuple[str, str], int] = {}
        for row in manifest_rows:
            key = (row["site_id"], row["observation_date"])
            counts[key] = counts.get(key, 0) + 1
        for row in prior_rows:
            key = (row["site_id"], row["observation_date"])
            if row.get("upload_status") == "empty_folder" and key not in counts:
                counts[key] = 0
        return [
            {
                "site_id": site_id,
                "observation_date": observed_on,
                "photo_count": count,
                "upload_status": "uploaded" if count else "empty_folder",
            }
            for (site_id, observed_on), count in sorted(
                counts.items(), key=lambda item: (int(item[0][0]), item[0][1])
            )
        ]
