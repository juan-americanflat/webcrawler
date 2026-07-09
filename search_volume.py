"""
Pull Google search volume for the tracked keyword list from DataForSEO and
join it with current Google rankings.

Uses the Google Ads Search Volume endpoint
(keywords_data/google_ads/search_volume/live) — priced per task (~$0.05),
not per keyword, so all keywords go in a single request.

Output: search_volume_export.csv with, per keyword:
  keyword, category, priority, search_volume (avg monthly, US),
  competition, competition_index, cpc, low_bid, high_bid, current_rank

Setup: put DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in .env (gitignored).
Usage:  python search_volume.py
"""

import base64
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

LOGIN = os.getenv("DATAFORSEO_LOGIN")
PASSWORD = os.getenv("DATAFORSEO_PASSWORD")
KEYWORDS_FILE = "keywords.csv"
RANK_FILE = "results_latest.csv"
OUTPUT_FILE = "search_volume_export.csv"

# United States / English — matches the rank tracker's gl=us, hl=en.
LOCATION_CODE = 2840
LANGUAGE_CODE = "en"

ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"


def _auth_header() -> str:
    raw = f"{LOGIN}:{PASSWORD}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def load_keywords() -> list[dict]:
    with open(KEYWORDS_FILE, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("keyword", "").strip()]


def load_ranks() -> dict[str, str]:
    """Map keyword -> current position (str) from results_latest.csv."""
    ranks: dict[str, str] = {}
    if not os.path.exists(RANK_FILE):
        return ranks
    with open(RANK_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            kw = (r.get("keyword") or "").strip().lower()
            pos = (r.get("position") or "").strip()
            if kw:
                ranks[kw] = pos
    return ranks


def fetch_volume(keywords: list[str]) -> dict[str, dict]:
    """POST all keywords in one task; return {keyword_lower: metrics}."""
    payload = [{
        "keywords": keywords,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
    }]
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())

    if data.get("status_code") != 20000:
        sys.exit(f"API error: {data.get('status_code')} {data.get('status_message')}")

    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        sys.exit(f"Task error: {task.get('status_code')} {task.get('status_message')}")

    out: dict[str, dict] = {}
    for item in (task.get("result") or []):
        kw = (item.get("keyword") or "").strip().lower()
        if kw:
            out[kw] = item
    return out


def main() -> None:
    if not LOGIN or not PASSWORD:
        sys.exit("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env")

    kw_rows = load_keywords()
    keywords = [r["keyword"].strip() for r in kw_rows]
    print(f"Loaded {len(keywords)} keywords. Requesting search volume "
          f"(US/English) in one task…")

    volume = fetch_volume(keywords)
    ranks = load_ranks()
    print(f"Got volume for {len(volume)} keywords; "
          f"joined against {len(ranks)} ranked rows.")

    out_rows = []
    for r in kw_rows:
        kw = r["keyword"].strip()
        v = volume.get(kw.lower(), {})
        pos = ranks.get(kw.lower(), "")
        out_rows.append({
            "keyword": kw,
            "category": r.get("category", ""),
            "priority": r.get("priority", ""),
            "search_volume": v.get("search_volume") if v.get("search_volume") is not None else "",
            "competition": v.get("competition") or "",
            "competition_index": v.get("competition_index") if v.get("competition_index") is not None else "",
            "cpc": v.get("cpc") if v.get("cpc") is not None else "",
            "low_top_of_page_bid": v.get("low_top_of_page_bid") if v.get("low_top_of_page_bid") is not None else "",
            "high_top_of_page_bid": v.get("high_top_of_page_bid") if v.get("high_top_of_page_bid") is not None else "",
            "current_rank": pos,
        })

    # Sort by search volume desc (blanks last) so the export opens on the
    # highest-opportunity terms.
    out_rows.sort(key=lambda x: (x["search_volume"] == "", -(x["search_volume"] or 0)))

    fieldnames = ["keyword", "category", "priority", "search_volume",
                  "competition", "competition_index", "cpc",
                  "low_top_of_page_bid", "high_top_of_page_bid", "current_rank"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    with_vol = sum(1 for r in out_rows if r["search_volume"] != "")
    print(f"\nWrote {OUTPUT_FILE}: {len(out_rows)} keywords, "
          f"{with_vol} with a volume figure.")
    print(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
