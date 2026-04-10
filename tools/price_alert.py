#!/usr/bin/env python3
"""Price anomaly detection — reads historical prices from SQLite, computes
Z-scores per route, and alerts on unusually low prices.

Supports multi-POS comparison: highlights cross-market price differences
and recommends the cheapest POS for each route.

Can run standalone or be called from price_tracker.py after a scan.

Usage:
    python3 tools/price_alert.py                          # check all routes
    python3 tools/price_alert.py --db data/prices.db      # custom DB path
    python3 tools/price_alert.py --notify                  # also send Telegram
    python3 tools/price_alert.py --summary                 # show price summary
    python3 tools/price_alert.py --daily-summary           # send daily Telegram summary
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

POS_LABELS = {
    "tw": "TW", "th": "TH", "tr": "TR",
    "in": "IN", "mx": "MX", "id": "ID",
}


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


def get_route_history(conn: sqlite3.Connection, route: dict, pos: str = "tw") -> list[tuple[str, int]]:
    """Get historical min prices per scan for a route+POS.

    Returns list of (scanned_at, min_price) tuples, ordered by time.
    """
    query = """
        SELECT scanned_at, MIN(price) as min_price
        FROM prices
        WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ? AND pos = ?
          AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
        GROUP BY scan_id
        ORDER BY scanned_at
    """
    return_date = route.get("return_date")
    rows = conn.execute(query, (
        route["origin"], route["dest"], route["depart_date"],
        route.get("cabin", "economy"), pos, return_date, return_date,
    )).fetchall()
    return rows


def get_latest_flights(conn: sqlite3.Connection, route: dict, pos: str = "tw") -> list[tuple]:
    """Get flights from the most recent scan for a route+POS."""
    query = """
        SELECT airline, price, stops, duration, departure_time, arrival_time
        FROM prices
        WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ? AND pos = ?
          AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
          AND scan_id = (
              SELECT MAX(scan_id) FROM prices
              WHERE origin = ? AND dest = ? AND depart_date = ? AND cabin = ? AND pos = ?
                AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
          )
        ORDER BY price
    """
    return_date = route.get("return_date")
    params = (
        route["origin"], route["dest"], route["depart_date"],
        route.get("cabin", "economy"), pos, return_date, return_date,
    )
    return conn.execute(query, params * 2).fetchall()


def get_latest_min_by_pos(conn: sqlite3.Connection, route: dict, pos_list: list[str]) -> dict[str, tuple]:
    """Get the cheapest flight per POS from the latest scan.

    Returns {pos: (airline, price)} for each POS that has data.
    """
    result = {}
    for pos in pos_list:
        flights = get_latest_flights(conn, route, pos)
        if flights:
            result[pos] = (flights[0][0], flights[0][1])  # (airline, price)
    return result


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
    """Check all routes for anomalies (using default POS 'tw' for Z-score).

    Returns list of triggered alerts.
    """
    settings = watchlist.get("settings", {})
    z_threshold = settings.get("z_threshold", -2.0)
    min_samples = settings.get("min_samples", 5)
    pos_countries = settings.get("pos_countries", ["tw"])

    # Telegram config
    tg_config = watchlist.get("notifications", {}).get("telegram", {})
    tg_enabled = notify and tg_config.get("enabled", False)
    bot_token = os.environ.get(tg_config.get("bot_token_env", ""), "")
    chat_id = os.environ.get(tg_config.get("chat_id_env", ""), "")

    triggered = []

    for route in watchlist["routes"]:
        route_label = f"{route['origin']}→{route['dest']} {route['depart_date']}"

        # Z-score per POS: find the best anomaly across all POS
        best_anomaly = None
        for pos in pos_countries:
            history = get_route_history(conn, route, pos)
            if len(history) < min_samples:
                continue

            historical_mins = [row[1] for row in history]
            current_min = historical_mins[-1]
            mean_price = statistics.mean(historical_mins)
            z_score = compute_zscore(historical_mins[:-1], current_min)
            if z_score is None:
                continue

            if z_score < z_threshold:
                if best_anomaly is None or current_min < best_anomaly["price"]:
                    latest = get_latest_flights(conn, route, pos)
                    airline = latest[0][0] if latest else "Unknown"
                    best_anomaly = {
                        "pos": pos, "price": current_min, "airline": airline,
                        "z_score": z_score, "mean": mean_price,
                    }

        # Report status
        # Get default POS history for display
        default_history = get_route_history(conn, route, pos_countries[0])
        if default_history:
            default_min = default_history[-1][1]
            default_mean = statistics.mean([r[1] for r in default_history])
            default_z = compute_zscore([r[1] for r in default_history[:-1]], default_min)
            z_str = f"z={default_z:+.2f}" if default_z is not None else "z=n/a"
            print(f"  {route_label}: NT${default_min:,} (mean NT${default_mean:,.0f}, {z_str})", end="")
        else:
            samples = len(get_route_history(conn, route, pos_countries[0]))
            print(f"  {route_label}: {samples}/{min_samples} samples", end="")

        if best_anomaly:
            a = best_anomaly
            if check_already_alerted(conn, route, a["airline"], a["price"]):
                print(f" !! [{a['pos']}] (already alerted today)")
                continue

            print(f" !! ANOMALY [{a['pos']}] NT${a['price']:,}")

            notified = False
            if tg_enabled and bot_token and chat_id:
                tg_msg = format_telegram_message(route, a["airline"], a["price"], a["z_score"], a["mean"])
                tg_msg += f"\nPOS: {a['pos'].upper()}"
                notified = send_telegram(bot_token, chat_id, tg_msg)
                if notified:
                    print("    Telegram notification sent.")

            record_alert(conn, route, a["airline"], a["price"], a["z_score"], a["mean"], notified)
            triggered.append({
                "route": route_label, "pos": a["pos"],
                "airline": a["airline"], "price": a["price"],
                "z_score": a["z_score"], "mean": a["mean"],
            })
        else:
            print()

    return triggered


def compute_trend(prices: list[int]) -> str:
    """Compute price trend from recent history."""
    if len(prices) < 3:
        return "—"
    overall_mean = statistics.mean(prices)
    recent_mean = statistics.mean(prices[-3:])
    pct = (recent_mean - overall_mean) / overall_mean * 100
    if pct < -5:
        return "↓↓ dropping"
    elif pct < -2:
        return "↓ declining"
    elif pct > 5:
        return "↑↑ rising"
    elif pct > 2:
        return "↑ rising"
    return "— stable"


def build_daily_summary(conn: sqlite3.Connection, watchlist: dict) -> list[str]:
    """Build daily price summary messages for Telegram.

    Returns a list of message strings (split to stay under Telegram's
    4096 char limit).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    settings = watchlist.get("settings", {})
    pos_countries = settings.get("pos_countries", ["tw"])
    savings_threshold = settings.get("pos_savings_threshold", 0.15)

    lines = [f"<b>Daily Price Summary</b> ({now} UTC)\n"]

    for route in watchlist["routes"]:
        cabin = route.get("cabin", "economy")
        rt = route.get("return_date", "OW")
        header = f"{route['origin']} → {route['dest']} {cabin} {route['depart_date']}~{rt}"

        # Collect cheapest per POS
        pos_prices = get_latest_min_by_pos(conn, route, pos_countries)
        if not pos_prices:
            lines.append(f"\n{header}\n  No data\n")
            continue

        # Find cheapest POS
        cheapest_pos = min(pos_prices, key=lambda p: pos_prices[p][1])
        cheapest_airline, cheapest_price = pos_prices[cheapest_pos]

        # Default POS (first in list) for trend
        default_pos = pos_countries[0]
        history = get_route_history(conn, route, default_pos)
        historical_mins = [row[1] for row in history]
        mean_price = statistics.mean(historical_mins) if historical_mins else cheapest_price
        trend = compute_trend(historical_mins)

        # Build POS comparison line
        pos_parts = []
        for pos in pos_countries:
            if pos in pos_prices:
                _, p = pos_prices[pos]
                marker = " *" if pos == cheapest_pos else ""
                pos_parts.append(f"{POS_LABELS.get(pos, pos)}:{p:,}{marker}")

        lines.append(f"\n{header}")
        lines.append(f"  NT${cheapest_price:,} ({cheapest_airline}) [{cheapest_pos.upper()}]")
        lines.append(f"  mean NT${mean_price:,.0f} | {trend}")
        lines.append(f"  POS: {' | '.join(pos_parts)}")

        # Cross-POS savings alert
        if default_pos in pos_prices and cheapest_pos != default_pos:
            default_price = pos_prices[default_pos][1]
            if default_price > 0:
                savings = default_price - cheapest_price
                pct = savings / default_price
                if pct >= savings_threshold:
                    lines.append(
                        f"  <b>Buy from {cheapest_pos.upper()} POS: save NT${savings:,} (-{pct:.0%})</b>"
                    )

    # Split into messages under 4096 chars
    messages = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3900:
            messages.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        messages.append(current)

    return messages


def send_daily_summary(conn: sqlite3.Connection, watchlist: dict) -> None:
    """Build and send the daily price summary via Telegram."""
    tg_config = watchlist.get("notifications", {}).get("telegram", {})
    bot_token = os.environ.get(tg_config.get("bot_token_env", ""), "")
    chat_id = os.environ.get(tg_config.get("chat_id_env", ""), "")

    messages = build_daily_summary(conn, watchlist)
    for msg in messages:
        print(msg)

    if tg_config.get("enabled", False) and bot_token and chat_id:
        for msg in messages:
            ok = send_telegram(bot_token, chat_id, msg)
            if not ok:
                print("  Failed to send daily summary to Telegram.")
                return
        print(f"  Daily summary sent to Telegram ({len(messages)} message(s)).")
    else:
        print("  Telegram not enabled — summary printed to terminal only.")


def print_summary(conn: sqlite3.Connection, watchlist: dict) -> None:
    """Print a summary of historical prices for all routes."""
    settings = watchlist.get("settings", {})
    pos_countries = settings.get("pos_countries", ["tw"])

    print("\n=== Price History Summary ===\n")
    for route in watchlist["routes"]:
        route_label = f"{route['origin']}→{route['dest']} {route['depart_date']}~{route.get('return_date', 'OW')} {route.get('cabin', 'economy')}"
        print(f"  {route_label}")

        for pos in pos_countries:
            history = get_route_history(conn, route, pos)
            if not history:
                continue
            prices = [row[1] for row in history]
            stdev_str = f", stdev NT${statistics.stdev(prices):,.0f}" if len(prices) >= 2 else ""
            print(f"    [{pos.upper()}] {len(prices)} scans: "
                  f"min NT${min(prices):,} / max NT${max(prices):,} / "
                  f"mean NT${statistics.mean(prices):,.0f}{stdev_str}")

        print()


def main():
    parser = argparse.ArgumentParser(description="Price anomaly detection via Z-score")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Path to watchlist.json")
    parser.add_argument("--notify", action="store_true", help="Send Telegram notifications")
    parser.add_argument("--summary", action="store_true", help="Show price history summary")
    parser.add_argument("--daily-summary", action="store_true", help="Send daily price summary via Telegram")
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

        if args.daily_summary:
            send_daily_summary(conn, watchlist)

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
