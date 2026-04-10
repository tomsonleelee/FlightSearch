#!/usr/bin/env python3
"""Scheduled price tracker — scans watchlist routes and stores results in SQLite.

Reads routes from watchlist.json, generates Google Flights URLs via build_url,
runs Playwright searches via search_flights, and persists every result to a
local SQLite database for historical analysis.

Supports multi-POS scanning: each route is searched from multiple country
points of sale (gl= parameter) to find cross-market price differences.

Usage:
    python3 tools/price_tracker.py                  # scan all routes x all POS
    python3 tools/price_tracker.py --dry-run        # show URLs without searching
    python3 tools/price_tracker.py --alert          # run alert check after scan
    python3 tools/price_tracker.py --watchlist path  # custom watchlist file
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_url import CABIN_MAP, build_url
from search_flights import search_urls

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.db"
DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL,
    watchlist_hash TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    scanned_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    cabin TEXT NOT NULL,
    airline TEXT NOT NULL,
    price INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'TWD',
    stops INTEGER NOT NULL,
    duration TEXT,
    departure_time TEXT,
    arrival_time TEXT,
    pos TEXT NOT NULL DEFAULT 'tw'
);

CREATE INDEX IF NOT EXISTS idx_route
    ON prices(origin, dest, depart_date, return_date, cabin);
CREATE INDEX IF NOT EXISTS idx_route_pos
    ON prices(origin, dest, depart_date, return_date, cabin, pos);
CREATE INDEX IF NOT EXISTS idx_scanned
    ON prices(scanned_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    cabin TEXT NOT NULL,
    airline TEXT NOT NULL,
    price INTEGER NOT NULL,
    z_score REAL NOT NULL,
    mean_price REAL NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATE_POS = """
-- Add pos column if missing (idempotent via pragma check)
ALTER TABLE prices ADD COLUMN pos TEXT NOT NULL DEFAULT 'tw';
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the database and ensure schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    # Migrate: add pos column to existing databases
    cols = [row[1] for row in conn.execute("PRAGMA table_info(prices)").fetchall()]
    if "pos" not in cols:
        conn.execute("ALTER TABLE prices ADD COLUMN pos TEXT NOT NULL DEFAULT 'tw'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_route_pos ON prices(origin, dest, depart_date, return_date, cabin, pos)")
        conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_watchlist(path: Path) -> dict:
    """Load and validate the watchlist config."""
    with open(path) as f:
        data = json.load(f)
    if "routes" not in data or not data["routes"]:
        raise ValueError("watchlist.json must contain a non-empty 'routes' array")
    return data


def watchlist_hash(data: dict) -> str:
    """Hash the watchlist config for change tracking."""
    raw = json.dumps(data["routes"], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def run_scan(watchlist: dict, dry_run: bool = False) -> list[dict]:
    """Generate URLs from watchlist and run searches for all POS countries.

    Returns a list of dicts, each with route info, pos, and search results.
    """
    settings = watchlist.get("settings", {})
    top = settings.get("top_per_route", 5)
    currency = settings.get("currency", "TWD")
    pos_countries = settings.get("pos_countries", ["tw"])
    batch_size = settings.get("batch_size", 6)
    batch_delay = settings.get("batch_delay_sec", 5)

    # Build all (route, pos) combinations
    all_urls = []
    all_labels = []
    all_meta = []  # (route_dict, pos_code)

    for route in watchlist["routes"]:
        cabin_name = route.get("cabin", "economy")
        cabin_code = CABIN_MAP.get(cabin_name, 1)
        rt = route.get("return_date", "OW")

        for pos in pos_countries:
            url = build_url(
                origin=route["origin"],
                dest=route["dest"],
                depart_date=route["depart_date"],
                return_date=route.get("return_date"),
                cabin=cabin_code,
                curr=currency,
                gl=pos,
            )
            label = f"{route['origin']}→{route['dest']} {route['depart_date']}~{rt} {cabin_name} [{pos}]"
            all_urls.append(url)
            all_labels.append(label)
            all_meta.append((route, pos))

    if dry_run:
        for label, url in zip(all_labels, all_urls):
            print(f"  {label}")
            print(f"    {url}")
        print(f"\nTotal: {len(all_urls)} queries ({len(watchlist['routes'])} routes x {len(pos_countries)} POS)")
        return []

    # Execute in batches to avoid Google throttling
    total_queries = len(all_urls)
    num_batches = (total_queries + batch_size - 1) // batch_size
    print(f"Scanning {total_queries} queries ({len(watchlist['routes'])} routes x {len(pos_countries)} POS) in {num_batches} batch(es)...")

    scan_data = []
    for batch_idx in range(num_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, total_queries)
        batch_urls = all_urls[start:end]
        batch_labels = all_labels[start:end]
        batch_meta = all_meta[start:end]

        if batch_idx > 0:
            print(f"  Waiting {batch_delay}s before batch {batch_idx + 1}/{num_batches}...")
            time.sleep(batch_delay)

        print(f"  Batch {batch_idx + 1}/{num_batches}: {len(batch_urls)} queries...")
        results = search_urls(batch_urls, batch_labels, top=top, parallel=len(batch_urls) > 1)

        for (route, pos), result in zip(batch_meta, results):
            scan_data.append({"route": route, "pos": pos, "result": result})

    return scan_data


def store_results(conn: sqlite3.Connection, scan_data: list[dict], wl_hash: str) -> int:
    """Store scan results in the database. Returns total records stored."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.execute(
        "INSERT INTO scans (scanned_at, watchlist_hash) VALUES (?, ?)",
        (now, wl_hash),
    )
    scan_id = cursor.lastrowid

    total = 0
    for item in scan_data:
        route = item["route"]
        pos = item["pos"]
        result = item["result"]
        if result.error:
            print(f"  Error for {route['origin']}→{route['dest']} [{pos}]: {result.error}")
            continue
        for flight in result.flights:
            conn.execute(
                """INSERT INTO prices
                   (scan_id, scanned_at, origin, dest, depart_date, return_date,
                    cabin, airline, price, currency, stops, duration,
                    departure_time, arrival_time, pos)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id, now,
                    route["origin"], route["dest"],
                    route["depart_date"], route.get("return_date"),
                    route.get("cabin", "economy"),
                    flight.airline, flight.price, flight.currency,
                    flight.stops, flight.duration,
                    flight.departure, flight.arrival,
                    pos,
                ),
            )
            total += 1

    conn.commit()
    return total


def main():
    parser = argparse.ArgumentParser(description="Price tracker — scan and store flight prices")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Path to watchlist.json")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show URLs without searching")
    parser.add_argument("--alert", action="store_true", help="Run alert check after scan")
    parser.add_argument("--notify", action="store_true", help="Send Telegram on anomaly (passed to price_alert)")
    parser.add_argument("--daily-summary", action="store_true", help="Send daily summary via Telegram (passed to price_alert)")
    args = parser.parse_args()

    watchlist = load_watchlist(args.watchlist)
    wl_hash = watchlist_hash(watchlist)

    if args.dry_run:
        print("Dry run — URLs that would be scanned:")
        run_scan(watchlist, dry_run=True)
        return

    conn = init_db(args.db)
    try:
        scan_data = run_scan(watchlist)
        if not scan_data:
            print("No results to store.")
            return

        total = store_results(conn, scan_data, wl_hash)
        print(f"Stored {total} price record(s).")

        if args.alert:
            import subprocess
            alert_script = Path(__file__).resolve().parent / "price_alert.py"
            cmd = [
                sys.executable, str(alert_script),
                "--db", str(args.db),
                "--watchlist", str(args.watchlist),
            ]
            if args.notify:
                cmd.append("--notify")
            if args.daily_summary:
                cmd.append("--daily-summary")
            subprocess.run(cmd)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
