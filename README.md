# Google Rank Checker — Americanflat Picture Frames

Track Google search rankings across 250+ picture frame keywords using SerpAPI.

---

## Setup

### 1. Install dependencies
```bash
pip install requests python-dotenv
```

### 2. Get a ValueSerp API key
- Go to https://valueserp.com
- Sign up — free trial available, then ~$5/mo for 5,000 searches
- At 359 keywords/week (~1,400/month) you're well within the 5,000 limit

### 3. Create a `.env` file in the same folder
```
VALUESERP_KEY=your_key_here
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
  --top 100
```

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
