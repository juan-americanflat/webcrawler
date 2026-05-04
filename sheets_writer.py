"""
Google Sheets Writer — SEO Rank Checker
Writes rank_checker.py CSV output to a dedicated Google Sheet.

Each Monday run appends a new tab named with the date (e.g. "2026-05-04")
and updates a "Latest" tab so you always have a current snapshot.

Setup (one time):
  1. Create a Google Cloud project at console.cloud.google.com
  2. Enable the Google Sheets API
  3. Create a Service Account → download JSON key
  4. Share your Google Sheet with the service account email (Editor access)
  5. Add these GitHub Secrets:
       GOOGLE_SERVICE_ACCOUNT_JSON  — full contents of the JSON key file
       GOOGLE_SHEET_ID              — the ID from your Sheet URL:
                                      docs.google.com/spreadsheets/d/SHEET_ID/edit

Usage:
  python sheets_writer.py --input results_latest.csv
"""

import os
import csv
import json
import argparse
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER = [
    "keyword", "category", "priority", "position", "position_label",
    "url", "in_featured", "total_results", "error", "checked_at"
]

# Column widths for readability
COL_WIDTHS = {
    "A": 280,  # keyword
    "B": 160,  # category
    "C": 80,   # priority
    "D": 70,   # position
    "E": 130,  # position_label
    "F": 320,  # url
    "G": 90,   # in_featured
    "H": 110,  # total_results
    "I": 180,  # error
    "J": 160,  # checked_at
}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_client() -> gspread.Client:
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds_dict = json.loads(sa_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def load_csv(filepath: str) -> list[dict]:
    rows = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def rows_to_values(rows: list[dict]) -> list[list]:
    values = [HEADER]
    for row in rows:
        values.append([row.get(col, "") for col in HEADER])
    return values


def format_sheet(sheet, num_rows: int):
    """Apply header formatting and freeze top row."""
    # Bold + background on header row
    sheet.format("A1:J1", {
        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })
    # Freeze header row
    sheet.spreadsheet.batch_update({
        "requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet.id,
                    "gridProperties": {"frozenRowCount": 1}
                },
                "fields": "gridProperties.frozenRowCount"
            }
        }]
    })


def write_tab(spreadsheet, tab_name: str, values: list[list], overwrite: bool = True):
    """Write values to a named tab, creating it if it doesn't exist."""
    try:
        sheet = spreadsheet.worksheet(tab_name)
        if overwrite:
            sheet.clear()
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=tab_name, rows=len(values) + 10, cols=len(HEADER))

    sheet.update(values, "A1")
    format_sheet(sheet, len(values))
    print(f"  ✅ Written to tab: {tab_name} ({len(values)-1} rows)")
    return sheet


def write_summary_tab(spreadsheet, rows: list[dict]):
    """Write a summary tab with key stats."""
    ranked     = [r for r in rows if r.get("position") and r["position"] != "None" and r["position"] != ""]
    try:
        ranked_num = [r for r in ranked if int(r["position"]) > 0]
        top3       = [r for r in ranked_num if int(r["position"]) <= 3]
        page1      = [r for r in ranked_num if int(r["position"]) <= 10]
        page2      = [r for r in ranked_num if 10 < int(r["position"]) <= 20]
        not_ranked = [r for r in rows if not r.get("position") or r["position"] in ("None", "")]
    except (ValueError, TypeError):
        top3 = page1 = page2 = not_ranked = []
        ranked_num = ranked

    date_str = datetime.now().strftime("%B %d, %Y")
    summary_values = [
        ["SEO Rank Report — Americanflat", "", ""],
        [f"Generated: {date_str}", "", ""],
        ["", "", ""],
        ["Metric", "Count", "% of Total"],
        ["Total keywords tracked", len(rows), "100%"],
        ["Ranked (any position)", len(ranked_num), f"{len(ranked_num)/len(rows)*100:.1f}%" if rows else "0%"],
        ["Top 3 🏆", len(top3), f"{len(top3)/len(rows)*100:.1f}%" if rows else "0%"],
        ["Page 1 (1–10) ✅", len(page1), f"{len(page1)/len(rows)*100:.1f}%" if rows else "0%"],
        ["Page 2 (11–20) ⚡ Quick wins", len(page2), f"{len(page2)/len(rows)*100:.1f}%" if rows else "0%"],
        ["Not ranked ❌", len(not_ranked), f"{len(not_ranked)/len(rows)*100:.1f}%" if rows else "0%"],
    ]

    write_tab(spreadsheet, "Summary", summary_values, overwrite=True)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run(input_file: str):
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID env var not set")

    print(f"\n{'='*50}")
    print(f"  Loading: {input_file}")

    rows = load_csv(input_file)
    values = rows_to_values(rows)
    date_tab = datetime.now().strftime("%Y-%m-%d")

    print(f"  Rows: {len(rows)}")
    print(f"  Connecting to Google Sheets...")

    client = get_client()
    spreadsheet = client.open_by_key(sheet_id)

    print(f"  Sheet: {spreadsheet.title}")
    print(f"  Writing tabs...")

    # Write dated tab (permanent history)
    write_tab(spreadsheet, date_tab, values, overwrite=True)

    # Write/overwrite "Latest" tab
    write_tab(spreadsheet, "Latest", values, overwrite=True)

    # Write summary tab
    write_summary_tab(spreadsheet, rows)

    print(f"\n  🎉 Done! View at:")
    print(f"  https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV file from rank_checker.py")
    args = parser.parse_args()
    run(args.input)
