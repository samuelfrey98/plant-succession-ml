#!/usr/bin/env python3
"""Backfill append-only candidate and verification events in local ingestion state."""

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
        "--verified-actor-type",
        choices=("human", "ai_assistant", "model", "rule", "legacy_unknown"),
        required=True,
    )
    parser.add_argument("--verified-actor-id", required=True)
    args = parser.parse_args()

    result = build_service().backfill_review_events(
        args.verified_actor_type, args.verified_actor_id
    )
    print(
        f"Created {result['candidate_events']} candidate events and "
        f"{result['verified_events']} verification events."
    )


if __name__ == "__main__":
    main()
