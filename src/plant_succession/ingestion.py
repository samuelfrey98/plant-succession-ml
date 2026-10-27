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
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import BinaryIO, Iterable, Iterator

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from plant_succession.analysis import write_analysis_ready_export
from plant_succession.google_sheets import GoogleSheetsSyncError, SheetsPublisher
from plant_succession.machine_review import MachineReviewError, MachineReviewProvider
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
DATABASE_SCHEMA_VERSION = 7
OBSERVATION_VERIFIER_ID = "observation_rules"
OBSERVATION_VERIFIER_VERSION = "observation_rules_v2"
SURFACE_COVER_TOLERANCE = 5
DERIVED_EXPORTS = {"analysis_ready_observations.csv"}
OBSERVATION_PERCENT_FIELDS = (
    "estimated_total_vegetation_cover_pct",
    "bare_ground_pct",
    "gravel_rock_pct",
    "litter_mulch_woodchip_pct",
    "standing_dead_pct",
)
OBSERVATION_BOOLEAN_FIELDS = (
    "dominant_flowering",
    "dominant_seed_set",
    "secondary_flowering",
    "secondary_seed_set",
    "tertiary_flowering",
    "tertiary_seed_set",
    "new_species_detected",
)
OBSERVATION_TEXT_FIELDS = (
    "dominant_species",
    "secondary_species",
    "tertiary_species",
    "new_taxon_labels",
    "notes",
    "completeness_notes",
)
OBSERVATION_CATEGORY_FIELDS = {
    "dominant_taxon_rank": {"species", "genus", "family", "unknown", "morphotype"},
    "secondary_taxon_rank": {"species", "genus", "family", "unknown", "morphotype"},
    "tertiary_taxon_rank": {"species", "genus", "family", "unknown", "morphotype"},
    "dominant_species_confidence": {"confident", "likely", "uncertain"},
    "secondary_species_confidence": {"confident", "likely", "uncertain"},
    "tertiary_species_confidence": {"confident", "likely", "uncertain"},
    "successional_stage": {
        "early_colonization",
        "developing_herb_layer",
        "dense_herb_layer",
        "shrub_establishment",
        "young_woodland",
    },
    "completion_status": {"partially_reviewed", "reviewed"},
}
OBSERVATION_REVIEW_FIELDS = (
    *OBSERVATION_PERCENT_FIELDS,
    "dominant_species",
    "dominant_taxon_rank",
    "dominant_species_confidence",
    "dominant_flowering",
    "dominant_seed_set",
    "secondary_species",
    "secondary_taxon_rank",
    "secondary_species_confidence",
    "secondary_flowering",
    "secondary_seed_set",
    "tertiary_species",
    "tertiary_taxon_rank",
    "tertiary_species_confidence",
    "tertiary_flowering",
    "tertiary_seed_set",
    "new_taxon_labels",
    "new_species_detected",
    "successional_stage",
    "notes",
    "completion_status",
    "completeness_notes",
)


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


def verify_observation_values(
    review: dict[str, object],
    *,
    photos: Iterable[dict[str, object]] = (),
    integrity: dict[str, object] | None = None,
    site_taxon_history: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run deterministic, non-destructive checks on one observation review."""

    issues: list[dict[str, str]] = []

    def add(
        rule_id: str,
        severity: str,
        field_name: str,
        message: str,
        suggested_fix: str,
    ) -> None:
        issues.append(
            {
                "rule_id": rule_id,
                "severity": severity,
                "field_name": field_name,
                "message": message,
                "suggested_fix": suggested_fix,
            }
        )

    for field in OBSERVATION_PERCENT_FIELDS:
        value = review.get(field)
        if value in {None, ""}:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            add(
                "cover.invalid_number",
                "blocking",
                field,
                f"{field.replace('_', ' ')} is not numeric.",
                "Enter a whole-number percentage from 0 to 100 or leave it blank.",
            )
            continue
        if not 0 <= number <= 100:
            add(
                "cover.out_of_range",
                "blocking",
                field,
                f"{field.replace('_', ' ')} is outside 0 to 100.",
                "Enter a percentage from 0 to 100.",
            )

    surface_fields = OBSERVATION_PERCENT_FIELDS[:4]
    surface_values = [review.get(field) for field in surface_fields]
    present_surface = [value not in {None, ""} for value in surface_values]
    if any(present_surface) and not all(present_surface):
        missing = [
            field.replace("_pct", "").replace("_", " ")
            for field, present in zip(surface_fields, present_surface)
            if not present
        ]
        add(
            "cover.surface_incomplete",
            "warning",
            "estimated_total_vegetation_cover_pct",
            f"The ground-surface estimate is incomplete; missing {', '.join(missing)}.",
            "Fill all four ground-surface fields when the framing supports it, or leave a completeness note explaining the blanks.",
        )
    elif all(present_surface):
        try:
            surface_sum = sum(float(value) for value in surface_values)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            surface_sum = None
        if surface_sum is None:
            pass
        elif abs(surface_sum - 100) > SURFACE_COVER_TOLERANCE:
            add(
                "cover.surface_sum",
                "blocking",
                "estimated_total_vegetation_cover_pct",
                f"Vegetation, bare ground, gravel/rock, and litter/mulch total {surface_sum:g}%.",
                f"Recheck the four ground-surface estimates so they total within {SURFACE_COVER_TOLERANCE} percentage points of 100. Standing dead is separate and may overlap.",
            )
    else:
        add(
            "cover.surface_missing",
            "warning",
            "estimated_total_vegetation_cover_pct",
            "No ground-surface cover estimate is recorded.",
            "Estimate cover when the photos show the monitored surface, or document why it is unavailable.",
        )

    taxon_labels: list[tuple[str, str]] = []
    for prefix in ("dominant", "secondary", "tertiary"):
        label = str(review.get(f"{prefix}_species") or "").strip()
        rank = str(review.get(f"{prefix}_taxon_rank") or "").strip()
        confidence = str(review.get(f"{prefix}_species_confidence") or "").strip()
        if not label:
            continue
        taxon_labels.append((prefix, label))
        if not rank:
            add(
                "taxon.rank_missing",
                "warning",
                f"{prefix}_taxon_rank",
                f"The {prefix} taxon has a label but no taxonomic rank.",
                "Choose species, genus, family, unknown, or morphotype.",
            )
        if not confidence:
            add(
                "taxon.confidence_missing",
                "warning",
                f"{prefix}_species_confidence",
                f"The {prefix} taxon has a label but no confidence value.",
                "Record confident, likely, or uncertain.",
            )

    if not taxon_labels:
        add(
            "taxon.dominant_missing",
            "warning",
            "dominant_species",
            "No dominant taxon is recorded.",
            "Add the best defensible label or explain why the photos cannot support one.",
        )

    normalized_taxa: dict[str, list[str]] = {}
    for prefix, label in taxon_labels:
        normalized_taxa.setdefault(_taxon_key(label), []).append(prefix)
    duplicates = [roles for roles in normalized_taxa.values() if len(roles) > 1]
    if duplicates:
        add(
            "taxon.duplicate_roles",
            "blocking",
            "dominant_species",
            "The same exact taxon label appears in more than one dominance role.",
            "Keep each taxon in only its highest applicable dominance role.",
        )

    new_labels = IngestionService._split_taxon_labels(review.get("new_taxon_labels"))
    new_flag = str(review.get("new_species_detected") or "").strip().casefold()
    if new_labels and new_flag == "false":
        add(
            "new_taxon.flag_conflict",
            "blocking",
            "new_species_detected",
            "New-taxon labels are present, but New taxon recorded is not Yes.",
            "Choose Yes or remove labels that are not newly recorded for this site.",
        )
    elif new_labels and not new_flag:
        add(
            "new_taxon.decision_missing",
            "warning",
            "new_species_detected",
            "Taxon labels are recorded, but the new-taxon decision is blank.",
            "Choose Yes, No, or leave a completeness note if the site-history comparison is still unresolved.",
        )
    if new_flag == "true" and not new_labels:
        add(
            "new_taxon.labels_missing",
            "blocking",
            "new_taxon_labels",
            "New taxon recorded is Yes, but no new label is named.",
            "List every newly recorded label, separated by semicolons.",
        )

    history = site_taxon_history or {}
    prior_taxa = {
        _taxon_key(str(item.get("label") or ""))
        for item in history.get("taxa", [])  # type: ignore[union-attr]
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    }
    history_count = int(history.get("observation_count") or 0)
    previously_recorded = [label for label in new_labels if _taxon_key(label) in prior_taxa]
    if previously_recorded:
        add(
            "new_taxon.previously_recorded",
            "warning",
            "new_taxon_labels",
            f"Already present in earlier structured records: {'; '.join(previously_recorded)}.",
            "Check spelling and synonyms, then remove labels that are not newly recorded.",
        )
    if new_flag == "true" and history_count == 0:
        add(
            "new_taxon.no_baseline",
            "warning",
            "new_species_detected",
            "No earlier structured observation exists to verify a new-taxon claim.",
            "Use Unknown unless dated field evidence outside this dataset establishes the prior baseline.",
        )
    if history_count:
        novel_current = [label for _, label in taxon_labels if _taxon_key(label) not in prior_taxa]
        named_new = {_taxon_key(label) for label in new_labels}
        omitted_novel = [label for label in novel_current if _taxon_key(label) not in named_new]
        if omitted_novel and new_flag in {"false", ""}:
            add(
                "new_taxon.possible_omission",
                "warning",
                "new_species_detected",
                f"Not found by exact label in earlier site records: {'; '.join(omitted_novel)}.",
                "Check earlier synonyms or morphotypes; if these are newly recorded, choose Yes and name them.",
            )

    photo_rows = list(photos)
    unresolved = [photo for photo in photo_rows if photo.get("review_status") != "verified"]
    if unresolved:
        add(
            "photos.roles_unresolved",
            "blocking",
            "photo_roles",
            f"{len(unresolved)} photo role{'s are' if len(unresolved) != 1 else ' is'} not verified.",
            "Verify every photo role before publishing the observation.",
        )
    verified_roles = {
        str(photo.get("reviewed_type") or "")
        for photo in photo_rows
        if photo.get("review_status") == "verified"
    }
    missing_roles = [role for role in ("wide", "quadrat") if role not in verified_roles]
    if photo_rows and missing_roles:
        add(
            "photos.protocol_roles_missing",
            "warning",
            "photo_roles",
            f"Protocol role{'s are' if len(missing_roles) != 1 else ' is'} missing: {', '.join(missing_roles)}.",
            "Confirm whether the batch is incomplete or whether a photo has the wrong role.",
        )

    if integrity and int(integrity.get("issue_count") or 0):
        override = bool(integrity.get("override_confirmed"))
        add(
            "batch.integrity_override" if override else "batch.integrity_unresolved",
            "warning" if override else "blocking",
            "batch_integrity",
            "The upload contains site/date integrity exceptions"
            + (" that were manually acknowledged." if override else "."),
            "Review the per-photo location and capture-date exceptions.",
        )

    if (
        str(review.get("completion_status") or "") == "partially_reviewed"
        and not str(review.get("completeness_notes") or "").strip()
    ):
        add(
            "review.partial_without_note",
            "warning",
            "completeness_notes",
            "The review is partial but does not explain what remains unavailable.",
            "Add a concise completeness note before completing the review.",
        )

    blocking_count = sum(issue["severity"] == "blocking" for issue in issues)
    warning_count = sum(issue["severity"] == "warning" for issue in issues)
    status = "blocked" if blocking_count else "warnings" if warning_count else "passed"
    return {
        "status": status,
        "issue_count": len(issues),
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "verifier_id": OBSERVATION_VERIFIER_ID,
        "verifier_version": OBSERVATION_VERIFIER_VERSION,
        "issues": issues,
    }


def _taxon_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(
    path: Path,
    fields: list[str],
    rows: Iterable[dict[str, object]],
    *,
    line_terminator: str = "\r\n",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator=line_terminator,
        )
        writer.writeheader()
        writer.writerows(rows)


def atomic_write_csv(
    path: Path,
    fields: list[str],
    rows: Iterable[dict[str, object]],
    *,
    line_terminator: str = "\r\n",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator=line_terminator,
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


class IngestionService:
    """Coordinates private staging, human review, and validated publication."""

    def __init__(
        self,
        paths: IngestionPaths,
        max_file_bytes: int = MAX_FILE_BYTES,
        machine_reviewer: MachineReviewProvider | None = None,
        sheets_publisher: SheetsPublisher | None = None,
    ):
        self.paths = paths
        self.max_file_bytes = max_file_bytes
        self.machine_reviewer = machine_reviewer
        self.sheets_publisher = sheets_publisher
        self._publish_lock = threading.Lock()
        self._machine_review_lock = threading.Lock()
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.batch_dir.mkdir(parents=True, exist_ok=True)
        self.paths.suggestion_dir.mkdir(parents=True, exist_ok=True)
        self.paths.archive_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        self._seed_canonical_store()
        self._ensure_observation_export_fields()
        self._backfill_observation_verifications()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )
                """
            )
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

                CREATE TABLE IF NOT EXISTS observation_reviews (
                    batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id) ON DELETE CASCADE,
                    estimated_total_vegetation_cover_pct INTEGER,
                    bare_ground_pct INTEGER,
                    gravel_rock_pct INTEGER,
                    litter_mulch_woodchip_pct INTEGER,
                    standing_dead_pct INTEGER,
                    dominant_species TEXT NOT NULL DEFAULT '',
                    dominant_taxon_rank TEXT NOT NULL DEFAULT '',
                    dominant_species_confidence TEXT NOT NULL DEFAULT '',
                    dominant_flowering TEXT NOT NULL DEFAULT '',
                    dominant_seed_set TEXT NOT NULL DEFAULT '',
                    secondary_species TEXT NOT NULL DEFAULT '',
                    secondary_taxon_rank TEXT NOT NULL DEFAULT '',
                    secondary_species_confidence TEXT NOT NULL DEFAULT '',
                    secondary_flowering TEXT NOT NULL DEFAULT '',
                    secondary_seed_set TEXT NOT NULL DEFAULT '',
                    tertiary_species TEXT NOT NULL DEFAULT '',
                    tertiary_taxon_rank TEXT NOT NULL DEFAULT '',
                    tertiary_species_confidence TEXT NOT NULL DEFAULT '',
                    tertiary_flowering TEXT NOT NULL DEFAULT '',
                    tertiary_seed_set TEXT NOT NULL DEFAULT '',
                    new_species_detected TEXT NOT NULL DEFAULT '',
                    successional_stage TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    completion_status TEXT NOT NULL DEFAULT 'partially_reviewed',
                    completeness_notes TEXT NOT NULL DEFAULT '',
                    is_final INTEGER NOT NULL DEFAULT 0,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observation_review_events (
                    review_event_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_version TEXT,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS observation_review_events_batch_id_idx
                ON observation_review_events(batch_id, created_at);

                CREATE TABLE IF NOT EXISTS publication_runs (
                    run_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    observation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    photo_rows_written INTEGER NOT NULL DEFAULT 0,
                    output_row_counts_json TEXT NOT NULL DEFAULT '{}',
                    output_sha256_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS publication_runs_batch_id_idx
                ON publication_runs(batch_id, started_at);

                CREATE TABLE IF NOT EXISTS destination_sync_attempts (
                    sync_attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES publication_runs(run_id) ON DELETE CASCADE,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS destination_sync_attempts_run_id_idx
                ON destination_sync_attempts(run_id, started_at);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
                VALUES (1, 'baseline_ingestion_schema', ?)
                """,
                (utc_now(),),
            )
            self._apply_migration(
                connection,
                2,
                "canonical_public_dataset_store",
                """
                CREATE TABLE canonical_datasets (
                    filename TEXT PRIMARY KEY,
                    fields_json TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE canonical_dataset_rows (
                    filename TEXT NOT NULL
                        REFERENCES canonical_datasets(filename) ON DELETE CASCADE,
                    row_position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (filename, row_position)
                );

                CREATE INDEX canonical_dataset_rows_filename_idx
                ON canonical_dataset_rows(filename, row_position);
                """,
            )
            self._apply_migration(
                connection,
                3,
                "preserve_export_line_endings",
                """
                ALTER TABLE canonical_datasets
                ADD COLUMN line_terminator TEXT NOT NULL DEFAULT '\n';
                """,
            )
            self._apply_migration(
                connection,
                4,
                "separate_derived_analysis_export",
                """
                DELETE FROM canonical_datasets
                WHERE filename = 'analysis_ready_observations.csv';
                """,
            )
            self._apply_migration(
                connection,
                5,
                "durable_machine_review_queue",
                """
                CREATE TABLE machine_review_jobs (
                    job_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX machine_review_jobs_status_idx
                ON machine_review_jobs(status, requested_at);

                CREATE INDEX machine_review_jobs_batch_idx
                ON machine_review_jobs(batch_id, requested_at);

                CREATE UNIQUE INDEX machine_review_jobs_active_batch_idx
                ON machine_review_jobs(batch_id)
                WHERE status IN ('queued', 'running');
                """,
            )
            self._apply_migration(
                connection,
                6,
                "name_new_taxa",
                """
                ALTER TABLE observation_reviews
                ADD COLUMN new_taxon_labels TEXT NOT NULL DEFAULT '';
                """,
            )
            self._apply_migration(
                connection,
                7,
                "observation_verification_audit",
                """
                CREATE TABLE observation_verification_runs (
                    verification_run_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    review_updated_at TEXT NOT NULL,
                    verifier_id TEXT NOT NULL,
                    verifier_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue_count INTEGER NOT NULL,
                    blocking_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE INDEX observation_verification_runs_batch_idx
                ON observation_verification_runs(batch_id, checked_at);

                CREATE TABLE observation_verification_issues (
                    verification_issue_id TEXT PRIMARY KEY,
                    verification_run_id TEXT NOT NULL
                        REFERENCES observation_verification_runs(verification_run_id)
                        ON DELETE CASCADE,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    suggested_fix TEXT NOT NULL,
                    resolution_status TEXT NOT NULL DEFAULT 'open',
                    resolved_at TEXT,
                    resolved_by TEXT
                );

                CREATE INDEX observation_verification_issues_run_idx
                ON observation_verification_issues(verification_run_id, severity, rule_id);
                """,
            )
            connection.execute(
                """
                UPDATE machine_review_jobs
                SET status = 'queued',
                    started_at = NULL,
                    error_message = CASE
                        WHEN error_message = '' THEN 'Recovered after service restart.'
                        ELSE error_message
                    END
                WHERE status = 'running'
                """
            )
            connection.execute(
                """
                UPDATE batches
                SET status = 'observation_review'
                WHERE status = 'ready'
                  AND NOT EXISTS (
                      SELECT 1 FROM observation_reviews review
                      WHERE review.batch_id = batches.batch_id AND review.is_final = 1
                  )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO publication_runs (
                    run_id, batch_id, observation_id, status, validation_status,
                    photo_rows_written, started_at, completed_at
                )
                SELECT
                    'legacy_' || batch_id,
                    batch_id,
                    printf(
                        'site_%02d_%s', CAST(site_id AS INTEGER),
                        replace(observation_date, '-', '_')
                    ),
                    'legacy',
                    'not_recorded',
                    0,
                    COALESCE(published_at, created_at),
                    published_at
                FROM batches
                WHERE status = 'published'
                """
            )

    @staticmethod
    def _apply_migration(
        connection: sqlite3.Connection,
        version: int,
        name: str,
        sql: str,
    ) -> None:
        applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if applied is not None:
            return
        connection.executescript(sql)
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, utc_now()),
        )

    @staticmethod
    def _normalize_dataset_rows(
        fields: list[str], rows: Iterable[dict[str, object]]
    ) -> list[dict[str, str]]:
        return [
            {
                field: "" if row.get(field) is None else str(row.get(field, ""))
                for field in fields
            }
            for row in rows
        ]

    @staticmethod
    def _dataset_digest(fields: list[str], rows: list[dict[str, str]]) -> str:
        payload = json.dumps(
            {"fields": fields, "rows": rows},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _replace_canonical_dataset(
        self,
        connection: sqlite3.Connection,
        filename: str,
        fields: list[str],
        rows: Iterable[dict[str, object]],
        *,
        imported_at: str | None = None,
        line_terminator: str | None = None,
    ) -> None:
        normalized = self._normalize_dataset_rows(fields, rows)
        now = utc_now()
        existing = connection.execute(
            """
            SELECT imported_at, line_terminator
            FROM canonical_datasets WHERE filename = ?
            """,
            (filename,),
        ).fetchone()
        first_imported_at = (
            str(existing["imported_at"])
            if existing is not None
            else (imported_at or now)
        )
        export_line_terminator = (
            str(existing["line_terminator"])
            if existing is not None
            else (line_terminator or "\n")
        )
        connection.execute("DELETE FROM canonical_datasets WHERE filename = ?", (filename,))
        connection.execute(
            """
            INSERT INTO canonical_datasets (
                filename, fields_json, row_count, content_sha256, line_terminator,
                imported_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                json.dumps(fields, separators=(",", ":")),
                len(normalized),
                self._dataset_digest(fields, normalized),
                export_line_terminator,
                first_imported_at,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO canonical_dataset_rows (
                filename, row_position, payload_json, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    filename,
                    position,
                    json.dumps(row, sort_keys=True, separators=(",", ":")),
                    now,
                )
                for position, row in enumerate(normalized)
            ],
        )

    @staticmethod
    def _read_canonical_dataset(
        connection: sqlite3.Connection, filename: str
    ) -> tuple[list[str], list[dict[str, str]]]:
        dataset = connection.execute(
            "SELECT fields_json FROM canonical_datasets WHERE filename = ?", (filename,)
        ).fetchone()
        if dataset is None:
            raise IngestionError(f"Canonical dataset '{filename}' is not initialized.")
        fields = json.loads(str(dataset["fields_json"]))
        rows = connection.execute(
            """
            SELECT payload_json FROM canonical_dataset_rows
            WHERE filename = ? ORDER BY row_position
            """,
            (filename,),
        ).fetchall()
        return list(fields), [json.loads(str(row["payload_json"])) for row in rows]

    def read_canonical_dataset(
        self, filename: str
    ) -> tuple[list[str], list[dict[str, str]]]:
        with self._connection() as connection:
            return self._read_canonical_dataset(connection, filename)

    def _seed_canonical_store(self) -> None:
        with self._connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM canonical_datasets"
            ).fetchone()["count"]
        if count:
            return
        self.import_public_exports(self.paths.public_dir, replace=False)

    def _ensure_observation_export_fields(self) -> None:
        """Add newly optional observation columns without replacing existing rows."""

        with self._connection() as connection:
            dataset = connection.execute(
                "SELECT 1 FROM canonical_datasets WHERE filename = 'observations.csv'"
            ).fetchone()
            if dataset is None:
                return
            fields, rows = self._read_canonical_dataset(connection, "observations.csv")
            if "new_taxon_labels" in fields:
                return
            insert_at = (
                fields.index("new_species_detected")
                if "new_species_detected" in fields
                else len(fields)
            )
            fields.insert(insert_at, "new_taxon_labels")
            for row in rows:
                row["new_taxon_labels"] = ""
            self._replace_canonical_dataset(
                connection,
                "observations.csv",
                fields,
                rows,
            )

    def import_public_exports(
        self, source_dir: Path, *, replace: bool = False
    ) -> dict[str, object]:
        """Explicitly load validated CSV snapshots into the canonical SQLite store."""

        source_dir = source_dir.expanduser().resolve()
        csv_paths = sorted(
            path for path in source_dir.glob("*.csv") if path.name not in DERIVED_EXPORTS
        )
        if not csv_paths:
            raise IngestionError(f"No CSV exports were found in '{source_dir}'.")
        issues = validate_public_photo_data(source_dir, self.paths.schema_dir)
        if issues:
            details = "; ".join(issue.format() for issue in issues[:8])
            raise IngestionError(f"Import failed validation: {details}")
        imported_at = utc_now()
        with self._connection() as connection:
            existing_count = connection.execute(
                "SELECT COUNT(*) AS count FROM canonical_datasets"
            ).fetchone()["count"]
            if existing_count and not replace:
                raise IngestionError(
                    "The canonical store is already initialized; use replace=True explicitly."
                )
            if replace:
                connection.execute("DELETE FROM canonical_datasets")
            row_counts: dict[str, int] = {}
            for path in csv_paths:
                fields, rows = read_csv(path)
                line_terminator = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
                self._replace_canonical_dataset(
                    connection,
                    path.name,
                    fields,
                    rows,
                    imported_at=imported_at,
                    line_terminator=line_terminator,
                )
                row_counts[path.name] = len(rows)
        return {
            "source_dir": str(source_dir),
            "dataset_count": len(csv_paths),
            "row_counts": row_counts,
            "imported_at": imported_at,
        }

    def export_public_data(self, output_dir: Path | None = None) -> dict[str, object]:
        """Materialize validated, deterministic CSV snapshots from canonical SQLite data."""

        destination = (output_dir or self.paths.public_dir).expanduser().resolve()
        with self._connection() as connection:
            filenames = [
                str(row["filename"])
                for row in connection.execute(
                    "SELECT filename FROM canonical_datasets ORDER BY filename"
                ).fetchall()
            ]
            datasets = {}
            for filename in filenames:
                fields, rows = self._read_canonical_dataset(connection, filename)
                line_terminator = connection.execute(
                    """
                    SELECT line_terminator FROM canonical_datasets
                    WHERE filename = ?
                    """,
                    (filename,),
                ).fetchone()["line_terminator"]
                datasets[filename] = (fields, rows, str(line_terminator))
        with tempfile.TemporaryDirectory(
            prefix="canonical-export-", dir=self.paths.state_dir
        ) as temporary:
            candidate_dir = Path(temporary)
            for filename, (fields, rows, line_terminator) in datasets.items():
                write_csv(
                    candidate_dir / filename,
                    fields,
                    rows,
                    line_terminator=line_terminator,
                )
            write_analysis_ready_export(candidate_dir)
            issues = validate_public_photo_data(candidate_dir, self.paths.schema_dir)
            if issues:
                details = "; ".join(issue.format() for issue in issues[:8])
                raise IngestionError(f"Export failed validation: {details}")
            destination.mkdir(parents=True, exist_ok=True)
            row_counts: dict[str, int] = {}
            output_sha256: dict[str, str] = {}
            export_paths = sorted(candidate_dir.glob("*.csv"))
            for source in export_paths:
                filename = source.name
                target = destination / filename
                with source.open("rb") as input_handle:
                    with tempfile.NamedTemporaryFile(
                        "wb", dir=destination, delete=False
                    ) as output_handle:
                        shutil.copyfileobj(input_handle, output_handle)
                        temporary_target = Path(output_handle.name)
                os.replace(temporary_target, target)
                _, rows = read_csv(source)
                row_counts[filename] = len(rows)
                output_sha256[filename] = self._sha256(target)
        return {
            "output_dir": str(destination),
            "dataset_count": len(export_paths),
            "canonical_dataset_count": len(datasets),
            "row_counts": row_counts,
            "output_sha256": output_sha256,
            "exported_at": utc_now(),
        }

    def backup_database(self, output_dir: Path | None = None) -> dict[str, object]:
        """Create a consistent online SQLite backup and checksum manifest."""

        destination_dir = (output_dir or self.paths.state_dir / "backups").expanduser()
        destination_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = destination_dir / f"ingestion-{timestamp}.sqlite"
        with closing(self._connect()) as source, closing(
            sqlite3.connect(destination)
        ) as target:
            source.backup(target)
        with closing(sqlite3.connect(destination)) as backup:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            versions = [
                row[0]
                for row in backup.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        if integrity != "ok":
            destination.unlink(missing_ok=True)
            raise IngestionError(f"SQLite backup integrity check failed: {integrity}")
        manifest = {
            "database": destination.name,
            "created_at": utc_now(),
            "sha256": self._sha256(destination),
            "size_bytes": destination.stat().st_size,
            "schema_versions": versions,
        }
        manifest_path = destination.with_suffix(".json")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {**manifest, "path": str(destination), "manifest": str(manifest_path)}

    def canonical_store_status(self) -> dict[str, object]:
        with self._connection() as connection:
            migrations = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            datasets = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM canonical_datasets ORDER BY filename"
                ).fetchall()
            ]
        return {
            "database": str(self.paths.database),
            "schema_version": migrations[-1]["version"] if migrations else 0,
            "expected_schema_version": DATABASE_SCHEMA_VERSION,
            "migrations": migrations,
            "datasets": datasets,
        }

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
        _, rows = self.read_canonical_dataset("site_registry.csv")
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
        sites = self.list_sites()
        site_names = {site["site_id"]: site["site_name"] for site in sites}
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
            batch_rows = connection.execute(
                """
                SELECT
                    b.*,
                    COUNT(p.photo_id) AS stored_photo_count,
                    SUM(CASE WHEN p.review_status = 'verified' THEN 1 ELSE 0 END)
                        AS reviewed_count,
                    r.completion_status,
                    r.estimated_total_vegetation_cover_pct,
                    r.bare_ground_pct,
                    r.gravel_rock_pct,
                    r.litter_mulch_woodchip_pct,
                    r.standing_dead_pct,
                    r.dominant_species,
                    r.reviewed_by,
                    r.updated_at AS review_updated_at,
                    integrity.status AS integrity_status,
                    integrity.issue_count AS integrity_issue_count,
                    integrity.warning_count AS integrity_warning_count,
                    verification.status AS verification_status,
                    verification.issue_count AS verification_issue_count,
                    verification.blocking_count AS verification_blocking_count,
                    verification.warning_count AS verification_warning_count,
                    verification.review_updated_at AS verification_review_updated_at,
                    verification.verifier_version AS verification_version,
                    (
                        SELECT job.status FROM machine_review_jobs job
                        WHERE job.batch_id = b.batch_id
                        ORDER BY job.requested_at DESC, job.job_id DESC
                        LIMIT 1
                    ) AS machine_review_status,
                    (
                        SELECT job.error_message FROM machine_review_jobs job
                        WHERE job.batch_id = b.batch_id
                        ORDER BY job.requested_at DESC, job.job_id DESC
                        LIMIT 1
                    ) AS machine_review_error,
                    EXISTS (
                        SELECT 1 FROM observation_review_events event
                        WHERE event.batch_id = b.batch_id
                          AND event.actor_type = 'model'
                          AND event.action = 'suggested'
                    ) AS machine_draft_available
                FROM batches b
                LEFT JOIN photos p ON p.batch_id = b.batch_id
                LEFT JOIN observation_reviews r ON r.batch_id = b.batch_id
                LEFT JOIN batch_integrity_checks integrity
                    ON integrity.batch_id = b.batch_id
                LEFT JOIN observation_verification_runs verification
                    ON verification.verification_run_id = (
                        SELECT run.verification_run_id
                        FROM observation_verification_runs run
                        WHERE run.batch_id = b.batch_id
                        ORDER BY run.checked_at DESC, run.verification_run_id DESC
                        LIMIT 1
                    )
                GROUP BY b.batch_id
                ORDER BY b.observation_date, CAST(b.site_id AS INTEGER), b.created_at
                """
            ).fetchall()
            latest_sync = connection.execute(
                """
                SELECT * FROM destination_sync_attempts
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            failed_syncs = connection.execute(
                """
                SELECT COUNT(*) AS count FROM destination_sync_attempts
                WHERE status = 'failed'
                """
            ).fetchone()["count"]
            machine_review_queue_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM machine_review_jobs GROUP BY status
                    """
                )
            }
            observation_events = connection.execute(
                """
                SELECT batch_id, actor_type, action, created_at
                FROM observation_review_events
                ORDER BY created_at
                """
            ).fetchall()

        batches = [dict(row) for row in batch_rows]
        review_status_counts: Counter[str] = Counter()
        provenance_by_batch: dict[str, str] = {}
        for event in observation_events:
            if event["action"] in {"draft_saved", "finalized"}:
                provenance_by_batch[event["batch_id"]] = event["actor_type"]
        provenance_counts = Counter(provenance_by_batch.values())

        quality_counts = Counter()
        issues: list[dict[str, object]] = []
        matrix_by_site: dict[str, dict[str, dict[str, object]]] = {
            site["site_id"]: {} for site in sites
        }
        longitudinal_by_site: dict[str, list[dict[str, object]]] = {
            site["site_id"]: [] for site in sites
        }
        for batch in batches:
            completion_status = str(batch.get("completion_status") or "not_started")
            review_status_counts[completion_status] += 1
            reasons: list[str] = []
            severity = "follow_up"
            unresolved_in_batch = int(batch.get("stored_photo_count") or 0) - int(
                batch.get("reviewed_count") or 0
            )
            if batch["status"] == "failed":
                reasons.append("Pipeline run failed")
                quality_counts["failed_batches"] += 1
                severity = "blocking"
            if unresolved_in_batch:
                reasons.append(f"{unresolved_in_batch} photo roles unresolved")
                quality_counts["batches_with_unresolved_photos"] += 1
                severity = "blocking"
            if completion_status == "not_started":
                reasons.append("Observation review not started")
                quality_counts["reviews_not_started"] += 1
                if severity != "blocking":
                    severity = "review"
            elif completion_status == "partially_reviewed":
                reasons.append("Observation is partially reviewed")
                quality_counts["partial_reviews"] += 1
            if batch.get("estimated_total_vegetation_cover_pct") is None:
                reasons.append("Vegetation cover is missing")
                quality_counts["missing_vegetation_cover"] += 1
            if not str(batch.get("dominant_species") or "").strip():
                reasons.append("Dominant taxon is missing")
                quality_counts["missing_dominant_taxon"] += 1
            integrity_issue_count = int(batch.get("integrity_issue_count") or 0)
            if integrity_issue_count:
                reasons.append(f"{integrity_issue_count} batch integrity issue(s)")
                quality_counts["integrity_attention"] += 1
                if severity != "blocking":
                    severity = "review"
            verification_status = str(batch.get("verification_status") or "")
            verification_stale = bool(batch.get("review_updated_at")) and (
                batch.get("verification_review_updated_at")
                != batch.get("review_updated_at")
                or batch.get("verification_version") != OBSERVATION_VERIFIER_VERSION
            )
            if completion_status != "not_started" and (
                not verification_status or verification_stale
            ):
                reasons.append("Automated verification is pending")
                quality_counts["verification_pending"] += 1
                if severity != "blocking":
                    severity = "review"
            elif int(batch.get("verification_blocking_count") or 0):
                count = int(batch.get("verification_blocking_count") or 0)
                reasons.append(f"{count} blocking verification issue(s)")
                quality_counts["verification_blocked"] += 1
                severity = "blocking"
            elif int(batch.get("verification_warning_count") or 0):
                count = int(batch.get("verification_warning_count") or 0)
                reasons.append(f"{count} automated verification warning(s)")
                quality_counts["verification_warnings"] += 1
                if severity != "blocking":
                    severity = "review"

            missing_field_count = sum(
                (
                    batch.get("estimated_total_vegetation_cover_pct") is None,
                    not str(batch.get("dominant_species") or "").strip(),
                )
            )
            cell = {
                "date": batch["observation_date"],
                "batch_id": batch["batch_id"],
                "pipeline_status": batch["status"],
                "completion_status": completion_status,
                "photo_count": int(batch.get("stored_photo_count") or 0),
                "verified_photo_count": int(batch.get("reviewed_count") or 0),
                "missing_field_count": missing_field_count,
                "verification_status": verification_status or "not_run",
            }
            matrix_by_site.setdefault(str(batch["site_id"]), {})[
                str(batch["observation_date"])
            ] = cell
            longitudinal_by_site.setdefault(str(batch["site_id"]), []).append(
                {
                    "date": batch["observation_date"],
                    "batch_id": batch["batch_id"],
                    "vegetation_cover_pct": batch.get(
                        "estimated_total_vegetation_cover_pct"
                    ),
                    "bare_ground_pct": batch.get("bare_ground_pct"),
                    "gravel_rock_pct": batch.get("gravel_rock_pct"),
                    "litter_mulch_woodchip_pct": batch.get(
                        "litter_mulch_woodchip_pct"
                    ),
                    "standing_dead_pct": batch.get("standing_dead_pct"),
                    "completion_status": completion_status,
                }
            )
            if reasons:
                issues.append(
                    {
                        "batch_id": batch["batch_id"],
                        "site_id": str(batch["site_id"]),
                        "site_name": site_names.get(str(batch["site_id"]), ""),
                        "observation_date": batch["observation_date"],
                        "severity": severity,
                        "reasons": reasons,
                    }
                )

        severity_order = {"blocking": 0, "review": 1, "follow_up": 2}
        issues.sort(
            key=lambda issue: (
                severity_order[str(issue["severity"])],
                -date.fromisoformat(str(issue["observation_date"])).toordinal(),
                int(str(issue["site_id"])),
            )
        )
        observation_dates = sorted(
            {str(batch["observation_date"]) for batch in batches}
        )
        matrix_rows = []
        longitudinal_sites = []
        for site in sites:
            site_id = site["site_id"]
            cells = matrix_by_site.get(site_id, {})
            matrix_rows.append(
                {
                    **site,
                    "observation_count": len(cells),
                    "cells": [cells.get(value) for value in observation_dates],
                }
            )
            longitudinal_sites.append(
                {
                    **site,
                    "points": longitudinal_by_site.get(site_id, []),
                }
            )

        latest_batch_created_at = max(
            (str(batch["created_at"]) for batch in batches), default=""
        )
        last_published_at = max(
            (str(batch.get("published_at") or "") for batch in batches), default=""
        )
        return {
            "generated_at": utc_now(),
            "refresh_interval_seconds": 15,
            "counts": {
                "needs_review": status_counts.get("needs_review", 0),
                "observation_review": status_counts.get("observation_review", 0),
                "ready": status_counts.get("ready", 0),
                "published": status_counts.get("published", 0),
                "failed": status_counts.get("failed", 0),
                "unresolved_photos": unresolved,
            },
            "summary": {
                "batch_count": len(batches),
                "observation_count": sum(review_status_counts.values()),
                "photo_count": sum(
                    int(batch.get("stored_photo_count") or 0) for batch in batches
                ),
                "verified_photo_count": sum(
                    int(batch.get("reviewed_count") or 0) for batch in batches
                ),
                "site_count": len(sites),
                "latest_batch_created_at": latest_batch_created_at,
                "last_published_at": last_published_at,
                "latest_sync": dict(latest_sync) if latest_sync is not None else None,
            },
            "review_status_counts": dict(review_status_counts),
            "review_provenance_counts": dict(provenance_counts),
            "machine_review_queue_counts": {
                status: machine_review_queue_counts.get(status, 0)
                for status in ("queued", "running", "succeeded", "failed", "skipped")
            },
            "quality": {
                **quality_counts,
                "failed_sync_attempts": failed_syncs,
                "issue_batch_count": len(issues),
            },
            "issues": issues,
            "site_matrix": {
                "dates": observation_dates,
                "rows": matrix_rows,
            },
            "longitudinal": {"sites": longitudinal_sites},
            "batches": sorted(
                batches,
                key=lambda batch: str(batch["created_at"]),
                reverse=True,
            ),
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

    def google_sheets_configuration(self) -> dict[str, object]:
        if self.sheets_publisher is None:
            return {
                "available": False,
                "destination": "google_sheets",
                "spreadsheet_url": "",
                "reason": "No Google Sheets destination is configured.",
            }
        return self.sheets_publisher.configuration()

    def published_data(self) -> dict[str, object]:
        _, observations = self.read_canonical_dataset("observations.csv")
        _, manifest = self.read_canonical_dataset("photo_manifest.csv")
        store = self.canonical_store_status()
        observations.sort(
            key=lambda row: (row.get("observation_date", ""), row.get("site_id", "")),
            reverse=True,
        )
        with self._connection() as connection:
            run_rows = connection.execute(
                "SELECT * FROM publication_runs ORDER BY started_at DESC"
            ).fetchall()
            published_at = connection.execute(
                "SELECT MAX(published_at) AS value FROM batches"
            ).fetchone()["value"]
            runs: list[dict[str, object]] = []
            for row in run_rows:
                run = dict(row)
                for field in ("output_row_counts_json", "output_sha256_json"):
                    try:
                        run[field.removesuffix("_json")] = json.loads(str(run.pop(field)))
                    except json.JSONDecodeError:
                        run[field.removesuffix("_json")] = {}
                latest_sync = connection.execute(
                    """
                    SELECT * FROM destination_sync_attempts
                    WHERE run_id = ? ORDER BY started_at DESC LIMIT 1
                    """,
                    (run["run_id"],),
                ).fetchone()
                if latest_sync is None:
                    run["latest_sync"] = None
                else:
                    sync = dict(latest_sync)
                    try:
                        sync["result"] = json.loads(str(sync.pop("result_json")))
                    except json.JSONDecodeError:
                        sync["result"] = {}
                    run["latest_sync"] = sync
                runs.append(run)
        return {
            "summary": {
                "observation_count": len(observations),
                "photo_count": len(manifest),
                "publication_run_count": len(runs),
                "last_published_at": published_at or "",
            },
            "observations": observations,
            "publication_runs": runs,
            "google_sheets": self.google_sheets_configuration(),
            "exports": [
                *[dataset["filename"] for dataset in store["datasets"]],
                *sorted(DERIVED_EXPORTS),
            ],
            "canonical_store": {
                "schema_version": store["schema_version"],
                "dataset_count": len(store["datasets"]),
            },
        }

    def sync_google_sheets(self, run_id: str | None = None) -> dict[str, object]:
        config = self.google_sheets_configuration()
        if not config["available"] or self.sheets_publisher is None:
            raise IngestionError(str(config["reason"]))
        with self._connection() as connection:
            if run_id:
                run = connection.execute(
                    "SELECT * FROM publication_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            else:
                run = connection.execute(
                    """
                    SELECT * FROM publication_runs
                    WHERE status IN ('succeeded', 'legacy')
                    ORDER BY completed_at DESC LIMIT 1
                    """
                ).fetchone()
            if run is None or run["status"] not in {"succeeded", "legacy"}:
                raise IngestionError("Choose a successful publication run before syncing.")
            sync_attempt_id = f"sync_{uuid.uuid4().hex}"
            started_at = utc_now()
            connection.execute(
                """
                INSERT INTO destination_sync_attempts (
                    sync_attempt_id, run_id, destination, status, started_at
                ) VALUES (?, ?, 'google_sheets', 'running', ?)
                """,
                (sync_attempt_id, run["run_id"], started_at),
            )
        try:
            self.export_public_data()
            result = self.sheets_publisher.sync(self.paths.public_dir)
        except (GoogleSheetsSyncError, IngestionError) as error:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE destination_sync_attempts
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE sync_attempt_id = ?
                    """,
                    (str(error), utc_now(), sync_attempt_id),
                )
            raise IngestionError(str(error)) from error
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE destination_sync_attempts
                SET status = 'succeeded', result_json = ?, completed_at = ?
                WHERE sync_attempt_id = ?
                """,
                (
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                    sync_attempt_id,
                ),
            )
        return {
            "sync_attempt_id": sync_attempt_id,
            "run_id": run["run_id"],
            "status": "succeeded",
            **result,
        }

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

    @staticmethod
    def _split_taxon_labels(value: object) -> list[str]:
        return [
            label.strip()
            for label in re.split(r"[;,\n]+", str(value or ""))
            if label.strip()
        ]

    def site_taxon_history(
        self,
        site_id: str,
        *,
        before_date: str | None = None,
        exclude_batch_id: str | None = None,
    ) -> dict[str, object]:
        """Summarize structured taxon labels recorded before a site visit."""

        with self._connection() as connection:
            local_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT b.batch_id, b.observation_date, review.*
                    FROM batches b
                    JOIN observation_reviews review ON review.batch_id = b.batch_id
                    WHERE b.site_id = ?
                      AND (? IS NULL OR b.observation_date < ?)
                      AND (? IS NULL OR b.batch_id != ?)
                    ORDER BY b.observation_date, b.created_at
                    """,
                    (
                        str(site_id),
                        before_date,
                        before_date,
                        exclude_batch_id,
                        exclude_batch_id,
                    ),
                ).fetchall()
            ]
        local_dates = {str(row["observation_date"]) for row in local_rows}
        _, published_rows = self.read_canonical_dataset("observations.csv")
        source_rows = [
            row
            for row in published_rows
            if str(row.get("site_id") or "") == str(site_id)
            and (not before_date or str(row.get("observation_date") or "") < before_date)
            and str(row.get("observation_date") or "") not in local_dates
        ]
        source_rows.extend(local_rows)

        history: dict[str, dict[str, object]] = {}
        observation_dates: set[str] = set()

        def record(label: object, observed_on: str, role: str) -> None:
            cleaned = " ".join(str(label or "").split())
            if not cleaned:
                return
            key = cleaned.casefold()
            item = history.setdefault(
                key,
                {
                    "label": cleaned,
                    "first_seen": observed_on,
                    "last_seen": observed_on,
                    "observation_dates": set(),
                    "roles": set(),
                },
            )
            item["first_seen"] = min(str(item["first_seen"]), observed_on)
            item["last_seen"] = max(str(item["last_seen"]), observed_on)
            item["observation_dates"].add(observed_on)  # type: ignore[union-attr]
            item["roles"].add(role)  # type: ignore[union-attr]

        for row in source_rows:
            observed_on = str(row.get("observation_date") or "")
            if not observed_on:
                continue
            observation_dates.add(observed_on)
            for role in ("dominant", "secondary", "tertiary"):
                record(row.get(f"{role}_species"), observed_on, role)
            for label in self._split_taxon_labels(row.get("new_taxon_labels")):
                record(label, observed_on, "new_taxon")

        taxa = []
        for item in history.values():
            taxa.append(
                {
                    "label": item["label"],
                    "first_seen": item["first_seen"],
                    "last_seen": item["last_seen"],
                    "observation_count": len(item["observation_dates"]),
                    "roles": sorted(item["roles"]),
                }
            )
        taxa.sort(key=lambda item: str(item["label"]).casefold())
        return {
            "site_id": str(site_id),
            "before_date": before_date or "",
            "observation_count": len(observation_dates),
            "taxon_count": len(taxa),
            "taxa": taxa,
        }

    @staticmethod
    def _latest_observation_verification(
        connection: sqlite3.Connection, batch_id: str
    ) -> dict[str, object] | None:
        run = connection.execute(
            """
            SELECT * FROM observation_verification_runs
            WHERE batch_id = ?
            ORDER BY checked_at DESC, verification_run_id DESC
            LIMIT 1
            """,
            (batch_id,),
        ).fetchone()
        if run is None:
            return None
        issues = connection.execute(
            """
            SELECT * FROM observation_verification_issues
            WHERE verification_run_id = ?
            ORDER BY
                CASE severity WHEN 'blocking' THEN 0 ELSE 1 END,
                rule_id,
                verification_issue_id
            """,
            (run["verification_run_id"],),
        ).fetchall()
        return {**dict(run), "issues": [dict(issue) for issue in issues]}

    def verify_observation_review(self, batch_id: str) -> dict[str, object]:
        """Persist an append-only deterministic verification run for one review."""

        batch = self.get_batch(batch_id)
        review = batch.get("observation_review")
        if not isinstance(review, dict):
            raise IngestionError("Start the observation review before running verification.")
        review_updated_at = str(review.get("updated_at") or "")
        if not review_updated_at:
            raise IngestionError("The observation review has no audit timestamp.")

        result = verify_observation_values(
            review,
            photos=batch.get("photos", []),  # type: ignore[arg-type]
            integrity=batch.get("integrity")
            if isinstance(batch.get("integrity"), dict)
            else None,
            site_taxon_history=batch.get("site_taxon_history")
            if isinstance(batch.get("site_taxon_history"), dict)
            else None,
        )
        verification_run_id = f"observation_verification_{uuid.uuid4().hex}"
        checked_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO observation_verification_runs (
                    verification_run_id, batch_id, review_updated_at,
                    verifier_id, verifier_version, status, issue_count,
                    blocking_count, warning_count, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification_run_id,
                    batch_id,
                    review_updated_at,
                    result["verifier_id"],
                    result["verifier_version"],
                    result["status"],
                    result["issue_count"],
                    result["blocking_count"],
                    result["warning_count"],
                    checked_at,
                ),
            )
            for issue in result["issues"]:  # type: ignore[union-attr]
                connection.execute(
                    """
                    INSERT INTO observation_verification_issues (
                        verification_issue_id, verification_run_id, rule_id,
                        severity, field_name, message, suggested_fix
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"observation_verification_issue_{uuid.uuid4().hex}",
                        verification_run_id,
                        issue["rule_id"],
                        issue["severity"],
                        issue["field_name"],
                        issue["message"],
                        issue["suggested_fix"],
                    ),
                )
            persisted = self._latest_observation_verification(connection, batch_id)
        if persisted is None:
            raise IngestionError("Observation verification could not be saved.")
        return persisted

    def _backfill_observation_verifications(self) -> None:
        """Verify existing reviews once per rule version or review revision."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT review.batch_id, review.updated_at,
                       latest.review_updated_at AS verified_review_updated_at,
                       latest.verifier_version
                FROM observation_reviews review
                LEFT JOIN observation_verification_runs latest
                  ON latest.verification_run_id = (
                      SELECT run.verification_run_id
                      FROM observation_verification_runs run
                      WHERE run.batch_id = review.batch_id
                      ORDER BY run.checked_at DESC, run.verification_run_id DESC
                      LIMIT 1
                  )
                ORDER BY review.updated_at
                """
            ).fetchall()
        pending = [
            str(row["batch_id"])
            for row in rows
            if row["verified_review_updated_at"] != row["updated_at"]
            or row["verifier_version"] != OBSERVATION_VERIFIER_VERSION
        ]
        for batch_id in pending:
            self.verify_observation_review(batch_id)

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
            observation_review = connection.execute(
                "SELECT * FROM observation_reviews WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            observation_review_events = connection.execute(
                """
                SELECT * FROM observation_review_events
                WHERE batch_id = ? ORDER BY created_at, review_event_id
                """,
                (batch_id,),
            ).fetchall()
            latest_machine_review_job = connection.execute(
                """
                SELECT * FROM machine_review_jobs
                WHERE batch_id = ?
                ORDER BY requested_at DESC, job_id DESC
                LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            latest_observation_verification = self._latest_observation_verification(
                connection, batch_id
            )
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
        result["observation_review"] = (
            dict(observation_review) if observation_review is not None else None
        )
        if result["observation_review"] is None and result["status"] == "published":
            _, observation_rows = self.read_canonical_dataset("observations.csv")
            published_observation = next(
                (
                    row
                    for row in observation_rows
                    if row.get("observation_id")
                    == observation_id(str(result["site_id"]), str(result["observation_date"]))
                ),
                None,
            )
            if published_observation is not None:
                completion_status = published_observation.get("review_status", "")
                if completion_status not in {"partially_reviewed", "reviewed"}:
                    completion_status = "partially_reviewed"
                legacy_review = {
                    field: published_observation.get(field, "")
                    for field in OBSERVATION_REVIEW_FIELDS
                }
                legacy_review.update(
                    {
                        "completion_status": completion_status,
                        "completeness_notes": "",
                        "is_final": 1,
                        "reviewed_by": "legacy_publication",
                        "updated_at": result.get("published_at", ""),
                    }
                )
                result["observation_review"] = legacy_review
        result["observation_review_events"] = [
            dict(event) for event in observation_review_events
        ]
        result["latest_machine_review_job"] = (
            dict(latest_machine_review_job)
            if latest_machine_review_job is not None
            else None
        )
        result["latest_observation_verification"] = latest_observation_verification
        latest_machine_review = next(
            (
                event
                for event in reversed(observation_review_events)
                if event["actor_type"] == "model" and event["action"] == "suggested"
            ),
            None,
        )
        if latest_machine_review is None:
            result["latest_machine_review"] = None
        else:
            try:
                machine_payload = json.loads(str(latest_machine_review["payload_json"]))
            except json.JSONDecodeError:
                machine_payload = None
            result["latest_machine_review"] = (
                {
                    **machine_payload,
                    "created_at": latest_machine_review["created_at"],
                }
                if isinstance(machine_payload, dict)
                else None
            )
        result["site_taxon_history"] = self.site_taxon_history(
            str(result["site_id"]),
            before_date=str(result["observation_date"]),
            exclude_batch_id=batch_id,
        )
        return result

    def machine_review_configuration(self) -> dict[str, object]:
        if self.machine_reviewer is None:
            return {
                "available": False,
                "provider": "",
                "model": "",
                "prompt_version": "",
                "reason": "No machine-review provider is configured.",
            }
        return self.machine_reviewer.configuration()

    def machine_review_queue(self, limit: int = 100) -> dict[str, object]:
        """Return durable machine-review work and aggregate queue state."""

        with self._connection() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM machine_review_jobs GROUP BY status"
                )
            }
            rows = connection.execute(
                """
                SELECT
                    job.*,
                    batch.site_id,
                    batch.observation_date,
                    review.completion_status
                FROM machine_review_jobs job
                JOIN batches batch ON batch.batch_id = job.batch_id
                LEFT JOIN observation_reviews review ON review.batch_id = job.batch_id
                ORDER BY job.requested_at DESC, job.job_id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return {
            "counts": {
                status: counts.get(status, 0)
                for status in ("queued", "running", "succeeded", "failed", "skipped")
            },
            "jobs": [dict(row) for row in rows],
        }

    def enqueue_machine_reviews(
        self,
        batch_ids: Iterable[str],
        *,
        requested_by: str = "project_owner",
        force: bool = False,
    ) -> dict[str, object]:
        """Queue selected, photo-complete observations without spending on duplicates."""

        if not requested_by.strip():
            raise IngestionError("Machine-review requester metadata is invalid.")
        configuration = self.machine_review_configuration()
        if not configuration.get("available"):
            raise IngestionError(
                str(configuration.get("reason") or "Machine review is not configured.")
            )
        unique_batch_ids = list(dict.fromkeys(str(value).strip() for value in batch_ids))
        unique_batch_ids = [value for value in unique_batch_ids if value]
        if not unique_batch_ids:
            raise IngestionError("Select at least one observation for machine review.")
        if len(unique_batch_ids) > 100:
            raise IngestionError("Queue at most 100 observations at a time.")

        created: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        with self._connection() as connection:
            for batch_id in unique_batch_ids:
                batch = connection.execute(
                    """
                    SELECT
                        batch.batch_id,
                        COUNT(photo.photo_id) AS photo_count,
                        SUM(CASE WHEN photo.review_status = 'verified' THEN 1 ELSE 0 END)
                            AS verified_count,
                        review.completion_status,
                        EXISTS (
                            SELECT 1 FROM observation_review_events event
                            WHERE event.batch_id = batch.batch_id
                              AND event.actor_type = 'model'
                              AND event.action = 'suggested'
                        ) AS has_machine_draft,
                        EXISTS (
                            SELECT 1 FROM machine_review_jobs active_job
                            WHERE active_job.batch_id = batch.batch_id
                              AND active_job.status IN ('queued', 'running')
                        ) AS has_active_job
                    FROM batches batch
                    LEFT JOIN photos photo ON photo.batch_id = batch.batch_id
                    LEFT JOIN observation_reviews review ON review.batch_id = batch.batch_id
                    WHERE batch.batch_id = ?
                    GROUP BY batch.batch_id
                    """,
                    (batch_id,),
                ).fetchone()
                reason = ""
                if batch is None:
                    reason = "Unknown observation."
                elif int(batch["photo_count"] or 0) == 0:
                    reason = "No photos are available."
                elif int(batch["verified_count"] or 0) != int(batch["photo_count"] or 0):
                    reason = "Photo-role review is incomplete."
                elif batch["completion_status"] == "reviewed":
                    reason = "Human review is already complete."
                elif batch["has_active_job"]:
                    reason = "Already queued or running."
                elif batch["has_machine_draft"] and not force:
                    reason = "A machine draft already exists."
                if reason:
                    skipped.append({"batch_id": batch_id, "reason": reason})
                    continue

                job_id = f"machine_review_job_{uuid.uuid4().hex}"
                requested_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                connection.execute(
                    """
                    INSERT INTO machine_review_jobs (
                        job_id, batch_id, status, requested_by, requested_at
                    ) VALUES (?, ?, 'queued', ?, ?)
                    """,
                    (job_id, batch_id, requested_by.strip(), requested_at),
                )
                created.append(
                    {
                        "job_id": job_id,
                        "batch_id": batch_id,
                        "status": "queued",
                        "requested_at": requested_at,
                    }
                )
        return {
            "created_count": len(created),
            "skipped_count": len(skipped),
            "created": created,
            "skipped": skipped,
        }

    def _claim_next_machine_review_job(self) -> dict[str, object] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM machine_review_jobs
                WHERE status = 'queued'
                ORDER BY requested_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            connection.execute(
                """
                UPDATE machine_review_jobs
                SET status = 'running', started_at = ?, completed_at = NULL,
                    attempt_count = attempt_count + 1, error_message = ''
                WHERE job_id = ? AND status = 'queued'
                """,
                (started_at, row["job_id"]),
            )
            connection.commit()
            return {**dict(row), "status": "running", "started_at": started_at}
        finally:
            connection.close()

    def _finish_machine_review_job(
        self, job_id: str, status: str, error_message: str = ""
    ) -> None:
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("Unsupported machine-review terminal status.")
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE machine_review_jobs
                SET status = ?, completed_at = ?, error_message = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    error_message.strip(),
                    job_id,
                ),
            )

    def process_machine_review_queue(self, limit: int | None = None) -> dict[str, object]:
        """Process queued jobs serially without holding SQLite during model requests."""

        if not self._machine_review_lock.acquire(blocking=False):
            return {"worker_started": False, "processed_count": 0, "results": []}
        processed: list[dict[str, str]] = []
        try:
            while limit is None or len(processed) < limit:
                job = self._claim_next_machine_review_job()
                if job is None:
                    break
                job_id = str(job["job_id"])
                batch_id = str(job["batch_id"])
                with self._connection() as connection:
                    review = connection.execute(
                        "SELECT completion_status FROM observation_reviews WHERE batch_id = ?",
                        (batch_id,),
                    ).fetchone()
                if review is not None and review["completion_status"] == "reviewed":
                    message = "Human review completed before machine processing."
                    self._finish_machine_review_job(job_id, "skipped", message)
                    processed.append(
                        {"job_id": job_id, "batch_id": batch_id, "status": "skipped"}
                    )
                    continue
                try:
                    self.generate_machine_observation_review(batch_id)
                except IngestionError as error:
                    self._finish_machine_review_job(job_id, "failed", str(error))
                    processed.append(
                        {"job_id": job_id, "batch_id": batch_id, "status": "failed"}
                    )
                else:
                    self._finish_machine_review_job(job_id, "succeeded")
                    processed.append(
                        {"job_id": job_id, "batch_id": batch_id, "status": "succeeded"}
                    )
        finally:
            self._machine_review_lock.release()
        return {
            "worker_started": True,
            "processed_count": len(processed),
            "results": processed,
        }

    def retry_failed_machine_reviews(self) -> dict[str, int]:
        """Return failed, still-incomplete observations to the queue."""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE machine_review_jobs
                SET status = 'queued', started_at = NULL, completed_at = NULL,
                    error_message = ''
                WHERE status = 'failed'
                  AND job_id = (
                      SELECT retry_job.job_id FROM machine_review_jobs retry_job
                      WHERE retry_job.batch_id = machine_review_jobs.batch_id
                        AND retry_job.status = 'failed'
                      ORDER BY retry_job.requested_at DESC, retry_job.job_id DESC
                      LIMIT 1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM machine_review_jobs active_job
                      WHERE active_job.batch_id = machine_review_jobs.batch_id
                        AND active_job.status IN ('queued', 'running')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM observation_reviews review
                      WHERE review.batch_id = machine_review_jobs.batch_id
                        AND review.completion_status = 'reviewed'
                  )
                """
            )
        return {"queued_count": cursor.rowcount}

    def generate_machine_observation_review(self, batch_id: str) -> dict[str, object]:
        if self.machine_reviewer is None:
            raise IngestionError("No machine-review provider is configured.")
        with self._connection() as connection:
            batch = connection.execute(
                "SELECT site_id, observation_date FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise IngestionError(f"Unknown batch '{batch_id}'.")
            photos = connection.execute(
                """
                SELECT source_filename, preview_filename, reviewed_type, review_status
                FROM photos WHERE batch_id = ? ORDER BY source_filename, photo_id
                """,
                (batch_id,),
            ).fetchall()
        if not photos or any(photo["review_status"] != "verified" for photo in photos):
            raise IngestionError("Finish photo-role review before requesting machine review.")

        review_photos: list[dict[str, str]] = []
        for photo in photos:
            preview_filename = str(photo["preview_filename"] or "")
            preview_path = self.paths.batch_dir / batch_id / "previews" / preview_filename
            if not preview_filename or not preview_path.exists():
                raise IngestionError(
                    f"Preview file is missing for '{photo['source_filename']}'."
                )
            review_photos.append(
                {
                    "filename": str(photo["source_filename"]),
                    "photo_type": str(photo["reviewed_type"]),
                    "path": str(preview_path),
                }
            )

        try:
            result = self.machine_reviewer.review(
                site_id=str(batch["site_id"]),
                observation_date=str(batch["observation_date"]),
                photos=review_photos,
            )
        except MachineReviewError as error:
            raise IngestionError(str(error)) from error

        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO observation_review_events (
                    review_event_id, batch_id, actor_type, actor_id,
                    actor_version, action, payload_json, created_at
                ) VALUES (?, ?, 'model', ?, ?, 'suggested', ?, ?)
                """,
                (
                    f"observation_review_{uuid.uuid4().hex}",
                    batch_id,
                    str(result.get("provider") or "machine_review"),
                    str(result.get("model") or "") or None,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    created_at,
                ),
            )
        return {**result, "created_at": created_at}

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

    @staticmethod
    def _normalize_observation_review(values: dict[str, object]) -> dict[str, object]:
        normalized: dict[str, object] = {}
        for field in OBSERVATION_PERCENT_FIELDS:
            value = values.get(field)
            if value is None or str(value).strip() == "":
                normalized[field] = None
                continue
            try:
                integer = int(str(value))
            except ValueError as error:
                raise IngestionError(f"{field} must be a whole number from 0 to 100.") from error
            if not 0 <= integer <= 100:
                raise IngestionError(f"{field} must be between 0 and 100.")
            normalized[field] = integer

        for field in OBSERVATION_BOOLEAN_FIELDS:
            value = values.get(field)
            if value is None or str(value).strip() == "":
                normalized[field] = ""
                continue
            if isinstance(value, bool):
                normalized[field] = "true" if value else "false"
                continue
            boolean = str(value).strip().casefold()
            if boolean not in {"true", "false"}:
                raise IngestionError(f"{field} must be true, false, or blank.")
            normalized[field] = boolean

        for field in OBSERVATION_TEXT_FIELDS:
            normalized[field] = str(values.get(field) or "").strip()

        for field, allowed in OBSERVATION_CATEGORY_FIELDS.items():
            value = str(values.get(field) or "").strip()
            if value and value not in allowed:
                raise IngestionError(f"{field} contains an unsupported value.")
            normalized[field] = value

        new_taxa: list[str] = []
        seen_taxa: set[str] = set()
        for label in IngestionService._split_taxon_labels(normalized["new_taxon_labels"]):
            key = label.casefold()
            if key in seen_taxa:
                continue
            seen_taxa.add(key)
            new_taxa.append(label)
        normalized["new_taxon_labels"] = "; ".join(new_taxa)
        if new_taxa and normalized["new_species_detected"] == "false":
            raise IngestionError(
                "New taxon labels conflict with 'No' for new taxon recorded."
            )

        if not normalized["completion_status"]:
            normalized["completion_status"] = "partially_reviewed"
        for prefix in ("dominant", "secondary", "tertiary"):
            if not normalized[f"{prefix}_species"]:
                for suffix in (
                    "taxon_rank",
                    "species_confidence",
                    "flowering",
                    "seed_set",
                ):
                    normalized[f"{prefix}_{suffix}"] = ""
        return normalized

    def save_observation_review(
        self,
        batch_id: str,
        values: dict[str, object],
        *,
        finalize: bool = False,
        actor_type: str = "human",
        actor_id: str = "project_owner",
        actor_version: str | None = None,
    ) -> dict[str, object]:
        if actor_type not in REVIEW_ACTOR_TYPES or not actor_id.strip():
            raise IngestionError("Review actor metadata is invalid.")
        normalized = self._normalize_observation_review(values)
        if finalize:
            substantive_fields = (
                *OBSERVATION_PERCENT_FIELDS,
                "dominant_species",
                "secondary_species",
                "tertiary_species",
                "successional_stage",
                "notes",
            )
            if not any(normalized[field] not in {None, ""} for field in substantive_fields):
                raise IngestionError(
                    "Enter at least one observation or note before completing review."
                )
            if (
                normalized["completion_status"] == "partially_reviewed"
                and not normalized["completeness_notes"]
            ):
                raise IngestionError(
                    "Describe what remains unavailable before completing a partial review."
                )

        with self._connection() as connection:
            batch = connection.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise IngestionError(f"Unknown batch '{batch_id}'.")
            unresolved = connection.execute(
                """
                SELECT COUNT(*) AS count FROM photos
                WHERE batch_id = ? AND review_status != 'verified'
                """,
                (batch_id,),
            ).fetchone()["count"]
            if unresolved:
                raise IngestionError("Finish photo-role review before reviewing the observation.")
            existing = connection.execute(
                "SELECT is_final FROM observation_reviews WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            columns = ", ".join(OBSERVATION_REVIEW_FIELDS)
            placeholders = ", ".join("?" for _ in OBSERVATION_REVIEW_FIELDS)
            updates = ", ".join(
                f"{field} = excluded.{field}" for field in OBSERVATION_REVIEW_FIELDS
            )
            connection.execute(
                f"""
                INSERT INTO observation_reviews (
                    batch_id, {columns}, is_final, reviewed_by, updated_at
                ) VALUES (?, {placeholders}, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    {updates},
                    is_final = excluded.is_final,
                    reviewed_by = excluded.reviewed_by,
                    updated_at = excluded.updated_at
                """,
                (
                    batch_id,
                    *(normalized[field] for field in OBSERVATION_REVIEW_FIELDS),
                    int(finalize),
                    actor_id.strip(),
                    utc_now(),
                ),
            )
            if finalize:
                action = "corrected" if existing and existing["is_final"] else "finalized"
            else:
                action = "draft_saved"
            connection.execute(
                """
                INSERT INTO observation_review_events (
                    review_event_id, batch_id, actor_type, actor_id,
                    actor_version, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"observation_review_{uuid.uuid4().hex}",
                    batch_id,
                    actor_type,
                    actor_id.strip(),
                    actor_version,
                    action,
                    json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                ),
            )
            connection.execute(
                "UPDATE batches SET status = ? WHERE batch_id = ?",
                ("ready" if finalize else "observation_review", batch_id),
            )
        self.verify_observation_review(batch_id)
        return self.get_batch(batch_id)

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
        observation_review = connection.execute(
            "SELECT is_final FROM observation_reviews WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if not counts["total"] or counts["total"] != counts["reviewed"]:
            status = "needs_review"
        elif observation_review is not None and observation_review["is_final"]:
            status = "ready"
        else:
            status = "observation_review"
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
                raise IngestionError(
                    "Photo roles and the observation review must be complete before publication."
                )
            review = batch.get("observation_review")
            verification = batch.get("latest_observation_verification")
            if not isinstance(review, dict):
                raise IngestionError("Complete the observation review before publication.")
            if (
                not isinstance(verification, dict)
                or verification.get("review_updated_at") != review.get("updated_at")
                or verification.get("verifier_version")
                != OBSERVATION_VERIFIER_VERSION
            ):
                verification = self.verify_observation_review(batch_id)
            if int(verification.get("blocking_count") or 0):
                raise IngestionError(
                    "Resolve blocking automated verification issues before publishing."
                )

            run_id = f"publication_{uuid.uuid4().hex}"
            obs_id = observation_id(str(batch["site_id"]), str(batch["observation_date"]))
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO publication_runs (
                        run_id, batch_id, observation_id, status,
                        validation_status, started_at
                    ) VALUES (?, ?, ?, 'running', 'pending', ?)
                    """,
                    (run_id, batch_id, obs_id, utc_now()),
                )

            copied_paths: list[Path] = []
            new_manifest_rows: list[dict[str, object]] = []
            try:
                manifest_fields, manifest_rows = self.read_canonical_dataset(
                    "photo_manifest.csv"
                )
                observation_fields, observation_rows = self.read_canonical_dataset(
                    "observations.csv"
                )
                summary_fields, summary_rows = self.read_canonical_dataset(
                    "photo_batch_summary.csv"
                )

                known_photo_ids = {row["photo_id"] for row in manifest_rows}
                site_id = str(batch["site_id"])
                observed_on = str(batch["observation_date"])
                for photo in batch["photos"]:
                    if photo["photo_id"] in known_photo_ids:
                        continue
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
                    batch["observation_review"],
                )
                summary_rows = self._rebuild_summary(manifest_rows, summary_rows)

                with tempfile.TemporaryDirectory(
                    prefix="ingestion-publish-", dir=self.paths.state_dir
                ) as temporary:
                    candidate_dir = Path(temporary)
                    with self._connection() as connection:
                        filenames = [
                            str(row["filename"])
                            for row in connection.execute(
                                "SELECT filename FROM canonical_datasets ORDER BY filename"
                            ).fetchall()
                        ]
                        for filename in filenames:
                            fields, rows = self._read_canonical_dataset(
                                connection, filename
                            )
                            line_terminator = connection.execute(
                                """
                                SELECT line_terminator FROM canonical_datasets
                                WHERE filename = ?
                                """,
                                (filename,),
                            ).fetchone()["line_terminator"]
                            write_csv(
                                candidate_dir / filename,
                                fields,
                                rows,
                                line_terminator=str(line_terminator),
                            )
                    write_csv(candidate_dir / "photo_manifest.csv", manifest_fields, manifest_rows)
                    write_csv(
                        candidate_dir / "observations.csv", observation_fields, observation_rows
                    )
                    write_csv(
                        candidate_dir / "photo_batch_summary.csv", summary_fields, summary_rows
                    )
                    write_analysis_ready_export(candidate_dir)
                    issues = validate_public_photo_data(candidate_dir, self.paths.schema_dir)
                    if issues:
                        details = "; ".join(issue.format() for issue in issues[:8])
                        raise IngestionError(f"Publication failed validation: {details}")

                    output_row_counts: dict[str, int] = {}
                    output_sha256: dict[str, str] = {}
                    for path in sorted(candidate_dir.glob("*.csv")):
                        _, rows = read_csv(path)
                        output_row_counts[path.name] = len(rows)
                        output_sha256[path.name] = self._sha256(path)

                with self._connection() as connection:
                    self._replace_canonical_dataset(
                        connection,
                        "photo_manifest.csv",
                        manifest_fields,
                        manifest_rows,
                    )
                    self._replace_canonical_dataset(
                        connection,
                        "observations.csv",
                        observation_fields,
                        observation_rows,
                    )
                    self._replace_canonical_dataset(
                        connection,
                        "photo_batch_summary.csv",
                        summary_fields,
                        summary_rows,
                    )
                    connection.execute(
                        """
                        UPDATE batches
                        SET status = 'published', published_at = ?
                        WHERE batch_id = ?
                        """,
                        (utc_now(), batch_id),
                    )
                    connection.execute(
                        """
                        UPDATE publication_runs
                        SET status = 'succeeded', validation_status = 'passed',
                            photo_rows_written = ?, output_row_counts_json = ?,
                            output_sha256_json = ?, completed_at = ?
                        WHERE run_id = ?
                        """,
                        (
                            len(new_manifest_rows),
                            json.dumps(
                                output_row_counts,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                output_sha256,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            utc_now(),
                            run_id,
                        ),
                    )
            except Exception as error:
                for path in copied_paths:
                    path.unlink(missing_ok=True)
                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE publication_runs
                        SET status = 'failed', validation_status = 'failed',
                            error_message = ?, completed_at = ?
                        WHERE run_id = ?
                        """,
                        (str(error), utc_now(), run_id),
                    )
                raise
            self.export_public_data()
            if self.google_sheets_configuration()["available"]:
                try:
                    self.sync_google_sheets(run_id)
                except IngestionError:
                    # The durable local publication succeeded; the failed sink is retryable.
                    pass
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
        observation_review: object,
    ) -> None:
        existing = next((row for row in rows if row["observation_id"] == obs_id), None)
        row = existing if existing is not None else {field: "" for field in fields}
        review = observation_review if isinstance(observation_review, dict) else {}
        row.update(
            {
                "observation_id": obs_id,
                "site_id": site_id,
                "observation_date": observed_on,
                "record_origin": "ingestion_app",
                "review_status": str(review.get("completion_status") or "partially_reviewed"),
                "source_photo_count": str(photo_count),
            }
        )
        for field in OBSERVATION_REVIEW_FIELDS:
            if field in {"completion_status", "completeness_notes"} or field not in row:
                continue
            value = review.get(field)
            row[field] = "" if value is None else str(value)
        review_notes = str(review.get("notes") or "").strip()
        completeness_notes = str(review.get("completeness_notes") or "").strip()
        note_parts = [part for part in (review_notes, notes.strip()) if part]
        if completeness_notes:
            note_parts.append(f"Data completeness: {completeness_notes}")
        row["notes"] = " ".join(dict.fromkeys(note_parts)) or (
            "Observation reviewed through the ingestion service."
        )
        if existing is None:
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
