#!/usr/bin/env python3
"""ANA Mileage Club award search using Patchright.

Searches ANA for Star Alliance award availability, parsing mileage costs
and cabin availability from the results page.

Uses Patchright (undetected Playwright fork) to bypass Akamai Bot Manager.
Requires pre-saved session cookies from ana_setup.py (manual login).

Usage:
    # First-time setup (manual login in browser)
    python3 tools/ana_setup.py

    # One-way search
    python3 tools/ana_award_search.py TPE NRT 2026-10-01

    # Round-trip search
    python3 tools/ana_award_search.py TPE NRT 2026-10-01 --return-date 2026-10-08

    # Specify cabin class
    python3 tools/ana_award_search.py TPE NRT 2026-10-01 --cabin business

    # Monthly calendar view (award availability grid)
    python3 tools/ana_award_search.py TPE NRT 2026-10-01 --calendar

    # JSON output
    python3 tools/ana_award_search.py TPE NRT 2026-10-01 --format json
"""

import argparse
import calendar as cal_mod
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AwardFlight:
    flight_number: str
    airline: str
    duration: str
    departure_time: str
    arrival_time: str
    origin: str
    dest: str
    stops: int
    cabin: str
    miles: int          # required miles (e.g. 35000)
    miles_str: str      # display string (e.g. "35k")
    status: str         # "available", "waitlisted", or "unavailable"
    aircraft: str


@dataclass
class AwardSearchResult:
    origin: str
    dest: str
    date: str
    return_date: str | None
    cabin: str
    total_results: int
    flights: list[AwardFlight]
    error: str | None


@dataclass
class CalendarDay:
    date: str           # YYYY-MM-DD
    economy: str        # "O" available, "X" unavailable, "-" unknown
    premium: str
    business: str
    first: str


@dataclass
class CalendarResult:
    origin: str
    dest: str
    year: int
    month: int
    months_data: list[list[CalendarDay]]  # up to 6 months
    error: str | None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOME_URL = "https://www.ana.co.jp/en/us/"
SEARCH_URL = (
    "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/"
    "roundtrip/award_search_roundtrip_input.xhtml"
    "?CONNECTION_KIND=JPN&LANG=en"
)
CALENDAR_URL = "https://cam.ana.co.jp/psz/tokutencal/form_e.jsp"

CABIN_MAP = {
    "economy": "Y",
    "premium": "PY",
    "business": "C",
    "first": "F",
}

CABIN_NAMES = {
    "economy class": "economy",
    "premium economy": "premium",
    "business class": "business",
    "first class": "first",
}


# ---------------------------------------------------------------------------
# Auth — cookie injection from ana_setup.py
# ---------------------------------------------------------------------------

AUTH_DIR = Path(__file__).resolve().parent.parent / "auth"
STATE_PATH = AUTH_DIR / "ana_state.json"
META_PATH = AUTH_DIR / "ana_meta.json"
PROFILE_DIR = AUTH_DIR / "ana_chrome_profile"

CDP_PORT = 9334  # different port from ana_setup.py

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

NO_AUTH_MSG = (
    "ANA session not found. Run setup first:\n"
    "  python3 tools/ana_setup.py\n"
)
EXPIRED_MSG = (
    "ANA session expired (redirected to login). Re-run setup:\n"
    "  python3 tools/ana_setup.py\n"
)


# ---------------------------------------------------------------------------
# Persistent Chrome manager — launch once, reuse across searches
# ---------------------------------------------------------------------------

class _ChromeManager:
    """Manages a persistent Chrome instance with CDP connection."""

    def __init__(self):
        self._proc = None
        self._pw = None
        self._browser = None
        self._page = None

    def _find_chrome(self) -> str | None:
        import os
        for p in CHROME_PATHS:
            if os.path.exists(p):
                return p
        return None

    def _is_alive(self) -> bool:
        """Check if Chrome + CDP connection is still usable."""
        if not self._proc or self._proc.poll() is not None:
            return False
        if not self._page:
            return False
        try:
            self._page.evaluate("() => true")
            return True
        except Exception:
            return False

    def _kill(self):
        """Tear down everything."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        except Exception:
            pass
        self._proc = self._browser = self._pw = self._page = None

    def get_page(self):
        """Return a usable page, launching Chrome if needed."""
        import subprocess
        import time as time_mod
        from patchright.sync_api import sync_playwright

        if self._is_alive():
            return self._page

        # Not alive — clean up and restart
        self._kill()

        chrome_path = self._find_chrome()
        if not chrome_path:
            raise RuntimeError("Chrome not found. Install Google Chrome.")

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            [
                chrome_path,
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time_mod.sleep(3)

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        self._page = context.pages[0] if context.pages else context.new_page()
        return self._page

    def close(self):
        """Explicitly shut down (called at process exit)."""
        self._kill()


# Module-level singleton
_chrome = _ChromeManager()


def load_auth() -> tuple[str | None, str | None]:
    """Load saved auth state path and userAgent.

    Returns (state_path, user_agent) or (None, None) if not set up.
    """
    if not STATE_PATH.exists():
        return None, None

    user_agent = None
    if META_PATH.exists():
        with open(META_PATH) as f:
            meta = json.load(f)
            user_agent = meta.get("userAgent")

    return str(STATE_PATH), user_agent


# ---------------------------------------------------------------------------
# Human-like behavior helpers
# ---------------------------------------------------------------------------

def _random_delay(page, min_ms: int = 500, max_ms: int = 2000):
    """Wait a random amount of time to simulate human behavior."""
    page.wait_for_timeout(random.randint(min_ms, max_ms))


def _auto_login(page, timeout: int = 180):
    """Auto-fill password and login, then wait for search page."""
    import os as _os
    env_path = Path(__file__).resolve().parent.parent / ".env"
    password = _os.environ.get("ANA_PASSWORD")
    if not password and env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANA_PASSWORD="):
                    password = line.split("=", 1)[1].strip().strip("'\"")

    if password:
        pwd_field = page.locator("#password")
        if pwd_field.count() > 0 and pwd_field.first.is_visible(timeout=3000):
            pwd_field.first.fill(password)
            _random_delay(page, 300, 600)
            login_btn = page.locator("#amcMemberLogin")
            if login_btn.count() == 0:
                login_btn = page.locator("input[value='Login']")
            if login_btn.count() > 0:
                login_btn.first.click()
                print("  Auto-login submitted, waiting for redirect...")

    # Wait for page to leave login
    poll_interval = 3
    waited = 0
    while waited < timeout:
        page.wait_for_timeout(poll_interval * 1000)
        waited += poll_interval
        if not _is_on_login_page(page):
            return
        if waited % 30 == 0:
            print(f"  Still waiting for login... ({waited}s / {timeout}s)")


def _is_on_login_page(page) -> bool:
    """Check if we're on the login page (by page title, not URL)."""
    try:
        title = page.title().lower()
        if "login" in title or "member login" in title:
            return True
        body_text = page.evaluate("() => document.body.innerText.substring(0, 300)")
        if "heavy traffic" in body_text.lower() or "server maintenance" in body_text.lower():
            return True
    except Exception:
        pass
    return False


def _is_session_expired(page) -> bool:
    """Check if redirected to login page."""
    return _is_on_login_page(page)


# ---------------------------------------------------------------------------
# Popup / reCAPTCHA helpers
# ---------------------------------------------------------------------------

def _dismiss_popups(page):
    """Dismiss cookie consent and promotional popups."""
    for selector in [
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "button:has-text('Close')",
        ".cookie-consent-accept",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1000):
                el.click()
                _random_delay(page, 300, 600)
        except Exception:
            pass


def _check_recaptcha(page) -> bool:
    """Check if reCAPTCHA challenge is present."""
    recaptcha_desc = page.locator("#reCaptchaDescription")
    if recaptcha_desc.count() > 0:
        text = recaptcha_desc.first.text_content() or ""
        if "prevent" in text.lower() or "captcha" in text.lower():
            return True

    # Also check for iframe-based reCAPTCHA
    recaptcha_frame = page.locator("iframe[src*='recaptcha']")
    if recaptcha_frame.count() > 0:
        return True

    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_awards(
    origin: str,
    dest: str,
    depart_date: str,
    return_date: str | None = None,
    cabin: str = "economy",
    headed: bool = True,
    top: int = 10,
    passengers: int = 1,
) -> AwardSearchResult:
    """Search ANA for award flights.

    Uses pre-saved session cookies from ana_setup.py. Navigates to the
    award search form, fills route/date/cabin, submits, and parses results.

    Note: headed=True (default) is required to bypass Akamai Bot Manager.
    """
    try:
        page = _chrome.get_page()
    except RuntimeError as e:
        return AwardSearchResult(
            origin=origin, dest=dest, date=depart_date,
            return_date=return_date, cabin=cabin, total_results=0,
            flights=[], error=str(e),
        )

    try:
        print(f"Navigating to award search...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        _random_delay(page, 3000, 5000)

        # If redirected to login, auto-login
        if _is_session_expired(page):
            print("  Session expired — attempting auto-login...")
            _auto_login(page, timeout=120)
            if _is_session_expired(page):
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, cabin=cabin, total_results=0,
                    flights=[], error=EXPIRED_MSG,
                )
            print("  Login successful!")
            _random_delay(page, 2000, 3000)

        if _check_recaptcha(page):
            return AwardSearchResult(
                origin=origin, dest=dest, date=depart_date,
                return_date=return_date, cabin=cabin, total_results=0,
                flights=[], error="reCAPTCHA blocked access",
            )

        # Submit search via JS form submission
        print(f"Submitting search: {origin}→{dest} {depart_date}...")
        if not return_date:
            return_date = (datetime.strptime(depart_date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")

        dt = datetime.strptime(depart_date, "%Y-%m-%d")
        rt = datetime.strptime(return_date, "%Y-%m-%d")
        weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

        cabin_code = CABIN_MAP.get(cabin, "Y")
        boarding_class_map = {
            "Y": "CFF1", "PY": "CFF4", "C": "CFF2", "F": "CFF3",
        }
        boarding_class = boarding_class_map.get(cabin_code, "CFF1")

        search_mode = "ROUND_TRIP"
        itin_check = "roundTrip"

        submit_name = page.evaluate(
            "() => document.querySelector('input[value=\"Search\"]')?.name || 'j_idt1080'"
        )

        dep_display = f"{dt.strftime('%m/%d/%Y')} ({weekdays[dt.weekday()]})"
        ret_display = f"{rt.strftime('%m/%d/%Y')} ({weekdays[rt.weekday()]})"

        # Set values on existing form and submit (preserves JSF state)
        page.evaluate(f"""(() => {{
            document.getElementById('departureAirportCode:field').value = '{origin}';
            document.getElementById('arrivalAirportCode:field').value = '{dest}';
            document.getElementById('awardDepartureDate:field').value = '{dt.strftime("%Y%m%d")}';
            document.getElementById('awardReturnDate:field').value = '{rt.strftime("%Y%m%d")}';
            document.getElementById('hiddenSearchMode').value = '{search_mode}';
            document.getElementById('itineraryButtonCheck').value = '{itin_check}';
            document.getElementById('hiddenAction').value = 'AwardRoundTripSearchInputAction';
            document.getElementById('hiddenBoardingClassType').value = '0';
            document.getElementById('boardingClass').value = '{boarding_class}';
            document.querySelector('#adult\\\\:count').value = '{passengers}';
            const comp = document.getElementById('comparisonSearchType');
            if (comp) comp.checked = true;

            const form = document.querySelector('[id="conditionInput"]') || document.forms[0];
            const btn = document.createElement('input');
            btn.type = 'hidden';
            btn.name = '{submit_name}';
            btn.value = 'Search';
            form.appendChild(btn);
            form.submit();
        }})()""")

        # Wait for results page to fully load
        page.wait_for_load_state("networkidle", timeout=60000)
        _random_delay(page, 3000, 5000)
        for _ in range(10):
            if "CalendarSearchResult" in page.content():
                break
            page.wait_for_timeout(2000)

        if _check_recaptcha(page):
            return AwardSearchResult(
                origin=origin, dest=dest, date=depart_date,
                return_date=return_date, cabin=cabin, total_results=0,
                flights=[], error="reCAPTCHA blocked search",
            )

        error = _check_error_messages(page)
        if error:
            return AwardSearchResult(
                origin=origin, dest=dest, date=depart_date,
                return_date=return_date, cabin=cabin, total_results=0,
                flights=[], error=error,
            )

        print(f"Parsing results...")
        return _parse_results(page, origin, dest, depart_date, return_date, cabin, top)

    except Exception as e:
        # Connection lost — kill Chrome so next call restarts it
        _chrome._kill()
        return AwardSearchResult(
            origin=origin, dest=dest, date=depart_date,
            return_date=return_date, cabin=cabin, total_results=0,
            flights=[], error=str(e),
        )


def _fill_search_form(
    page, origin: str, dest: str, depart_date: str, return_date: str | None,
):
    """Fill the ANA award search form.

    Uses Flightplan-discovered field name patterns:
    - requestedSegment:N:departureAirportCode:field
    - requestedSegment:N:departureDate:field
    """
    # Fill departure airport — type code and select from autocomplete
    _fill_airport_field(page, "#departureAirportCode\\:field_pctext", origin)
    _random_delay(page, 500, 1000)

    # Fill arrival airport
    _fill_airport_field(page, "#arrivalAirportCode\\:field_pctext", dest)
    _random_delay(page, 500, 1000)

    # Fill departure date (readonly calendar field — must use JS)
    dt = datetime.strptime(depart_date, "%Y-%m-%d")
    date_val = dt.strftime("%Y%m%d")
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    date_display = dt.strftime("%m/%d/%Y") + f" ({weekdays[dt.weekday()]})"
    page.evaluate(f"""(() => {{
        document.getElementById('awardDepartureDate:field').value = '{date_val}';
        const el = document.getElementById('awardDepartureDate:field_pctext');
        el.removeAttribute('readonly');
        el.value = '{date_display}';
        el.setAttribute('readonly', 'readonly');
    }})()""")
    _random_delay(page, 500, 1000)

    # Fill return date (ANA requires round-trip; default +7 days if not given)
    if not return_date:
        return_date = (dt + timedelta(days=7)).strftime("%Y-%m-%d")
    rt = datetime.strptime(return_date, "%Y-%m-%d")
    ret_date_val = rt.strftime("%Y%m%d")
    ret_date_display = rt.strftime("%m/%d/%Y") + f" ({weekdays[rt.weekday()]})"
    page.evaluate(f"""(() => {{
        document.getElementById('awardReturnDate:field').value = '{ret_date_val}';
        const el = document.getElementById('awardReturnDate:field_pctext');
        el.removeAttribute('readonly');
        el.value = '{ret_date_display}';
        el.setAttribute('readonly', 'readonly');
    }})()""")

    _random_delay(page, 300, 600)


def _fill_airport_field(page, selector: str, code: str):
    """Type airport code into field and select from autocomplete dropdown."""
    field = page.locator(selector)
    field.click()
    _random_delay(page, 200, 400)
    # Clear existing text
    field.fill("")
    _random_delay(page, 200, 300)
    # Type code character by character for autocomplete to trigger
    for ch in code:
        page.keyboard.type(ch, delay=100)
    _random_delay(page, 1000, 1500)

    # Try to click matching autocomplete option
    # ANA uses a suggestion list with airport codes
    suggestion = page.locator(f"li:has-text('{code}')").first
    try:
        if suggestion.is_visible(timeout=3000):
            suggestion.click()
            _random_delay(page, 300, 500)
            return
    except Exception:
        pass

    # Fallback: try any visible suggestion list item
    any_suggestion = page.locator(".ui-autocomplete li, .suggestionList li, [role='option']").first
    try:
        if any_suggestion.is_visible(timeout=1000):
            any_suggestion.click()
            _random_delay(page, 300, 500)
            return
    except Exception:
        pass

    # Last resort: Tab out to confirm
    page.keyboard.press("Tab")


def _select_airport_option(page, code: str):
    """Try to select an airport from the autocomplete dropdown."""
    _random_delay(page, 500, 800)
    option = page.locator(f"text={code}").first
    try:
        if option.is_visible(timeout=2000):
            option.click()
            _random_delay(page, 200, 500)
    except Exception:
        page.keyboard.press("Tab")


def _fill_airport_fallback(page, direction: str, code: str):
    """Fallback airport filling when named fields aren't found."""
    # Try common patterns
    for selector in [
        f'input[placeholder*="{direction}"]',
        f'input[aria-label*="{direction}"]',
        f'input[id*="{direction}Airport"]',
    ]:
        el = page.locator(selector)
        if el.count() > 0:
            el.first.fill(code)
            _random_delay(page, 500, 800)
            page.keyboard.press("Tab")
            return
    print(f"  Warning: Could not find {direction} airport field")


def _fill_date_fallback(page, direction: str, date_str: str):
    """Fallback date filling when named fields aren't found."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    display = dt.strftime("%m/%d/%Y")
    for selector in [
        f'input[placeholder*="{direction}"]',
        f'input[aria-label*="{direction}"]',
        f'input[id*="{direction}Date"]',
    ]:
        el = page.locator(selector)
        if el.count() > 0:
            el.first.fill(display)
            _random_delay(page, 300, 600)
            return
    print(f"  Warning: Could not find {direction} date field")


# ---------------------------------------------------------------------------
# Loading / error helpers
# ---------------------------------------------------------------------------

def _wait_for_loading(page, timeout_ms: int = 30000):
    """Wait for ANA's loading spinner to appear and disappear."""
    # Wait for spinner to appear (may already be gone)
    try:
        page.locator("div.loadingArea").wait_for(state="visible", timeout=5000)
    except Exception:
        pass

    # Wait for spinner to disappear
    try:
        page.locator("div.loadingArea").wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass

    # Extra settle time
    _random_delay(page, 1000, 2000)


def _check_error_messages(page) -> str | None:
    """Check for ANA error messages on the page."""
    # Modal errors
    modal = page.locator(".modalError")
    if modal.count() > 0 and modal.first.is_visible():
        text = modal.first.text_content() or ""
        return f"ANA error: {text.strip()[:200]}"

    # Message area errors
    msg_area = page.locator("#cmnContainer .messageArea")
    if msg_area.count() > 0 and msg_area.first.is_visible():
        text = msg_area.first.text_content() or ""
        # Try to dismiss
        try:
            page.locator("#cmnContainer .buttonArea input").first.click()
        except Exception:
            pass
        return f"ANA error: {text.strip()[:200]}"

    return None


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_results(
    page, origin: str, dest: str, depart_date: str,
    return_date: str | None, cabin: str, top: int,
) -> AwardSearchResult:
    """Parse award search results from ANA's calendar comparison page.

    ANA returns a calendar page with JavaScript data containing miles costs
    per departure/return date combination. We extract CalendarSearchResult
    entries and also parse the per-day availability from the HTML.
    """
    html = page.content()

    # Extract CalendarSearchResult data from JavaScript
    # Format: CalendarSearchResult("20260929","20261005","20,000")
    # These are grouped by departure date (tempArray blocks)
    entries = re.findall(
        r'var returnDate = "(\d{8})";var milesCost = \'([^\']*)\';'
        r'\s*tempArray\.push\(new CalendarList\.CalendarSearchResult\("(\d{8})"',
        html,
    )

    # Build miles grid: {(depart_date, return_date): miles}
    miles_grid = {}
    for ret_date_raw, miles_str, dep_date_raw in entries:
        if miles_str != '-':
            dep = f"{dep_date_raw[:4]}-{dep_date_raw[4:6]}-{dep_date_raw[6:]}"
            ret = f"{ret_date_raw[:4]}-{ret_date_raw[4:6]}-{ret_date_raw[6:]}"
            miles_val = int(miles_str.replace(',', ''))
            miles_grid[(dep, ret)] = miles_val

    # Extract per-day availability from HTML status elements
    day_avail = {}  # {(direction, date_str): "available" | "unavailable"}
    for match in re.finditer(
        r'simpleCalendarDateGroup(OutBound|InBound)\d+.*?</td>', html, re.S
    ):
        direction = match.group(1)
        cell = match.group(0)
        date_match = re.search(r'>((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+)', cell)
        status_match = re.search(r'class="status[^"]*">([^<]+)<', cell)
        if date_match and status_match:
            day_avail[(direction, date_match.group(1))] = status_match.group(1).strip()

    # Build AwardFlight entries from the miles grid
    all_flights = []
    for (dep, ret), miles in sorted(miles_grid.items()):
        miles_k = f"{miles // 1000}k" if miles >= 1000 else str(miles)
        all_flights.append(AwardFlight(
            flight_number="",
            airline="ANA",
            duration="",
            departure_time=dep,
            arrival_time=ret,
            origin=origin,
            dest=dest,
            stops=0,
            cabin=cabin,
            miles=miles,
            miles_str=miles_k,
            status="available",
            aircraft="",
        ))

    # Sort by miles (cheapest first)
    all_flights.sort(key=lambda f: f.miles)

    if top > 0:
        all_flights = all_flights[:top]

    total = len(all_flights)

    return AwardSearchResult(
        origin=origin, dest=dest, date=depart_date,
        return_date=return_date, cabin=cabin,
        total_results=total, flights=all_flights, error=None,
    )


# ---------------------------------------------------------------------------
# Calendar search
# ---------------------------------------------------------------------------

def search_calendar(
    origin: str,
    dest: str,
    year: int,
    month: int,
    headed: bool = True,
) -> CalendarResult:
    """Fetch ANA award calendar showing availability per day.

    ANA's calendar page (cam.ana.co.jp) shows 6 months of availability
    with O/X indicators per cabin class. Uses pre-saved session cookies.
    """
    try:
        page = _chrome.get_page()
    except RuntimeError as e:
        return CalendarResult(
            origin=origin, dest=dest, year=year, month=month,
            months_data=[], error=str(e),
        )

    try:
        print("Navigating to award calendar...")
        page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=30000)
        _random_delay(page, 3000, 5000)

        if _is_session_expired(page):
            print("  Session expired — attempting auto-login...")
            _auto_login(page, timeout=120)
            if _is_session_expired(page):
                return CalendarResult(
                    origin=origin, dest=dest, year=year, month=month,
                    months_data=[], error=EXPIRED_MSG,
                )

        if _check_recaptcha(page):
            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                months_data=[], error="reCAPTCHA blocked access",
            )

        print(f"Searching calendar: {origin}→{dest} from {year}-{month:02d}...")
        _fill_calendar_form(page, origin, dest, year, month)

        submit = page.locator('input[value="Search"]')
        if submit.count() == 0:
            submit = page.locator("button:has-text('Search')")
        if submit.count() > 0:
            submit.first.click()
        else:
            page.keyboard.press("Enter")

        _wait_for_loading(page, timeout_ms=45000)

        if _check_recaptcha(page):
            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                months_data=[], error="reCAPTCHA blocked calendar search",
            )

        error = _check_error_messages(page)
        if error:
            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                months_data=[], error=error,
            )

        print("Parsing calendar...")
        return _parse_calendar(page, origin, dest, year, month)

    except Exception as e:
        _chrome._kill()
        return CalendarResult(
            origin=origin, dest=dest, year=year, month=month,
            months_data=[], error=str(e),
        )


def _fill_calendar_form(page, origin: str, dest: str, year: int, month: int):
    """Fill the ANA award calendar search form."""
    # Origin/destination fields
    for name_pattern, value in [
        ("departureAirportCode", origin),
        ("arrivalAirportCode", dest),
    ]:
        field = page.locator(f'input[name*="{name_pattern}"]').first
        try:
            field.fill(value)
            _random_delay(page, 300, 600)
            _select_airport_option(page, value)
        except Exception:
            print(f"  Warning: Could not fill {name_pattern}")

    _random_delay(page, 500, 800)

    # Month/year selection
    month_select = page.locator('select[name*="month"], select[name*="Month"]')
    if month_select.count() > 0:
        target_val = f"{year}{month:02d}"
        try:
            month_select.first.select_option(value=target_val)
        except Exception:
            # Try label-based selection
            month_name = cal_mod.month_name[month]
            try:
                month_select.first.select_option(label=f"{month_name} {year}")
            except Exception:
                print(f"  Warning: Could not select month {year}-{month:02d}")

    _random_delay(page, 300, 600)


def _parse_calendar(
    page, origin: str, dest: str, year: int, month: int,
) -> CalendarResult:
    """Parse the ANA award calendar grid.

    The calendar shows O (available) / X (unavailable) per day per cabin.
    """
    data = page.evaluate(r"""() => {
        const months = [];
        // Calendar tables — one per month
        const tables = document.querySelectorAll('table.calendarTable, table.resultCalendar');

        tables.forEach(table => {
            const monthData = { header: '', days: [] };

            // Month header
            const header = table.querySelector('th, caption');
            monthData.header = header ? header.textContent.trim() : '';

            // Day rows
            const rows = table.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length >= 2) {
                    const day = {
                        date: '',
                        cells: []
                    };
                    cells.forEach(cell => {
                        day.cells.push(cell.textContent.trim());
                    });
                    // First cell is typically the date
                    day.date = cells[0].textContent.trim();
                    monthData.days.push(day);
                }
            });

            months.push(monthData);
        });

        return months;
    }""")

    months_data = []
    for month_block in data:
        days = []
        for day_data in month_block.get("days", []):
            cells = day_data.get("cells", [])
            date_str = day_data.get("date", "")

            # Try to parse date
            date_match = re.search(r"(\d{1,2})", date_str)
            if not date_match:
                continue

            day_num = int(date_match.group(1))

            # Parse header to get month/year context
            header = month_block.get("header", "")
            header_match = re.search(r"(\w+)\s*(\d{4})", header)
            if header_match:
                try:
                    m_name = header_match.group(1)
                    m_year = int(header_match.group(2))
                    m_month = list(cal_mod.month_name).index(m_name)
                    full_date = f"{m_year}-{m_month:02d}-{day_num:02d}"
                except (ValueError, IndexError):
                    full_date = f"{year}-{month:02d}-{day_num:02d}"
            else:
                full_date = f"{year}-{month:02d}-{day_num:02d}"

            # Map cells to cabin availability (O/X/-)
            economy = _parse_availability_cell(cells[1] if len(cells) > 1 else "")
            premium = _parse_availability_cell(cells[2] if len(cells) > 2 else "")
            business = _parse_availability_cell(cells[3] if len(cells) > 3 else "")
            first_cls = _parse_availability_cell(cells[4] if len(cells) > 4 else "")

            days.append(CalendarDay(
                date=full_date,
                economy=economy,
                premium=premium,
                business=business,
                first=first_cls,
            ))

        if days:
            months_data.append(days)

    return CalendarResult(
        origin=origin, dest=dest, year=year, month=month,
        months_data=months_data, error=None,
    )


def _parse_availability_cell(text: str) -> str:
    """Parse a calendar cell into O/X/- indicator."""
    text = text.strip().upper()
    if "O" in text or "○" in text or "AVAILABLE" in text:
        return "O"
    elif "X" in text or "×" in text or "UNAVAIL" in text:
        return "X"
    elif text == "" or text == "-":
        return "-"
    return text[:1] if text else "-"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_table(result: AwardSearchResult) -> str:
    """Format search results as a human-readable table."""
    lines = []
    lines.append(f"\n{'='*85}")
    trip = f"{result.origin}→{result.dest} {result.date}"
    if result.return_date:
        trip += f" ~ {result.return_date}"
    lines.append(f"  ANA Award Search: {trip} ({result.cabin})")
    lines.append(f"  {result.total_results} total results")
    lines.append(f"{'='*85}")

    if result.error:
        lines.append(f"  Error: {result.error}")
        return "\n".join(lines)

    if not result.flights:
        lines.append("  No award flights found")
        return "\n".join(lines)

    # Check if this is calendar-style data (dates in departure/arrival fields)
    is_calendar = result.flights[0].miles > 0 and "-" in result.flights[0].departure_time

    if is_calendar:
        lines.append(
            f"  {'#':<3} {'Depart':<12} {'Return':<12} {'Miles':>8} {'Route'}"
        )
        lines.append(
            f"  {'-'*3} {'-'*12} {'-'*12} {'-'*8} {'-'*20}"
        )
        for i, f in enumerate(result.flights, 1):
            lines.append(
                f"  {i:<3} {f.departure_time:<12} {f.arrival_time:<12} {f.miles:>8,} {f.origin}-{f.dest}"
            )
    else:
        lines.append(
            f"  {'#':<3} {'Flight':<10} {'Cabin':<10} {'Status':<12} "
            f"{'Duration':<8} {'Depart':>8} {'Arrive':>8} {'Stops':>5} {'Aircraft'}"
        )
        lines.append(
            f"  {'-'*3} {'-'*10} {'-'*10} {'-'*12} "
            f"{'-'*8} {'-'*8} {'-'*8} {'-'*5} {'-'*8}"
        )
        for i, f in enumerate(result.flights, 1):
            status_display = {
                "available": "avail",
                "waitlisted": "waitlist",
                "unavailable": "n/a",
            }.get(f.status, f.status)
            lines.append(
                f"  {i:<3} {f.flight_number:<10} {f.cabin:<10} {status_display:<12} "
                f"{f.duration:<8} {f.departure_time:>8} {f.arrival_time:>8} {f.stops:>5} {f.aircraft}"
            )

    return "\n".join(lines)


def format_json(result: AwardSearchResult) -> str:
    """Format search results as JSON."""
    data = {
        "origin": result.origin,
        "dest": result.dest,
        "date": result.date,
        "return_date": result.return_date,
        "cabin": result.cabin,
        "total_results": result.total_results,
        "error": result.error,
        "flights": [asdict(f) for f in result.flights],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_calendar_table(result: CalendarResult) -> str:
    """Format calendar results as a readable grid."""
    lines = []
    lines.append(f"\n  ANA Award Calendar: {result.origin} → {result.dest}")
    lines.append(f"  {'='*60}")

    if result.error:
        lines.append(f"  Error: {result.error}")
        return "\n".join(lines)

    if not result.months_data:
        lines.append("  No calendar data available")
        return "\n".join(lines)

    for month_days in result.months_data:
        if not month_days:
            continue

        # Get month/year from first day
        first = month_days[0]
        parts = first.date.split("-")
        y, m = int(parts[0]), int(parts[1])
        month_name = cal_mod.month_name[m]

        lines.append(f"\n  {month_name} {y}")
        lines.append(f"  {'Day':>4} {'Econ':>6} {'Prem':>6} {'Biz':>6} {'First':>6}")
        lines.append(f"  {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

        for d in month_days:
            day_num = d.date.split("-")[2]
            lines.append(
                f"  {day_num:>4} {d.economy:>6} {d.premium:>6} "
                f"{d.business:>6} {d.first:>6}"
            )

    lines.append(f"\n  Legend: O = available, X = unavailable, - = unknown")
    return "\n".join(lines)


def format_calendar_json(result: CalendarResult) -> str:
    """Format calendar results as JSON."""
    data = {
        "origin": result.origin,
        "dest": result.dest,
        "year": result.year,
        "month": result.month,
        "error": result.error,
        "months": [],
    }
    for month_days in result.months_data:
        data["months"].append([asdict(d) for d in month_days])
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ANA Mileage Club award flight search"
    )
    parser.add_argument("origin", help="Origin IATA code (e.g. TPE)")
    parser.add_argument("dest", help="Destination IATA code (e.g. NRT)")
    parser.add_argument("date", nargs="?", help="Departure date YYYY-MM-DD")
    parser.add_argument(
        "--return-date", help="Return date YYYY-MM-DD (makes it round-trip)"
    )
    parser.add_argument(
        "--cabin",
        choices=["economy", "premium", "business", "first"],
        default="economy",
        help="Cabin class (default: economy)",
    )
    parser.add_argument(
        "--start", help="Start date for date range search YYYY-MM-DD"
    )
    parser.add_argument(
        "--end", help="End date for date range search YYYY-MM-DD"
    )
    parser.add_argument(
        "--format", choices=["table", "json"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="Max results per search (default: 10)",
    )
    parser.add_argument(
        "--calendar", action="store_true",
        help="Show monthly calendar view with award availability",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode (may be blocked by anti-bot)",
    )
    parser.add_argument(
        "--passengers", type=int, default=1,
        help="Number of adult passengers (default: 1)",
    )

    args = parser.parse_args()

    # Calendar mode
    if args.calendar:
        if not args.date and not args.start:
            parser.error("Provide a date for --calendar (any day in the target month)")
        ref_date = args.date or args.start
        dt = datetime.strptime(ref_date, "%Y-%m-%d")
        result = search_calendar(
            origin=args.origin,
            dest=args.dest,
            year=dt.year,
            month=dt.month,
            headed=not args.headless,
        )
        if args.format == "json":
            print(format_calendar_json(result))
        else:
            print(format_calendar_table(result))
        return

    # Build list of dates to search
    if args.start and args.end:
        dates = []
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
        current = start
        while current <= end:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
    elif args.date:
        dates = [args.date]
    else:
        parser.error("Provide either a date or --start/--end range")

    for i, date in enumerate(dates):
        if i > 0:
            # Rate limiting between searches
            delay = random.randint(5, 10)
            print(f"\n  Waiting {delay}s before next search...")
            time.sleep(delay)

        result = search_awards(
            origin=args.origin,
            dest=args.dest,
            depart_date=date,
            return_date=args.return_date,
            cabin=args.cabin,
            headed=not args.headless,
            top=args.top,
            passengers=args.passengers,
        )

        if args.format == "json":
            print(format_json(result))
        else:
            print(format_table(result))


if __name__ == "__main__":
    main()
