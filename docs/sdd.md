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
| `ana_setup.py` | ANA login via CDP Chrome + cookie save | Chrome + Patchright (CDP) |
| `ana_award_search.py` | ANA Mileage Club award search + calendar | Chrome + Patchright (CDP) |

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

ANA Mileage Club uses Akamai Bot Manager which blocks all automated browsers —
including Patchright (undetected Playwright fork) and system Chrome launched via
Playwright's `channel="chrome"`. Even `launch_persistent_context` with a real
Chrome profile is detected. The solution uses CDP (Chrome DevTools Protocol) to
launch a completely normal Chrome process and connect to it externally.

**Phase 1 — Setup (`ana_setup.py`):**
1. Launch system Chrome via `subprocess.Popen()` with
   `--remote-debugging-port=9333` and `--user-data-dir=auth/ana_chrome_profile/`.
   Chrome runs as a normal browser — zero Playwright/automation hooks injected.
2. Navigate to ANA award search URL (redirects to login page).
3. Poll CDP endpoint (`/json/list`) every 10s to detect when the user logs in
   (URL no longer contains `login` and contains `award_search`).
4. Connect via Patchright `connect_over_cdp()` to extract cookies.
5. Save `context.storage_state()` → `auth/ana_state.json`.
6. Save browser metadata (userAgent) → `auth/ana_meta.json`.
7. Terminate Chrome process.

**Phase 2 — Search (`ana_award_search.py`):**
1. Launch system Chrome via `subprocess.Popen()` with CDP port 9334 and
   the same persistent profile directory (`auth/ana_chrome_profile/`).
2. Connect via Patchright `connect_over_cdp()`.
3. Navigate to ANA award search form. If redirected to login page (detected
   by page title containing "login"), auto-login:
   - Read `ANA_PASSWORD` from `.env`
   - Fill `#password` field via Playwright locator
   - Click Login button with `force=True`
   - Poll until page title no longer contains "login"
4. Set form field values via `page.evaluate()` JavaScript:
   - Hidden fields: `departureAirportCode:field`, `arrivalAirportCode:field`,
     `awardDepartureDate:field`, `awardReturnDate:field`, `hiddenSearchMode`,
     `boardingClass`, etc.
   - Add a hidden input with the submit button's `name` attribute (JSF requires
     the button name in POST data to identify the action).
   - Call `form.submit()` on the existing JSF form (NOT a dynamically created
     form — JSF rejects requests from forms not matching its ViewState).
5. Wait for `networkidle` + poll for `CalendarSearchResult` in page content.
6. Parse results from embedded JavaScript (see below).
7. Terminate Chrome process.

**Why CDP instead of Patchright launch:**
Akamai's Sensor JS (`asw-fingerprints.js`, 64KB) detects Playwright-launched
browsers via multiple signals (WebDriver flag, plugin enumeration, canvas
fingerprint, etc.). CDP connection to a normally-launched Chrome has zero
detectable automation markers — Patchright is only used as a CDP client to
read DOM and extract cookies, not to control browser launch.

**Why JS form submission instead of UI automation:**
ANA's JSF form has multiple validation layers: autocomplete dropdowns that set
internal JS state, readonly calendar pickers, overlay elements (`maskForClose`)
that intercept clicks, and onclick handlers that check form state. Setting hidden
field values via JS + `form.submit()` bypasses all of these while preserving
the JSF ViewState and conversation context.

**Result extraction:**
ANA's search returns a calendar comparison page (`award_search_roundtrip_calendar.xhtml`)
with embedded JavaScript containing `CalendarSearchResult` entries:
```javascript
var returnDate = "20261005"; var milesCost = '20,000';
tempArray.push(new CalendarList.CalendarSearchResult("20260929", returnDate, milesCost));
```
Parser extracts all `(departureDate, returnDate, milesCost)` tuples via regex,
filters out `'-'` (unavailable), and sorts by miles ascending.

**Anti-bot strategy summary:**
- Chrome launched via `subprocess` (not Playwright) = zero automation fingerprint.
- CDP connection is external/passive — does not modify browser behavior.
- Persistent Chrome profile retains cookies across sessions.
- Auto-login fills password in a real Chrome window (Akamai sees normal user).
- `auth/` directory is gitignored to prevent credential leakage.

**Calendar view:**
- Queries `cam.ana.co.jp/psz/tokutencal/form_e.jsp` (separate from main search).
- Returns per-day, per-cabin availability markers (O = available, X = unavailable).
- Covers up to 6 months in a single request.
- Also uses CDP Chrome launch pattern.

**Data structures:**
- `AwardFlight`: flight_number, airline, duration, departure_time (date for
  calendar mode), arrival_time (return date for calendar mode), origin, dest,
  stops, cabin, miles, miles_str, status, aircraft.
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
