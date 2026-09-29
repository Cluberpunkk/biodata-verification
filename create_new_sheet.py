"""
create_new_sheet.py
Creates a BRAND-NEW Google Sheet (separate from the source sheet), writes
output/verification_results.csv into it, formats the header, and shares it
with the user so they can open it. The original sheet is never touched.
"""

from __future__ import annotations

import csv
import sys

import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
NEW_TITLE = "Project Trust V1 — Verification Results"
SHARE_WITH = "aicontent@gyantv.in"


def main() -> None:
    cfg = yaml.safe_load(open("config.yaml"))
    creds_path = cfg["google_sheets"]["credentials_path"]
    csv_path = cfg["output"]["csv_path"]

    with open(csv_path, newline="", encoding="utf-8") as f:
        grid = [row for row in csv.reader(f)]
    print(f"Loaded {len(grid)-1} data rows")

    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # 1. Create the new spreadsheet
    created = sheets.create(body={
        "properties": {"title": NEW_TITLE},
        "sheets": [{"properties": {
            "title": "Results",
            "gridProperties": {"rowCount": len(grid) + 10, "columnCount": len(grid[0])},
        }}],
    }).execute()
    sheet_id = created["spreadsheetId"]
    gid = created["sheets"][0]["properties"]["sheetId"]
    print(f"Created spreadsheet {sheet_id}")

    # 2. Write all values
    sheets.values().update(
        spreadsheetId=sheet_id, range="'Results'!A1",
        valueInputOption="RAW", body={"values": grid},
    ).execute()

    # 3. Bold + freeze header
    sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [
        {"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]}).execute()

    # 4. Share with the user (writer) so they can open/edit it
    drive.permissions().create(
        fileId=sheet_id,
        body={"type": "user", "role": "writer", "emailAddress": SHARE_WITH},
        sendNotificationEmail=True,
        fields="id",
    ).execute()
    print(f"Shared with {SHARE_WITH} (writer)")

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"\n✓ DONE — {len(grid)-1} rows in a new sheet")
    print(f"URL: {url}")


if __name__ == "__main__":
    try:
        main()
    except HttpError as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg or "Drive API" in msg:
            sys.exit(
                "\n✗ Google DRIVE API is not enabled in project 'saathi-sheets'.\n"
                "  Enable it: Cloud Console → APIs & Services → Library → 'Google Drive API' → Enable.\n"
                "  (Needed to create & share a new file.)\n"
            )
        if "storageQuotaExceeded" in msg:
            sys.exit(
                "\n✗ Service accounts can't own new Drive files in this setup (no storage quota).\n"
                "  Easiest fix: I write into a NEW TAB of the original sheet instead (now that it has Editor access).\n"
            )
        raise
