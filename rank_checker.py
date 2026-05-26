"""
Google Rank Checker — Americanflat Picture Frame SEO
Uses SerpAPI to check Google rankings for a keyword list.

Setup:
  pip install requests python-dotenv
  Add SERPAPI_KEY=your_key to a .env file (or set as env var)
  Get a free API key at https://serpapi.com (100 free searches/month)

Usage:
  python rank_checker.py
  python rank_checker.py --domain americanflat.com --keywords keywords.csv --output results.csv
  python rank_checker.py --top 20  # only check top 20 keywords by priority
"""

import os
import csv
import time
import json
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DEFAULT_DOMAIN   = "americanflat.com"
DEFAULT_KEYWORDS = "keywords.csv"
DEFAULT_OUTPUT   = f"rank_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
VALUESERP_KEY    = os.getenv("VALUESERP_KEY", "")
RESULTS_PER_PAGE = 100   # positions to scan (max 100 per ValueSerp call)
DELAY_SECONDS    = 1.2   # polite delay between API calls
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


def find_domain_position(organic_results: list, domain: str) -> tuple[int | None, str | None]:
    """Return (position, url) of first result matching domain, or (None, None)."""
    for result in organic_results:
        link = result.get("link", "")
        if domain.lower() in link.lower():
            return result.get("position"), link
    return None, None


def check_ranking(keyword: str, domain: str, api_key: str) -> dict:
    """Call ValueSerp for a single keyword and return ranking data."""
    params = {
        "api_key":  api_key,
        "q":        keyword,
        "num":      RESULTS_PER_PAGE,
        "gl":       "us",
        "hl":       "en",
        "location": "United States",
        "output":   "json",
    }
    try:
        resp = requests.get("https://api.valueserp.com/search", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        organic = data.get("organic_results", [])
        position, url = find_domain_position(organic, domain)

        featured = data.get("answer_box", {})
        featured_url = featured.get("link", "")
        in_featured = domain.lower() in featured_url.lower() if featured_url else False

        return {
            "position":      position,
            "url":           url or "",
            "in_featured":   in_featured,
            "total_results": data.get("search_information", {}).get("total_results", ""),
            "error":         "",
        }

    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            return {"position": None, "url": "", "in_featured": False, "total_results": "", "error": "Invalid API key"}
        return {"position": None, "url": "", "in_featured": False, "total_results": "", "error": str(e)}
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
    if not VALUESERP_KEY and not dry_run:
        print("\n❌  VALUESERP_KEY not set. Add it to a .env file or set as environment variable.")
        print("    Get a free trial key at https://valueserp.com (~$5/mo for 5,000 searches)\n")
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
            data = check_ranking(keyword, domain, VALUESERP_KEY)
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
