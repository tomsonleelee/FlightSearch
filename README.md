# FlightSearch

Automated Google Flights search and price monitoring. Finds the cheapest
flights using headless browser automation — zero API keys, zero LLM tokens.

## Features

- **URL Generation** — encode route parameters into Google Flights protobuf URLs
- **Combo Ticket Strategies** — open jaw, reverse ticket, split ticket via cheap hubs
- **Automated Search** — Playwright headless Chromium, parallel execution, structured output
- **Price Tracking** — scheduled scans with SQLite persistence
- **Anomaly Detection** — Z-score based low-price alerts with Telegram notifications

## Requirements

- Python 3.11+
- Playwright (`pip install playwright && playwright install chromium`)

No other dependencies. All tools use Python standard library only.

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

### Scheduled Scans (crontab)

```bash
# Scan twice daily at 9:00 and 21:00
0 9,21 * * * cd ~/Projects/FlightSearch && python3 tools/price_tracker.py --alert >> data/tracker.log 2>&1
```

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
│   └── watchlist.json      # Route monitoring configuration
├── data/                   # SQLite database (gitignored)
├── docs/                   # PRD, SDD, research notes
├── results/                # Search result files
└── sites/                  # Per-site operation notes
```

## License

Private project.
