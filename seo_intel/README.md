# SEO Intelligence

In-house "Ahrefs" data layer for **americanflat.com** — organic rankings,
keyword opportunities, competitor analysis, and keyword gaps, powered by the
[DataForSEO Labs](https://dataforseo.com/apis/dataforseo-labs-api) API.

The `amf-catalog-tools` React app reads the committed JSON files under
`data/` (same static-file pattern the rank checker uses for
`results_latest.csv`). No API keys ever reach the browser.

## Data files (`seo_intel/data/`)

| File | Contents |
|---|---|
| `overview.json` | Headline metrics: total ranked keywords, est. monthly traffic, position-band distribution, `generated_at`. |
| `ranked_keywords.json` | Every keyword americanflat.com ranks for (capped, richest-first): position, volume, CPC, URL, traffic estimate. |
| `opportunities.json` | High-volume keywords ranking page 2–3 (positions 11–30, vol ≥ 200) — the near-term win list. |
| `competitors.json` | Auto-discovered domains by keyword overlap, classified `marketplace` (Amazon/Walmart/etc.) vs `competitor` (frame-focused). |
| `keyword_gap.json` | Per true-peer competitor: frame-relevant keywords they rank top-20 for where we rank worse / not at all. |

## Running

```bash
# credentials in .env (gitignored): DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD
python seo_intel/pull_seo_intel.py                    # full pull
python seo_intel/pull_seo_intel.py --only overview,competitors
python seo_intel/pull_seo_intel.py --ranked-limit 500 --gap-competitors 2
```

A full pull costs **~$0.27** and is guarded by a `MAX_SPEND` ceiling in the
script (aborts before exceeding it).

## Scheduled runs

`.github/workflows/seo_intel.yml` runs a full pull **weekly** (Mondays 14:00
UTC) and commits the refreshed `data/`. Competitor/gap data moves slowly, so
weekly keeps spend ~$1.15/month.

**Required repo secrets:** `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`
(Settings → Secrets and variables → Actions).

## Design notes

- **Marketplace filter** (`MARKETPLACES` in the script): raw competitor
  discovery is dominated by Amazon/Walmart/Etsy/etc. — real signal only.
  Those are flagged `marketplace`; frame-focused domains stay `competitor`.
- **Gap relevance filter** (`FRAME_TOKENS`): broad retailers rank for
  off-topic terms (legos, halloween costumes). Gap keywords must contain a
  frame/wall-art/print token and must not be the competitor's brand term.
- **Peer selection for gaps**: defaults to competitors under a 10M-traffic
  ceiling, so gaps target true frame peers (arttoframe, pictureframes,
  modernmemorydesign) rather than giant general retailers.
