# FlightSearch

Automated Google Flights search and price monitoring. Finds the cheapest
flights using headless browser automation — zero API keys, zero LLM tokens.

## Features

- **URL Generation** — encode route parameters into Google Flights protobuf URLs
- **Combo Ticket Strategies** — open jaw, reverse ticket, split ticket via cheap hubs
- **Automated Search** — Playwright headless Chromium, parallel execution, structured output
- **Price Tracking** — scheduled scans with SQLite persistence
- **Anomaly Detection** — Z-score based low-price alerts with Telegram notifications
- **Bug-Fare Aggregator** — hourly digest of mistake fares from external sources (TheFlightDeal, SecretFlying) parsed via vision LLM
- **Award Search** — Alaska Airlines mileage ticket search with anti-bot bypass (Patchright)
- **Lightweight Award Watch** — Alaska Atmos award scan with bounded parallel `curl` requests
- **ANA Award Search** — ANA Mileage Club international award search via CDP Chrome + auto-login

## Requirements

- Python 3.11+
- Playwright (`pip install playwright && playwright install chromium`)
- Patchright (`pip install patchright && patchright install chromium`) — for award search only
- Google Chrome — required for ANA award search (CDP mode uses system Chrome)
- `curl` — required for `alaska_award_watch.py`

No other dependencies. All tools use Python standard library + browser automation.
A virtual environment (`.venv/`) is recommended for dependency isolation.

## Quick Start

```bash
# Install Playwright
pip install playwright
playwright install chromium

# Search a single route
python3 tools/search_flights.py "$(python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business)"

# Batch search multiple dates
python3 tools/build_url.py TPE ATH --cabin business --batch \
    2026-09-01,2026-09-11 \
    2026-09-04,2026-09-14
# Copy URLs and pass to search_flights.py --parallel
```

## Tools

### build_url.py — URL Generation

```bash
# Round-trip
python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business

# One-way
python3 tools/build_url.py TPE ATH 2026-09-01 --cabin economy

# Batch mode
python3 tools/build_url.py TPE ATH --cabin business --batch \
    2026-09-01,2026-09-11 2026-09-04,2026-09-14

# Options: --cabin (economy|premium|business|first), --stops, --passengers, --curr
```

### combo_search.py — Combo Ticket Strategies

```bash
# Generate all strategies
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business

# JSON output
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business --json

# Specific strategies only
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --types baseline open_jaw
```

Strategies: baseline (round-trip), open_jaw, reverse, split.

### search_flights.py — Automated Search

```bash
# Single URL
python3 tools/search_flights.py "<google-flights-url>"

# Parallel search with labels
python3 tools/search_flights.py --parallel --top 5 \
    --labels "9/1-9/11,9/4-9/14" "<url1>" "<url2>"

# JSON output
python3 tools/search_flights.py --format json "<url>"

# From file
python3 tools/search_flights.py --parallel --file urls.txt
```

### price_tracker.py — Price Tracking

```bash
# Scan all routes in watchlist
python3 tools/price_tracker.py

# Scan + run anomaly detection
python3 tools/price_tracker.py --alert

# Dry run (show URLs only)
python3 tools/price_tracker.py --dry-run

# Custom watchlist
python3 tools/price_tracker.py --watchlist path/to/watchlist.json
```

### price_alert.py — Anomaly Detection

```bash
# Check for anomalies
python3 tools/price_alert.py

# With Telegram notifications
python3 tools/price_alert.py --notify

# Price history summary
python3 tools/price_alert.py --summary
```

### award_search.py — Alaska Airlines Award Search

Search Alaska Airlines for award (mileage) tickets using Patchright, an undetected
Playwright fork that bypasses Akamai anti-bot protection.

```bash
# Install Patchright
pip install patchright
patchright install chromium

# One-way award search
python3 tools/award_search.py SEA LAX 2026-10-01

# Round-trip
python3 tools/award_search.py SEA LAX 2026-10-01 --return-date 2026-10-08

# Date range (search multiple days)
python3 tools/award_search.py SEA LAX --start 2026-10-01 --end 2026-10-03

# Monthly calendar view (lowest miles per day)
python3 tools/award_search.py SEA NRT 2026-10-01 --calendar

# JSON output
python3 tools/award_search.py SEA LAX 2026-10-01 --format json

# Options: --top N, --headless, --return-date, --calendar, --format {table,json}
```

**Note:** Headed mode (default) is required — Akamai blocks headless browsers.
The `--headless` flag is available but results may be empty.

### alaska_award_watch.py — Lightweight Alaska Atmos Award Watch

Fetches Alaska's award-result page with `curl` and extracts serialized award
itineraries locally, so it does not launch a browser. It supports repeatable
routes, passenger count, cabin, point caps, explicit dates, and fixed monthly
scan schedules.

```bash
python3 tools/alaska_award_watch.py \
    --route BKK-FRA --route HKG-LHR \
    --passengers 2 --cabin business \
    --max-points 75000 \
    --start 2026-08-01 --end 2027-06-30 \
    --day-of-month 3,10,17,24 \
    --workers 4 \
    --output results/alaska_award_watch.md
```

Build a browser-free monthly calendar by scanning every day with bounded curl
concurrency:

```bash
python3 tools/alaska_award_watch.py \
    --route BKK-FRA --passengers 2 --cabin business \
    --max-points 75000 --calendar 2026-10 --workers 4 \
    --output results/bkk_fra_2026-10_calendar.md
```

`--max-points` caps points per person; use `--max-total-points` to cap the
entire booking. Each report includes the Alaska search link, taxes, seat count,
flight numbers, and aircraft. `--calendar YYYY-MM` makes one curl request per
day and marks each route's lowest point price with `*`. The page payload is not
a stable public API, so always open the link and verify availability before
booking or transferring points.

### alaska_daily_watch.py — Scheduled Daily Award Sweep (2 pax)

Wraps `alaska_award_watch.py` for the standing wishlist, writing one
Traditional-Chinese report into `results/` plus a Telegram digest. Read-only —
it never logs in, transfers points, or books. Each run makes two passes:

| Pass | Routes | Cabin | Cap / person |
|---|---|---|---|
| 1 | all 32 long-haul | business | 75,000 |
| 2 | 20 short-haul TPE→Asia | economy | 30,000 |

The economy pass was added 2026-08-01: the Helsinki routes turned out to have
no business award space at all — not even for a single passenger on days that
had awards — so a business-only sweep was filtering out the only thing on
offer. It covers short-haul Asia only (no transoceanic legs) and is an explicit
list, not a `TPE-*` prefix match, which would pull in London and Los Angeles.

Most of these routes do have award space: TPE-HKG and TPE-BKK price from 7,500
points/person, TPE-NRT 7,500 on a Starlux direct, and a second tier around
25,000. (An initial probe concluded only NRT and HND had anything — it was
wrong, because the calendar endpoint had begun rate-limiting and the probe read
a failed call as an empty result. The pre-filter now reports its failed-call
count so that degradation is visible rather than silent.)

Both passes appear as separate sections in the report and the digest.

```bash
python3 tools/alaska_daily_watch.py                        # full sweep + Telegram
python3 tools/alaska_daily_watch.py --routes BKK-FRA --no-notify   # test subset
python3 tools/alaska_daily_watch.py --horizon-days 60 --no-notify  # short window
python3 tools/alaska_daily_watch.py --routes HKG-HEL --cabin economy --no-notify
python3 tools/alaska_daily_watch.py --skip-economy                 # business only
python3 tools/alaska_daily_watch.py --prefilter                    # retry the calendar pre-filter
```

A two-stage funnel used to keep the request count down: the `shoulderDates`
calendar endpoint was asked first (31 days per call) and any date with **no
award at all** was skipped. That endpoint has no cabin dimension, so it could
only rule dates out, never in.

**Disabled by default since 2026-08-04.** Alaska began rejecting nearly every
call — 561 of 572 failed — and halving the request rate the day before made no
difference (556/572), so the throttling was not ours to back off from. The
calls that survived saved 1.1% of day-searches, which does not justify 572
doomed requests per sweep. It always failed open, so nothing was ever missed;
`--prefilter` re-enables it if Alaska loosens up.

The Telegram digest leads with **what changed** versus the previous sweep —
routes gained, lost, or now cheaper — because an identical list every morning
stops being read. Yesterday's per-route best is kept in
`data/alaska_award_state.json`, keyed per cabin so the two passes never
overwrite each other; only a full sweep updates that baseline, so a `--routes`
subset can never make unscanned routes look like they disappeared.

Scheduled by `alaska-award-watch.timer` at **03:00 Asia/Taipei**
(`TimeoutStartSec=21600`). It runs at 2 parallel requests rather than 4: the
faster pace finished at 08:43 and tomson wants results waiting at 08:00, so the
sweep is now gentler and starts earlier instead. At 4 workers a full two-pass
sweep was 16,957 requests in 100 minutes, so budget roughly 3.5 hours at 2 —
still leaving over an hour of slack. `--workers N` overrides it for one-off
manual runs.

### ana_award_search.py — ANA Mileage Club Award Search

Search ANA for international award (mileage) ticket availability. Uses CDP
(Chrome DevTools Protocol) to launch system Chrome — zero automation fingerprint,
fully bypasses Akamai Bot Manager.

**First-time setup:**

```bash
# Install Patchright (used for CDP connection only)
pip install patchright && patchright install chromium

# Add credentials to .env
echo "ANA_MEMBER_NUMBER=your-member-number" >> .env
echo "ANA_PASSWORD=your-password" >> .env

# First login (opens Chrome, you log in manually, cookies saved to profile)
python3 tools/ana_setup.py --prefill
```

**Search usage:**

```bash
# Award search (auto-login + JS form submission + calendar results)
python3 tools/ana_award_search.py TPE NRT 2026-10-01 --top 5

# Round-trip
python3 tools/ana_award_search.py TPE NRT 2026-10-01 --return-date 2026-10-08

# Monthly calendar view (availability per cabin per day)
python3 tools/ana_award_search.py TPE NRT 2026-10-01 --calendar

# JSON output
python3 tools/ana_award_search.py TPE NRT 2026-10-01 --format json --top 5
```

**How it works:** Launches system Chrome via CDP (no automation hooks) → auto-fills
password from `.env` → submits search form via JavaScript → parses miles costs from
calendar page. Session expired? Auto-login handles it transparently.

## Configuration

### watchlist.json

Define monitored routes in `tools/watchlist.json`:

```json
{
  "routes": [
    {
      "origin": "TPE",
      "dest": "ATH",
      "depart_date": "2026-09-01",
      "return_date": "2026-09-11",
      "cabin": "business"
    }
  ],
  "settings": {
    "z_threshold": -2.0,
    "min_samples": 5,
    "top_per_route": 5,
    "currency": "TWD"
  },
  "notifications": {
    "telegram": {
      "enabled": true,
      "bot_token_env": "TELEGRAM_BOT_TOKEN",
      "chat_id_env": "TELEGRAM_CHAT_ID"
    }
  }
}
```

### Telegram Notifications

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Send `/start` to your bot
3. Create `.env` in the project root:

```
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

4. Set `notifications.telegram.enabled` to `true` in `watchlist.json`

### Scheduled Scans (systemd timers)

Migrated from Mac cron to systemd **user** timers on the VPS
(srv1673030). The VPS is `Etc/UTC`; `OnCalendar` carries an explicit
`Asia/Taipei` suffix so the wall-clock matches the original Mac schedule.

| Timer | OnCalendar (Asia/Taipei) | Runs |
|-------|--------------------------|------|
| `flightsearch-summary.timer` | `08:00` — `Persistent=true` | `tools/price_tracker.py --alert --daily-summary` |
| `flightsearch-alert.timer` | `02:00, 14:00, 20:00` — `Persistent=false` | `tools/price_tracker.py --alert --notify` |
| `flightsearch-bugfare.timer` | `08..23:00` hourly — `Persistent=true` | `fare_aggregator.py` (bug-fare digest) |

- Units live in `~/.config/systemd/user/`; `Type=oneshot`,
  `WorkingDirectory=/home/tomson/workspace/FlightSearch`.
- `ExecStart` uses the project venv `.venv/bin/python` — the VPS is
  PEP668-managed, so deps install into `.venv`, never system pip.
- stdout+stderr append to `data/tracker.log` (mirrors the old cron
  `>> data/tracker.log 2>&1`).
- `.env` (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) is read natively by
  `load_dotenv()` from the repo root (chmod 600, gitignored).

```bash
# Manage
systemctl --user list-timers | grep flightsearch
systemctl --user status flightsearch-summary.timer flightsearch-alert.timer
systemctl --user start flightsearch-summary.service   # run a scan now
```

### fare_aggregator.py — Bug-Fare Aggregator

Aggregates mistake-fare deals from external sources, parses them with a
vision-capable LLM (Gemini 2.5 Flash via OpenRouter), dedups via SHA-256
of `(source, guid, pub_date)`, and pushes a Telegram digest of any new
finds. Completely separate concern from the watchlist Z-score path —
own table (`bug_fares`), own timer, own log.

```bash
# Manual run (one tick)
.venv/bin/python fare_aggregator.py

# Backfill DB without spamming Telegram (use after long downtime)
.venv/bin/python fare_aggregator.py --no-alert

# Preview parsing without DB writes
.venv/bin/python fare_aggregator.py --dry-run
```

**Sources (phase 1):**

| Source | Fetcher | Notes |
|--------|---------|-------|
| TheFlightDeal | plain `urllib.request` | Standard WP RSS, 16 items/day |
| SecretFlying | headless Chromium homepage scrape | `/feed/` is 301-redirected by the WP server, all alt feed paths sit behind Cloudflare managed-challenge — the homepage HTML exposes 10-15 deal cards with full route+price in the `<a title=...>` attr, which is the only signal the LLM needs |
| bonbon.map (IG) | _disabled in phase 1_ | rsshub.app public instance is fully blocked by Cloudflare (managed-challenge survives even Playwright stealth). Hook remains in `SOURCES` for phase-2 re-enable via self-hosted rsshub or alt IG path |

**Env:** `OPENROUTER_API_KEY` in `.env` (model `google/gemini-2.5-flash`).
Telegram uses the same `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` as the
anomaly alert. Output log at `data/bugfare.log`.

**Storage:** `data/prices.db` adds a `bug_fares` table; existing tables
are untouched.

## How Anomaly Detection Works

The system uses Z-score analysis on historical minimum prices:

1. Each scan records the minimum price per route
2. After collecting enough samples (default: 5), it computes mean and
   standard deviation
3. If the latest price is more than 2 standard deviations below the mean
   (Z-score < -2.0), an alert is triggered
4. Same-day deduplication prevents notification spam

## Project Structure

```
FlightSearch/
├── tools/
│   ├── build_url.py        # URL generation (protobuf encoding)
│   ├── combo_search.py     # Combo ticket strategy generation
│   ├── search_flights.py   # Playwright automated search
│   ├── price_tracker.py    # Scan orchestrator + SQLite storage
│   ├── price_alert.py      # Z-score anomaly detection + alerts
│   ├── award_search.py     # Alaska Airlines award search (Patchright)
│   ├── ana_setup.py        # ANA manual login setup (saves cookies)
│   ├── ana_award_search.py # ANA Mileage Club award search (Patchright)
│   └── watchlist.json      # Route monitoring configuration
├── auth/                   # Chrome profile + saved cookies (gitignored)
├── data/                   # SQLite database (gitignored)
├── docs/                   # PRD, SDD, research notes
├── results/                # Search result files
└── sites/                  # Per-site operation notes
```

## License

Private project.
