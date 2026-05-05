---
name: flight-search
description: Search Google Flights for cheapest flights. Use when user asks to search flights, compare airfares, or find cheap tickets. Triggers on "search flights", "find flights", "flight prices", "cheapest flight".
allowed-tools: Bash(python3 tools/*), Bash(python3 -c *), Bash(playwright install *)
disable-model-invocation: true
---

# Flight Search

Search Google Flights using the automated Playwright tool chain.

**User request:** $ARGUMENTS

## Step 0: Check Playwright

Run this check first. If it fails, install automatically:

```bash
python3 -c "from playwright.sync_api import sync_playwright; print('OK')"
```

If import fails:
```bash
pip install playwright && playwright install chromium
```

## Step 1: Parse Arguments

Extract from `$ARGUMENTS`:
- **origin**: IATA code (e.g. TPE) — required
- **dest**: IATA code (e.g. ATH) — required
- **dates**: one of these formats:
  - Specific dates: `2026-09-01 2026-09-11` (depart + return)
  - Month only: `2026-09` (expand to representative dates, see below)
  - Depart only: `2026-09-01` (one-way)
- **--cabin**: economy (default), premium, business, first
- **--combo**: enable combo ticket strategies (Open Jaw / Reverse / Split)
- **--top N**: max results per search (default 5)

### Month expansion

When only a month is given (e.g. `2026-09`), generate 4 representative date pairs:
- Early month weekday: 1st~3rd → +10 days
- Early month weekend: first Saturday → +10 days
- Mid month weekday: 15th~17th → +10 days
- Mid month weekend: Saturday closest to 15th → +10 days

Example: `2026-09` expands to:
```
2026-09-01,2026-09-11
2026-09-05,2026-09-15
2026-09-15,2026-09-25
2026-09-19,2026-09-29
```

## Step 2: Generate URLs

### Standard search (no --combo)

Use `build_url.py --batch`:

```bash
python3 tools/build_url.py <origin> <dest> --cabin <cabin> --batch \
    <date1_depart>,<date1_return> \
    <date2_depart>,<date2_return> ...
```

### Combo search (with --combo)

Use `combo_search.py` to generate multi-strategy URLs:

```bash
python3 tools/combo_search.py <origin> <dest> <depart> <return> --cabin <cabin> --json
```

Then deduplicate URLs across strategies. Many segments share the same URL (e.g. all Open Jaw strategies share the same outbound one-way).

## Step 3: Run Search

Run `search_flights.py` with all URLs. Split into batches of 5-6 URLs for parallel execution:

```bash
python3 tools/search_flights.py --parallel --top <N> --format json \
    --labels "<label1>,<label2>,..." \
    "<url1>" "<url2>" ...
```

For large URL sets, write URLs to a temp file:
```bash
python3 tools/search_flights.py --parallel --top <N> --format json \
    --labels "<labels>" --file /tmp/flight_urls.txt
```

Run multiple batches concurrently using background Bash commands when there are 7+ URLs.

## Step 4: Compile Results

### Standard search

Present results as a ranked table:

```
## <origin> → <dest> <month> <cabin> Search Results

| # | Date       | Airline    | Price (TWD) | Stops | Duration | Departure |
|---|------------|------------|-------------|-------|----------|-----------|
| 1 | 9/1→9/11   | Etihad     | 108,423     | 1     | 15h50m   | 19:25     |
| 2 | ...        | ...        | ...         | ...   | ...      | ...       |

**Cheapest: [airline] [dates] [price] TWD**
```

### Combo search

Calculate totals for each strategy by summing segment prices:

```
## Strategy Comparison

| # | Strategy         | Total (TWD) | vs Baseline | Segments               |
|---|-----------------|-------------|-------------|------------------------|
| 1 | Baseline RT      | 108,423     | —           | Direct round-trip      |
| 2 | Split BKK        | 132,896     | +24,473     | 14615+52422+65859      |
| ...

**Recommendation: [strategy] at [price] TWD**
```

### Important notes for combo results
- Always include baseline for comparison
- Sum ALL segments including supplement legs
- Note if any strategy segment returned 0 results
- Combo strategies with 3+ one-way legs rarely beat baseline for business class

## Output

- Use Traditional Chinese (繁體中文) for explanations
- Use TWD as currency
- Highlight the cheapest option
- If combo was used and all combos are more expensive than baseline, explicitly say so
- Save detailed results to `results/YYYY-MM-DD_<origin>_<dest>.md`
