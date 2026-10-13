#!/usr/bin/env python
"""Build public photo metadata from a private Google Drive inventory export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "data" / "raw" / "photo_inventory.csv"
DEFAULT_FOLDERS = ROOT / "data" / "raw" / "photo_folders.csv"
DEFAULT_MANIFEST = ROOT / "data" / "public" / "photo_manifest.csv"
DEFAULT_SUMMARY = ROOT / "data" / "public" / "photo_batch_summary.csv"
SITE_PATTERN = re.compile(r"^site_(\d+)$")
NUMBER_PATTERN = re.compile(r"(\d+)")
TOKEN_PATTERN = re.compile(r"[^a-z0-9]+")

MANIFEST_FIELDS = [
    "photo_id",
    "observation_id",
    "site_id",
    "observation_date",
    "photo_type",
    "source_filename",
    "relative_path",
    "file_size_bytes",
    "classification_status",
    "notes",
]

SUMMARY_FIELDS = ["site_id", "observation_date", "photo_count", "upload_status"]
REVIEW_FIELDS = {"photo_type", "classification_status", "notes"}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing input file: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_site_id(value: str) -> int:
    match = SITE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Invalid site folder '{value}'; expected site_<number>")
    return int(match.group(1))


def parse_date(value: str) -> str:
    parsed = date.fromisoformat(value.strip())
    return parsed.isoformat()


def natural_filename_key(row: dict[str, str]) -> tuple[object, ...]:
    filename = row["source_filename"]
    pieces: list[object] = []
    for piece in NUMBER_PATTERN.split(filename.casefold()):
        pieces.append(int(piece) if piece.isdigit() else piece)
    # Drive ID is a stable tie-breaker for duplicate source filenames.
    return (*pieces, row["drive_file_id"])


def filename_token(filename: str) -> str:
    token = TOKEN_PATTERN.sub("_", Path(filename).stem.casefold()).strip("_")
    return token or "photo"


def existing_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    rows = read_rows(path)
    counts = Counter(row.get("photo_id", "").strip() for row in rows)
    duplicates = sorted(key for key, count in counts.items() if key and count > 1)
    if duplicates:
        raise ValueError(f"Existing manifest has duplicate photo IDs: {', '.join(duplicates)}")
    return {row["photo_id"].strip(): row for row in rows}


def has_review_data(row: dict[str, str]) -> bool:
    return (
        row.get("photo_type", "").strip() not in {"", "unclassified"}
        or row.get("classification_status", "").strip()
        not in {"", "pending_visual_review"}
        or bool(row.get("notes", "").strip())
    )


def build_manifest(
    rows: list[dict[str, str]],
    prior_by_id: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, object]]:
    required = {
        "drive_file_id",
        "source_site_folder",
        "source_date_folder",
        "source_filename",
        "file_size_bytes",
    }
    if rows and not required.issubset(rows[0]):
        missing = ", ".join(sorted(required - set(rows[0])))
        raise ValueError(f"Photo inventory is missing columns: {missing}")

    file_ids = [row["drive_file_id"].strip() for row in rows]
    duplicates = sorted(key for key, count in Counter(file_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate Drive file IDs: {', '.join(duplicates)}")

    grouped: dict[tuple[int, str], list[dict[str, str]]] = {}
    for row in rows:
        site_id = parse_site_id(row["source_site_folder"])
        observation_date = parse_date(row["source_date_folder"])
        grouped.setdefault((site_id, observation_date), []).append(row)

    prior_by_id = prior_by_id or {}
    output: list[dict[str, object]] = []
    for (site_id, observation_date), batch in sorted(grouped.items()):
        observation_token = f"site_{site_id:02d}_{observation_date.replace('-', '_')}"
        tokens = Counter(filename_token(row["source_filename"]) for row in batch)
        for row in sorted(batch, key=natural_filename_key):
            source_filename = row["source_filename"].strip()
            suffix = Path(source_filename).suffix.lower() or ".heic"
            source_token = filename_token(source_filename)
            if tokens[source_token] > 1:
                digest = hashlib.sha256(row["drive_file_id"].encode("utf-8")).hexdigest()[:8]
                source_token = f"{source_token}_{digest}"
            photo_token = f"{observation_token}_{source_token}"
            normalized_name = f"site_{site_id:02d}_{observation_date}_{source_token}{suffix}"
            try:
                file_size_bytes = int(row["file_size_bytes"])
            except ValueError as exc:
                raise ValueError(f"Invalid file size for {source_filename}") from exc

            manifest_row: dict[str, object] = {
                "photo_id": photo_token,
                "observation_id": observation_token,
                "site_id": site_id,
                "observation_date": observation_date,
                "photo_type": "unclassified",
                "source_filename": source_filename,
                "relative_path": (
                    f"photos/site_{site_id:02d}/{observation_date}/{normalized_name}"
                ),
                "file_size_bytes": file_size_bytes,
                "classification_status": "pending_visual_review",
                "notes": "",
            }
            prior = prior_by_id.get(photo_token, {})
            for field in REVIEW_FIELDS:
                if prior.get(field, "").strip():
                    manifest_row[field] = prior[field]
            output.append(manifest_row)
    return output


def build_summary(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in rows:
        count = int(row["photo_count"])
        output.append(
            {
                "site_id": parse_site_id(row["source_site_folder"]),
                "observation_date": parse_date(row["source_date_folder"]),
                "photo_count": count,
                "upload_status": "empty_folder" if count == 0 else "uploaded",
            }
        )
    return sorted(output, key=lambda row: (int(row["site_id"]), str(row["observation_date"])))


def write_rows(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--folders", type=Path, default=DEFAULT_FOLDERS)
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()

    prior_by_id = existing_manifest(args.manifest_output)
    manifest = build_manifest(read_rows(args.inventory), prior_by_id)
    summary = build_summary(read_rows(args.folders))
    write_rows(args.manifest_output, MANIFEST_FIELDS, manifest)
    write_rows(args.summary_output, SUMMARY_FIELDS, summary)

    batch_count = len({(row["site_id"], row["observation_date"]) for row in manifest})
    empty_count = sum(row["upload_status"] == "empty_folder" for row in summary)
    preserved_review_count = sum(
        row["photo_id"] in prior_by_id
        and has_review_data(prior_by_id[row["photo_id"]])
        for row in manifest
    )
    print(
        f"Wrote {len(manifest)} photos across {batch_count} non-empty batches; "
        f"{empty_count} empty folder(s) recorded; "
        f"{preserved_review_count} existing review row(s) preserved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
