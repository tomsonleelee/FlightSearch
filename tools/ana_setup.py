#!/usr/bin/env python3
"""ANA Mileage Club interactive login setup.

Opens a headed browser for manual login, then saves session cookies
for reuse by ana_award_search.py. This bypasses Akamai bot detection
because the login is performed by a human.

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
import sys
from pathlib import Path

AUTH_DIR = Path(__file__).resolve().parent.parent / "auth"
STATE_PATH = AUTH_DIR / "ana_state.json"
META_PATH = AUTH_DIR / "ana_meta.json"

SEARCH_URL = (
    "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/"
    "roundtrip/award_search_roundtrip_input.xhtml"
    "?CONNECTION_KIND=JPN&LANG=en"
)


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

    from patchright.sync_api import sync_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  ANA Mileage Club — Login Setup")
    print("=" * 60)
    print()
    print("A browser window will open with ANA's login page.")
    print("Please log in manually. Once logged in, the script")
    print("will save your session cookies for future searches.")
    print()

    member_number = load_member_number() if args.prefill else None
    if member_number:
        print(f"  Member number: {member_number[:4]}*** (will be pre-filled)")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        # Navigate to search URL — redirects to login page
        print("Opening ANA login page...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        # Dismiss popups
        for selector in [
            "button:has-text('Accept')",
            "#onetrust-accept-btn-handler",
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=1000):
                    el.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

        # Pre-fill member number if requested
        if member_number:
            account_field = page.locator("#accountNumber")
            if account_field.count() > 0:
                account_field.first.fill(member_number)
                print("  Member number pre-filled. Please enter your password and click Login.")
            else:
                print("  Warning: Could not find login form to pre-fill.")
        else:
            print("  Please enter your member number, password, and click Login.")

        print()
        print("Waiting for login...")
        print("  (The script will detect when you're logged in)")
        print()

        # Poll for login success:
        # - URL changes to award search page (redirect after login)
        # - Or logout button appears
        # - Or we land on a page that's not the login page
        max_wait = 300  # 5 minutes
        poll_interval = 2000  # 2 seconds
        waited = 0
        logged_in = False

        while waited < max_wait:
            page.wait_for_timeout(poll_interval)
            waited += poll_interval // 1000

            current_url = page.url

            # Check for traffic block
            body_text = page.evaluate(
                "() => document.body.innerText.substring(0, 500)"
            )
            if "heavy traffic" in body_text.lower() or "server maintenance" in body_text.lower():
                print("  ANA returned 'heavy traffic' page — this may be temporary.")
                print("  Try refreshing the page in the browser (F5) and logging in again.")
                continue

            # Success: redirected to search page
            if "award_search" in current_url.lower() or "award" in current_url.lower():
                if "login" not in current_url.lower():
                    logged_in = True
                    break

            # Success: mypage or other authenticated page
            if "mypage" in current_url.lower():
                logged_in = True
                break

            # Success: logout button appeared (we're on some authenticated page)
            logout = page.locator("li.btnLogoutArea")
            if logout.count() > 0:
                try:
                    if logout.first.is_visible(timeout=500):
                        logged_in = True
                        break
                except Exception:
                    pass

            # Still on login page — keep waiting
            if waited % 10 == 0:
                print(f"  Still waiting... ({waited}s)")

        if not logged_in:
            print()
            print("Timeout — login was not detected within 5 minutes.")
            print("If you did log in, the cookies will still be saved.")
            print()

        # Save storageState regardless (user might have logged in
        # even if our detection didn't catch it)
        print("Saving session cookies...")
        state = context.storage_state()
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        print(f"  Saved: {STATE_PATH}")

        # Save browser metadata
        user_agent = page.evaluate("() => navigator.userAgent")
        meta = {"userAgent": user_agent}
        with open(META_PATH, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"  Saved: {META_PATH}")

        cookie_count = len(state.get("cookies", []))
        print(f"  Cookies: {cookie_count}")
        print()

        if logged_in:
            print("Setup complete! You can now run:")
            print("  python3 tools/ana_award_search.py TPE NRT 2026-10-01 --top 5")
        else:
            print("Setup saved (login status uncertain). Try running a search:")
            print("  python3 tools/ana_award_search.py TPE NRT 2026-10-01 --top 5")
            print()
            print("If cookies are expired, re-run: python3 tools/ana_setup.py")

        browser.close()


if __name__ == "__main__":
    main()
