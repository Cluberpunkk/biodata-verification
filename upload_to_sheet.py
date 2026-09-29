"""
upload_to_sheet.py
Pushes output/verification_results.csv into a new tab of the source Google
Sheet. Requires the service account to have EDITOR access (Viewer is not
enough to write).
"""

from __future__ import annotations

import csv
import sys

import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAME = "Verification Results V1"


def main() -> None:
    cfg = yaml.safe_load(open("config.yaml"))
    sheet_id = cfg["google_sheets"]["sheet_id"]
    creds_path = cfg["google_sheets"]["credentials_path"]
    csv_path = cfg["output"]["csv_path"]

    cs_reasons = cfg.get("cs_reasons", [])

    # Load CSV as a grid (header + rows), then append two empty CS-action columns.
    with open(csv_path, newline="", encoding="utf-8") as f:
        grid = [row for row in csv.reader(f)]
    grid[0] += ["CS Decision", "CS Reject Reason"]
    for r in grid[1:]:
        r += ["", ""]
    n_cols = len(grid[0])
    dec_col = n_cols - 2          # "CS Decision" column index (0-based)
    reason_col = n_cols - 1       # "CS Reject Reason" column index
    print(f"Loaded {len(grid)-1} data rows from {csv_path}")

    creds = Credentials.from_service_account_file(creds_path, scopes=WRITE_SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    # Look for an existing tab of this name; reuse (clear) it, else create it.
    meta = svc.get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta["sheets"]}

    if TAB_NAME in existing:
        sheet_gid = existing[TAB_NAME]
        svc.values().clear(spreadsheetId=sheet_id, range=f"'{TAB_NAME}'").execute()
        print(f"Reusing existing tab '{TAB_NAME}' (cleared)")
    else:
        resp = svc.batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": TAB_NAME,
                "gridProperties": {"rowCount": len(grid) + 10, "columnCount": len(grid[0])},
            }}}]},
        ).execute()
        sheet_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        print(f"Created new tab '{TAB_NAME}'")

    # Write all values in one shot.
    svc.values().update(
        spreadsheetId=sheet_id,
        range=f"'{TAB_NAME}'!A1",
        valueInputOption="RAW",
        body={"values": grid},
    ).execute()

    # Bold + freeze header, and add dropdowns to the two CS-action columns.
    n_rows = len(grid)
    requests = [
        {"repeatCell": {
            "range": {"sheetId": sheet_gid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_gid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # CS Decision dropdown: Approve / Reject / Pending
        {"setDataValidation": {
            "range": {"sheetId": sheet_gid, "startRowIndex": 1, "endRowIndex": n_rows,
                      "startColumnIndex": dec_col, "endColumnIndex": dec_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": v} for v in ("Approve", "Reject", "Pending")
            ]}, "showCustomUi": True, "strict": False},
        }},
        # CS Reject Reason dropdown: the standard reason list
        {"setDataValidation": {
            "range": {"sheetId": sheet_gid, "startRowIndex": 1, "endRowIndex": n_rows,
                      "startColumnIndex": reason_col, "endColumnIndex": reason_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": v} for v in cs_reasons
            ]}, "showCustomUi": True, "strict": False},
        }},
    ]
    svc.batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={sheet_gid}"
    print(f"\n✓ DONE — {len(grid)-1} rows written to tab '{TAB_NAME}'")
    print(f"URL: {url}")


if __name__ == "__main__":
    try:
        main()
    except HttpError as e:
        if e.resp.status == 403:
            sys.exit(
                "\n✗ PERMISSION DENIED (403).\n"
                "  The service account currently has VIEW-only access.\n"
                "  In the Sheet: Share → saathi-reader@saathi-sheets.iam.gserviceaccount.com → change to EDITOR.\n"
            )
        raise


