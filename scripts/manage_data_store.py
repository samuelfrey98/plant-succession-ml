#!/usr/bin/env python3
"""Inspect, import, export, and back up the canonical SQLite data store."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.ingestion import IngestionPaths, IngestionService  # noqa: E402


def build_service() -> IngestionService:
    public_dir = Path(
        os.environ.get("PLANT_PUBLIC_DATA", ROOT / "data" / "public")
    ).expanduser()
    state_dir = Path(
        os.environ.get(
            "PLANT_INGESTION_STATE", ROOT / "data" / "raw" / "ingestion"
        )
    ).expanduser()
    archive_dir = Path(
        os.environ.get("PLANT_PHOTO_ARCHIVE", ROOT / "photos")
    ).expanduser()
    anchor_value = os.environ.get(
        "PLANT_SITE_ANCHORS", str(ROOT / "data" / "raw" / "site_anchors.csv")
    )
    return IngestionService(
        IngestionPaths(
            public_dir=public_dir,
            schema_dir=ROOT / "data" / "schema",
            state_dir=state_dir,
            archive_dir=archive_dir,
            site_anchor_file=Path(anchor_value).expanduser(),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show migrations and canonical datasets.")

    export_parser = subparsers.add_parser(
        "export", help="Materialize validated CSV snapshots from SQLite."
    )
    export_parser.add_argument("--output", type=Path)

    import_parser = subparsers.add_parser(
        "import", help="Replace SQLite snapshots from a validated CSV directory."
    )
    import_parser.add_argument("--source", type=Path, default=ROOT / "data" / "public")
    import_parser.add_argument(
        "--replace",
        action="store_true",
        help="Required when the canonical store is already initialized.",
    )

    backup_parser = subparsers.add_parser(
        "backup", help="Create an online SQLite backup and checksum manifest."
    )
    backup_parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    service = build_service()
    if args.command == "status":
        result = service.canonical_store_status()
    elif args.command == "export":
        result = service.export_public_data(args.output)
    elif args.command == "import":
        result = service.import_public_exports(args.source, replace=args.replace)
    else:
        result = service.backup_database(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
