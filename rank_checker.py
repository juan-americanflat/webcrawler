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
DFS_BASE         = "https://api.dataforseo.com/v3/serp/google/organic"
LOCATION_CODE    = 2840  # United States
LANGUAGE_CODE    = "en"
RESULTS_PER_PAGE = 100   # SERP depth to scan
MAX_POSITION     = 100   # report as "Not ranked" if beyond this

# ── Why the STANDARD (async) queue, not live ────────────────────────────────
# DataForSEO prices SERPs per 10 results of depth. At depth=100 the LIVE
# endpoint costs $0.02/keyword ($0.002 only at depth=10 — a 10x pricing trap
# we hit in July 2026: ~$150/mo). The standard task_post queue returns the
# SAME depth-100 advanced payload for $0.006/keyword; results just arrive a
# few minutes later, which a scheduled cron doesn't care about (~$49/mo).
POST_BATCH_SIZE  = 100    # task_post accepts up to 100 tasks per request
POLL_INTERVAL    = 20     # seconds between tasks_ready polls
COLLECT_TIMEOUT  = 45*60  # give the queue up to 45 min before flagging timeouts

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


def _parse_result(result: dict, domain: str) -> dict:
    """Turn a task result payload (same shape for live and task_get) into the
    row fields the CSV writer expects."""
    items = result.get("items") or []
    position, url = find_domain_position(items, domain)
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


def _err(msg: str) -> dict:
    return {"position": None, "url": "", "in_featured": False, "total_results": "", "error": msg}


def post_tasks(keywords: list[str]) -> tuple[dict, dict]:
    """POST all keywords to the standard queue in batches.

    Returns (id_map, errors): id_map is {task_id: keyword} for successfully
    queued tasks; errors is {keyword: error_row} for tasks the API rejected
    at post time. Total post cost is printed (cost is charged at post).
    """
    id_map: dict[str, str] = {}
    errors: dict[str, dict] = {}
    total_cost = 0.0
    for i in range(0, len(keywords), POST_BATCH_SIZE):
        batch = keywords[i:i + POST_BATCH_SIZE]
        payload = [{
            "keyword": kw,
            "location_code": LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "depth": RESULTS_PER_PAGE,
        } for kw in batch]
        try:
            resp = requests.post(
                f"{DFS_BASE}/task_post", data=json.dumps(payload),
                headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            for kw in batch:
                errors[kw] = _err(f"task_post failed: {e}")
            continue
        total_cost += float(data.get("cost") or 0)
        for task in data.get("tasks") or []:
            kw = (task.get("data") or {}).get("keyword", "")
            # 20100 = "Task Created"
            if task.get("status_code") == 20100 and task.get("id"):
                id_map[task["id"]] = kw
            else:
                errors[kw] = _err(f"post {task.get('status_code')}: {task.get('status_message')}")
        print(f"  posted {min(i+POST_BATCH_SIZE, len(keywords))}/{len(keywords)}", flush=True)
    print(f"  queue cost: ${total_cost:.2f}", flush=True)
    return id_map, errors


def collect_results(id_map: dict, domain: str) -> dict:
    """Poll tasks_ready and task_get until every queued task is retrieved or
    COLLECT_TIMEOUT passes. Returns {keyword: row_fields}. Tasks that never
    complete are returned as error rows (so the workflow safeguard can see
    a partial/failed run honestly)."""
    results: dict[str, dict] = {}
    pending = dict(id_map)  # id -> keyword
    retry_counts: dict[str, int] = {}
    deadline = time.time() + COLLECT_TIMEOUT
    while pending and time.time() < deadline:
        # tasks_ready lists completed-but-unretrieved tasks account-wide;
        # we only act on ids we own and ignore anything else.
        try:
            resp = requests.get(
                f"{DFS_BASE}/tasks_ready",
                headers={"Authorization": _auth_header()},
                timeout=60,
            )
            ready = resp.json()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        ready_ids = []
        for t in (ready.get("tasks") or []):
            for r in (t.get("result") or []):
                tid = r.get("id")
                if tid in pending:
                    ready_ids.append(tid)
        # Tasks whose task_get previously failed may no longer appear in
        # tasks_ready (the server can mark them retrieved even when our read
        # timed out), so retry them directly — task_get works either way.
        ready_ids.extend(tid for tid in list(retry_counts)
                         if tid in pending and tid not in ready_ids)
        for tid in ready_ids:
            kw = pending[tid]
            try:
                resp = requests.get(
                    f"{DFS_BASE}/task_get/advanced/{tid}",
                    headers={"Authorization": _auth_header()},
                    timeout=60,
                )
                data = resp.json()
                task = (data.get("tasks") or [{}])[0]
                if task.get("status_code") == 20000 and task.get("result"):
                    results[kw] = _parse_result(task["result"][0], domain)
                else:
                    results[kw] = _err(f"task_get {task.get('status_code')}: {task.get('status_message')}")
                del pending[tid]
            except Exception as e:
                # Transient fetch failure (e.g. read timeout): keep the task
                # in `pending` and retry on a later poll — the result is
                # already paid for and sitting in the queue. Give up after
                # 3 attempts so one poisoned task can't stall the run.
                attempts = retry_counts.get(tid, 0) + 1
                retry_counts[tid] = attempts
                if attempts >= 3:
                    results[kw] = _err(f"task_get failed after {attempts} attempts: {e}")
                    del pending[tid]
        if pending:
            done = len(id_map) - len(pending)
            print(f"  collected {done}/{len(id_map)}…", flush=True)
            time.sleep(POLL_INTERVAL)
    for tid, kw in pending.items():
        results[kw] = _err(f"timeout: task {tid} not ready within {COLLECT_TIMEOUT//60} min")
    return results


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

    if dry_run:
        results = [{**kw, "position": None, "position_label": "DRY RUN", "url": "",
                    "in_featured": False, "total_results": "", "error": "",
                    "checked_at": datetime.now().isoformat()}
                   for kw in keywords]
    else:
        # Standard queue: post everything up front (cost charged here), then
        # poll until the queue has processed all tasks (typically minutes).
        print("Posting to DataForSEO standard queue…", flush=True)
        id_map, post_errors = post_tasks([k["keyword"] for k in keywords])
        print(f"Queued {len(id_map)} tasks; waiting for results…", flush=True)
        by_kw = collect_results(id_map, domain)
        by_kw.update(post_errors)
        # Assemble rows in keywords.csv order so the output CSV is stable.
        results = []
        for kw in keywords:
            data = by_kw.get(kw["keyword"], _err("no result returned"))
            results.append({
                **kw,
                "position":       data["position"],
                "position_label": position_label(data["position"]),
                "url":            data["url"],
                "in_featured":    data["in_featured"],
                "total_results":  data["total_results"],
                "error":          data["error"],
                "checked_at":     datetime.now().isoformat(),
            })

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
