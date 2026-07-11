#!/usr/bin/env python
"""Export sample site registry coordinates to GeoJSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT_CSV = ROOT / "data" / "sample" / "site_registry_sample.csv"
OUTPUT_GEOJSON = ROOT / "outputs" / "maps" / "site_registry_sample.geojson"


def parse_float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def row_to_feature(row: dict[str, str]) -> dict | None:
    latitude = parse_float(row.get("latitude", ""))
    longitude = parse_float(row.get("longitude", ""))
    if latitude is None or longitude is None:
        return None

    properties = {
        "site_id": row.get("site_id", ""),
        "site_name": row.get("site_name", ""),
        "habitat_context": row.get("habitat_context", ""),
        "priority_rank": row.get("priority_rank", ""),
        "active": row.get("active", ""),
        "notes": row.get("notes", ""),
    }

    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [longitude, latitude],
        },
        "properties": properties,
    }


def main() -> int:
    features = []
    with INPUT_CSV.open(newline="", encoding="utf-8") as csv_file:
        for row in csv.DictReader(csv_file):
            feature = row_to_feature(row)
            if feature is not None:
                features.append(feature)

    collection = {
        "type": "FeatureCollection",
        "features": features,
    }

    OUTPUT_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_GEOJSON.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(features)} site point(s) to {OUTPUT_GEOJSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

