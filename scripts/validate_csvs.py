#!/usr/bin/env python
"""Validate sample CSVs against project schemas."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.validate import validate_dataset, validate_public_photo_data  # noqa: E402


def main() -> int:
    schema_dir = ROOT / "data" / "schema"
    issues = validate_dataset(ROOT / "data" / "sample", schema_dir)
    issues.extend(validate_public_photo_data(ROOT / "data" / "public", schema_dir))
    if issues:
        print(f"Found {len(issues)} validation issue(s):")
        for issue in issues:
            print(f"- {issue.format()}")
        return 1

    print("Validation passed: sample and public CSVs match schemas and project rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
