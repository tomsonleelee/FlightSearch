#!/usr/bin/env python3
"""Price anomaly detection — reads historical prices from SQLite, computes
Z-scores per route, and alerts on unusually low prices.

Can run standalone or be called from price_tracker.py after a scan.

Usage:
    python3 tools/price_alert.py                          # check all routes
    python3 tools/price_alert.py --db data/prices.db      # custom DB path
    python3 tools/price_alert.py --notify                  # also send Telegram
    python3 tools/price_alert.py --summary                 # show price summary
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.db"
DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.json"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def load_watchlist(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def get_route_history(conn: sqlite3.Connection, route: dict) -> list[tuple[str, int]]:
    """Get historical min prices per scan for a route.

    Returns list of (scanned_at, min_price) tuples, ordered by time.
    """
    query = """
        SELECT scanned_at, MIN(price) as min_price
        FROM prices
        WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ?
          AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
        GROUP BY scan_id
        ORDER BY scanned_at
    """
    return_date = route.get("return_date")
    rows = conn.execute(query, (
        route["origin"], route["dest"], route["depart_date"],
        route.get("cabin", "economy"), return_date, return_date,
    )).fetchall()
    return rows


def get_latest_flights(conn: sqlite3.Connection, route: dict) -> list[tuple]:
    """Get flights from the most recent scan for a route."""
    query = """
        SELECT airline, price, stops, duration, departure_time, arrival_time
        FROM prices
        WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ?
          AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
          AND scan_id = (
              SELECT MAX(scan_id) FROM prices
              WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ?
                AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
          )
        ORDER BY price
    """
    return_date = route.get("return_date")
    params = (
        route["origin"], route["dest"], route["depart_date"],
        route.get("cabin", "economy"), return_date, return_date,
    )
    return conn.execute(query, params * 2).fetchall()


def compute_zscore(prices: list[int], current: int) -> float | None:
    """Compute Z-score for current price against historical prices."""
    if len(prices) < 2:
        return None
    try:
        mean = statistics.mean(prices)
        stdev = statistics.stdev(prices)
        if stdev == 0:
            return 0.0
        return (current - mean) / stdev
    except statistics.StatisticsError:
        return None


def check_already_alerted(
    conn: sqlite3.Connection, route: dict, airline: str, price: int,
) -> bool:
    """Check if we already sent an alert for this route+airline+price today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        """SELECT COUNT(*) FROM alerts
           WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ?
             AND airline = ? AND price = ?
             AND created_at LIKE ?""",
        (
            route["origin"], route["dest"], route["depart_date"],
            route.get("cabin", "economy"), airline, price, f"{today}%",
        ),
    ).fetchone()
    return row[0] > 0


def record_alert(
    conn: sqlite3.Connection, route: dict,
    airline: str, price: int, z_score: float, mean_price: float,
    notified: bool,
) -> None:
    """Record an alert in the database."""
    conn.execute(
        """INSERT INTO alerts
           (origin, dest, depart_date, return_date, cabin,
            airline, price, z_score, mean_price, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            route["origin"], route["dest"], route["depart_date"],
            route.get("return_date"), route.get("cabin", "economy"),
            airline, price, z_score, mean_price, int(notified),
        ),
    )
    conn.commit()


def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a Telegram message via Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"})
    req = urllib.request.Request(
        url, data=payload.encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        print(f"  Telegram send failed: {e}")
        return False


def format_alert_message(route: dict, airline: str, price: int,
                         z_score: float, mean_price: float) -> str:
    """Format alert for both terminal and Telegram."""
    cabin_label = route.get("cabin", "economy")
    rt = route.get("return_date", "one-way")
    return (
        f"Anomaly detected\n"
        f"{route['origin']} > {route['dest']} {cabin_label}\n"
        f"{route['depart_date']}~{rt}  {airline}\n"
        f"NT${price:,} (mean NT${mean_price:,.0f})\n"
        f"Z-score: {z_score:.2f}"
    )


def format_telegram_message(route: dict, airline: str, price: int,
                            z_score: float, mean_price: float) -> str:
    cabin_label = route.get("cabin", "economy")
    rt = route.get("return_date", "one-way")
    return (
        f"<b>Price Alert</b>\n"
        f"{route['origin']} → {route['dest']} {cabin_label}\n"
        f"{route['depart_date']}~{rt}\n"
        f"{airline}\n"
        f"<b>NT${price:,}</b> (mean NT${mean_price:,.0f})\n"
        f"Z-score: {z_score:.2f}"
    )


def run_alerts(
    conn: sqlite3.Connection, watchlist: dict, notify: bool = False,
) -> list[dict]:
    """Check all routes for anomalies. Returns list of triggered alerts."""
    settings = watchlist.get("settings", {})
    z_threshold = settings.get("z_threshold", -2.0)
    min_samples = settings.get("min_samples", 5)

    # Telegram config
    tg_config = watchlist.get("notifications", {}).get("telegram", {})
    tg_enabled = notify and tg_config.get("enabled", False)
    bot_token = os.environ.get(tg_config.get("bot_token_env", ""), "")
    chat_id = os.environ.get(tg_config.get("chat_id_env", ""), "")

    triggered = []

    for route in watchlist["routes"]:
        route_label = f"{route['origin']}→{route['dest']} {route['depart_date']}"
        history = get_route_history(conn, route)

        if len(history) < min_samples:
            print(f"  {route_label}: {len(history)}/{min_samples} samples (skipped)")
            continue

        historical_mins = [row[1] for row in history]
        current_min = historical_mins[-1]
        mean_price = statistics.mean(historical_mins)
        z_score = compute_zscore(historical_mins[:-1], current_min)

        if z_score is None:
            continue

        # Get the airline for the cheapest flight in latest scan
        latest = get_latest_flights(conn, route)
        airline = latest[0][0] if latest else "Unknown"

        status = f"NT${current_min:,} (mean NT${mean_price:,.0f}, z={z_score:+.2f})"

        if z_score < z_threshold:
            # Check dedup
            if check_already_alerted(conn, route, airline, current_min):
                print(f"  {route_label}: {status} !! (already alerted today)")
                continue

            print(f"  {route_label}: {status} !! ANOMALY")
            msg = format_alert_message(route, airline, current_min, z_score, mean_price)
            print(f"\n{msg}\n")

            # Telegram notification
            notified = False
            if tg_enabled and bot_token and chat_id:
                tg_msg = format_telegram_message(route, airline, current_min, z_score, mean_price)
                notified = send_telegram(bot_token, chat_id, tg_msg)
                if notified:
                    print("  Telegram notification sent.")

            record_alert(conn, route, airline, current_min, z_score, mean_price, notified)
            triggered.append({
                "route": route_label, "airline": airline,
                "price": current_min, "z_score": z_score,
                "mean": mean_price,
            })
        else:
            print(f"  {route_label}: {status}")

    return triggered


def print_summary(conn: sqlite3.Connection, watchlist: dict) -> None:
    """Print a summary of historical prices for all routes."""
    print("\n=== Price History Summary ===\n")
    for route in watchlist["routes"]:
        route_label = f"{route['origin']}→{route['dest']} {route['depart_date']}~{route.get('return_date', 'OW')} {route.get('cabin', 'economy')}"
        history = get_route_history(conn, route)

        if not history:
            print(f"  {route_label}: no data")
            continue

        prices = [row[1] for row in history]
        print(f"  {route_label}")
        print(f"    Scans: {len(prices)}")
        print(f"    Min:   NT${min(prices):,}")
        print(f"    Max:   NT${max(prices):,}")
        print(f"    Mean:  NT${statistics.mean(prices):,.0f}")
        if len(prices) >= 2:
            print(f"    Stdev: NT${statistics.stdev(prices):,.0f}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Price anomaly detection via Z-score")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Path to watchlist.json")
    parser.add_argument("--notify", action="store_true", help="Send Telegram notifications")
    parser.add_argument("--summary", action="store_true", help="Show price history summary")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)

    if not args.db.exists():
        print(f"Database not found: {args.db}")
        print("Run price_tracker.py first to collect data.")
        sys.exit(1)

    watchlist = load_watchlist(args.watchlist)
    conn = sqlite3.connect(str(args.db))

    try:
        if args.summary:
            print_summary(conn, watchlist)

        print("Checking for price anomalies...")
        triggered = run_alerts(conn, watchlist, notify=args.notify)

        if triggered:
            print(f"\n{len(triggered)} anomaly alert(s) triggered.")
        else:
            print("\nNo anomalies detected.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
