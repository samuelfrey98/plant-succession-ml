#!/usr/bin/env python
"""Validate sample CSVs against project schemas."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.validate import validate_dataset, validate_public_photo_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--public-dir",
        type=Path,
        default=ROOT / "data" / "public",
        help="Public export directory to validate.",
    )
    parser.add_argument(
        "--skip-sample",
        action="store_true",
        help="Validate only the selected public export directory.",
    )
    args = parser.parse_args()
    schema_dir = ROOT / "data" / "schema"
    issues = []
    if not args.skip_sample:
        issues.extend(validate_dataset(ROOT / "data" / "sample", schema_dir))
    issues.extend(validate_public_photo_data(args.public_dir, schema_dir))
    if issues:
        print(f"Found {len(issues)} validation issue(s):")
        for issue in issues:
            print(f"- {issue.format()}")
        return 1

    scope = "public CSVs" if args.skip_sample else "sample and public CSVs"
    print(f"Validation passed: {scope} match schemas and project rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
