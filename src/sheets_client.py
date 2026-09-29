"""
sheets_client.py
Reads rows from the Project Trust govt-ID-verification Google Sheet
using a service account.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetsClient:
    """Thin wrapper around Google Sheets API for read-only access."""

    def __init__(self, credentials_path: str | Path):
        creds_path = Path(credentials_path)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Service account file not found at: {creds_path}\n"
                f"Make sure service_account.json is in credentials/ folder."
            )

        creds = Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)
        self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        logger.info("Authenticated with service account")

    def read_range(self, sheet_id: str, range_a1: str) -> List[List[str]]:
        """Read a range from the sheet. Returns list of rows (each row = list of cells)."""
        try:
            result = (
                self.service.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=range_a1)
                .execute()
            )
            rows = result.get("values", [])
            logger.info(f"Read {len(rows)} rows from {range_a1}")
            return rows
        except HttpError as e:
            logger.error(f"Sheets API error: {e}")
            raise

    def rows_to_dicts(
        self, rows: List[List[str]], column_map: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        """Convert raw rows into list of dicts using the column mapping from config."""
        out = []
        for idx, row in enumerate(rows, start=2):  # row 1 is header
            record: Dict[str, Any] = {"_row_number": idx}
            for field, col_idx in column_map.items():
                record[field] = row[col_idx] if col_idx < len(row) else ""
            out.append(record)
        return out
