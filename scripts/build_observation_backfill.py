#!/usr/bin/env python
"""Initialize photo-backed observation rows without inventing ecological values."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "public" / "photo_manifest.csv"
DEFAULT_SCHEMA = ROOT / "data" / "schema" / "observations_schema.csv"
DEFAULT_OUTPUT = ROOT / "data" / "public" / "observations.csv"

DERIVED_FIELDS = {
    "observation_id",
    "site_id",
    "observation_date",
    "record_origin",
    "source_photo_count",
}
PENDING_NOTE = (
    "Observation shell generated from photo archive; ecological fields pending visual review."
)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing input file: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def schema_fields(path: Path) -> list[str]:
    fields = [row["field_name"].strip() for row in read_rows(path)]
    missing = DERIVED_FIELDS - set(fields)
    if missing:
        raise ValueError(f"Observation schema is missing fields: {', '.join(sorted(missing))}")
    return fields


def existing_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    rows = read_rows(path)
    counts = Counter(row.get("observation_id", "").strip() for row in rows)
    duplicates = sorted(key for key, count in counts.items() if key and count > 1)
    if duplicates:
        raise ValueError(f"Existing output has duplicate observation IDs: {', '.join(duplicates)}")
    return {row["observation_id"].strip(): row for row in rows}


def build_rows(
    manifest: list[dict[str, str]],
    fields: list[str],
    prior_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], int]:
    photo_counts = Counter(row["observation_id"].strip() for row in manifest)
    photo_evidence: dict[str, tuple[str, str]] = {}
    for row in manifest:
        observation_id = row["observation_id"].strip()
        key = (row["site_id"].strip(), row["observation_date"].strip())
        previous = photo_evidence.setdefault(observation_id, key)
        if previous != key:
            raise ValueError(
                f"Observation ID {observation_id} maps to multiple site/date combinations"
            )

    output: list[dict[str, str]] = []
    for observation_id, (site_id, observation_date) in sorted(
        photo_evidence.items(), key=lambda item: (int(item[1][0]), item[1][1])
    ):
        prior = prior_by_id.get(observation_id, {})
        row = {field: prior.get(field, "") for field in fields}
        row.update(
            {
                "observation_id": observation_id,
                "site_id": site_id,
                "observation_date": observation_date,
                "record_origin": "photo_archive_backfill",
                "source_photo_count": str(photo_counts[observation_id]),
            }
        )
        if not row.get("review_status", "").strip():
            row["review_status"] = "pending_visual_review"
        if not row.get("notes", "").strip():
            row["notes"] = PENDING_NOTE
        output.append(row)

    extra_ids = sorted(set(prior_by_id) - set(photo_evidence))
    for observation_id in extra_ids:
        prior = prior_by_id[observation_id]
        output.append({field: prior.get(field, "") for field in fields})

    output.sort(
        key=lambda row: (
            int(row["site_id"]) if row.get("site_id", "").isdigit() else 10**9,
            row.get("observation_date", ""),
            row.get("observation_id", ""),
        )
    )
    return output, len(extra_ids)


def write_rows(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    fields = schema_fields(args.schema)
    prior_by_id = existing_rows(args.output)
    rows, preserved_extra_count = build_rows(read_rows(args.manifest), fields, prior_by_id)
    write_rows(args.output, fields, rows)

    pending_count = sum(row["review_status"] == "pending_visual_review" for row in rows)
    print(
        f"Wrote {len(rows)} observation rows; {pending_count} pending visual review; "
        f"{preserved_extra_count} non-photo row(s) preserved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
