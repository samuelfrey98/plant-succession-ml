#!/usr/bin/env python3
"""Extract private site coordinates from the original Excel site registry."""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_PATTERN = re.compile(r"([A-Z]+)")


def column_index(reference: str) -> int:
    match = CELL_PATTERN.match(reference)
    if match is None:
        raise ValueError(f"Invalid cell reference: {reference}")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def sheet_rows(workbook: Path, sheet_name: str) -> list[list[str]]:
    namespaces = {"m": MAIN_NS, "r": REL_NS}
    with ZipFile(workbook) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(".//m:t", namespaces))
                for item in shared_root.findall("m:si", namespaces)
            ]

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        relationship_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationship_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        }
        sheet = next(
            (
                item
                for item in workbook_root.findall("m:sheets/m:sheet", namespaces)
                if item.attrib.get("name") == sheet_name
            ),
            None,
        )
        if sheet is None:
            raise ValueError(f"Workbook does not contain a '{sheet_name}' sheet.")
        relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
        target = relationships[relationship_id].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheet_root = ET.fromstring(archive.read(target))

        rows: list[list[str]] = []
        for row in sheet_root.findall(".//m:sheetData/m:row", namespaces):
            values: dict[int, str] = {}
            for cell in row.findall("m:c", namespaces):
                index = column_index(cell.attrib.get("r", ""))
                cell_type = cell.attrib.get("t")
                value_node = cell.find("m:v", namespaces)
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
                elif cell_type == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.findall(".//m:t", namespaces)
                    )
                values[index] = value
            width = max(values, default=-1) + 1
            rows.append([values.get(index, "") for index in range(width)])
        return rows


def extract_anchors(workbook: Path) -> list[dict[str, str]]:
    rows = sheet_rows(workbook, "Site Registry")
    if not rows:
        raise ValueError("Site Registry is empty.")
    headers = {value.strip(): index for index, value in enumerate(rows[0])}
    required = {"site_id", "latitude", "longitude"}
    missing = sorted(required - headers.keys())
    if missing:
        raise ValueError(f"Site Registry is missing columns: {', '.join(missing)}")

    anchors: list[dict[str, str]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        def value(column: str) -> str:
            index = headers[column]
            return row[index].strip() if index < len(row) else ""

        raw_site_id = value("site_id")
        if not raw_site_id:
            continue
        site_id = str(int(float(raw_site_id)))
        latitude = float(value("latitude"))
        longitude = float(value("longitude"))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError(f"Site Registry row {row_number} has out-of-range coordinates.")
        anchors.append(
            {
                "site_id": site_id,
                "latitude": f"{latitude:.7f}",
                "longitude": f"{longitude:.7f}",
            }
        )
    if len({row["site_id"] for row in anchors}) != len(anchors):
        raise ValueError("Site Registry contains duplicate site IDs.")
    return sorted(anchors, key=lambda row: int(row["site_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/site_anchors.csv"),
    )
    args = parser.parse_args()
    anchors = extract_anchors(args.workbook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["site_id", "latitude", "longitude"])
        writer.writeheader()
        writer.writerows(anchors)
    print(f"Wrote {len(anchors)} private site anchors to {args.output}")


if __name__ == "__main__":
    main()
