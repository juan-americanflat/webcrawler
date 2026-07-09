"""
SEO Intelligence data pull — the "in-house Ahrefs" backend for americanflat.com.

Pulls organic-search intelligence from DataForSEO Labs and writes JSON files
that the amf-catalog-tools React app reads (same static-file pattern as the
rank checker's results_latest.csv). Run on a schedule via GitHub Actions.

Outputs (into seo_intel/data/):
  overview.json        — headline metrics: total ranked keywords, est. traffic,
                         position-band distribution, generated timestamp.
  ranked_keywords.json — every keyword americanflat.com ranks for (capped),
                         with position, volume, CPC, URL, traffic estimate.
  opportunities.json   — high-volume keywords where we rank page 2+ (11-30):
                         the near-term win list.
  competitors.json     — auto-discovered domains by keyword overlap, classified
                         marketplace vs direct competitor.
  keyword_gap.json     — for each pinned/direct competitor, keywords THEY rank
                         top-10 for that we don't rank (or rank poorly).

Auth: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD in .env (gitignored) locally, or
repo secrets in CI.

Usage:
  python seo_intel/pull_seo_intel.py                 # full pull, default caps
  python seo_intel/pull_seo_intel.py --ranked-limit 500
  python seo_intel/pull_seo_intel.py --only overview,competitors
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOGIN = os.getenv("DATAFORSEO_LOGIN")
PASSWORD = os.getenv("DATAFORSEO_PASSWORD")

TARGET = "americanflat.com"
LOCATION_CODE = 2840  # United States
LANGUAGE_CODE = "en"

BASE = "https://api.dataforseo.com/v3/dataforseo_labs/google"
SERP_BASE = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
DATA_DIR = Path(__file__).parent / "data"

# Domains that dominate keyword overlap but aren't meaningful "competitors"
# for a DTC frame brand — mega-marketplaces, search engines, general retail.
# Everything not in here is treated as a potential direct competitor and
# surfaced for the user to pin/remove. Kept deliberately conservative:
# borderline retailers (michaels, hobbylobby) are left as competitors since
# they DO compete on frame SERPs.
MARKETPLACES = {
    "amazon.com", "walmart.com", "etsy.com", "ebay.com", "target.com",
    "youtube.com", "google.com", "wikipedia.org", "pinterest.com",
    "homedepot.com", "lowes.com", "aliexpress.com", "temu.com",
    "overstock.com", "wayfair.com", "costco.com", "kohls.com",
    "bestbuy.com", "facebook.com", "instagram.com", "reddit.com",
    "tiktok.com", "shein.com", "macys.com",
}

# Frame-relevance filter for the keyword gap. Broad competitors (michaels,
# etc.) rank for tons of off-topic terms (legos, halloween costumes,
# periodic tables) — a "gap" is only actionable if the keyword is in
# americanflat's product space. A gap keyword must contain at least one of
# these tokens to be kept. Covers frames, wall art, posters, prints,
# canvas, matting, and the specialty/gifting angles in the catalog.
FRAME_TOKENS = (
    "frame", "framed", "poster", "gallery wall", "mat ", "matboard",
    "mat board", "matted", "wall art", "wall decor", "wall décor",
    "print", "canvas", "picture", "photo", "diploma", "certificate",
    "shadow box", "floating", "collage", "art prints",
)


def _frame_relevant(keyword: str) -> bool:
    k = (keyword or "").lower()
    return any(tok in k for tok in FRAME_TOKENS)

# Total spend guardrail (USD). The script tracks reported task costs and
# aborts before starting a new phase if this would be exceeded — cheap
# insurance against a runaway loop draining the DataForSEO balance.
MAX_SPEND = 1.00
_spend = 0.0


def _auth() -> str:
    return "Basic " + base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()


def _post(path: str, payload: list) -> dict:
    """POST to a Labs endpoint, track cost, return the first task's result."""
    global _spend
    if _spend >= MAX_SPEND:
        sys.exit(f"::error::Spend guardrail hit (${_spend:.4f} >= ${MAX_SPEND}). Aborting.")
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status_code") != 20000:
        sys.exit(f"::error::API error {data.get('status_code')}: {data.get('status_message')}")
    _spend += float(data.get("cost") or 0)
    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        sys.exit(f"::error::Task error {task.get('status_code')}: {task.get('status_message')}")
    results = task.get("result") or []
    return results[0] if results else {}


def _post_serp(keyword: str, depth: int = 10) -> list[dict]:
    """One live organic SERP for a keyword. Separate endpoint (SERP API, not
    Labs) so it has its own poster. ~$0.002/keyword."""
    global _spend
    if _spend >= MAX_SPEND:
        sys.exit(f"::error::Spend guardrail hit (${_spend:.4f}). Aborting.")
    payload = [{"keyword": keyword, "location_code": LOCATION_CODE,
                "language_code": LANGUAGE_CODE, "depth": depth}]
    req = urllib.request.Request(
        SERP_BASE, data=json.dumps(payload).encode(),
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status_code") != 20000:
        sys.exit(f"::error::SERP API error {data.get('status_code')}: {data.get('status_message')}")
    _spend += float(data.get("cost") or 0)
    task = data["tasks"][0]
    if task.get("status_code") != 20000 or not task.get("result"):
        return []
    rows = []
    for i in task["result"][0].get("items") or []:
        if i.get("type") != "organic":
            continue
        dom = i.get("domain") or ""
        rows.append({
            "position": i.get("rank_absolute"),
            "domain": dom,
            "url": i.get("url"),
            "title": i.get("title"),
            "is_americanflat": TARGET in dom,
        })
        if len(rows) >= depth:
            break
    return rows


def _band(rank: int | None) -> str:
    if not rank:
        return "unranked"
    if rank <= 3:
        return "top3"
    if rank <= 10:
        return "page1"
    if rank <= 20:
        return "page2"
    return "page3plus"


# -----------------------------------------------------------------------------
# Pulls
# -----------------------------------------------------------------------------
def pull_ranked_keywords(limit: int) -> list[dict]:
    """Every keyword the target ranks for, richest first (by traffic estimate)."""
    res = _post("ranked_keywords/live", [{
        "target": TARGET,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "limit": limit,
        "order_by": ["ranked_serp_element.serp_item.etv,desc"],
    }])
    rows = []
    for it in (res.get("items") or []):
        kd = it.get("keyword_data", {})
        ki = kd.get("keyword_info", {})
        se = it.get("ranked_serp_element", {}).get("serp_item", {})
        rows.append({
            "keyword": kd.get("keyword"),
            "position": se.get("rank_absolute"),
            "url": se.get("url"),
            "search_volume": ki.get("search_volume"),
            "cpc": ki.get("cpc"),
            "competition": ki.get("competition_level"),
            "traffic_estimate": round(se.get("etv") or 0, 1),
        })
    return rows


def pull_competitors(limit: int) -> list[dict]:
    res = _post("competitors_domain/live", [{
        "target": TARGET,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "limit": limit,
        "order_by": ["intersections,desc"],
    }])
    rows = []
    for it in (res.get("items") or []):
        dom = it.get("domain", "")
        if dom == TARGET:
            continue
        org = (it.get("full_domain_metrics") or {}).get("organic") or {}
        rows.append({
            "domain": dom,
            "shared_keywords": it.get("intersections"),
            "their_traffic": round(org.get("etv") or 0),
            "their_keywords": org.get("count"),
            "type": "marketplace" if dom in MARKETPLACES else "competitor",
        })
    return rows


def pull_keyword_gap(competitor: str, limit: int) -> list[dict]:
    """Keywords `competitor` ranks top-20 for where americanflat ranks worse
    or not at all. Uses Domain Intersection with intersecting_domains toggled
    so we get THEIR keyword + both positions."""
    # Over-fetch (limit*4, capped) because the frame-relevance + brand +
    # beats-us filters below discard a lot for broad competitors.
    res = _post("domain_intersection/live", [{
        "target1": competitor,
        "target2": TARGET,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "intersections": False,   # keywords where target1 ranks; target2 may not
        "limit": min(limit * 4, 1000),
        "order_by": ["keyword_data.keyword_info.search_volume,desc"],
    }])
    # Exclude the competitor's own brand token (e.g. "michaels", "arttoframe")
    brand = competitor.split(".")[0].replace("-", " ")
    rows = []
    for it in (res.get("items") or []):
        kd = it.get("keyword_data", {})
        kw = kd.get("keyword") or ""
        ki = kd.get("keyword_info", {})
        first = (it.get("first_domain_serp_element") or {})
        second = (it.get("second_domain_serp_element") or {})
        their = first.get("rank_absolute")
        ours = second.get("rank_absolute")
        # A gap = they rank top-20, they beat us (or we're absent), the term
        # is in our product space, and it isn't their brand.
        if not (their and their <= 20):
            continue
        if ours is not None and ours <= their:
            continue
        if not _frame_relevant(kw) or brand in kw.lower():
            continue
        rows.append({
            "keyword": kw,
            "search_volume": ki.get("search_volume"),
            "their_position": their,
            "our_position": ours,
            "their_url": first.get("url"),
        })
        if len(rows) >= limit:
            break
    return rows


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------
def build_overview(ranked: list[dict]) -> dict:
    bands = {"top3": 0, "page1": 0, "page2": 0, "page3plus": 0}
    total_traffic = 0.0
    for r in ranked:
        bands[_band(r["position"])] = bands.get(_band(r["position"]), 0) + 1
        total_traffic += r.get("traffic_estimate") or 0
    return {
        "target": TARGET,
        "total_ranked_keywords": len(ranked),
        "estimated_monthly_traffic": round(total_traffic),
        "position_bands": bands,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_opportunities(ranked: list[dict]) -> list[dict]:
    """High-volume keywords sitting on page 2-3 (positions 11-30) — the
    'push these over the line' list. Sorted by volume."""
    opps = [
        r for r in ranked
        if r["position"] and 11 <= r["position"] <= 30 and (r["search_volume"] or 0) >= 200
    ]
    opps.sort(key=lambda r: r["search_volume"] or 0, reverse=True)
    return opps


def write(name: str, payload) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    n = len(payload) if isinstance(payload, list) else "—"
    print(f"  wrote {path.relative_to(DATA_DIR.parent.parent)} ({n} rows)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ranked-limit", type=int, default=1000)
    ap.add_argument("--competitors-limit", type=int, default=25)
    ap.add_argument("--gap-limit", type=int, default=100)
    ap.add_argument("--gap-competitors", type=int, default=3,
                    help="How many top DIRECT competitors to compute a gap for.")
    ap.add_argument("--serp-limit", type=int, default=50,
                    help="How many curated keywords to pre-pull live SERPs for.")
    ap.add_argument("--only", default="",
                    help="Comma list: overview,ranked,opportunities,competitors,gap,serp")
    args = ap.parse_args()

    if not LOGIN or not PASSWORD:
        sys.exit("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    want = lambda k: (not only) or k in only

    print(f"SEO Intelligence pull for {TARGET} (US/en)")

    ranked = []
    if want("ranked") or want("overview") or want("opportunities"):
        print("- ranked keywords…")
        ranked = pull_ranked_keywords(args.ranked_limit)
        if want("ranked"):
            write("ranked_keywords", ranked)
        if want("overview"):
            write("overview", build_overview(ranked))
        if want("opportunities"):
            write("opportunities", build_opportunities(ranked))

    competitors = []
    if want("competitors") or want("gap"):
        print("- competitors…")
        competitors = pull_competitors(args.competitors_limit)
        if want("competitors"):
            write("competitors", competitors)

    if want("gap"):
        # Default gap targets = true peers: classified as competitor AND not a
        # giant broad retailer. A frame DTC's real peers sit well under ~10M
        # monthly organic visits (arttoframe ~1.1M, pictureframes ~1.1M),
        # whereas michaels (~25M) ranks for frames but competes as a
        # general craft retailer — its "gap" is mostly off-topic. Fall back
        # to all competitor-type domains if the peer filter empties out.
        PEER_TRAFFIC_CEILING = 10_000_000
        comp = [c for c in competitors if c["type"] == "competitor"]
        peers = [c for c in comp if (c["their_traffic"] or 0) < PEER_TRAFFIC_CEILING]
        direct = (peers or comp)[:args.gap_competitors]
        print(f"  gap targets: {', '.join(c['domain'] for c in direct) or '(none)'}")
        gap = {}
        for c in direct:
            print(f"- keyword gap vs {c['domain']}…")
            gap[c["domain"]] = pull_keyword_gap(c["domain"], args.gap_limit)
        write("keyword_gap", gap)

    if want("serp"):
        # Curated set = top opportunities (where we're close and want to see
        # who's ahead) + our top ranked winners (who's around us). Falls back
        # to reading the committed JSON if this run didn't pull ranked.
        if not ranked and (DATA_DIR / "ranked_keywords.json").exists():
            ranked = json.load(open(DATA_DIR / "ranked_keywords.json"))
        opps = build_opportunities(ranked) if ranked else []
        top_opp = [o["keyword"] for o in opps[:args.serp_limit // 2]]
        top_won = [r["keyword"] for r in
                   sorted(ranked, key=lambda r: r.get("traffic_estimate") or 0, reverse=True)]
        seen, curated = set(), []
        for kw in top_opp + top_won:
            k = (kw or "").strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                curated.append(k)
            if len(curated) >= args.serp_limit:
                break
        print(f"- SERP for {len(curated)} curated keywords…")
        serp = {kw: _post_serp(kw) for kw in curated}
        write("serp", serp)

    print(f"\nDone. DataForSEO spend this run: ${_spend:.4f}")


if __name__ == "__main__":
    main()
