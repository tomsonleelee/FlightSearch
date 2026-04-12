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
| `award_search.py` | Alaska Airlines award search + calendar | Patchright |
| `ana_setup.py` | ANA manual login + cookie save | Patchright |
| `ana_award_search.py` | ANA Mileage Club award search + calendar | Patchright |

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

### Award DOM Parsing (award_search.py)

Alaska Airlines uses the Auro Design System with Shadow DOM web components.
Flight results are rendered as `[data-testid="flight-card-{n}"]` elements.

**Anti-bot strategy:**
- Patchright (undetected Playwright fork) handles browser fingerprinting.
- Headed mode is required — Akamai still detects headless browsers.

**Search flow (dual-path):**
1. **Primary**: construct direct URL with query params
   (`/search/results?O={origin}&D={dest}&OD={date}&ShoppingMethod=onlineaward`).
2. **Fallback**: if redirected away from `/results`, fill the search form via
   Patchright locators. Airport dropdowns use `[data-testid="airport-option-{CODE}"]`
   with `force=True` click to bypass Shadow DOM pointer interception.

**Fare extraction:**
- JavaScript `page.evaluate()` with raw strings (`r"""..."""`) to prevent Python
  from double-escaping regex backslashes.
- Fare regex: `/(Main|First|Saver|Premium)\s*([\d,.]+k?)\s*points\s*pts\s*\+\s*\$(\d+)/gi`
- Each flight card yields multiple fares (one per cabin class).

**Calendar view:**
- The `<shoulder-dates>` web component embeds a JSON `dates` attribute with ~31
  days of lowest award prices (±15 days centered on search date).
- Searching for the 15th of the target month covers the full month in one request.
- JSON entries contain `awardPoints`, `price` (taxes), and `isDiscounted` fields.

### ANA Award Search (`ana_award_search.py` + `ana_setup.py`)

ANA Mileage Club uses Akamai Bot Manager which blocks automated login at the
server level (returns "heavy traffic" page regardless of browser fingerprint).
The solution uses a two-phase approach inspired by GrokAPI:

**Phase 1 — Manual Login (`ana_setup.py`):**
1. Launch headed Patchright browser and navigate to ANA award search URL.
2. ANA redirects to login page; user manually enters credentials.
3. Poll every 2s for login success (URL change to `award_search*`, logout button
   visible, or redirect to `mypage`).
4. Save session via `context.storage_state()` → `auth/ana_state.json`.
5. Save browser metadata (userAgent) → `auth/ana_meta.json`.
6. Optional `--prefill` reads `ANA_MEMBER_NUMBER` from `.env` to pre-fill the
   member number field.

**Phase 2 — Automated Search (`ana_award_search.py`):**
1. Load saved state from `auth/ana_state.json` and userAgent from `auth/ana_meta.json`.
2. Create Patchright context with `storage_state=` and `user_agent=` to inject
   the authenticated session.
3. Navigate to ANA award search form; fill origin, destination, date, cabin.
4. Submit and wait for results table.
5. Parse `tr.oneWayDisplayPlan` rows for flight details (number, airline, times,
   duration, stops, miles, availability status).
6. If redirected to login page or "heavy traffic" page, report session expired.

**Anti-bot strategy summary:**
- Human login bypasses Akamai challenge entirely.
- Cookie injection reuses the authenticated session without re-triggering detection.
- Browser metadata (userAgent) is preserved to maintain fingerprint consistency.
- `auth/` directory is gitignored to prevent credential leakage.

**Calendar view:**
- Queries `cam.ana.co.jp/psz/tokutencal/form_e.jsp` (separate from main search).
- Returns per-day, per-cabin availability markers (O = available, X = unavailable).
- Covers up to 6 months in a single request.

**Data structures:**
- `AwardFlight`: flight_number, airline, duration, times, origin, dest, stops,
  cabin, miles, miles_str, status, aircraft.
- `AwardSearchResult`: route info, cabin, total_results, flights list, error.
- `CalendarDay`: date, economy/premium/business/first availability markers.
- `CalendarResult`: route info, year, month, months_data (list of CalendarDay lists).

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
    ├── award_search.py     # Alaska Airlines award search
    ├── ana_setup.py        # ANA manual login setup
    ├── ana_award_search.py # ANA Mileage Club award search
    └── watchlist.json      # Route monitoring config
```
