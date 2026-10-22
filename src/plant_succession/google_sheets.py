"""Optional Google Sheets destination for validated public CSV snapshots."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Protocol


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
PUBLIC_SHEET_TABS = {
    "site_registry.csv": "Sites",
    "observations.csv": "Observations",
    "photo_manifest.csv": "Photo Manifest",
    "photo_batch_summary.csv": "Photo Batches",
    "field_notes.csv": "Field Notes",
    "site_checks.csv": "Site Checks",
    "disturbance_events.csv": "Disturbance Events",
    "analysis_ready_observations.csv": "Analysis Ready",
}


class GoogleSheetsSyncError(RuntimeError):
    """Raised when a configured Google Sheets destination cannot be updated."""


class SheetsPublisher(Protocol):
    def configuration(self) -> dict[str, object]: ...

    def sync(self, public_dir: Path) -> dict[str, object]: ...


class GoogleSheetsPublisher:
    """Replace workbook tabs with the latest validated public CSV snapshots."""

    def __init__(
        self,
        spreadsheet_id: str | None = None,
        credentials_file: str | None = None,
        service_account_json: str | None = None,
    ):
        self.spreadsheet_id = (
            spreadsheet_id if spreadsheet_id is not None else os.getenv("PLANT_GOOGLE_SHEETS_ID", "")
        ).strip()
        self.credentials_file = (
            credentials_file
            if credentials_file is not None
            else os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        ).strip()
        self.service_account_json = (
            service_account_json
            if service_account_json is not None
            else os.getenv("PLANT_GOOGLE_SERVICE_ACCOUNT_JSON", "")
        ).strip()

    def configuration(self) -> dict[str, object]:
        credentials_configured = bool(self.credentials_file or self.service_account_json)
        configured = bool(self.spreadsheet_id and credentials_configured)
        reason = ""
        if not self.spreadsheet_id:
            reason = "Set PLANT_GOOGLE_SHEETS_ID to enable the Google Sheets mirror."
        elif not credentials_configured:
            reason = (
                "Set GOOGLE_APPLICATION_CREDENTIALS or PLANT_GOOGLE_SERVICE_ACCOUNT_JSON "
                "to enable the Google Sheets mirror."
            )
        return {
            "available": configured,
            "destination": "google_sheets",
            "spreadsheet_url": (
                f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
                if self.spreadsheet_id
                else ""
            ),
            "reason": reason,
        }

    def _credentials(self):
        try:
            from google.oauth2 import service_account
        except ImportError as error:
            raise GoogleSheetsSyncError(
                "Google Sheets dependencies are not installed. Reinstall the project dependencies."
            ) from error

        try:
            if self.service_account_json:
                info = json.loads(self.service_account_json)
                return service_account.Credentials.from_service_account_info(
                    info, scopes=[SHEETS_SCOPE]
                )
            return service_account.Credentials.from_service_account_file(
                self.credentials_file, scopes=[SHEETS_SCOPE]
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise GoogleSheetsSyncError(f"Could not load Google credentials: {error}") from error

    @staticmethod
    def _csv_values(path: Path) -> list[list[str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))

    def sync(self, public_dir: Path) -> dict[str, object]:
        config = self.configuration()
        if not config["available"]:
            raise GoogleSheetsSyncError(str(config["reason"]))
        try:
            from googleapiclient.discovery import build
        except ImportError as error:
            raise GoogleSheetsSyncError(
                "Google Sheets dependencies are not installed. Reinstall the project dependencies."
            ) from error

        try:
            service = build(
                "sheets", "v4", credentials=self._credentials(), cache_discovery=False
            )
            metadata = (
                service.spreadsheets()
                .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title")
                .execute()
            )
            existing_titles = {
                sheet["properties"]["title"] for sheet in metadata.get("sheets", [])
            }
            missing_titles = [
                title
                for filename, title in PUBLIC_SHEET_TABS.items()
                if (public_dir / filename).exists() and title not in existing_titles
            ]
            if missing_titles:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": title}}}
                            for title in missing_titles
                        ]
                    },
                ).execute()

            row_counts: dict[str, int] = {}
            for filename, title in PUBLIC_SHEET_TABS.items():
                path = public_dir / filename
                if not path.exists():
                    continue
                values = self._csv_values(path)
                service.spreadsheets().values().clear(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{title}'",
                    body={},
                ).execute()
                if values:
                    service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"'{title}'!A1",
                        valueInputOption="RAW",
                        body={"majorDimension": "ROWS", "values": values},
                    ).execute()
                row_counts[filename] = max(len(values) - 1, 0)
        except GoogleSheetsSyncError:
            raise
        except Exception as error:
            raise GoogleSheetsSyncError(f"Google Sheets sync failed: {error}") from error

        return {
            "destination": "google_sheets",
            "spreadsheet_url": config["spreadsheet_url"],
            "row_counts": row_counts,
        }
