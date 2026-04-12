# FlightSearch — Product Requirements Document

## Overview

FlightSearch is an automated flight price search and monitoring system that
finds the cheapest flights on Google Flights. It operates without API keys or
LLM tokens — all data extraction uses headless browser automation with
Playwright.

## Goals

1. **Minimize manual effort** — automate the repetitive process of searching
   Google Flights across multiple date/route combinations.
2. **Find hidden deals** — explore combo ticket strategies (open jaw, reverse,
   split) that manual searches would miss.
3. **Detect price anomalies** — continuously monitor routes and alert when
   prices drop significantly below historical averages.

## User Personas

- Travelers planning international trips who want the cheapest fares.
- Power users comfortable running CLI tools and reading structured output.

## Features

### F1: URL Generation (`build_url.py`)

Generate valid Google Flights search URLs by encoding route parameters into
the protobuf-based `tfs` query parameter.

- **Inputs**: origin, destination, dates, cabin class, passengers, stops.
- **Modes**: single URL, batch (multiple date combos), multi-city (multiple legs).
- **Output**: ready-to-use Google Flights `/search` URLs.

### F2: Combo Ticket Strategies (`combo_search.py`)

Generate multiple search strategies to find cheaper alternatives to standard
round-trip tickets.

- **Baseline**: standard round-trip.
- **Open Jaw**: fly into destination, return from a nearby city (3 one-way segments).
- **Reverse Ticket**: round-trip originating from destination + one-way supplement.
- **Split Ticket**: break journey via known cheap hub cities.
- **Output**: list of strategies, each with labeled segment URLs.

### F3: Automated Search (`search_flights.py`)

Execute searches on Google Flights using Playwright headless Chromium.

- Opens pre-filled URLs, clicks search, parses results from DOM.
- Each search runs in an isolated incognito browser context.
- Supports sequential and parallel execution (subprocess-based).
- Extracts: airline, price, stops, duration, departure/arrival times, layover details.
- Output formats: human-readable table or JSON.

### F4: Price Tracking (`price_tracker.py`)

Scheduled price collection that stores results in a local SQLite database.

- Reads monitored routes from `watchlist.json`.
- Generates URLs via `build_url` and runs searches via `search_flights`.
- Persists every flight result with full metadata to SQLite.
- Supports dry-run mode to preview URLs without searching.
- Optionally triggers anomaly detection after each scan.

### F5: Anomaly Detection (`price_alert.py`)

Statistical anomaly detection using Z-score analysis on historical price data.

- Computes per-route min price time series from scan history.
- Calculates Z-score of latest price against historical distribution.
- Triggers alert when Z-score falls below configurable threshold (default: -2.0).
- Requires minimum sample count before alerting (default: 5 scans).
- Same-day deduplication prevents repeated alerts.
- **Notifications**: terminal output + optional Telegram Bot push.
- **Summary mode**: display price statistics per route (min, max, mean, stdev).

### F6: Calendar Exploration (agent-browser)

Manual browser automation for exploring Google Flights date grid view.

- Used only when dates are fully flexible and calendar scanning is needed.
- Leverages `agent-browser` CLI for DOM interaction.
- Results feed into the fast search mode for detailed queries.

### F7: Alaska Airlines Award Search (`award_search.py`)

Search Alaska Airlines for award (mileage) ticket availability.

- **Inputs**: origin, destination, departure date, optional return date.
- **Browser**: Patchright (undetected Playwright fork) to bypass Akamai anti-bot.
- **Search strategy**: direct URL construction (`/search/results?...&ShoppingMethod=onlineaward`);
  falls back to form-based search if direct URL is redirected.
- **Extraction**: flight cards parsed via `[data-testid="flight-card-{n}"]` selectors;
  fare regex extracts cabin class, mileage points, and cash co-pay per card.
- **Date range**: `--start`/`--end` iterates over consecutive dates with individual searches.
- **Calendar view**: `--calendar` flag reads `<shoulder-dates>` web component's `dates` JSON
  attribute (±15 days around search date) to show a monthly grid of lowest award prices.
  Single request covers a full month by searching the 15th.
- **Output formats**: human-readable table or JSON.
- **Limitation**: headed mode required (Akamai blocks headless); `--headless` opt-in available.

### F8: ANA Mileage Club Award Search (`ana_award_search.py` + `ana_setup.py`)

Search ANA's international award booking system for mileage ticket availability.

- **Setup**: `ana_setup.py` opens a headed browser for manual login; saves session
  cookies (`auth/ana_state.json`) and browser metadata (`auth/ana_meta.json`).
  Supports `--prefill` to pre-fill member number from `.env`.
- **Authentication**: cookie injection via Playwright `storage_state` — no
  automated login (Akamai Bot Manager blocks programmatic login attempts).
- **Inputs**: origin, destination, departure date, optional return date, cabin class.
- **Browser**: Patchright (undetected Playwright fork) with saved cookies.
- **Search flow**: navigate to ANA award search form, fill origin/dest/date/cabin,
  submit, parse result table rows (`tr.oneWayDisplayPlan`).
- **Calendar view**: `--calendar` queries ANA's availability calendar
  (`cam.ana.co.jp`) to show per-cabin availability (O/X) for each day across
  up to 6 months.
- **Session management**: detects expired sessions (redirected to login page or
  "heavy traffic" block) and prompts user to re-run setup.
- **Date range**: `--start`/`--end` iterates over consecutive dates.
- **Output formats**: human-readable table or JSON.

## Non-Functional Requirements

- **Zero external API dependencies** — no paid APIs, no LLM tokens for search.
- **Minimal Python dependencies** — stdlib + Playwright + Patchright (award search only).
- **Privacy** — all data stored locally in SQLite; credentials in `.env` only.
- **Robustness** — isolated browser contexts prevent session interference;
  parallel searches use subprocess isolation.

## Configuration

### watchlist.json

Defines monitored routes and system settings:

```json
{
  "routes": [
    { "origin": "TPE", "dest": "ATH", "depart_date": "...", "return_date": "...", "cabin": "business" }
  ],
  "settings": {
    "z_threshold": -2.0,
    "min_samples": 5,
    "top_per_route": 5,
    "currency": "TWD"
  },
  "notifications": {
    "telegram": { "enabled": true, "bot_token_env": "TELEGRAM_BOT_TOKEN", "chat_id_env": "TELEGRAM_CHAT_ID" }
  }
}
```

### .env

Stores sensitive credentials (excluded from version control):

```
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>
ANA_MEMBER_NUMBER=<member-number>   # optional, for ana_setup.py --prefill
ANA_PASSWORD=<password>             # reserved for future use
```

## Known Limitations

- Multi-city URLs are silently rewritten by Google to round-trip; open jaw
  uses separate one-way searches instead.
- Reverse ticket strategy may fail on routes with low Google Flights index
  coverage.
- Business class one-way tickets cost ~60-70% of round-trip, making 3-segment
  combo strategies rarely cheaper than baseline.
