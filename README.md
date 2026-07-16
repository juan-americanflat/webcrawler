# Google Rank Checker — Americanflat Picture Frames

Track Google search rankings across 400+ picture frame keywords via the
**DataForSEO SERP API**.

> Migrated off ValueSerp → DataForSEO on 2026-07-16 to consolidate on one
> vendor (DataForSEO already powers the SEO Intelligence pipeline in
> `seo_intel/`). DataForSEO reports positions slightly differently, so
> there's a one-time discontinuity in `results_history/` at the switch date.

---

## Setup

### 1. Install dependencies
```bash
pip install requests python-dotenv
```

### 2. DataForSEO credentials
- Get your **API login + API password** from https://app.dataforseo.com (API Access).
- The API password is distinct from your dashboard login password.
- Live SERP is ~$0.002/keyword. At the tiered cadence below (~6,900
  checks/month) that's ~$14/month.

### 3. Create a `.env` file in the same folder
```
DATAFORSEO_LOGIN=you@example.com
DATAFORSEO_PASSWORD=your_api_password
```

---

## Usage

### Run full keyword list (~250 keywords)
```bash
python rank_checker.py
```

### Run on a custom domain
```bash
python rank_checker.py --domain americanflat.com
```

### Only run top N high-priority keywords (saves API credits)
```bash
python rank_checker.py --top 50
```

### Only run a single priority tier
```bash
python rank_checker.py --priority high     # 147 keywords
python rank_checker.py --priority medium   # 179 keywords
python rank_checker.py --priority low      # 33 keywords
```
The GitHub Action uses this on a tiered schedule (see below) to keep
DataForSEO SERP spend predictable (~$14/month at ~6,900 checks).

### Test the pipeline without using API credits
```bash
python rank_checker.py --dry-run
```

### Full options
```bash
python rank_checker.py \
  --domain americanflat.com \
  --keywords keywords.csv \
  --output results_may2026.csv \
  --priority high \
  --top 100
```

---

## Scheduled runs (GitHub Actions)

`.github/workflows/rank_checker.yml` runs the checker on a tiered cron:

| Tier   | Cron (UTC)       | Days       | Keywords | Searches/mo |
|--------|------------------|------------|---------:|------------:|
| high   | `0 13 * * *`     | daily      |      147 |      ~4,410 |
| medium | `15 13 * * 1,3,5`| Mon/Wed/Fri |     179 |      ~2,327 |
| low    | `30 13 * * 1`    | Mondays    |       33 |        ~143 |
|        |                  | **total**  |      359 |  **~6,880** |

Times are staggered by minute so `github.event.schedule` is
unambiguous on Monday (when all three crons fire) and the runs queue
via `concurrency: rank-checker` instead of racing on `git push`.

Each run writes to two places:
- `results_latest.csv` — full 359-row table (untouched tiers keep
  their previous values, so the Streamlit dashboard never goes blank).
- `results_history/results_YYYY-MM-DD.csv` — only the keywords
  actually re-checked that day, so the per-keyword chart shows
  honest measurement points.

**Manual rerun:** Actions tab → "SEO Rank Checker" → Run workflow →
choose a priority tier (or `all`).

---

## Output

A CSV file with these columns:

| Column | Description |
|--------|-------------|
| keyword | The search term |
| category | Keyword group (size, style, use case, etc.) |
| priority | high / medium / low |
| position | Google ranking position (1–100) |
| position_label | Human-readable label (#3 🏆 Top 3, ✅ Page 1, etc.) |
| url | The ranked URL |
| in_featured | Whether the site appears in a featured snippet |
| total_results | Estimated total Google results |
| error | Any API error |
| checked_at | Timestamp |

---

## Keyword Categories

| Category | Count | Description |
|----------|-------|-------------|
| core - brand | ~20 | Head terms, brand name |
| size - standard | ~20 | 4x6, 5x7, 8x10, 11x14, 16x20 etc. |
| size - small | ~20 | 2x2 through 6x9 |
| size - square/medium | ~12 | Square sizes, mid-range |
| size - large | ~20 | 12x16 through 24x36 |
| size - extra large | ~16 | 27x40 through 48x36 |
| size - odd/custom | ~14 | Non-standard sizes |
| color/style | ~30 | Black, white, gold, wood etc. |
| material | ~10 | Acrylic, metal, wood etc. |
| gallery wall | ~30 | Gallery wall terms |
| multi-pack/sets | ~15 | Bundle/set terms |
| poster frames | ~15 | Poster-specific |
| specialty frames | ~20 | Diploma, shadow box, floating etc. |
| long tail - sets | ~15 | Size + color + quantity combos |
| long tail - room/use | ~12 | Room-specific placement terms |
| long tail - style/decor | ~15 | Style/aesthetic terms |
| features | ~25 | Mat, glass, hanging, front-loading etc. |
| use case - art/family/occasions/sports | ~40 | Intent-based terms |
| buying intent | ~20 | Where to buy, best, sale etc. |
| educational | ~20 | How-to and sizing guides |

---

## Tips

- Run weekly and compare CSVs to track movement
- Filter `position_label` for "Page 2" entries — those are the quickest wins
- Cross-reference with your paid search campaigns: if you rank #1 organically, pause that ad
- Share the output CSV with the SEO agency as your target keyword list
- Use `--top 50` for a quick weekly pulse check (saves API credits)
