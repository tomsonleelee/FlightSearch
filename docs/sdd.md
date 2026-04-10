# FlightSearch — Software Design Document

## Architecture Overview

```
                          watchlist.json
                               │
┌──────────────┐    ┌──────────┴───────────┐    ┌─────────────────┐
│ build_url.py │◄───│  price_tracker.py    │───►│ search_flights  │
│ (URL encode) │    │  (orchestrator)      │    │ (Playwright)    │
└──────────────┘    └──────────┬───────────┘    └─────────────────┘
                               │
                          prices.db (SQLite)
                               │
                    ┌──────────┴───────────┐
                    │  price_alert.py      │───► Terminal / Telegram
                    │  (Z-score detect)    │
                    └──────────────────────┘
```

### Module Responsibilities

| Module | Role | Dependencies |
|--------|------|-------------|
| `build_url.py` | Encode route params into Google Flights protobuf URLs | None (stdlib) |
| `combo_search.py` | Generate multi-strategy search URL sets | `build_url` |
| `search_flights.py` | Headless browser search + DOM parsing | Playwright |
| `price_tracker.py` | Scan orchestration + SQLite persistence | `build_url`, `search_flights` |
| `price_alert.py` | Statistical anomaly detection + notifications | None (stdlib) |

All inter-module communication is via direct Python imports. No RPC, no
message queues, no external services (except Telegram for optional alerts).

## Data Model

### SQLite Schema (`data/prices.db`)

#### `scans` — Scan execution log

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| scanned_at | TEXT | UTC timestamp |
| watchlist_hash | TEXT | SHA-256 prefix of routes config |

#### `prices` — Flight price records (core table)

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| scan_id | INTEGER FK | References `scans.id` |
| scanned_at | TEXT | UTC timestamp (denormalized for query convenience) |
| origin | TEXT | IATA code (e.g., TPE) |
| dest | TEXT | IATA code (e.g., ATH) |
| depart_date | TEXT | YYYY-MM-DD |
| return_date | TEXT | YYYY-MM-DD or NULL (one-way) |
| cabin | TEXT | economy / premium / business / first |
| airline | TEXT | Carrier name |
| price | INTEGER | Fare in local currency |
| currency | TEXT | Default TWD |
| stops | INTEGER | Number of layovers |
| duration | TEXT | Total travel time |
| departure_time | TEXT | Departure time string |
| arrival_time | TEXT | Arrival time string |

**Indexes**: `idx_route(origin, dest, depart_date, return_date, cabin)`,
`idx_scanned(scanned_at)`.

#### `alerts` — Sent alert log (deduplication)

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| created_at | TEXT | UTC timestamp |
| origin, dest, depart_date, return_date, cabin | TEXT | Route identifiers |
| airline | TEXT | Cheapest carrier |
| price | INTEGER | Alerted price |
| z_score | REAL | Computed Z-score |
| mean_price | REAL | Historical mean at alert time |
| notified | INTEGER | 0 = terminal only, 1 = Telegram sent |

## Algorithms

### URL Encoding (build_url.py)

Google Flights uses a protobuf-encoded `tfs` parameter in the URL. The
encoding process:

1. Build protobuf fields: version (field 1), passengers (field 2), legs
   (field 3, nested with date/origin/dest), stops (field 8), cabin (field 9).
2. Append fixed trailer bytes for search configuration.
3. Base64url-encode the protobuf bytes (no padding).
4. Construct the full URL with `tfs`, `tfu`, `hl`, and `curr` parameters.

### DOM Parsing (search_flights.py)

Google Flights renders results as `<li class="pIav2d">` elements. Each
contains a `.JMc5Xc` element with an `aria-label` attribute that includes
all flight details in natural language (Chinese locale).

Parsing pipeline:
1. `querySelectorAll('li.pIav2d')` — collect result cards.
2. Extract `aria-label` text from `.JMc5Xc` child.
3. Regex extraction for: price (`總價\s*([\d,]+)\s*新台幣`), airline
   (`搭乘(.+?)的航班`), stops (`中途停留\s*(\d+)\s*次`), duration
   (`總交通時間[：:]\s*(.+?)`), departure/arrival times.
4. Deduplicate by (airline, price, duration) tuple.
5. Sort by price ascending.

### Z-Score Anomaly Detection (price_alert.py)

For each monitored route:

1. **Build time series**: query `MIN(price)` per `scan_id` from the `prices`
   table, grouped by scan, filtered by route key
   `(origin, dest, depart_date, return_date, cabin)`.

2. **Check sample size**: skip if fewer than `min_samples` (default 5) data
   points.

3. **Compute statistics**: `mean` and `stdev` of all historical min prices
   excluding the latest scan (using `statistics.mean()` and
   `statistics.stdev()` from Python stdlib).

4. **Calculate Z-score**: `z = (current_min - mean) / stdev`.

5. **Threshold check**: if `z < z_threshold` (default -2.0), the price is
   statistically anomalous — approximately 2 standard deviations below the
   mean.

6. **Deduplication**: query the `alerts` table for matching
   `(route, airline, price, today's date)` to prevent repeat notifications.

7. **Notification**: output to terminal; optionally send via Telegram Bot API.

### Alert Deduplication

Same-day dedup query:
```sql
SELECT COUNT(*) FROM alerts
WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ?
  AND airline = ? AND price = ?
  AND created_at LIKE '{today}%'
```

This allows re-alerting on subsequent days if the anomaly persists, while
preventing notification spam within a single day.

## Notification System

### Terminal Output

Always active. Prints route status with price, mean, and Z-score for every
monitored route. Anomalies are highlighted with `!! ANOMALY` and a formatted
alert block.

### Telegram Bot

Optional, enabled via `watchlist.json` and `.env` credentials.

- Uses `urllib.request` (stdlib) to call `POST /bot{token}/sendMessage`.
- HTML parse mode for bold formatting.
- 10-second timeout; failures are caught and logged without crashing.
- Bot token and chat ID are read from environment variables (loaded from
  `.env` via built-in `load_dotenv` helper).

## Concurrency Model

- **Sequential search**: single Playwright browser, multiple incognito
  contexts created/destroyed per URL.
- **Parallel search**: each URL gets its own subprocess (because Playwright
  sync API doesn't support threading). Each subprocess launches its own
  browser instance.
- **Database**: single-writer SQLite with WAL journal mode. No concurrent
  write contention since only `price_tracker.py` writes.

## File Structure

```
FlightSearch/
├── CLAUDE.md               # AI assistant instructions
├── README.md               # Project documentation
├── .env                    # Credentials (gitignored)
├── .gitignore
├── data/
│   └── prices.db           # SQLite database (gitignored)
├── docs/
│   ├── prd.md              # Product requirements
│   ├── sdd.md              # This document
│   ├── ai-scraping-tools.md
│   └── error-fare-research.md
├── results/                # Search result files
├── sites/                  # Per-site operation notes
└── tools/
    ├── build_url.py        # URL generation
    ├── combo_search.py     # Combo ticket strategies
    ├── search_flights.py   # Playwright search
    ├── price_tracker.py    # Scan orchestrator
    ├── price_alert.py      # Anomaly detection
    └── watchlist.json      # Route monitoring config
```
