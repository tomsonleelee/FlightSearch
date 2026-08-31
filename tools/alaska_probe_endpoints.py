#!/usr/bin/env python3
"""One-off probe: watch what the Alaska award search page actually calls.

Loads the public award-results page in a headless browser and records every
XHR/fetch it makes, so we can tell whether a month-at-a-time ("flexible
dates" / calendar) endpoint exists. If one does, the daily sweep can drop
from ~10k per-day requests to ~330 per-month requests.

Read-only: navigates public URLs, no login, no booking, no point transfer.

    python3 tools/alaska_probe_endpoints.py --origin BKK --destination FRA \
        --date 2026-10-20 [--flexible] [--seconds 25]
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

SEARCH_URL = "https://www.alaskaair.com/search/results"
INTERESTING = ("api", "shop", "search", "avail", "award", "fare", "calendar", "flex", "price")


def build_url(origin: str, destination: str, date: str, passengers: int, flexible: bool) -> str:
    query = {
        "A": passengers,
        "C": 0,
        "D": destination,
        "L": 0,
        "O": origin,
        "OD": date,
        "OT": "Anytime",
        "RT": "false",
        "ShoppingMethod": "onlineaward",
        "UPG": "none",
        "awardType": "MilesOnly",
    }
    if flexible:
        # Best-effort: some Alaska flows accept a flexible-date flag. Harmless
        # if ignored — we are only observing what the page then requests.
        query["FL"] = "true"
    return f"{SEARCH_URL}?{urlencode(query)}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Alaska award search network calls")
    parser.add_argument("--origin", default="BKK")
    parser.add_argument("--destination", default="FRA")
    parser.add_argument("--date", default="2026-10-20")
    parser.add_argument("--passengers", type=int, default=2)
    parser.add_argument("--flexible", action="store_true")
    parser.add_argument("--seconds", type=int, default=25, help="How long to watch after load")
    args = parser.parse_args()

    url = build_url(args.origin, args.destination, args.date, args.passengers, args.flexible)
    calls: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        def on_request(request) -> None:  # noqa: ANN001 — playwright type
            if request.resource_type not in {"xhr", "fetch"}:
                return
            try:
                # Analytics beacons post binary bodies; decoding them raises.
                post = request.post_data or ""
            except Exception:  # noqa: BLE001 — never let the listener kill the probe
                post = "<binary>"
            calls.append({"method": request.method, "url": request.url, "post": post})

        page.on("request", on_request)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(args.seconds * 1000)
        title = page.title()
        body_len = len(page.content())
        browser.close()

    print(f"page title: {title}")
    print(f"rendered html: {body_len:,} chars")
    print(f"xhr/fetch calls: {len(calls)}\n")

    ranked = sorted(
        calls,
        key=lambda c: any(k in str(c["url"]).lower() for k in INTERESTING),
        reverse=True,
    )
    for call in ranked[:40]:
        mark = "★" if any(k in str(call["url"]).lower() for k in INTERESTING) else " "
        print(f"{mark} {call['method']:5} {str(call['url'])[:170]}")
        if call["post"]:
            print(f"        body: {str(call['post'])[:200]}")

    with open("/tmp/alaska_probe_calls.json", "w", encoding="utf-8") as fh:
        json.dump(calls, fh, ensure_ascii=False, indent=2)
    print("\nfull list -> /tmp/alaska_probe_calls.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
