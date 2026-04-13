#!/usr/bin/env python3
"""ANA Mileage Club interactive login setup.

Launches system Chrome as a normal browser (no automation hooks) so
Akamai bot detection is not triggered. After you log in manually,
press Enter and the script connects via CDP to extract cookies.

Usage:
    python3 tools/ana_setup.py              # open login page
    python3 tools/ana_setup.py --prefill    # pre-fill member number from .env

After logging in, the script saves:
    auth/ana_state.json   — Playwright storageState (cookies + localStorage)
    auth/ana_meta.json    — browser metadata (userAgent)
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

AUTH_DIR = Path(__file__).resolve().parent.parent / "auth"
STATE_PATH = AUTH_DIR / "ana_state.json"
META_PATH = AUTH_DIR / "ana_meta.json"
PROFILE_DIR = AUTH_DIR / "ana_chrome_profile"

CDP_PORT = 9333

SEARCH_URL = (
    "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/"
    "roundtrip/award_search_roundtrip_input.xhtml"
    "?CONNECTION_KIND=JPN&LANG=en"
)

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def load_member_number() -> str | None:
    """Load ANA member number from .env or environment."""
    val = os.environ.get("ANA_MEMBER_NUMBER")
    if val:
        return val

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANA_MEMBER_NUMBER="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


def main():
    parser = argparse.ArgumentParser(
        description="ANA Mileage Club login setup — save cookies for award search"
    )
    parser.add_argument(
        "--prefill", action="store_true",
        help="Pre-fill member number from .env (you still type the password)",
    )
    args = parser.parse_args()

    chrome_path = find_chrome()
    if not chrome_path:
        print("Error: Chrome not found. Install Google Chrome first.")
        sys.exit(1)

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    member_number = load_member_number() if args.prefill else None

    print("=" * 60)
    print("  ANA Mileage Club — Login Setup (CDP mode)")
    print("=" * 60)
    print()
    print("1. A normal Chrome window will open with ANA's login page.")
    print("2. Log in manually (this is a real Chrome — no bot detection).")
    print("3. After login succeeds, come back here and press Enter.")
    print()
    if member_number:
        print(f"  Member number: {member_number[:4]}*** (copy-paste it yourself)")
    print()

    # Launch Chrome with remote debugging, separate profile
    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        SEARCH_URL,
    ]

    print("Launching Chrome...")
    chrome_proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("  Chrome opened. Please log in to ANA in the browser window.")
    print()
    print("Waiting 3 minutes for you to log in...")
    print("  (The script will check every 10s if login succeeded)")
    print()

    # Wait for login by polling CDP endpoint for ANA cookies
    import urllib.request
    import urllib.error

    max_wait = 180  # 3 minutes
    poll_interval = 10
    waited = 0
    cdp_ready = False

    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval

        # Check if CDP is reachable
        try:
            req = urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2
            )
            tabs = json.loads(req.read())
            # Check if any tab has navigated past the login page
            for tab in tabs:
                tab_url = tab.get("url", "")
                if "ana.co.jp" in tab_url and "login" not in tab_url.lower():
                    if "award_search" in tab_url or "mypage" in tab_url:
                        print(f"  Login detected! ({tab_url[:80]}...)")
                        cdp_ready = True
                        break
            if cdp_ready:
                break
        except (urllib.error.URLError, OSError):
            pass

        if waited % 30 == 0:
            print(f"  Still waiting... ({waited}s / {max_wait}s)")

    if not cdp_ready:
        print()
        print("  Timeout or login not auto-detected.")
        print("  Will still try to extract cookies...")

    print()
    print("Connecting to Chrome via CDP to extract cookies...")

    from patchright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            contexts = browser.contexts
            if not contexts:
                print("Error: No browser contexts found. Is Chrome still open?")
                chrome_proc.terminate()
                sys.exit(1)

            context = contexts[0]
            pages = context.pages
            if not pages:
                print("Error: No pages found.")
                chrome_proc.terminate()
                sys.exit(1)

            # Find the ANA page or use the first one
            target_page = pages[0]
            for pg in pages:
                if "ana.co.jp" in pg.url:
                    target_page = pg
                    break

            print(f"  Connected. Current page: {target_page.url}")

            # Save storageState
            state = context.storage_state()
            with open(STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
            print(f"  Saved: {STATE_PATH}")

            # Save browser metadata
            user_agent = target_page.evaluate("() => navigator.userAgent")
            meta = {"userAgent": user_agent}
            with open(META_PATH, "w") as f:
                json.dump(meta, f, indent=2)
            print(f"  Saved: {META_PATH}")

            cookie_count = len(state.get("cookies", []))
            ana_cookies = [
                c for c in state.get("cookies", [])
                if "ana.co.jp" in c.get("domain", "")
            ]
            print(f"  Total cookies: {cookie_count} (ANA: {len(ana_cookies)})")

            # Disconnect (don't close — let user keep browsing if they want)
            browser.close()

    except Exception as e:
        print(f"Error connecting to Chrome: {e}")
        print("  Make sure Chrome is still open and you completed login.")
        chrome_proc.terminate()
        sys.exit(1)

    print()
    print("Setup complete! You can now close Chrome and run:")
    print("  python3 tools/ana_award_search.py TPE NRT 2026-10-01 --top 5")
    print()

    # Terminate Chrome
    chrome_proc.terminate()


if __name__ == "__main__":
    main()
