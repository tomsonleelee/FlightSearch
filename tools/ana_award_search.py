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

NO_AUTH_MSG = (
    "ANA session not found. Run setup first:\n"
    "  python3 tools/ana_setup.py\n"
)
EXPIRED_MSG = (
    "ANA session expired (redirected to login). Re-run setup:\n"
    "  python3 tools/ana_setup.py\n"
)


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


def _is_session_expired(page) -> bool:
    """Check if the browser was redirected to the login page (session expired)."""
    url = page.url.lower()
    if "login" in url:
        return True
    body_text = page.evaluate("() => document.body.innerText.substring(0, 500)")
    if "heavy traffic" in body_text.lower() or "server maintenance" in body_text.lower():
        return True
    return False


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
) -> AwardSearchResult:
    """Search ANA for award flights.

    Uses pre-saved session cookies from ana_setup.py. Navigates to the
    award search form, fills route/date/cabin, submits, and parses results.

    Note: headed=True (default) is required to bypass Akamai Bot Manager.
    """
    from patchright.sync_api import sync_playwright

    state_path, user_agent = load_auth()
    if not state_path:
        return AwardSearchResult(
            origin=origin, dest=dest, date=depart_date,
            return_date=return_date, cabin=cabin, total_results=0,
            flights=[], error=NO_AUTH_MSG,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx_opts = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
            "storage_state": state_path,
        }
        if user_agent:
            ctx_opts["user_agent"] = user_agent
        context = browser.new_context(**ctx_opts)
        page = context.new_page()

        try:
            # Navigate directly to search page (cookies handle auth)
            print(f"Navigating to award search...")
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            _random_delay(page, 3000, 5000)

            # Check if session expired (redirected to login)
            if _is_session_expired(page):
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, cabin=cabin, total_results=0,
                    flights=[], error=EXPIRED_MSG,
                )

            if _check_recaptcha(page):
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, cabin=cabin, total_results=0,
                    flights=[], error="reCAPTCHA blocked access",
                )

            # Fill the search form
            print(f"Filling search form: {origin}→{dest} {depart_date}...")
            _fill_search_form(page, origin, dest, depart_date, return_date)
            _random_delay(page, 1000, 2000)

            # Submit search
            print(f"Submitting search...")
            submit_btn = page.locator('input[value="Search"]')
            if submit_btn.count() == 0:
                submit_btn = page.locator("button:has-text('Search')")
            submit_btn.first.click()

            # Wait for results (loading spinner)
            _wait_for_loading(page)

            if _check_recaptcha(page):
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, cabin=cabin, total_results=0,
                    flights=[], error="reCAPTCHA blocked search",
                )

            # Check for error messages
            error = _check_error_messages(page)
            if error:
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, cabin=cabin, total_results=0,
                    flights=[], error=error,
                )

            # Parse results
            print(f"Parsing results...")
            return _parse_results(page, origin, dest, depart_date, return_date, cabin, top)

        except Exception as e:
            return AwardSearchResult(
                origin=origin, dest=dest, date=depart_date,
                return_date=return_date, cabin=cabin, total_results=0,
                flights=[], error=str(e),
            )
        finally:
            browser.close()


def _fill_search_form(
    page, origin: str, dest: str, depart_date: str, return_date: str | None,
):
    """Fill the ANA award search form.

    Uses Flightplan-discovered field name patterns:
    - requestedSegment:N:departureAirportCode:field
    - requestedSegment:N:departureDate:field
    """
    # Switch to "Multiple cities / mixed classes" for better control
    multi_tab = page.locator("li.lastChild.deselection")
    if multi_tab.count() > 0:
        multi_tab.first.click()
        _random_delay(page, 1000, 2000)

    # If one-way, try to find and click One-way tab
    if not return_date:
        oneway_tab = page.locator("text=One Way")
        if oneway_tab.count() > 0:
            oneway_tab.first.click()
            _random_delay(page, 500, 1000)

    # Fill departure airport
    dep_airport = page.locator(
        '[name="requestedSegment:0:departureAirportCode:field"]'
    )
    if dep_airport.count() > 0:
        dep_airport.first.fill("")
        _random_delay(page, 200, 400)
        dep_airport.first.fill(origin)
        _random_delay(page, 500, 1000)
        # Try to select from autocomplete dropdown
        _select_airport_option(page, origin)
    else:
        # Fallback: try generic airport input
        _fill_airport_fallback(page, "departure", origin)

    _random_delay(page, 500, 1000)

    # Fill arrival airport
    arr_airport = page.locator(
        '[name="requestedSegment:0:arrivalAirportCode:field"]'
    )
    if arr_airport.count() > 0:
        arr_airport.first.fill("")
        _random_delay(page, 200, 400)
        arr_airport.first.fill(dest)
        _random_delay(page, 500, 1000)
        _select_airport_option(page, dest)
    else:
        _fill_airport_fallback(page, "arrival", dest)

    _random_delay(page, 500, 1000)

    # Fill departure date
    dt = datetime.strptime(depart_date, "%Y-%m-%d")
    date_val = dt.strftime("%Y%m%d")
    date_display = dt.strftime("%m/%d/%Y")

    dep_date_field = page.locator(
        '[name="requestedSegment:0:departureDate:field"]'
    )
    if dep_date_field.count() > 0:
        page.evaluate(
            f"document.querySelector('[name=\"requestedSegment:0:departureDate:field\"]').value = '{date_val}'"
        )
        # Also set the display field
        dep_date_display = page.locator(
            '[name="requestedSegment:0:departureDate:field_pctext"]'
        )
        if dep_date_display.count() > 0:
            dep_date_display.first.fill(date_display)
    else:
        _fill_date_fallback(page, "departure", depart_date)

    _random_delay(page, 500, 1000)

    # Fill return date (if round-trip)
    if return_date:
        rt = datetime.strptime(return_date, "%Y-%m-%d")
        ret_date_val = rt.strftime("%Y%m%d")
        ret_date_display = rt.strftime("%m/%d/%Y")

        # Return segment airports (reversed)
        ret_dep = page.locator(
            '[name="requestedSegment:1:departureAirportCode:field"]'
        )
        if ret_dep.count() > 0:
            ret_dep.first.fill(dest)
            _random_delay(page, 300, 600)

        ret_arr = page.locator(
            '[name="requestedSegment:1:arrivalAirportCode:field"]'
        )
        if ret_arr.count() > 0:
            ret_arr.first.fill(origin)
            _random_delay(page, 300, 600)

        ret_date_field = page.locator(
            '[name="requestedSegment:1:departureDate:field"]'
        )
        if ret_date_field.count() > 0:
            page.evaluate(
                f"document.querySelector('[name=\"requestedSegment:1:departureDate:field\"]').value = '{ret_date_val}'"
            )
            ret_date_display_el = page.locator(
                '[name="requestedSegment:1:departureDate:field_pctext"]'
            )
            if ret_date_display_el.count() > 0:
                ret_date_display_el.first.fill(ret_date_display)
        else:
            _fill_date_fallback(page, "return", return_date)

    _random_delay(page, 300, 600)


def _select_airport_option(page, code: str):
    """Try to select an airport from the autocomplete dropdown."""
    _random_delay(page, 500, 800)
    # ANA autocomplete shows airport suggestions
    option = page.locator(f"text={code}").first
    try:
        if option.is_visible(timeout=2000):
            option.click()
            _random_delay(page, 200, 500)
    except Exception:
        # If no dropdown, the code in the field should suffice
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
    """Parse award search results from ANA's result page.

    Results are in tr.oneWayDisplayPlan rows with cabin columns
    defined by tr.fareGroup header.
    """
    # NOTE: r-string for JS regex patterns
    data = page.evaluate(r"""() => {
        const result = { columns: [], flights: [] };

        // Parse cabin columns from header
        const fareHeaders = document.querySelectorAll('tr.fareGroup th');
        fareHeaders.forEach(th => {
            result.columns.push(th.textContent.trim().toLowerCase());
        });

        // Parse flight rows
        const rows = document.querySelectorAll('tr.oneWayDisplayPlan');
        rows.forEach(row => {
            const flight = { segments: [], availability: {} };

            // Parse availability per cabin column
            const cells = row.querySelectorAll('td');
            result.columns.forEach((cabin, i) => {
                if (i < cells.length) {
                    const text = cells[i].textContent.trim().toLowerCase();
                    if (text.includes('available')) {
                        flight.availability[cabin] = 'available';
                    } else if (text.includes('waitlisted')) {
                        flight.availability[cabin] = 'waitlisted';
                    } else {
                        flight.availability[cabin] = 'unavailable';
                    }
                }
            });

            // Parse segment info from designTr elements
            const segDivs = row.querySelectorAll('div.designTr');
            segDivs.forEach(seg => {
                const tds = seg.querySelectorAll('div.designTd');
                if (tds.length >= 2) {
                    flight.segments.push({
                        text: Array.from(tds).map(td => td.textContent.trim()).join(' | ')
                    });
                }
            });

            // Get full row text for additional parsing
            flight.rowText = row.textContent.trim();

            result.flights.push(flight);
        });

        // Also try to count total results
        const countEl = document.querySelector('.resultCount, .searchResultCount');
        result.totalCount = countEl ? countEl.textContent.trim() : '';

        return result;
    }""")

    columns = data.get("columns", [])
    flights_data = data.get("flights", [])

    all_flights = []
    for fd in flights_data:
        availability = fd.get("availability", {})
        row_text = fd.get("rowText", "")

        # Extract flight number
        flight_match = re.search(r"([A-Z]{2}\s*\d{1,5})", row_text)
        flight_number = flight_match.group(1) if flight_match else ""
        airline_code = flight_number[:2] if flight_number else ""

        # Extract times
        times = re.findall(r"(\d{2}:\d{2})", row_text)
        dep_time = times[0] if len(times) > 0 else ""
        arr_time = times[1] if len(times) > 1 else ""

        # Extract duration
        dur_match = re.search(r"(\d+h\s*\d*m?)", row_text)
        duration = dur_match.group(1) if dur_match else ""

        # Count stops
        segments = fd.get("segments", [])
        stops = max(0, len(segments) // 3 - 1)  # rough estimate

        # Aircraft
        aircraft_match = re.search(r"\b([A-Z0-9]{3,4})\b", row_text)
        aircraft = ""
        if aircraft_match:
            code = aircraft_match.group(1)
            if code not in (origin, dest, flight_number.replace(" ", "")):
                aircraft = code

        # Create one AwardFlight per available cabin
        for cabin_name, status in availability.items():
            mapped_cabin = CABIN_NAMES.get(cabin_name, cabin_name)
            # ANA award miles vary by route/cabin but we don't see the
            # exact number on the availability page — mark as available/not
            all_flights.append(AwardFlight(
                flight_number=flight_number,
                airline=airline_code,
                duration=duration,
                departure_time=dep_time,
                arrival_time=arr_time,
                origin=origin,
                dest=dest,
                stops=stops,
                cabin=mapped_cabin,
                miles=0,        # ANA doesn't show miles on result page
                miles_str="—",  # availability only
                status=status,
                aircraft=aircraft,
            ))

    # Filter to requested cabin if specified
    if cabin != "all":
        all_flights = [f for f in all_flights if f.cabin == cabin or f.status == "available"]

    # Sort: available first, then by cabin class
    cabin_order = {"first": 0, "business": 1, "premium": 2, "economy": 3}
    all_flights.sort(
        key=lambda f: (
            0 if f.status == "available" else 1,
            cabin_order.get(f.cabin, 9),
        )
    )

    if top > 0:
        all_flights = all_flights[:top]

    total_text = data.get("totalCount", "")
    total_match = re.search(r"(\d+)", total_text)
    total = int(total_match.group(1)) if total_match else len(flights_data)

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
    from patchright.sync_api import sync_playwright

    state_path, user_agent = load_auth()
    if not state_path:
        return CalendarResult(
            origin=origin, dest=dest, year=year, month=month,
            months_data=[], error=NO_AUTH_MSG,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx_opts = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
            "storage_state": state_path,
        }
        if user_agent:
            ctx_opts["user_agent"] = user_agent
        context = browser.new_context(**ctx_opts)
        page = context.new_page()

        try:
            # Navigate to calendar page (cookies handle auth)
            print("Navigating to award calendar...")
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=30000)
            _random_delay(page, 3000, 5000)

            # Check if session expired
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

            # Fill calendar form
            print(f"Searching calendar: {origin}→{dest} from {year}-{month:02d}...")
            _fill_calendar_form(page, origin, dest, year, month)

            # Submit
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

            # Parse calendar results
            print("Parsing calendar...")
            return _parse_calendar(page, origin, dest, year, month)

        except Exception as e:
            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                months_data=[], error=str(e),
            )
        finally:
            browser.close()


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
            "available": "✓ avail",
            "waitlisted": "~ waitlist",
            "unavailable": "✗ n/a",
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
        )

        if args.format == "json":
            print(format_json(result))
        else:
            print(format_table(result))


if __name__ == "__main__":
    main()
