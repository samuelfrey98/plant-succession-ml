#!/usr/bin/env python3
"""Rebuild stored GPS/date integrity audits for local ingestion batches."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plant_succession.app import build_service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "batch_ids",
        nargs="*",
        help="Optional batch IDs. Omit to audit every local batch.",
    )
    args = parser.parse_args()

    service = build_service()
    batch_ids = args.batch_ids or [
        str(batch["batch_id"]) for batch in service.list_batches()
    ]
    failures = 0
    for batch_id in batch_ids:
        try:
            result = service.audit_batch_integrity(batch_id)
            print(
                f"{batch_id}: {result['status']} "
                f"({result['issue_count']} issues, {result['warning_count']} warnings)"
            )
        except Exception as error:
            failures += 1
            print(f"{batch_id}: ERROR: {error}", file=sys.stderr)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
