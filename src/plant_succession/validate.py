"""CSV validation helpers for the Plant Succession ML Project.

The validator intentionally uses only the Python standard library so the basic
data-quality check can run before notebook or analysis dependencies are installed.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ValidationIssue:
    table: str
    row: int | None
    column: str | None
    message: str

    def format(self) -> str:
        parts = [self.table]
        if self.row is not None:
            parts.append(f"row {self.row}")
        if self.column is not None:
            parts.append(self.column)
        return f"{' / '.join(parts)}: {self.message}"


SCHEMA_FILES = {
    "site_registry": "site_registry_schema.csv",
    "observations": "observations_schema.csv",
    "disturbance_events": "disturbance_events_schema.csv",
    "field_notes": "field_notes_schema.csv",
    "site_checks": "site_checks_schema.csv",
    "photo_manifest": "photo_manifest_schema.csv",
}

PERCENT_COLUMNS = {
    "estimated_total_vegetation_cover_pct",
    "bare_ground_pct",
    "gravel_rock_pct",
    "litter_mulch_woodchip_pct",
    "standing_dead_pct",
}

DATE_COLUMNS = {
    "site_registry": ["established_date"],
    "observations": ["observation_date"],
    "disturbance_events": ["disturbance_window_start", "disturbance_window_end"],
    "field_notes": ["note_date"],
    "site_checks": ["check_date"],
    "photo_manifest": ["observation_date"],
}

DATETIME_COLUMNS = {
    "observations": ["observation_datetime"],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def read_headers(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.reader(csv_file)
        return next(reader)


def required_columns(schema: list[dict[str, str]]) -> list[str]:
    return [
        row["field_name"]
        for row in schema
        if row.get("required", "").strip().lower() == "yes"
    ]


def schema_columns(schema: list[dict[str, str]]) -> set[str]:
    return {row["field_name"] for row in schema}


def allowed_values(schema: list[dict[str, str]]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for row in schema:
        values = row.get("allowed_values", "").strip()
        if values:
            allowed[row["field_name"]] = {value.strip() for value in values.split(";")}
    return allowed


def row_number(index: int) -> int:
    return index + 2


def is_blank(value: str | None) -> bool:
    return value is None or str(value).strip() == ""


def parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def validate_columns(
    table: str, headers: list[str], schema: list[dict[str, str]]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    defined_columns = schema_columns(schema)

    for column in required_columns(schema):
        if column not in headers:
            issues.append(ValidationIssue(table, None, column, "required column is missing"))

    for column in headers:
        if column not in defined_columns:
            issues.append(ValidationIssue(table, None, column, "column is not defined in schema"))

    return issues


def validate_required_values(
    table: str, rows: list[dict[str, str]], schema: list[dict[str, str]]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for column in required_columns(schema):
        for index, row in enumerate(rows):
            if column in row and is_blank(row[column]):
                issues.append(
                    ValidationIssue(table, row_number(index), column, "required value is blank")
                )
    return issues


def validate_allowed_values(
    table: str, rows: list[dict[str, str]], schema: list[dict[str, str]]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for column, values in allowed_values(schema).items():
        for index, row in enumerate(rows):
            value = row.get(column, "").strip()
            if value and value not in values:
                expected = ", ".join(sorted(values))
                issues.append(
                    ValidationIssue(
                        table,
                        row_number(index),
                        column,
                        f"'{value}' not in allowed values: {expected}",
                    )
                )
    return issues


def validate_dates(table: str, rows: list[dict[str, str]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for column in DATE_COLUMNS.get(table, []):
        for index, row in enumerate(rows):
            value = row.get(column, "").strip()
            if value and parse_datetime(value) is None:
                issues.append(
                    ValidationIssue(table, row_number(index), column, f"invalid date: {value}")
                )

    for column in DATETIME_COLUMNS.get(table, []):
        for index, row in enumerate(rows):
            value = row.get(column, "").strip()
            if value and parse_datetime(value) is None:
                issues.append(
                    ValidationIssue(table, row_number(index), column, f"invalid datetime: {value}")
                )

    return issues


def validate_percent_ranges(table: str, rows: list[dict[str, str]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for column in PERCENT_COLUMNS:
        for index, row in enumerate(rows):
            raw_value = row.get(column, "").strip()
            if not raw_value:
                continue
            try:
                value = int(raw_value)
            except ValueError:
                issues.append(
                    ValidationIssue(table, row_number(index), column, "must be an integer from 0 to 100")
                )
                continue
            if value < 0 or value > 100:
                issues.append(
                    ValidationIssue(table, row_number(index), column, "must be an integer from 0 to 100")
                )
    return issues


def validate_site_references(
    tables: dict[str, list[dict[str, str]]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if "site_registry" not in tables:
        return issues

    known_site_ids = {row.get("site_id", "") for row in tables["site_registry"]}
    for table in ("observations", "disturbance_events", "site_checks", "photo_manifest"):
        for index, row in enumerate(tables.get(table, [])):
            site_id = row.get("site_id", "").strip()
            if site_id and site_id not in known_site_ids:
                issues.append(
                    ValidationIssue(table, row_number(index), "site_id", f"unknown site_id '{site_id}'")
                )
    return issues


def validate_unique_values(
    table: str, rows: list[dict[str, str]], column: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    counts = Counter(row.get(column, "").strip() for row in rows)
    duplicate_values = {value for value, count in counts.items() if value and count > 1}
    for index, row in enumerate(rows):
        value = row.get(column, "").strip()
        if value in duplicate_values:
            issues.append(
                ValidationIssue(table, row_number(index), column, f"duplicate value '{value}'")
            )
    return issues


def validate_observation_content(rows: list[dict[str, str]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    taxon_columns = ["dominant_species", "secondary_species", "tertiary_species", "notes"]
    for index, row in enumerate(rows):
        if all(is_blank(row.get(column, "")) for column in taxon_columns):
            issues.append(
                ValidationIssue(
                    "observations",
                    row_number(index),
                    None,
                    "row needs at least one taxon field or notes",
                )
            )
    return issues


def validate_disturbance_windows(rows: list[dict[str, str]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, row in enumerate(rows):
        start_raw = row.get("disturbance_window_start", "")
        end_raw = row.get("disturbance_window_end", "")
        start = parse_datetime(start_raw)
        end = parse_datetime(end_raw)
        if start is not None and end is not None and start > end:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "disturbance_window_start",
                    "start must be before or equal to end",
                )
            )
    return issues


def validate_disturbance_observation_references(
    disturbance_rows: list[dict[str, str]], observation_rows: list[dict[str, str]]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    known_observation_ids = {
        row.get("observation_id", "").strip() for row in observation_rows
    }
    for index, row in enumerate(disturbance_rows):
        observation_id = row.get("related_observation_id", "").strip()
        if observation_id and observation_id not in known_observation_ids:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "related_observation_id",
                    f"unknown observation_id '{observation_id}'",
                )
            )
    return issues


def parse_related_site_ids(value: str) -> list[str]:
    return [site_id.strip() for site_id in value.split(";") if site_id.strip()]


def validate_field_note_site_references(
    field_note_rows: list[dict[str, str]], site_rows: list[dict[str, str]]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    known_site_ids = {row.get("site_id", "").strip() for row in site_rows}
    for index, row in enumerate(field_note_rows):
        site_ids = parse_related_site_ids(row.get("related_site_ids", ""))
        if len(site_ids) != len(set(site_ids)):
            issues.append(
                ValidationIssue(
                    "field_notes",
                    row_number(index),
                    "related_site_ids",
                    "contains a duplicate site_id",
                )
            )
        for site_id in site_ids:
            if site_id not in known_site_ids:
                issues.append(
                    ValidationIssue(
                        "field_notes",
                        row_number(index),
                        "related_site_ids",
                        f"unknown site_id '{site_id}'",
                    )
                )

        site_scope = row.get("site_scope", "").strip()
        if site_scope == "site_specific" and len(site_ids) != 1:
            issues.append(
                ValidationIssue(
                    "field_notes",
                    row_number(index),
                    "related_site_ids",
                    "site_specific notes must reference exactly one site",
                )
            )
        if site_scope in {"multi_site", "between_sites"} and len(site_ids) < 2:
            issues.append(
                ValidationIssue(
                    "field_notes",
                    row_number(index),
                    "related_site_ids",
                    f"{site_scope} notes must reference at least two sites",
                )
            )
    return issues


def validate_site_check_references(
    site_check_rows: list[dict[str, str]],
    field_note_rows: list[dict[str, str]],
    observation_rows: list[dict[str, str]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    field_notes = {
        row.get("field_note_id", "").strip(): row for row in field_note_rows
    }
    observations = {
        row.get("observation_id", "").strip(): row for row in observation_rows
    }
    for index, row in enumerate(site_check_rows):
        field_note_id = row.get("field_note_id", "").strip()
        note = field_notes.get(field_note_id)
        if note is None:
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "field_note_id",
                    f"unknown field_note_id '{field_note_id}'",
                )
            )
        elif row.get("check_date", "").strip() != note.get("note_date", "").strip():
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "check_date",
                    "must match the source field note date",
                )
            )
        elif row.get("site_id", "").strip() not in parse_related_site_ids(
            note.get("related_site_ids", "")
        ):
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "site_id",
                    "must be explicitly named in the source field note",
                )
            )
        elif note.get("note_type", "").strip() != "disturbance":
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "field_note_id",
                    "source field note must have note_type 'disturbance'",
                )
            )

        observation_id = row.get("related_observation_id", "").strip()
        observation = observations.get(observation_id)
        if observation_id and observation is None:
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "related_observation_id",
                    f"unknown observation_id '{observation_id}'",
                )
            )
        elif observation is not None:
            if row.get("site_id", "").strip() != observation.get("site_id", "").strip():
                issues.append(
                    ValidationIssue(
                        "site_checks",
                        row_number(index),
                        "related_observation_id",
                        "linked observation must have the same site_id",
                    )
                )
            if row.get("check_date", "").strip() != observation.get(
                "observation_date", ""
            ).strip():
                issues.append(
                    ValidationIssue(
                        "site_checks",
                        row_number(index),
                        "related_observation_id",
                        "linked observation must have the same date",
                    )
                )
    return issues


def validate_disturbance_lineage(
    disturbance_rows: list[dict[str, str]],
    site_check_rows: list[dict[str, str]],
    field_note_rows: list[dict[str, str]],
    *,
    require_lineage: bool = False,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    checks = {row.get("site_check_id", "").strip(): row for row in site_check_rows}
    note_ids = {row.get("field_note_id", "").strip() for row in field_note_rows}
    events_by_check: Counter[str] = Counter()

    for index, row in enumerate(disturbance_rows):
        note_id = row.get("source_field_note_id", "").strip()
        check_id = row.get("site_check_id", "").strip()
        if require_lineage and not note_id:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "source_field_note_id",
                    "public events must identify a source field note",
                )
            )
        if require_lineage and not check_id:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "site_check_id",
                    "public events must identify a site check",
                )
            )
        if note_id and note_id not in note_ids:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "source_field_note_id",
                    f"unknown field_note_id '{note_id}'",
                )
            )

        if not check_id:
            continue
        events_by_check[check_id] += 1
        check = checks.get(check_id)
        if check is None:
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "site_check_id",
                    f"unknown site_check_id '{check_id}'",
                )
            )
            continue
        comparisons = {
            "site_id": "site_id",
            "disturbance_event_type": "disturbance_type_assessed",
            "source_field_note_id": "field_note_id",
            "disturbance_window_end": "check_date",
        }
        for event_column, check_column in comparisons.items():
            if row.get(event_column, "").strip() != check.get(check_column, "").strip():
                issues.append(
                    ValidationIssue(
                        "disturbance_events",
                        row_number(index),
                        event_column,
                        f"must match linked site check {check_column}",
                    )
                )
        if check.get("disturbance_assessment", "").strip() != "observed_event":
            issues.append(
                ValidationIssue(
                    "disturbance_events",
                    row_number(index),
                    "site_check_id",
                    "linked site check must have assessment 'observed_event'",
                )
            )

    for index, check in enumerate(site_check_rows):
        check_id = check.get("site_check_id", "").strip()
        event_count = events_by_check[check_id]
        assessment = check.get("disturbance_assessment", "").strip()
        if assessment == "observed_event" and event_count != 1:
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "disturbance_assessment",
                    f"observed_event must link to exactly one event; found {event_count}",
                )
            )
        if assessment != "observed_event" and event_count:
            issues.append(
                ValidationIssue(
                    "site_checks",
                    row_number(index),
                    "disturbance_assessment",
                    "non-event assessments must not link to a disturbance event",
                )
            )
    return issues


def validate_table(
    table: str,
    rows: list[dict[str, str]],
    headers: list[str],
    schema: list[dict[str, str]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(validate_columns(table, headers, schema))
    issues.extend(validate_required_values(table, rows, schema))
    issues.extend(validate_allowed_values(table, rows, schema))
    issues.extend(validate_dates(table, rows))
    issues.extend(validate_percent_ranges(table, rows))
    return issues


def validate_dataset(data_dir: Path, schema_dir: Path) -> list[ValidationIssue]:
    tables: dict[str, list[dict[str, str]]] = {}
    issues: list[ValidationIssue] = []

    for table, schema_file in SCHEMA_FILES.items():
        csv_path = data_dir / f"{table}_sample.csv"
        if not csv_path.exists():
            continue

        schema_path = schema_dir / schema_file
        rows = read_csv(csv_path)
        headers = read_headers(csv_path)
        schema = read_csv(schema_path)

        tables[table] = rows
        issues.extend(validate_table(table, rows, headers, schema))

    issues.extend(validate_site_references(tables))
    if "observations" in tables:
        issues.extend(validate_observation_content(tables["observations"]))
    if "disturbance_events" in tables:
        issues.extend(validate_disturbance_windows(tables["disturbance_events"]))
    if {"field_notes", "site_registry"}.issubset(tables):
        issues.extend(
            validate_field_note_site_references(
                tables["field_notes"], tables["site_registry"]
            )
        )
    if {"site_checks", "field_notes", "observations"}.issubset(tables):
        issues.extend(
            validate_site_check_references(
                tables["site_checks"], tables["field_notes"], tables["observations"]
            )
        )
    if {"disturbance_events", "site_checks", "field_notes"}.issubset(tables):
        issues.extend(
            validate_disturbance_lineage(
                tables["disturbance_events"],
                tables["site_checks"],
                tables["field_notes"],
            )
        )

    return issues


def validate_public_photo_data(public_dir: Path, schema_dir: Path) -> list[ValidationIssue]:
    """Validate sanitized longitudinal metadata derived from the photo archive."""

    issues: list[ValidationIssue] = []
    required_files = {
        "site_registry": public_dir / "site_registry.csv",
        "observations": public_dir / "observations.csv",
        "field_notes": public_dir / "field_notes.csv",
        "site_checks": public_dir / "site_checks.csv",
        "photo_manifest": public_dir / "photo_manifest.csv",
        "photo_batch_summary": public_dir / "photo_batch_summary.csv",
    }
    disturbance_path = public_dir / "disturbance_events.csv"
    for table, path in required_files.items():
        if not path.exists():
            issues.append(ValidationIssue(table, None, None, f"missing file: {path}"))
    if issues:
        return issues

    site_rows = read_csv(required_files["site_registry"])
    observation_rows = read_csv(required_files["observations"])
    photo_rows = read_csv(required_files["photo_manifest"])
    summary_rows = read_csv(required_files["photo_batch_summary"])
    field_note_rows = read_csv(required_files["field_notes"])
    site_check_rows = read_csv(required_files["site_checks"])
    tables = {
        "site_registry": site_rows,
        "observations": observation_rows,
        "field_notes": field_note_rows,
        "site_checks": site_check_rows,
        "photo_manifest": photo_rows,
    }

    if disturbance_path.exists():
        tables["disturbance_events"] = read_csv(disturbance_path)

    for table in (
        "site_registry",
        "observations",
        "disturbance_events",
        "field_notes",
        "site_checks",
        "photo_manifest",
    ):
        if table not in tables:
            continue
        path = disturbance_path if table == "disturbance_events" else required_files[table]
        schema = read_csv(schema_dir / SCHEMA_FILES[table])
        issues.extend(validate_table(table, tables[table], read_headers(path), schema))

    issues.extend(validate_site_references(tables))
    issues.extend(validate_observation_content(observation_rows))
    issues.extend(validate_unique_values("site_registry", site_rows, "site_id"))
    issues.extend(validate_unique_values("observations", observation_rows, "observation_id"))
    issues.extend(validate_unique_values("field_notes", field_note_rows, "field_note_id"))
    issues.extend(validate_unique_values("site_checks", site_check_rows, "site_check_id"))
    issues.extend(validate_unique_values("photo_manifest", photo_rows, "photo_id"))
    issues.extend(validate_unique_values("photo_manifest", photo_rows, "relative_path"))
    if "disturbance_events" in tables:
        disturbance_rows = tables["disturbance_events"]
        issues.extend(
            validate_unique_values(
                "disturbance_events", disturbance_rows, "disturbance_event_id"
            )
        )
        issues.extend(validate_disturbance_windows(disturbance_rows))
        issues.extend(
            validate_disturbance_observation_references(
                disturbance_rows, observation_rows
            )
        )
        issues.extend(
            validate_disturbance_lineage(
                disturbance_rows,
                site_check_rows,
                field_note_rows,
                require_lineage=True,
            )
        )
    issues.extend(validate_field_note_site_references(field_note_rows, site_rows))
    issues.extend(
        validate_site_check_references(
            site_check_rows, field_note_rows, observation_rows
        )
    )

    private_columns = {"drive_file_id", "drive_file_url", "drive_folder_url"}
    leaked_columns = private_columns.intersection(read_headers(required_files["photo_manifest"]))
    for column in sorted(leaked_columns):
        issues.append(
            ValidationIssue(
                "photo_manifest",
                None,
                column,
                "private Drive metadata must not appear in the public manifest",
            )
        )

    manifest_counts: Counter[tuple[str, str]] = Counter()
    manifest_observation_counts: Counter[str] = Counter()
    for index, row in enumerate(photo_rows):
        site_id = row.get("site_id", "").strip()
        observation_date = row.get("observation_date", "").strip()
        observation_id = row.get("observation_id", "").strip()
        expected_id = f"site_{int(site_id):02d}_{observation_date.replace('-', '_')}"
        if observation_id != expected_id:
            issues.append(
                ValidationIssue(
                    "photo_manifest",
                    row_number(index),
                    "observation_id",
                    f"expected '{expected_id}' for site/date",
                )
            )

        photo_type = row.get("photo_type", "").strip()
        status = row.get("classification_status", "").strip()
        if status == "pending_visual_review" and photo_type != "unclassified":
            issues.append(
                ValidationIssue(
                    "photo_manifest",
                    row_number(index),
                    "photo_type",
                    "pending visual review must use photo_type 'unclassified'",
                )
            )
        if status == "verified" and photo_type == "unclassified":
            issues.append(
                ValidationIssue(
                    "photo_manifest",
                    row_number(index),
                    "classification_status",
                    "verified photos must have a classified photo_type",
                )
            )

        try:
            if int(row.get("file_size_bytes", "")) <= 0:
                raise ValueError
        except ValueError:
            issues.append(
                ValidationIssue(
                    "photo_manifest",
                    row_number(index),
                    "file_size_bytes",
                    "must be a positive integer",
                )
            )
        manifest_counts[(site_id, observation_date)] += 1
        manifest_observation_counts[observation_id] += 1

    known_observation_ids = {
        row.get("observation_id", "").strip() for row in observation_rows
    }
    for observation_id in sorted(set(manifest_observation_counts) - known_observation_ids):
        issues.append(
            ValidationIssue(
                "photo_manifest",
                None,
                "observation_id",
                f"unknown observation_id '{observation_id}'",
            )
        )

    for index, row in enumerate(observation_rows):
        site_id = row.get("site_id", "").strip()
        observation_date = row.get("observation_date", "").strip()
        observation_id = row.get("observation_id", "").strip()
        if row.get("record_origin", "").strip() == "photo_archive_backfill":
            try:
                expected_id = f"site_{int(site_id):02d}_{observation_date.replace('-', '_')}"
            except ValueError:
                expected_id = ""
            if expected_id and observation_id != expected_id:
                issues.append(
                    ValidationIssue(
                        "observations",
                        row_number(index),
                        "observation_id",
                        f"expected '{expected_id}' for photo-backed site/date",
                    )
                )
            try:
                declared_count = int(row.get("source_photo_count", ""))
                if declared_count <= 0:
                    raise ValueError
            except ValueError:
                declared_count = -1
                issues.append(
                    ValidationIssue(
                        "observations",
                        row_number(index),
                        "source_photo_count",
                        "must be a positive integer for photo-backed rows",
                    )
                )
            actual_count = manifest_observation_counts.get(observation_id, 0)
            if declared_count != actual_count:
                issues.append(
                    ValidationIssue(
                        "observations",
                        row_number(index),
                        "source_photo_count",
                        f"declares {declared_count}; manifest contains {actual_count}",
                    )
                )

    summary_headers = set(read_headers(required_files["photo_batch_summary"]))
    expected_summary_headers = {
        "site_id",
        "observation_date",
        "photo_count",
        "upload_status",
    }
    for column in sorted(expected_summary_headers - summary_headers):
        issues.append(
            ValidationIssue("photo_batch_summary", None, column, "required column is missing")
        )

    summary_keys: set[tuple[str, str]] = set()
    known_site_ids = {row.get("site_id", "").strip() for row in site_rows}
    for index, row in enumerate(summary_rows):
        site_id = row.get("site_id", "").strip()
        observation_date = row.get("observation_date", "").strip()
        key = (site_id, observation_date)
        if key in summary_keys:
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    None,
                    f"duplicate site/date batch {site_id}/{observation_date}",
                )
            )
        summary_keys.add(key)

        if site_id not in known_site_ids:
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    "site_id",
                    f"unknown site_id '{site_id}'",
                )
            )
        if parse_datetime(observation_date) is None:
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    "observation_date",
                    f"invalid date: {observation_date}",
                )
            )

        try:
            declared_count = int(row.get("photo_count", ""))
        except ValueError:
            declared_count = -1
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    "photo_count",
                    "must be a non-negative integer",
                )
            )
        actual_count = manifest_counts.get(key, 0)
        if declared_count != actual_count:
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    "photo_count",
                    f"declares {declared_count}; manifest contains {actual_count}",
                )
            )

        expected_status = "empty_folder" if declared_count == 0 else "uploaded"
        if row.get("upload_status", "").strip() != expected_status:
            issues.append(
                ValidationIssue(
                    "photo_batch_summary",
                    row_number(index),
                    "upload_status",
                    f"expected '{expected_status}'",
                )
            )

    for site_id, observation_date in sorted(set(manifest_counts) - summary_keys):
        issues.append(
            ValidationIssue(
                "photo_batch_summary",
                None,
                None,
                f"missing manifest batch {site_id}/{observation_date}",
            )
        )

    return issues


def raise_for_issues(issues: Iterable[ValidationIssue]) -> None:
    issue_list = list(issues)
    if issue_list:
        formatted = "\n".join(issue.format() for issue in issue_list)
        raise ValueError(f"Validation failed:\n{formatted}")
