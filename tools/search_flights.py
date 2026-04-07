#!/usr/bin/env python3
"""Automated Google Flights search using Playwright.

Removes LLM from the search loop — opens URLs, clicks search, parses results.
Each search runs in an isolated incognito context to avoid session interference.

Usage:
    # Single URL
    python3 tools/search_flights.py "<google-flights-url>"

    # Multiple URLs (sequential)
    python3 tools/search_flights.py "<url1>" "<url2>" "<url3>"

    # Batch from file (one URL per line)
    python3 tools/search_flights.py --file urls.txt

    # Parallel execution
    python3 tools/search_flights.py --parallel "<url1>" "<url2>" "<url3>"

    # Limit results per search
    python3 tools/search_flights.py --top 5 "<url>"

    # Output format
    python3 tools/search_flights.py --format table "<url>"   # human-readable (default)
    python3 tools/search_flights.py --format json "<url>"    # machine-readable

    # With labels for each URL
    python3 tools/search_flights.py --labels "9/1 RT,9/2 RT" "<url1>" "<url2>"
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, Page


@dataclass
class Flight:
    airline: str
    price: int
    currency: str
    stops: int
    duration: str
    departure: str
    arrival: str
    stop_details: str


@dataclass
class SearchResult:
    url: str
    label: str
    flights: list[Flight]
    error: str | None


def parse_aria_label(label: str) -> Flight | None:
    """Parse a Google Flights aria-label string into a Flight object."""
    if not label:
        return None

    # Price: "來回總價 113233 新台幣起" or "總價 65899 新台幣起" or just numbers
    price_m = re.search(r"總價\s*([\d,]+)\s*新台幣", label)
    if not price_m:
        price_m = re.search(r"([\d,]+)\s*新台幣", label)
    price = int(price_m.group(1).replace(",", "")) if price_m else 0

    # Airline: "搭乘XXX的航班" or "搭乘XXX和YYY的航班"
    airline_m = re.search(r"搭乘(.+?)的航班", label)
    airline = airline_m.group(1) if airline_m else "unknown"

    # Stops: "中途停留 N 次" or "直達航班"
    stops_m = re.search(r"中途停留\s*(\d+)\s*次", label)
    stops = int(stops_m.group(1)) if stops_m else 0

    # Duration: "總交通時間：22 小時 10 分鐘"
    dur_m = re.search(r"總交通時間[：:]\s*(.+?)(?:\s*第|\s*選擇|\s*$)", label)
    duration = dur_m.group(1).strip() if dur_m else ""

    # Departure/arrival times from the label
    # Pattern: "晚上7:25 於臺灣桃園國際機場出發" ... "中午12:35 抵達雅典..."
    dep_m = re.search(r"(\S+\d+:\d+)\s*於.*?出發", label)
    departure = dep_m.group(1) if dep_m else ""

    arr_m = re.search(r"(\S+\d+:\d+)\s*抵達", label)
    arrival = arr_m.group(1) if arr_m else ""

    # Stop details: extract clean layover info
    # Pattern: "於XXX的YYY停留 N 小時 M 分鐘"
    # Limit place to 40 chars to avoid matching "於臺灣桃園國際機場出發...停留"
    stop_details_all = re.findall(
        r"於(.{2,40}?)停留\s*(\d+\s*小時(?:\s*\d+\s*分鐘)?)", label
    )
    stop_parts = []
    for place, dur in stop_details_all:
        place = place.strip()
        # Shorten: "新加坡的新加坡樟宜國際機場" → "新加坡"
        if "的" in place:
            place = place.split("的")[0]
        stop_parts.append(f"{place} {dur}")
    stop_details = "; ".join(stop_parts)

    return Flight(
        airline=airline,
        price=price,
        currency="TWD",
        stops=stops,
        duration=duration,
        departure=departure,
        arrival=arrival,
        stop_details=stop_details,
    )


def _is_one_way_url(url: str) -> bool:
    """Heuristic: count field-3 (leg) occurrences in the tfs protobuf.

    One-way has 1 leg, round-trip has 2. We detect by checking
    if the protobuf contains only one leg segment.
    """
    import base64
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    tfs = params.get("tfs", [""])[0]
    if not tfs:
        return False

    # Add padding
    tfs_padded = tfs + "=" * (-len(tfs) % 4)
    try:
        data = base64.urlsafe_b64decode(tfs_padded)
    except Exception:
        return False

    # Count field 3 (wire type 2 = length-delimited): tag byte = (3 << 3) | 2 = 0x1a
    return data.count(b"\x1a") == 1


def _switch_to_one_way(page: Page) -> None:
    """Switch the Google Flights form from round-trip to one-way mode."""
    # The trip-type menu items ('來回', '單程', '多停點') are in the DOM
    # even without opening the dropdown. Click '單程' directly via JS.
    page.evaluate("""() => {
        const items = document.querySelectorAll('li');
        for (const li of items) {
            if (li.textContent.trim() === '單程') {
                li.click();
                return true;
            }
        }
        return false;
    }""")
    page.wait_for_timeout(1000)


def search_one_url(browser: Browser, url: str, label: str, top: int) -> SearchResult:
    """Search a single Google Flights URL in an isolated incognito context."""
    context = None
    try:
        # Incognito context — no cookies, no session interference
        context = browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        page = context.new_page()

        # Navigate to the pre-filled search URL
        page.goto(url, wait_until="networkidle", timeout=30000)

        # For one-way URLs, switch form to one-way mode
        if _is_one_way_url(url):
            _switch_to_one_way(page)

        # Click the search button
        search_btn = page.get_by_role("button", name="搜尋航班")
        if search_btn.count() > 0:
            search_btn.first.click()
        else:
            # Fallback: try clicking any button containing "搜尋"
            fallback = page.locator("button:has-text('搜尋')").first
            if fallback.is_visible():
                fallback.click()

        # Wait for results to load
        page.wait_for_load_state("networkidle", timeout=30000)
        # Extra wait for dynamic rendering
        page.wait_for_timeout(3000)

        # Retry once if no results found (page may need more time)
        if page.locator("li.pIav2d").count() == 0:
            page.wait_for_timeout(3000)

        # Extract aria-labels from flight result cards
        aria_labels = page.evaluate(
            """() => {
            const items = document.querySelectorAll('li.pIav2d');
            return Array.from(items).map(li => {
                const link = li.querySelector('.JMc5Xc');
                return link ? link.getAttribute('aria-label') : null;
            }).filter(Boolean);
        }"""
        )

        flights = []
        for al in aria_labels:
            flight = parse_aria_label(al)
            if flight and flight.price > 0:
                flights.append(flight)

        # Deduplicate by (airline, price, duration)
        seen = set()
        unique_flights = []
        for f in flights:
            key = (f.airline, f.price, f.duration)
            if key not in seen:
                seen.add(key)
                unique_flights.append(f)

        # Sort by price
        unique_flights.sort(key=lambda f: f.price)

        if top > 0:
            unique_flights = unique_flights[:top]

        return SearchResult(url=url, label=label, flights=unique_flights, error=None)

    except Exception as e:
        return SearchResult(url=url, label=label, flights=[], error=str(e))

    finally:
        if context:
            context.close()


def search_urls_sequential(
    urls: list[str],
    labels: list[str],
    top: int = 10,
) -> list[SearchResult]:
    """Search multiple URLs sequentially, sharing one browser."""
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for url, lbl in zip(urls, labels):
            result = search_one_url(browser, url, lbl, top)
            results.append(result)
        browser.close()
    return results


def search_urls_parallel(
    urls: list[str],
    labels: list[str],
    top: int = 10,
    max_workers: int = 4,
) -> list[SearchResult]:
    """Search multiple URLs in parallel using subprocesses.

    Playwright sync API doesn't support threading, so we spawn separate
    processes — each gets its own browser and incognito context.
    """
    script = str(Path(__file__).resolve())
    procs = []
    for url, lbl in zip(urls, labels):
        cmd = [
            sys.executable, script,
            "--format", "json", "--top", str(top), "--labels", lbl, url,
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        procs.append((proc, lbl, url))

    results = []
    for proc, lbl, url in procs:
        stdout, stderr = proc.communicate(timeout=120)
        if proc.returncode == 0:
            try:
                data = json.loads(stdout.decode())
                flights = [
                    Flight(**f) for f in data[0].get("flights", [])
                ]
                results.append(SearchResult(
                    url=url, label=lbl, flights=flights, error=data[0].get("error"),
                ))
            except (json.JSONDecodeError, IndexError, KeyError) as e:
                results.append(SearchResult(url=url, label=lbl, flights=[], error=f"Parse error: {e}"))
        else:
            err_msg = stderr.decode().strip().split("\n")[-1] if stderr else "Unknown error"
            results.append(SearchResult(url=url, label=lbl, flights=[], error=err_msg))

    return results


def search_urls(
    urls: list[str],
    labels: list[str],
    top: int = 10,
    parallel: bool = False,
) -> list[SearchResult]:
    """Search multiple URLs, optionally in parallel."""
    if parallel and len(urls) > 1:
        return search_urls_parallel(urls, labels, top)
    return search_urls_sequential(urls, labels, top)


def format_table(results: list[SearchResult]) -> str:
    """Format results as human-readable tables."""
    lines = []
    for res in results:
        lines.append(f"\n{'='*70}")
        lines.append(f"🔍 {res.label}")
        if res.error:
            lines.append(f"  ❌ Error: {res.error}")
            continue
        if not res.flights:
            lines.append("  ⚠️  No results found")
            continue

        lines.append(
            f"  {'#':<3} {'航空公司':<20} {'票價(TWD)':>10} {'轉機':>4} {'飛行時間':<18} {'出發':>8} {'抵達':>10}"
        )
        lines.append(f"  {'-'*3} {'-'*20} {'-'*10} {'-'*4} {'-'*18} {'-'*8} {'-'*10}")
        for i, f in enumerate(res.flights, 1):
            lines.append(
                f"  {i:<3} {f.airline:<20} {f.price:>10,} {f.stops:>4} {f.duration:<18} {f.departure:>8} {f.arrival:>10}"
            )
            if f.stop_details:
                lines.append(f"      └ 轉機: {f.stop_details}")

    return "\n".join(lines)


def format_json(results: list[SearchResult]) -> str:
    """Format results as JSON."""
    data = []
    for res in results:
        data.append(
            {
                "label": res.label,
                "url": res.url,
                "error": res.error,
                "flights": [asdict(f) for f in res.flights],
            }
        )
    return json.dumps(data, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Automated Google Flights search via Playwright"
    )
    parser.add_argument("urls", nargs="*", help="Google Flights search URLs")
    parser.add_argument("--file", help="Read URLs from file (one per line)")
    parser.add_argument(
        "--labels", help="Comma-separated labels for each URL"
    )
    parser.add_argument(
        "--top", type=int, default=10, help="Max results per URL (default: 10)"
    )
    parser.add_argument(
        "--parallel", action="store_true", help="Run searches in parallel"
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file) as f:
            urls.extend(line.strip() for line in f if line.strip())

    if not urls:
        parser.error("No URLs provided. Pass URLs as arguments or use --file.")

    if args.labels:
        labels = args.labels.split(",")
    else:
        labels = [f"Search {i + 1}" for i in range(len(urls))]

    # Pad labels if needed
    while len(labels) < len(urls):
        labels.append(f"Search {len(labels) + 1}")

    results = search_urls(urls, labels, top=args.top, parallel=args.parallel)

    if args.format == "json":
        print(format_json(results))
    else:
        print(format_table(results))


if __name__ == "__main__":
    main()
