"""CSV validation helpers for the Plant Succession ML Project.

The validator intentionally uses only the Python standard library so the basic
data-quality check can run before notebook or analysis dependencies are installed.
"""

from __future__ import annotations

import csv
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
    for table in ("observations", "disturbance_events", "photo_manifest"):
        for index, row in enumerate(tables.get(table, [])):
            site_id = row.get("site_id", "").strip()
            if site_id and site_id not in known_site_ids:
                issues.append(
                    ValidationIssue(table, row_number(index), "site_id", f"unknown site_id '{site_id}'")
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

    return issues


def raise_for_issues(issues: Iterable[ValidationIssue]) -> None:
    issue_list = list(issues)
    if issue_list:
        formatted = "\n".join(issue.format() for issue in issue_list)
        raise ValueError(f"Validation failed:\n{formatted}")
