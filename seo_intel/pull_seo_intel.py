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
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
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
BL_BASE = "https://api.dataforseo.com/v3/backlinks"
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

# Total keywords the target ranks for, per the API (may exceed the pulled
# cap). Captured during pull_ranked_keywords, surfaced in overview.json.
_ranked_total = None


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


def _post_bl(path: str, payload: list) -> dict:
    """POST to a Backlinks API endpoint (separate base from Labs). Tracks
    cost, returns the first task's result dict."""
    global _spend
    if _spend >= MAX_SPEND:
        sys.exit(f"::error::Spend guardrail hit (${_spend:.4f}). Aborting.")
    req = urllib.request.Request(
        f"{BL_BASE}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status_code") != 20000:
        sys.exit(f"::error::Backlinks API error {data.get('status_code')}: {data.get('status_message')}")
    _spend += float(data.get("cost") or 0)
    task = data["tasks"][0]
    if task.get("status_code") != 20000:
        sys.exit(f"::error::Backlinks task error {task.get('status_code')}: {task.get('status_message')}")
    results = task.get("result") or []
    return results[0] if results else {}


def pull_backlinks(peers: list[str], ref_limit: int, anchor_limit: int) -> dict:
    """Backlink intelligence: a DR-style comparison of us vs peers, our top
    referring domains, and our anchor-text distribution.

    DataForSEO 'rank' is a 0-1000 domain authority score (higher = stronger),
    the direct analogue of Ahrefs Domain Rating.
    """
    def _summary(domain: str) -> dict:
        r = _post_bl("summary/live", [{"target": domain}])
        return {
            "domain": domain,
            "rank": r.get("rank"),
            "backlinks": r.get("backlinks"),
            "referring_domains": r.get("referring_domains"),
            "referring_main_domains": r.get("referring_main_domains"),
            "is_target": domain == TARGET,
        }

    summary = [_summary(TARGET)] + [_summary(d) for d in peers]

    # Over-fetch, then drop self-referrers (our own Shopify subdomain) and
    # raw-IP "domains" which aren't meaningful external links. Ordered by
    # rank so the most authoritative linking domains lead.
    rd = _post_bl("referring_domains/live", [{
        "target": TARGET, "limit": ref_limit * 2, "order_by": ["rank,desc"],
    }])
    brand = TARGET.split(".")[0]
    _ip_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    referring_domains = []
    for it in (rd.get("items") or []):
        dom = it.get("domain") or ""
        if brand in dom or "myshopify.com" in dom or _ip_re.match(dom):
            continue
        referring_domains.append({
            "domain": dom,
            "rank": it.get("rank"),
            "backlinks": it.get("backlinks"),
            "first_seen": (it.get("first_seen") or "")[:10],
        })
        if len(referring_domains) >= ref_limit:
            break

    an = _post_bl("anchors/live", [{
        "target": TARGET, "limit": anchor_limit, "order_by": ["backlinks,desc"],
    }])
    anchors = [{
        "anchor": (it.get("anchor") or "").strip() or "(empty / branded)",
        "backlinks": it.get("backlinks"),
        "referring_domains": it.get("referring_domains"),
    } for it in (an.get("items") or [])]

    # ── New / lost link velocity ───────────────────────────────────────
    # Monthly time series of new vs lost referring domains + backlinks.
    date_from = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    hist = _post_bl("history/live", [{"target": TARGET, "date_from": date_from}])
    history = [{
        "date": (it.get("date") or "")[:10],
        "referring_domains": it.get("referring_domains"),
        "new_referring_domains": it.get("new_referring_domains"),
        "lost_referring_domains": it.get("lost_referring_domains"),
        "new_backlinks": it.get("new_backlinks"),
        "lost_backlinks": it.get("lost_backlinks"),
    } for it in (hist.get("items") or [])]

    # Recently GAINED referring domains: most-recent first_seen, still active.
    g = _post_bl("referring_domains/live", [{
        "target": TARGET, "limit": 30, "order_by": ["first_seen,desc"],
    }])
    gained = [{
        "domain": it.get("domain"), "rank": it.get("rank"),
        "backlinks": it.get("backlinks"), "first_seen": (it.get("first_seen") or "")[:10],
    } for it in (g.get("items") or []) if not it.get("lost_date")][:25]

    # Recently LOST referring domains: the endpoint defaults to live links,
    # so backlinks_status_type="lost" is required to surface dropped ones.
    lo = _post_bl("referring_domains/live", [{
        "target": TARGET, "limit": 25, "backlinks_status_type": "lost",
        "order_by": ["lost_date,desc"],
    }])
    lost = [{
        "domain": it.get("domain"), "rank": it.get("rank"),
        "backlinks": it.get("backlinks"), "lost_date": (it.get("lost_date") or "")[:10],
    } for it in (lo.get("items") or []) if it.get("lost_date")][:25]

    return {
        "summary": summary,
        "referring_domains": referring_domains,
        "anchors": anchors,
        "history": history,
        "gained": gained,
        "lost": lost,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
    """Top `limit` keywords the target ranks for, richest first (by traffic
    estimate). Also records the API's total_count (full footprint) globally
    so the overview can report it even though we only store the top slice."""
    global _ranked_total
    res = _post("ranked_keywords/live", [{
        "target": TARGET,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "limit": limit,
        "order_by": ["ranked_serp_element.serp_item.etv,desc"],
    }])
    _ranked_total = res.get("total_count")
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


_SIZE_RE = re.compile(r"(\d{1,2})\s*[x×]\s*(\d{1,2})")
# Non-size page topics: canonical key -> substrings that signal it.
_CATEGORY_TOPICS = {
    "gallery wall": ["gallery wall", "gallery-wall"],
    "diploma/certificate": ["diploma", "certificate", "document frame"],
    "poster": ["poster"],
    "movie poster": ["movie poster", "one sheet", "one-sheet"],
    "shadow box": ["shadow box", "shadowbox"],
    "collage": ["collage"],
    "floating": ["floating", "float frame"],
    "canvas": ["canvas"],
    "matted / with mat": ["with mat", "matted", "matboard", "mat board"],
    "wood": ["wood", "oak", "walnut", "bamboo", "mahogany"],
    "metal": ["metal", "aluminum", "aluminium"],
    "digital": ["digital frame", "digital picture"],
}


def _topic_keys(text: str) -> set[str]:
    """Normalize a page URL (or keyword) into topic keys: canonical frame
    sizes (e.g. '8x10') plus category topics. Used to compare which page
    topics a competitor covers vs. us."""
    s = (text or "").lower().replace("_", " ").replace("-", " ")
    keys = set()
    for m in _SIZE_RE.finditer(s):
        keys.add(f"{int(m.group(1))}x{int(m.group(2))}")
    for canon, toks in _CATEGORY_TOPICS.items():
        if any(t in s for t in toks):
            keys.add(canon)
    return keys


def pull_relevant_pages(domain: str, limit: int) -> list[dict]:
    """Top landing pages of a domain by organic traffic estimate."""
    res = _post("relevant_pages/live", [{
        "target": domain,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "limit": limit,
        "order_by": ["metrics.organic.etv,desc"],
    }])
    rows = []
    for it in (res.get("items") or []):
        m = (it.get("metrics") or {}).get("organic") or {}
        url = it.get("page_address") or ""
        rows.append({
            "url": url,
            "keywords": m.get("count") or 0,
            "traffic_estimate": round(m.get("etv") or 0),
            "topics": sorted(_topic_keys(url)),
        })
    return rows


def build_page_gaps(our_pages: list[dict], comp_pages: dict[str, list[dict]]) -> dict:
    """Topics a competitor has a ranking page for that we have no page for.

    Aggregates by topic key: which competitors cover it, the best (max)
    competitor page traffic for it, and an example competitor URL. Sorted
    by that traffic so the biggest missing pages surface first.
    """
    # Everything we already have a page for (order-sensitive keys).
    ours = set()
    for p in our_pages:
        ours.update(p["topics"])
    # Order-independent size set — "18x24" and "24x18" collapse to "18x24" —
    # so we can tell a truly-novel size from a landscape/portrait variant of
    # one we already cover.
    def _canon_size(key):
        m = _SIZE_RE.fullmatch(key.replace(" ", ""))
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        return f"{min(a, b)}x{max(a, b)}"
    our_canon_sizes = {c for c in (_canon_size(k) for k in ours) if c}

    gaps: dict[str, dict] = {}
    for comp, pages in comp_pages.items():
        for p in pages:
            for topic in p["topics"]:
                if topic in ours:
                    continue
                canon = _canon_size(topic)
                # Classify: is this a novel topic, or just the other
                # orientation of a size we already have a page for?
                if canon and canon in our_canon_sizes:
                    kind = "orientation-variant"   # we have this size, other orientation
                elif canon:
                    kind = "novel-size"            # we have no page for this size at all
                else:
                    kind = "novel-topic"           # non-size topic (category)
                g = gaps.setdefault(topic, {
                    "topic": topic, "kind": kind, "competitors": set(),
                    "best_traffic": 0, "example_url": "", "example_competitor": "",
                })
                g["competitors"].add(comp)
                if p["traffic_estimate"] > g["best_traffic"]:
                    g["best_traffic"] = p["traffic_estimate"]
                    g["example_url"] = p["url"]
                    g["example_competitor"] = comp

    out = []
    for g in gaps.values():
        g["competitors"] = sorted(g["competitors"])
        out.append(g)
    out.sort(key=lambda g: g["best_traffic"], reverse=True)
    # Persist raw page lists too, so the gap model can be reprocessed
    # without re-hitting the API.
    return {
        "our_page_count": len(our_pages),
        "gaps": out,
        "_our_pages": our_pages,
        "_competitor_pages": comp_pages,
    }


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
        # Full footprint from the API; falls back to pulled count if absent.
        "total_ranked_keywords": _ranked_total or len(ranked),
        # Position bands + traffic are computed over the pulled top slice, so
        # label it honestly for the frontend.
        "analyzed_keywords": len(ranked),
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


# Intent weighting for the priority score — transactional/commercial terms
# are worth more to a DTC store than informational/navigational ones.
_INTENT_WEIGHT = {
    "transactional": 1.0, "commercial": 0.9,
    "navigational": 0.6, "informational": 0.5,
}


def pull_keyword_metrics(keywords: list[str]) -> dict[str, dict]:
    """Keyword difficulty (0-100) + search intent for a keyword list, via two
    bulk Labs endpoints. Returns {keyword_lower: {kd, intent, intent_prob}}."""
    out: dict[str, dict] = {kw.lower(): {} for kw in keywords}
    if not keywords:
        return out
    kd = _post("bulk_keyword_difficulty/live", [{
        "keywords": keywords, "location_code": LOCATION_CODE, "language_code": LANGUAGE_CODE,
    }])
    for it in (kd.get("items") or []):
        k = (it.get("keyword") or "").lower()
        if k in out:
            out[k]["kd"] = it.get("keyword_difficulty")
    si = _post("search_intent/live", [{
        "keywords": keywords, "language_code": LANGUAGE_CODE,
    }])
    for it in (si.get("items") or []):
        k = (it.get("keyword") or "").lower()
        intent = (it.get("keyword_intent") or {})
        if k in out:
            out[k]["intent"] = intent.get("label")
            out[k]["intent_prob"] = intent.get("probability")
    return out


def enrich_opportunities(opps: list[dict], metrics: dict[str, dict]) -> list[dict]:
    """Add KD, intent, traffic value ($/mo) and a priority score to each
    opportunity, then sort by priority.

    priority_score = volume × winnability × intent_weight × proximity
      winnability = (100 - KD) / 100   → low-difficulty terms score higher
      intent_weight                     → commercial/transactional worth more
      proximity   = (31 - position)/20  → closer to page 1 (pos 11) worth more
    It reads as "intent-weighted winnable volume, favouring near-page-1
    terms" — the keywords worth working first.
    """
    for o in opps:
        m = metrics.get((o["keyword"] or "").lower(), {})
        kd = m.get("kd")
        o["keyword_difficulty"] = kd
        o["intent"] = m.get("intent")
        vol = o.get("search_volume") or 0
        cpc = o.get("cpc") or 0
        o["traffic_value"] = round(vol * cpc)
        winnable = (100 - kd) / 100 if isinstance(kd, (int, float)) else 0.6
        iw = _INTENT_WEIGHT.get(o.get("intent"), 0.6)
        pos = o.get("position") or 30
        proximity = max(0.05, (31 - pos) / 20)
        o["priority_score"] = round(vol * winnable * iw * proximity)
    opps.sort(key=lambda r: r.get("priority_score") or 0, reverse=True)
    return opps


def build_movers(window_days: int = 7, top_n: int = 25) -> dict:
    """Rank changes over ~`window_days`, diffed from the ValueSerp daily
    snapshots in ../results_history. No API calls.

    Per keyword: compare its latest position against the reading closest to
    `window_days` ago. Errored readings (e.g. the 402 credit-out period) are
    ignored so they don't masquerade as lost rankings. Classifies:
      improved  — moved up (lower position number)
      declined  — moved down
      new       — was checked-but-unranked, now ranks
      lost      — was ranking, now checked-but-unranked
    """
    import csv as _csv
    hist_dir = Path(__file__).parent.parent / "results_history"
    snaps = sorted(hist_dir.glob("results_*.csv"))
    if len(snaps) < 2:
        return {"summary": {}, "improved": [], "declined": [], "new": [], "lost": [],
                "note": "not enough history yet", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def _pos(val):
        s = str(val or "").strip()
        if not s:
            return None            # checked but not ranked
        try:
            n = int(float(s))
            return n if n > 0 else None
        except ValueError:
            return None

    # keyword -> list of (date_str, position_or_None), only non-errored rows
    from datetime import date as _date
    series: dict[str, list] = {}
    meta: dict[str, dict] = {}
    for snap in snaps:
        dt = snap.stem.replace("results_", "")
        for r in _csv.DictReader(open(snap, encoding="utf-8")):
            if (r.get("error") or "").strip():
                continue
            kw = (r.get("keyword") or "").strip()
            if not kw:
                continue
            series.setdefault(kw, []).append((dt, _pos(r.get("position"))))
            meta[kw] = {"category": r.get("category", ""), "priority": r.get("priority", "")}

    def _to_date(s):
        y, m, d = (int(x) for x in s.split("-"))
        return _date(y, m, d)

    improved, declined, new, lost = [], [], [], []
    for kw, readings in series.items():
        readings.sort(key=lambda x: x[0])
        cur_date, cur_pos = readings[-1]
        target = _to_date(cur_date) - timedelta(days=window_days)
        # nearest prior reading within [window-3, window+7] days before current
        prior = None
        best_gap = None
        for dt, pos in readings[:-1]:
            gap = abs((_to_date(dt) - target).days)
            if _to_date(dt) < _to_date(cur_date) and (best_gap is None or gap < best_gap):
                best_gap, prior = gap, (dt, pos)
        if prior is None:
            continue
        _, prior_pos = prior
        row = {"keyword": kw, "current": cur_pos, "prior": prior_pos,
               "category": meta[kw]["category"], "priority": meta[kw]["priority"]}
        if prior_pos is None and cur_pos is not None:
            new.append(row)
        elif prior_pos is not None and cur_pos is None:
            lost.append(row)
        elif prior_pos is not None and cur_pos is not None:
            delta = prior_pos - cur_pos      # >0 = improved (moved up)
            if delta == 0:
                continue
            row["delta"] = delta
            (improved if delta > 0 else declined).append(row)

    improved.sort(key=lambda r: r["delta"], reverse=True)
    declined.sort(key=lambda r: r["delta"])
    new.sort(key=lambda r: r["current"])
    lost.sort(key=lambda r: r["prior"])

    return {
        "window_days": window_days,
        "summary": {"improved": len(improved), "declined": len(declined),
                    "new": len(new), "lost": len(lost)},
        "improved": improved[:top_n],
        "declined": declined[:top_n],
        "new": new[:top_n],
        "lost": lost[:top_n],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
                    help="Comma list: movers,overview,ranked,opportunities,competitors,gap,pagegaps,backlinks,serp")
    args = ap.parse_args()

    if not LOGIN or not PASSWORD:
        sys.exit("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    want = lambda k: (not only) or k in only

    print(f"SEO Intelligence pull for {TARGET} (US/en)")

    if want("movers"):
        print("- movers (rank changes from results_history, no API)…")
        write("movers", build_movers())

    ranked = []
    if want("ranked") or want("overview") or want("opportunities"):
        print("- ranked keywords…")
        ranked = pull_ranked_keywords(args.ranked_limit)
        if want("ranked"):
            write("ranked_keywords", ranked)
        if want("overview"):
            write("overview", build_overview(ranked))
        if want("opportunities"):
            opps = build_opportunities(ranked)
            print(f"- enriching {len(opps)} opportunities with KD + intent…")
            metrics = pull_keyword_metrics([o["keyword"] for o in opps])
            write("opportunities", enrich_opportunities(opps, metrics))

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

    if want("pagegaps"):
        # Which page topics do peer competitors rank with that we lack?
        if not competitors:
            competitors = pull_competitors(args.competitors_limit)
        PEER_CEILING = 10_000_000
        comp = [c for c in competitors if c["type"] == "competitor"]
        peers = [c for c in comp if (c["their_traffic"] or 0) < PEER_CEILING]
        targets = (peers or comp)[:args.gap_competitors]
        print(f"- page gaps vs {', '.join(c['domain'] for c in targets)}…")
        our_pages = pull_relevant_pages(TARGET, 700)
        comp_pages = {c["domain"]: pull_relevant_pages(c["domain"], 150) for c in targets}
        write("page_gaps", build_page_gaps(our_pages, comp_pages))

    if want("backlinks"):
        if not competitors:
            competitors = pull_competitors(args.competitors_limit)
        PEER_CEILING = 10_000_000
        comp = [c for c in competitors if c["type"] == "competitor"]
        peers = [c for c in comp if (c["their_traffic"] or 0) < PEER_CEILING]
        peer_domains = [c["domain"] for c in (peers or comp)[:args.gap_competitors]]
        print(f"- backlinks (us + {', '.join(peer_domains)})…")
        write("backlinks", pull_backlinks(peer_domains, ref_limit=50, anchor_limit=20))

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
