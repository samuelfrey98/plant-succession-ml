"""Durable staging and publication for field-photo ingestion batches."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
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
UPLOAD_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
MAX_SITE_MATCH_DISTANCE_METERS = 150.0
UPLOAD_SUGGESTION_MAX_AGE_SECONDS = 24 * 60 * 60
REVIEW_ACTOR_TYPES = {"human", "ai_assistant", "model", "rule", "legacy_unknown"}


class IngestionError(ValueError):
    """A user-correctable ingestion or publication error."""


@dataclass(frozen=True)
class CandidateLabel:
    photo_type: str
    confidence: float
    reason: str
    model_version: str = "filename_rules_v1"


@dataclass(frozen=True)
class SiteAnchor:
    site_id: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PhotoMetadata:
    filename: str
    captured_at: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class IngestionPaths:
    public_dir: Path
    schema_dir: Path
    state_dir: Path
    archive_dir: Path
    site_anchor_file: Path | None = None

    @property
    def database(self) -> Path:
        return self.state_dir / "ingestion.sqlite"

    @property
    def batch_dir(self) -> Path:
        return self.state_dir / "batches"

    @property
    def suggestion_dir(self) -> Path:
        return self.state_dir / "upload_suggestions"


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


def _exif_capture_time(exif: object) -> str | None:
    mappings = []
    try:
        mappings.append(exif.get_ifd(34665))  # type: ignore[attr-defined]
    except Exception:
        pass
    mappings.append(exif)
    for mapping in mappings:
        for tag in (36867, 36868, 306):
            try:
                raw = mapping.get(tag)  # type: ignore[attr-defined]
            except Exception:
                raw = None
            if not raw:
                continue
            try:
                return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").isoformat(
                    timespec="seconds"
                )
            except ValueError:
                continue
    return None


def _gps_coordinates(exif: object) -> tuple[float | None, float | None]:
    try:
        gps = exif.get_ifd(34853)  # type: ignore[attr-defined]
    except Exception:
        return None, None
    try:
        latitude_values = gps.get(2)
        longitude_values = gps.get(4)
        if not latitude_values or not longitude_values:
            return None, None
        latitude = float(latitude_values[0]) + float(latitude_values[1]) / 60
        latitude += float(latitude_values[2]) / 3600
        longitude = float(longitude_values[0]) + float(longitude_values[1]) / 60
        longitude += float(longitude_values[2]) / 3600
        latitude_ref = str(gps.get(1, "N")).upper()
        longitude_ref = str(gps.get(3, "E")).upper()
        if latitude_ref == "S":
            latitude *= -1
        if longitude_ref == "W":
            longitude *= -1
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None, None
        return latitude, longitude
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None, None


def extract_photo_metadata(path: Path, source_filename: str) -> PhotoMetadata:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            latitude, longitude = _gps_coordinates(exif)
            return PhotoMetadata(
                filename=source_filename,
                captured_at=_exif_capture_time(exif),
                latitude=latitude,
                longitude=longitude,
            )
    except Exception:
        return PhotoMetadata(source_filename, None, None, None)


def haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_meters = 6_371_008.8
    latitude_a_rad = math.radians(latitude_a)
    latitude_b_rad = math.radians(latitude_b)
    latitude_delta = math.radians(latitude_b - latitude_a)
    longitude_delta = math.radians(longitude_b - longitude_a)
    haversine = math.sin(latitude_delta / 2) ** 2
    haversine += (
        math.cos(latitude_a_rad)
        * math.cos(latitude_b_rad)
        * math.sin(longitude_delta / 2) ** 2
    )
    return radius_meters * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def suggest_batch_metadata(
    photos: list[PhotoMetadata], anchors: list[SiteAnchor]
) -> dict[str, object]:
    warnings: list[str] = []
    file_results: list[dict[str, object]] = []
    location_rows = [
        photo for photo in photos if photo.latitude is not None and photo.longitude is not None
    ]
    site_votes: Counter[str] = Counter()
    nearest_by_index: dict[int, tuple[str, float]] = {}
    margin_by_index: dict[int, float | None] = {}
    for index, photo in enumerate(photos):
        if photo.latitude is None or photo.longitude is None:
            continue
        distances = sorted(
            (
                (
                    anchor.site_id,
                    haversine_meters(
                        float(photo.latitude),
                        float(photo.longitude),
                        anchor.latitude,
                        anchor.longitude,
                    ),
                )
                for anchor in anchors
            ),
            key=lambda item: item[1],
        )
        if distances:
            nearest_by_index[index] = distances[0]
            margin_by_index[index] = (
                distances[1][1] - distances[0][1] if len(distances) > 1 else None
            )
            if distances[0][1] <= MAX_SITE_MATCH_DISTANCE_METERS:
                site_votes[distances[0][0]] += 1

    suggested_site: str | None = None
    site_confidence: str | None = None
    median_distance: float | None = None
    median_margin: float | None = None
    vote_fraction: float | None = None
    if site_votes:
        ranked_votes = site_votes.most_common()
        if len(ranked_votes) > 1 and ranked_votes[0][1] == ranked_votes[1][1]:
            warnings.append("GPS metadata is split evenly across multiple sites.")
        else:
            suggested_site = ranked_votes[0][0]
            winning_count = ranked_votes[0][1]
            vote_fraction = winning_count / sum(site_votes.values())
            winning_distances = [
                distance
                for site_id, distance in nearest_by_index.values()
                if site_id == suggested_site
            ]
            median_distance = median(winning_distances)
            winning_margins = [
                margin_by_index[index]
                for index, (site_id, _) in nearest_by_index.items()
                if site_id == suggested_site and margin_by_index[index] is not None
            ]
            median_margin = median(winning_margins) if winning_margins else None
            has_clear_high_margin = median_margin is None or median_margin >= 15
            has_clear_medium_margin = median_margin is None or median_margin >= 5
            if vote_fraction == 1 and median_distance <= 25 and has_clear_high_margin:
                site_confidence = "high"
            elif (
                vote_fraction >= 0.75
                and median_distance <= 75
                and has_clear_medium_margin
            ):
                site_confidence = "medium"
            else:
                site_confidence = "low"
            if median_margin is not None and median_margin < 15:
                warnings.append(
                    "The nearest site is close to another site anchor; confirm the site manually."
                )
    elif location_rows and not anchors:
        warnings.append("GPS is present, but the private site-anchor file is unavailable.")
    elif location_rows:
        warnings.append("GPS locations are not close enough to a known monitoring site.")
    else:
        warnings.append("No usable GPS metadata was found in the selected photos.")

    if len(site_votes) > 1:
        distribution = ", ".join(
            f"Site {site_id}: {count}" for site_id, count in site_votes.most_common()
        )
        warnings.append(f"The selected photos may span multiple sites ({distribution}).")

    captured_dates = [photo.captured_at[:10] for photo in photos if photo.captured_at]
    date_votes = Counter(captured_dates)
    suggested_date: str | None = None
    date_confidence: str | None = None
    if date_votes:
        ranked_dates = date_votes.most_common()
        if len(ranked_dates) > 1 and ranked_dates[0][1] == ranked_dates[1][1]:
            warnings.append("Capture timestamps are split evenly across multiple dates.")
        else:
            suggested_date, date_count = ranked_dates[0]
            date_fraction = date_count / len(captured_dates)
            if date_fraction == 1:
                date_confidence = "high"
            elif date_fraction >= 0.75:
                date_confidence = "medium"
            else:
                date_confidence = "low"
        if len(date_votes) > 1:
            distribution = ", ".join(
                f"{captured_date}: {count}"
                for captured_date, count in date_votes.most_common()
            )
            warnings.append(f"The selected photos contain multiple capture dates ({distribution}).")
    else:
        warnings.append("No usable capture timestamp was found in the selected photos.")

    for index, photo in enumerate(photos):
        nearest = nearest_by_index.get(index)
        file_results.append(
            {
                "filename": photo.filename,
                "captured_at": photo.captured_at,
                "gps_present": photo.latitude is not None and photo.longitude is not None,
                "nearest_site_id": nearest[0] if nearest else None,
                "distance_meters": round(nearest[1], 1) if nearest else None,
                "runner_up_margin_meters": round(margin_by_index[index], 1)
                if margin_by_index.get(index) is not None
                else None,
            }
        )

    return {
        "site_id": suggested_site,
        "site_confidence": site_confidence,
        "site_vote_fraction": round(vote_fraction, 2) if vote_fraction is not None else None,
        "median_distance_meters": round(median_distance, 1)
        if median_distance is not None
        else None,
        "median_runner_up_margin_meters": round(median_margin, 1)
        if median_margin is not None
        else None,
        "observation_date": suggested_date,
        "date_confidence": date_confidence,
        "gps_photo_count": len(location_rows),
        "capture_time_count": len(captured_dates),
        "anchor_count": len(anchors),
        "warnings": warnings,
        "files": file_results,
    }


def assess_batch_integrity(
    files: list[dict[str, object]], site_id: str, observation_date: str
) -> dict[str, object]:
    """Compare every prepared photo with the site and date selected for its batch."""

    checks: list[dict[str, object]] = []
    issue_count = 0
    warning_count = 0
    for index, item in enumerate(files, start=1):
        filename = str(item.get("source_filename") or item.get("filename") or f"Photo {index}")
        captured_at = str(item.get("captured_at") or "")
        capture_date = captured_at[:10] if len(captured_at) >= 10 else None
        nearest_site_id = str(item.get("nearest_site_id") or "") or None
        gps_present = bool(item.get("gps_present"))
        distance = item.get("distance_meters")
        margin = item.get("runner_up_margin_meters")

        if not gps_present or nearest_site_id is None:
            site_check = "missing_metadata"
        elif distance is not None and float(distance) > MAX_SITE_MATCH_DISTANCE_METERS:
            site_check = "outside_site_range"
        elif nearest_site_id != str(site_id):
            site_check = "site_mismatch"
        elif margin is not None and float(margin) < 15:
            site_check = "ambiguous_site"
        else:
            site_check = "match"

        if capture_date is None:
            date_check = "missing_metadata"
        elif capture_date != observation_date:
            date_check = "date_mismatch"
        else:
            date_check = "match"

        blocking = site_check not in {"match", "ambiguous_site"} or date_check != "match"
        warning = site_check == "ambiguous_site"
        issue_count += int(blocking)
        warning_count += int(warning)
        checks.append(
            {
                "file_index": index,
                "filename": filename,
                "captured_at": captured_at or None,
                "capture_date": capture_date,
                "gps_present": gps_present,
                "nearest_site_id": nearest_site_id,
                "distance_meters": distance,
                "runner_up_margin_meters": margin,
                "site_check": site_check,
                "date_check": date_check,
                "blocking": blocking,
                "warning": warning,
            }
        )

    if issue_count:
        status = "attention_required"
    elif warning_count:
        status = "passed_with_warnings"
    else:
        status = "passed"
    return {
        "status": status,
        "site_id": str(site_id),
        "observation_date": observation_date,
        "photo_count": len(checks),
        "issue_count": issue_count,
        "warning_count": warning_count,
        "requires_override": issue_count > 0,
        "files": checks,
    }


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
        self.paths.suggestion_dir.mkdir(parents=True, exist_ok=True)
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

                CREATE TABLE IF NOT EXISTS batch_integrity_checks (
                    batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    observation_date TEXT NOT NULL,
                    photo_count INTEGER NOT NULL,
                    issue_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    override_confirmed INTEGER NOT NULL DEFAULT 0,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS batch_integrity_files (
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    file_index INTEGER NOT NULL,
                    source_filename TEXT NOT NULL,
                    captured_at TEXT,
                    nearest_site_id TEXT,
                    distance_meters REAL,
                    runner_up_margin_meters REAL,
                    site_check TEXT NOT NULL,
                    date_check TEXT NOT NULL,
                    blocking INTEGER NOT NULL,
                    warning INTEGER NOT NULL,
                    PRIMARY KEY (batch_id, file_index)
                );

                CREATE TABLE IF NOT EXISTS review_events (
                    review_event_id TEXT PRIMARY KEY,
                    photo_id TEXT NOT NULL REFERENCES photos(photo_id) ON DELETE CASCADE,
                    label_field TEXT NOT NULL,
                    label_value TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_version TEXT,
                    action TEXT NOT NULL,
                    confidence REAL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS review_events_photo_id_idx
                ON review_events(photo_id, created_at);
                """
            )

    @staticmethod
    def _record_review_event(
        connection: sqlite3.Connection,
        photo_id: str,
        label_value: str,
        actor_type: str,
        actor_id: str,
        action: str,
        *,
        actor_version: str | None = None,
        confidence: float | None = None,
        notes: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_events (
                review_event_id, photo_id, label_field, label_value,
                actor_type, actor_id, actor_version, action,
                confidence, notes, created_at
            ) VALUES (?, ?, 'photo_type', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"review_{uuid.uuid4().hex}",
                photo_id,
                label_value,
                actor_type,
                actor_id,
                actor_version,
                action,
                confidence,
                notes.strip(),
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ),
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

    def _read_site_anchors(self) -> list[SiteAnchor]:
        path = self.paths.site_anchor_file
        if path is None or not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        anchors: list[SiteAnchor] = []
        known_sites = {site["site_id"] for site in self.list_sites()}
        for row_number, row in enumerate(rows, start=2):
            site_id = str(row.get("site_id", "")).strip()
            if site_id not in known_sites:
                raise IngestionError(
                    f"Private site-anchor row {row_number} has unknown site_id '{site_id}'."
                )
            try:
                latitude = float(row.get("latitude", ""))
                longitude = float(row.get("longitude", ""))
            except (TypeError, ValueError) as exc:
                raise IngestionError(
                    f"Private site-anchor row {row_number} has invalid coordinates."
                ) from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise IngestionError(
                    f"Private site-anchor row {row_number} has out-of-range coordinates."
                )
            anchors.append(SiteAnchor(site_id, latitude, longitude))
        if len({anchor.site_id for anchor in anchors}) != len(anchors):
            raise IngestionError("Private site-anchor file contains a duplicate site_id.")
        return anchors

    def _cleanup_upload_suggestions(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - UPLOAD_SUGGESTION_MAX_AGE_SECONDS
        for path in self.paths.suggestion_dir.iterdir():
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)

    def create_upload_suggestion(
        self, uploads: Iterable[tuple[str, BinaryIO]]
    ) -> dict[str, object]:
        """Cache uploads once and infer a reviewable site/date from EXIF metadata."""

        self._cleanup_upload_suggestions()
        upload_token = uuid.uuid4().hex
        suggestion_root = self.paths.suggestion_dir / upload_token
        suggestion_root.mkdir(parents=True)
        records: list[dict[str, object]] = []
        metadata_rows: list[PhotoMetadata] = []
        try:
            for index, (filename, stream) in enumerate(uploads, start=1):
                safe_name = sanitize_filename(filename)
                extension = Path(safe_name).suffix.casefold()
                if extension not in ALLOWED_EXTENSIONS:
                    allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
                    raise IngestionError(
                        f"Unsupported file '{safe_name}'. Allowed extensions: {allowed}."
                    )
                stored_filename = f"{index:04d}_{uuid.uuid4().hex[:8]}{extension}"
                stored_path = suggestion_root / stored_filename
                size = 0
                with stored_path.open("wb") as destination:
                    while chunk := stream.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > self.max_file_bytes:
                            raise IngestionError(f"'{safe_name}' exceeds the 50 MB upload limit.")
                        destination.write(chunk)
                metadata_rows.append(extract_photo_metadata(stored_path, safe_name))
                records.append(
                    {
                        "source_filename": safe_name,
                        "stored_filename": stored_filename,
                        "file_size_bytes": size,
                    }
                )
            if not records:
                raise IngestionError("Choose at least one supported photo.")

            result = suggest_batch_metadata(metadata_rows, self._read_site_anchors())
            for record, item in zip(records, result["files"], strict=True):  # type: ignore[call-overload]
                record.update(
                    {
                        "captured_at": item.get("captured_at"),
                        "gps_present": item.get("gps_present", False),
                        "nearest_site_id": item.get("nearest_site_id"),
                        "distance_meters": item.get("distance_meters"),
                        "runner_up_margin_meters": item.get(
                            "runner_up_margin_meters"
                        ),
                    }
                )
            manifest = {
                "upload_token": upload_token,
                "created_at": utc_now(),
                "files": records,
            }
            temporary_manifest = suggestion_root / ".metadata.json"
            temporary_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(temporary_manifest, suggestion_root / "metadata.json")
        except Exception:
            shutil.rmtree(suggestion_root, ignore_errors=True)
            raise

        result["upload_token"] = upload_token
        result["uploaded_photo_count"] = len(records)
        result["uploaded_bytes"] = sum(int(record["file_size_bytes"]) for record in records)
        return result

    def create_batch_from_upload_suggestion(
        self,
        upload_token: str,
        site_id: str,
        observation_date: str,
        observation_notes: str,
        integrity_override_confirmed: bool = False,
    ) -> dict[str, object]:
        if not UPLOAD_TOKEN_PATTERN.fullmatch(upload_token):
            raise IngestionError("The prepared upload token is invalid.")
        suggestion_root = self.paths.suggestion_dir / upload_token
        manifest_path = suggestion_root / "metadata.json"
        if not manifest_path.exists():
            raise IngestionError("The prepared upload has expired or no longer exists.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(str(manifest["created_at"]))
            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            if age > UPLOAD_SUGGESTION_MAX_AGE_SECONDS:
                shutil.rmtree(suggestion_root, ignore_errors=True)
                raise IngestionError("The prepared upload expired; choose the photos again.")
            file_records = manifest["files"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IngestionError("The prepared upload metadata is unreadable.") from exc
        if not isinstance(file_records, list) or not file_records:
            raise IngestionError("The prepared upload metadata has no photos.")
        if not all(isinstance(record, dict) for record in file_records):
            raise IngestionError("The prepared upload metadata is unreadable.")

        integrity = assess_batch_integrity(file_records, site_id, observation_date)
        if integrity["requires_override"] and not integrity_override_confirmed:
            filenames = [
                str(item["filename"])
                for item in integrity["files"]  # type: ignore[index]
                if item["blocking"]
            ]
            preview = ", ".join(filenames[:4])
            suffix = "" if len(filenames) <= 4 else f" and {len(filenames) - 4} more"
            raise IngestionError(
                "Batch integrity found site/date exceptions in "
                f"{preview}{suffix}. Review them and confirm the override before staging."
            )

        with ExitStack() as stack:
            uploads: list[tuple[str, BinaryIO]] = []
            for record in file_records:
                source_filename = str(record.get("source_filename", ""))
                stored_filename = sanitize_filename(str(record.get("stored_filename", "")))
                path = suggestion_root / stored_filename
                if not path.is_file():
                    raise IngestionError(
                        f"Prepared photo '{source_filename}' is missing; choose the photos again."
                    )
                uploads.append((source_filename, stack.enter_context(path.open("rb"))))
            batch = self.create_batch(site_id, observation_date, observation_notes, uploads)
        self._record_batch_integrity(
            str(batch["batch_id"]), integrity, integrity_override_confirmed
        )
        shutil.rmtree(suggestion_root, ignore_errors=True)
        return self.get_batch(str(batch["batch_id"]))

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
            self._record_review_event(
                connection,
                photo_id,
                candidate.photo_type,
                "rule",
                "filename_rules",
                "suggested",
                actor_version=candidate.model_version,
                confidence=candidate.confidence,
                notes=candidate.reason,
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
        return _exif_capture_time(image.getexif())

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

    def _record_batch_integrity(
        self,
        batch_id: str,
        integrity: dict[str, object],
        override_confirmed: bool,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO batch_integrity_checks (
                    batch_id, status, site_id, observation_date, photo_count,
                    issue_count, warning_count, override_confirmed, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    integrity["status"],
                    integrity["site_id"],
                    integrity["observation_date"],
                    integrity["photo_count"],
                    integrity["issue_count"],
                    integrity["warning_count"],
                    int(override_confirmed),
                    utc_now(),
                ),
            )
            connection.execute(
                "DELETE FROM batch_integrity_files WHERE batch_id = ?", (batch_id,)
            )
            connection.executemany(
                """
                INSERT INTO batch_integrity_files (
                    batch_id, file_index, source_filename, captured_at,
                    nearest_site_id, distance_meters, runner_up_margin_meters,
                    site_check, date_check, blocking, warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        item["file_index"],
                        item["filename"],
                        item["captured_at"],
                        item["nearest_site_id"],
                        item["distance_meters"],
                        item["runner_up_margin_meters"],
                        item["site_check"],
                        item["date_check"],
                        int(bool(item["blocking"])),
                        int(bool(item["warning"])),
                    )
                    for item in integrity["files"]  # type: ignore[index]
                ],
            )

    def audit_batch_integrity(self, batch_id: str) -> dict[str, object]:
        """Rebuild an integrity audit for a staged or published local batch."""

        batch = self.get_batch(batch_id)
        metadata: list[PhotoMetadata] = []
        for photo in batch["photos"]:  # type: ignore[index]
            source = (
                self.paths.batch_dir
                / batch_id
                / "original"
                / str(photo["stored_filename"])
            )
            if not source.exists():
                raise IngestionError(
                    f"Cannot audit '{batch_id}'; staged original '{source.name}' is missing."
                )
            metadata.append(extract_photo_metadata(source, str(photo["source_filename"])))
        suggestion = suggest_batch_metadata(metadata, self._read_site_anchors())
        integrity = assess_batch_integrity(
            suggestion["files"],  # type: ignore[arg-type]
            str(batch["site_id"]),
            str(batch["observation_date"]),
        )
        self._record_batch_integrity(batch_id, integrity, False)
        return integrity

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
            integrity = connection.execute(
                "SELECT * FROM batch_integrity_checks WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            integrity_files = connection.execute(
                """
                SELECT * FROM batch_integrity_files
                WHERE batch_id = ? ORDER BY file_index
                """,
                (batch_id,),
            ).fetchall()
            review_events = connection.execute(
                """
                SELECT event.* FROM review_events event
                JOIN photos photo ON photo.photo_id = event.photo_id
                WHERE photo.batch_id = ?
                ORDER BY event.created_at,
                         CASE WHEN event.action = 'suggested' THEN 0 ELSE 1 END,
                         event.review_event_id
                """,
                (batch_id,),
            ).fetchall()
        result = dict(batch)
        events_by_photo: dict[str, list[dict[str, object]]] = {}
        for event in review_events:
            events_by_photo.setdefault(str(event["photo_id"]), []).append(dict(event))
        result["photos"] = [
            {
                **dict(photo),
                "review_events": events_by_photo.get(str(photo["photo_id"]), []),
            }
            for photo in photos
        ]
        result["integrity"] = (
            {**dict(integrity), "files": [dict(item) for item in integrity_files]}
            if integrity is not None
            else None
        )
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

    def review_photo(
        self,
        photo_id: str,
        photo_type: str,
        notes: str = "",
        actor_type: str = "human",
        actor_id: str = "project_owner",
    ) -> dict[str, object]:
        if photo_type not in PHOTO_TYPES or photo_type == "unclassified":
            raise IngestionError("Choose a resolved photo type before marking the photo reviewed.")
        if actor_type not in REVIEW_ACTOR_TYPES or not actor_id.strip():
            raise IngestionError("Review actor metadata is invalid.")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT batch_id, reviewed_type, review_status
                FROM photos WHERE photo_id = ?
                """,
                (photo_id,),
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
            if row["reviewed_type"] is None:
                action = "verified"
            elif row["reviewed_type"] == photo_type:
                action = "confirmed"
            else:
                action = "corrected"
            self._record_review_event(
                connection,
                photo_id,
                photo_type,
                actor_type,
                actor_id.strip(),
                action,
                notes=notes,
            )
            self._refresh_batch_status(connection, row["batch_id"])
        return self.get_batch(row["batch_id"])

    def backfill_review_events(
        self, verified_actor_type: str, verified_actor_id: str
    ) -> dict[str, int]:
        """Create audit events for legacy ingestion rows without inventing reviewer identity."""

        if verified_actor_type not in REVIEW_ACTOR_TYPES or not verified_actor_id.strip():
            raise IngestionError("Review actor metadata is invalid.")
        candidate_events = 0
        verified_events = 0
        with self._connection() as connection:
            photos = connection.execute("SELECT * FROM photos ORDER BY created_at").fetchall()
            for photo in photos:
                candidate_exists = connection.execute(
                    """
                    SELECT 1 FROM review_events
                    WHERE photo_id = ? AND action = 'suggested'
                    LIMIT 1
                    """,
                    (photo["photo_id"],),
                ).fetchone()
                if candidate_exists is None:
                    self._record_review_event(
                        connection,
                        str(photo["photo_id"]),
                        str(photo["candidate_type"]),
                        "rule",
                        "filename_rules",
                        "suggested",
                        actor_version=str(photo["label_model_version"]),
                        confidence=float(photo["candidate_confidence"]),
                        notes=str(photo["candidate_reason"]),
                    )
                    candidate_events += 1

                verified_exists = connection.execute(
                    """
                    SELECT 1 FROM review_events
                    WHERE photo_id = ? AND action != 'suggested'
                    LIMIT 1
                    """,
                    (photo["photo_id"],),
                ).fetchone()
                if photo["reviewed_type"] and verified_exists is None:
                    self._record_review_event(
                        connection,
                        str(photo["photo_id"]),
                        str(photo["reviewed_type"]),
                        verified_actor_type,
                        verified_actor_id.strip(),
                        "verified",
                        notes=str(photo["review_notes"])
                        or "Backfilled from an existing verified ingestion review.",
                    )
                    verified_events += 1
        return {
            "candidate_events": candidate_events,
            "verified_events": verified_events,
        }

    def accept_explicit_suggestions(
        self,
        batch_id: str,
        actor_type: str = "human",
        actor_id: str = "project_owner",
    ) -> dict[str, object]:
        if actor_type not in REVIEW_ACTOR_TYPES or not actor_id.strip():
            raise IngestionError("Review actor metadata is invalid.")
        with self._connection() as connection:
            accepted = connection.execute(
                """
                SELECT photo_id, candidate_type FROM photos
                WHERE batch_id = ?
                  AND review_status != 'verified'
                  AND candidate_confidence >= 0.95
                  AND candidate_type != 'unclassified'
                """,
                (batch_id,),
            ).fetchall()
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
            for photo in accepted:
                self._record_review_event(
                    connection,
                    str(photo["photo_id"]),
                    str(photo["candidate_type"]),
                    actor_type,
                    actor_id.strip(),
                    "accepted",
                    notes="Accepted explicit filename suggestion.",
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
