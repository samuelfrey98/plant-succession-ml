#!/usr/bin/env python3
"""Seed and run an isolated public demo of the ingestion workflow."""

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

from plant_succession.demo import prepare_demo_workspace  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    parser.add_argument("--workspace", type=Path, default=ROOT / ".demo")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Rebuild the isolated demo from committed public data and synthetic batches.",
    )
    parser.add_argument(
        "--writable",
        action="store_true",
        help="Allow local demo changes. Hosted demos should remain read-only.",
    )
    args = parser.parse_args()

    manifest = prepare_demo_workspace(
        args.workspace,
        source_public_dir=ROOT / "data" / "public",
        schema_dir=ROOT / "data" / "schema",
        reset=args.reset,
    )
    os.environ.update(
        {
            "PLANT_PUBLIC_DATA": str(Path(manifest["root"]) / "public"),
            "PLANT_INGESTION_STATE": str(Path(manifest["root"]) / "state"),
            "PLANT_PHOTO_ARCHIVE": str(Path(manifest["root"]) / "photos"),
            "PLANT_SITE_ANCHORS": str(Path(manifest["root"]) / "site_anchors.csv"),
            "PLANT_REVIEWER_ID": "demo_visitor",
            "PLANT_DEMO_MODE": "true",
            "PLANT_DEMO_READ_ONLY": "false" if args.writable else "true",
        }
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(
        f"Starting {'writable' if args.writable else 'read-only'} demo at "
        f"http://{args.host}:{args.port}"
    )

    import uvicorn

    uvicorn.run(
        "plant_succession.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
