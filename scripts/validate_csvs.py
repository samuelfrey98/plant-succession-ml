#!/usr/bin/env python
"""Validate sample CSVs against project schemas."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.validate import validate_dataset  # noqa: E402


def main() -> int:
    issues = validate_dataset(ROOT / "data" / "sample", ROOT / "data" / "schema")
    if issues:
        print(f"Found {len(issues)} validation issue(s):")
        for issue in issues:
            print(f"- {issue.format()}")
        return 1

    print("Validation passed: sample CSVs match schemas and project rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

