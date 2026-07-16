"""
Google Rank Checker — Americanflat Picture Frame SEO
Checks Google rankings for a keyword list via the DataForSEO SERP API.

(Migrated off ValueSerp 2026-07-16 — consolidated onto DataForSEO, which
already powers the SEO Intelligence pipeline. One vendor, one credential.
Note: DataForSEO reports positions slightly differently than ValueSerp, so
there is a one-time discontinuity in results_history at the switch date.)

Setup:
  pip install requests python-dotenv
  Add DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (the API password) to .env.

Usage:
  python rank_checker.py
  python rank_checker.py --domain americanflat.com --keywords keywords.csv --output results.csv
  python rank_checker.py --priority high      # only the high-priority tier
  python rank_checker.py --top 20             # only top 20 keywords by priority
"""

from __future__ import annotations

import os
import csv
import time
import json
import base64
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DEFAULT_DOMAIN   = "americanflat.com"
DEFAULT_KEYWORDS = "keywords.csv"
DEFAULT_OUTPUT   = f"rank_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
DFS_LOGIN        = os.getenv("DATAFORSEO_LOGIN", "")
DFS_PASSWORD     = os.getenv("DATAFORSEO_PASSWORD", "")
DFS_ENDPOINT     = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
LOCATION_CODE    = 2840  # United States
LANGUAGE_CODE    = "en"
RESULTS_PER_PAGE = 100   # SERP depth to scan
DELAY_SECONDS    = 0.0   # DataForSEO live has generous rate limits; no delay needed
MAX_POSITION     = 100   # report as "Not ranked" if beyond this

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def load_keywords(filepath: str) -> list[dict]:
    """Load keywords from CSV. Expected columns: keyword, category, priority"""
    keywords = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keywords.append({
                "keyword":  row.get("keyword", "").strip(),
                "category": row.get("category", "").strip(),
                "priority": row.get("priority", "medium").strip(),
            })
    return [k for k in keywords if k["keyword"]]


def _auth_header() -> str:
    return "Basic " + base64.b64encode(f"{DFS_LOGIN}:{DFS_PASSWORD}".encode()).decode()


def find_domain_position(items: list, domain: str) -> tuple[int | None, str | None]:
    """Return (rank_absolute, url) of the first organic result matching
    domain, or (None, None). Uses rank_absolute so the position counts all
    SERP elements the way a user sees them (matches how ValueSerp reported)."""
    for it in items:
        if it.get("type") != "organic":
            continue
        dom = (it.get("domain") or "").lower()
        if domain.lower() in dom:
            return it.get("rank_absolute"), it.get("url")
    return None, None


def check_ranking(keyword: str, domain: str, _unused: str = "") -> dict:
    """Query the DataForSEO SERP API for one keyword and return ranking data
    in the same shape the CSV writer expects."""
    payload = [{
        "keyword": keyword,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "depth": RESULTS_PER_PAGE,
    }]
    try:
        resp = requests.post(
            DFS_ENDPOINT, data=json.dumps(payload),
            headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status_code") != 20000:
            return {"position": None, "url": "", "in_featured": False, "total_results": "",
                    "error": f"{data.get('status_code')}: {data.get('status_message')}"}
        task = data["tasks"][0]
        if task.get("status_code") != 20000:
            return {"position": None, "url": "", "in_featured": False, "total_results": "",
                    "error": f"{task.get('status_code')}: {task.get('status_message')}"}
        result = (task.get("result") or [{}])[0]
        items = result.get("items") or []

        position, url = find_domain_position(items, domain)

        # Featured snippet ownership
        in_featured = any(
            it.get("type") == "featured_snippet" and domain.lower() in (it.get("domain") or "").lower()
            for it in items
        )

        return {
            "position":      position,
            "url":           url or "",
            "in_featured":   in_featured,
            "total_results": result.get("se_results_count", ""),
            "error":         "",
        }

    except requests.exceptions.HTTPError as e:
        code = getattr(resp, "status_code", "?")
        return {"position": None, "url": "", "in_featured": False, "total_results": "",
                "error": f"{code} {e}"}
    except Exception as e:
        return {"position": None, "url": "", "in_featured": False, "total_results": "", "error": str(e)}


def position_label(pos: int | None) -> str:
    if pos is None:
        return "Not ranked"
    if pos <= 3:
        return f"#{pos} 🏆 Top 3"
    if pos <= 10:
        return f"#{pos} ✅ Page 1"
    if pos <= 20:
        return f"#{pos} Page 2"
    return f"#{pos}"


def run(domain: str, keywords_file: str, output: str, top: int | None, dry_run: bool, priority: str = "all"):
    output_file = output
    if (not DFS_LOGIN or not DFS_PASSWORD) and not dry_run:
        print("\n❌  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set. Add them to .env or env vars.")
        print("    (Use the API password from your DataForSEO dashboard, not the login password.)\n")
        return

    keywords = load_keywords(keywords_file)
    # --priority filters the keyword list BEFORE --top is applied. The
    # GitHub Action uses this to run only one tier per cron schedule so
    # we stay under the SERP plan's monthly search budget. Passing
    # "all" (the default) preserves the original behaviour.
    if priority and priority.lower() != "all":
        wanted = priority.lower()
        before = len(keywords)
        keywords = [k for k in keywords if k["priority"].lower() == wanted]
        print(f"  Priority filter '{wanted}': {before} → {len(keywords)} keywords")
        if not keywords:
            print(f"\n⚠️  No keywords match priority='{wanted}'. Nothing to do.")
            return
    if top:
        # prioritize: high → medium → low
        order = {"high": 0, "medium": 1, "low": 2}
        keywords = sorted(keywords, key=lambda k: order.get(k["priority"], 1))[:top]

    print(f"\n{'='*60}")
    print(f"  Domain   : {domain}")
    print(f"  Priority : {priority}")
    print(f"  Keywords : {len(keywords)}")
    print(f"  Output   : {output_file}")
    print(f"  Dry run  : {dry_run}")
    print(f"{'='*60}\n")

    results = []
    for i, kw in enumerate(keywords, 1):
        keyword = kw["keyword"]
        print(f"[{i:>3}/{len(keywords)}] {keyword:<50}", end="", flush=True)

        if dry_run:
            row = {**kw, "position": None, "position_label": "DRY RUN", "url": "",
                   "in_featured": False, "total_results": "", "error": "", "checked_at": datetime.now().isoformat()}
        else:
            data = check_ranking(keyword, domain)
            row = {
                **kw,
                "position":       data["position"],
                "position_label": position_label(data["position"]),
                "url":            data["url"],
                "in_featured":    data["in_featured"],
                "total_results":  data["total_results"],
                "error":          data["error"],
                "checked_at":     datetime.now().isoformat(),
            }
            time.sleep(DELAY_SECONDS)

        results.append(row)
        print(row["position_label"])

    # ── Write CSV ──
    fieldnames = ["keyword", "category", "priority", "position", "position_label",
                  "url", "in_featured", "total_results", "error", "checked_at"]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # ── Summary ──
    ranked    = [r for r in results if r["position"] is not None]
    top3      = [r for r in ranked if r["position"] <= 3]
    page1     = [r for r in ranked if r["position"] <= 10]
    not_ranked = [r for r in results if r["position"] is None and not r["error"]]

    print(f"\n{'─'*60}")
    print(f"  ✅ Ranked  : {len(ranked)}/{len(results)}")
    print(f"  🏆 Top 3   : {len(top3)}")
    print(f"  📄 Page 1  : {len(page1)}")
    print(f"  ❌ Not ranked: {len(not_ranked)}")
    print(f"  💾 Saved to: {output_file}")
    print(f"{'─'*60}\n")

    # ── Top wins ──
    if top3:
        print("TOP 3 RANKINGS:")
        for r in sorted(top3, key=lambda x: x["position"]):
            print(f"  #{r['position']} — {r['keyword']}  ({r['category']})")

    # ── Opportunities (ranked 11-30) ──
    opportunities = [r for r in ranked if 10 < r["position"] <= 30]
    if opportunities:
        print("\nQUICK-WIN OPPORTUNITIES (positions 11–30):")
        for r in sorted(opportunities, key=lambda x: x["position"]):
            print(f"  #{r['position']} — {r['keyword']}  ({r['category']})")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google rank checker for picture frame keywords")
    parser.add_argument("--domain",   default=DEFAULT_DOMAIN,   help="Domain to track (default: americanflat.com)")
    parser.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="Keywords CSV file (default: keywords.csv)")
    parser.add_argument("--output",   default=DEFAULT_OUTPUT,   help="Output CSV file")
    parser.add_argument("--top",      type=int, default=None,   help="Only check top N keywords by priority")
    parser.add_argument("--priority", default="all",
                        choices=["all", "high", "medium", "low"],
                        help="Only check keywords with this priority tier (default: all). "
                             "Used by the GitHub Action to run tiers on different schedules.")
    parser.add_argument("--dry-run",  action="store_true",      help="Skip API calls, just test the pipeline")
    args = parser.parse_args()

    run(
        domain=args.domain,
        keywords_file=args.keywords,
        output=args.output,
        top=args.top,
        dry_run=args.dry_run,
        priority=args.priority,
    )
