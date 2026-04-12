#!/usr/bin/env python3
"""Alaska Airlines award (mileage) search using Patchright.

Searches Alaska Airlines for award availability, parsing mileage prices
and flight details from the results page.

Uses Patchright (undetected Playwright fork) to bypass Akamai anti-bot.

Usage:
    # One-way search
    python3 tools/award_search.py SEA LAX 2026-10-01

    # Round-trip search
    python3 tools/award_search.py SEA LAX 2026-10-01 --return-date 2026-10-08

    # Date range (search multiple days)
    python3 tools/award_search.py SEA LAX --start 2026-10-01 --end 2026-10-03

    # Monthly calendar view (lowest miles per day)
    python3 tools/award_search.py SEA NRT 2026-10-01 --calendar

    # JSON output
    python3 tools/award_search.py SEA LAX 2026-10-01 --format json
"""

import argparse
import calendar
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta


@dataclass
class AwardFlight:
    flight_number: str
    duration: str
    departure_time: str
    arrival_time: str
    origin: str
    dest: str
    stops: int
    cabin: str
    miles: str          # e.g. "12.5k" or "25k"
    miles_int: int      # e.g. 12500 or 25000
    taxes: str          # e.g. "$6"
    taxes_int: int      # e.g. 6
    seats_left: str     # e.g. "last 5 seats" or ""
    badge: str          # e.g. "Best value" or ""


@dataclass
class AwardSearchResult:
    origin: str
    dest: str
    date: str
    return_date: str | None
    total_results: int
    flights: list[AwardFlight]
    error: str | None


@dataclass
class CalendarDay:
    date: str           # YYYY-MM-DD
    miles: int          # lowest award points (e.g. 35000)
    miles_str: str      # display string (e.g. "35k")
    taxes: float        # cash co-pay (e.g. 5.6)
    is_discounted: bool


@dataclass
class CalendarResult:
    origin: str
    dest: str
    year: int
    month: int
    days: list[CalendarDay]
    error: str | None


def build_search_url(
    origin: str,
    dest: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
) -> str:
    """Construct direct Alaska Airlines award search results URL."""
    url = (
        f"https://www.alaskaair.com/search/results"
        f"?O={origin}&D={dest}&OD={depart_date}&A={adults}"
    )
    if return_date:
        url += f"&DD={return_date}&RT=true"
    else:
        url += "&RT=false"
    url += "&ShoppingMethod=onlineaward&locale=en-us"
    return url


def parse_miles(text: str) -> tuple[str, int]:
    """Parse miles string like '12.5k' into display string and integer value."""
    text = text.strip().lower().replace(",", "")
    m = re.match(r"([\d.]+)\s*k?", text)
    if not m:
        return text, 0
    num = float(m.group(1))
    if "k" in text:
        return text.upper().replace("K", "k"), int(num * 1000)
    return text, int(num)


def parse_taxes(text: str) -> tuple[str, int]:
    """Parse taxes string like '$6' into display string and integer."""
    m = re.search(r"\$(\d+(?:\.\d+)?)", text)
    if not m:
        return text.strip(), 0
    amount = float(m.group(1))
    return f"${int(amount)}", int(amount)


def parse_flight_card(card_data: dict) -> list[AwardFlight]:
    """Parse a flight card's extracted data into AwardFlight objects.

    Each card can have multiple fare classes (Main, First, etc).
    Returns one AwardFlight per available fare.
    """
    flights = []
    flight_number = card_data.get("flight_number", "")
    duration = card_data.get("duration", "")
    departure_time = card_data.get("departure_time", "")
    arrival_time = card_data.get("arrival_time", "")
    origin = card_data.get("origin", "")
    dest = card_data.get("dest", "")
    stops = card_data.get("stops", 0)
    badge = card_data.get("badge", "")

    for fare in card_data.get("fares", []):
        miles_str, miles_int = parse_miles(fare.get("miles", "0"))
        taxes_str, taxes_int = parse_taxes(fare.get("taxes", "$0"))
        flights.append(AwardFlight(
            flight_number=flight_number,
            duration=duration,
            departure_time=departure_time,
            arrival_time=arrival_time,
            origin=origin,
            dest=dest,
            stops=stops,
            cabin=fare.get("cabin", ""),
            miles=miles_str,
            miles_int=miles_int,
            taxes=taxes_str,
            taxes_int=taxes_int,
            seats_left=fare.get("seats_left", ""),
            badge=badge,
        ))

    return flights


def search_awards(
    origin: str,
    dest: str,
    depart_date: str,
    return_date: str | None = None,
    headed: bool = True,
    top: int = 10,
) -> AwardSearchResult:
    """Search Alaska Airlines for award flights.

    Uses direct URL navigation with Patchright to bypass anti-bot detection.
    Falls back to form-based search if direct URL is blocked.

    Note: headed=True (default) is required to bypass Akamai anti-bot.
    Headless mode is blocked by Akamai fingerprinting.
    """
    from patchright.sync_api import sync_playwright

    url = build_search_url(origin, dest, depart_date, return_date)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)

            # Dismiss popups
            for btn_text in ["Accept all", "No, thanks"]:
                try:
                    el = page.get_by_text(btn_text, exact=True)
                    if el.count() > 0 and el.first.is_visible():
                        el.first.click()
                        page.wait_for_timeout(500)
                except Exception:
                    pass

            # Check if we got redirected to the search form
            current_url = page.url
            if "/results" not in current_url:
                return _search_via_form(
                    page, origin, dest, depart_date, return_date, top
                )

            # Wait for results to load
            page.wait_for_timeout(3000)

            # Check for error states
            body_text = page.evaluate(
                "() => document.body.innerText.substring(0, 1000)"
            )
            if "no results" in body_text.lower() or "sorry" in body_text.lower():
                return AwardSearchResult(
                    origin=origin, dest=dest, date=depart_date,
                    return_date=return_date, total_results=0,
                    flights=[], error="No award flights available",
                )

            return _parse_results(page, origin, dest, depart_date, return_date, top)

        except Exception as e:
            return AwardSearchResult(
                origin=origin, dest=dest, date=depart_date,
                return_date=return_date, total_results=0,
                flights=[], error=str(e),
            )
        finally:
            browser.close()


def _search_via_form(
    page, origin: str, dest: str, depart_date: str,
    return_date: str | None, top: int,
) -> AwardSearchResult:
    """Fallback: fill the search form manually when direct URL is blocked."""
    try:
        page.goto(
            "https://www.alaskaair.com/search",
            wait_until="domcontentloaded", timeout=30000,
        )
        page.wait_for_timeout(4000)

        # Dismiss popups
        for btn_text in ["Accept all", "No, thanks"]:
            try:
                el = page.get_by_text(btn_text, exact=True)
                if el.count() > 0 and el.first.is_visible():
                    el.first.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

        # Set trip type
        if not return_date:
            page.get_by_text("One way", exact=True).first.click()
            page.wait_for_timeout(500)

        # Enable award search
        page.get_by_text("Use points", exact=True).first.click()
        page.wait_for_timeout(500)

        # Fill origin
        _fill_airport_field(page, "From", origin)
        # Fill destination
        _fill_airport_field(page, "To", dest)

        # Fill date
        date_label = "Date" if not return_date else "Dates"
        date_el = page.get_by_text(date_label, exact=True)
        for i in range(date_el.count()):
            el = date_el.nth(i)
            if el.is_visible():
                box = el.bounding_box()
                if box and 100 < box["y"] < 500:
                    el.click()
                    page.wait_for_timeout(1000)
                    dt = datetime.strptime(depart_date, "%Y-%m-%d")
                    page.keyboard.type(dt.strftime("%m/%d/%Y"), delay=50)
                    page.wait_for_timeout(500)
                    if return_date:
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(500)
                        rt = datetime.strptime(return_date, "%Y-%m-%d")
                        page.keyboard.type(rt.strftime("%m/%d/%Y"), delay=50)
                        page.wait_for_timeout(500)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                    break

        # Click search
        search_btn = page.get_by_role("button", name="Search flights")
        search_btn.first.click()
        page.wait_for_timeout(10000)

        return _parse_results(page, origin, dest, depart_date, return_date, top)

    except Exception as e:
        return AwardSearchResult(
            origin=origin, dest=dest, date=depart_date,
            return_date=return_date, total_results=0,
            flights=[], error=f"Form search failed: {e}",
        )


def _fill_airport_field(page, label_text: str, code: str):
    """Click an airport field label, type the code, select from dropdown."""
    el = page.get_by_text(label_text, exact=True)
    for i in range(el.count()):
        item = el.nth(i)
        if item.is_visible():
            box = item.bounding_box()
            if box and 100 < box["y"] < 500:
                item.click()
                break
    page.wait_for_timeout(1000)
    page.keyboard.type(code, delay=100)
    page.wait_for_timeout(1500)

    option = page.locator(f'[data-testid="airport-option-{code}"]').first
    option.click(force=True)
    page.wait_for_timeout(1000)


def _parse_results(
    page, origin: str, dest: str, depart_date: str,
    return_date: str | None, top: int,
) -> AwardSearchResult:
    """Parse award flight results from the loaded page."""
    # NOTE: r-string is critical — prevents Python from interpreting \d, \s etc.
    cards_data = page.evaluate(r"""() => {
        const cards = [];
        let idx = 0;

        while (true) {
            const card = document.querySelector(`[data-testid="flight-card-${idx}"]`);
            if (!card) break;

            const data = { fares: [] };

            // Flight number
            const fnEl = card.querySelector('.flight-number, [class*="flight-number"]');
            data.flight_number = fnEl ? fnEl.textContent.trim() : '';

            // Duration
            const durEl = card.querySelector('[class*="flight-top-info"]');
            if (durEl) {
                const durText = durEl.textContent.trim();
                const durMatch = durText.match(/(\d+h\s*\d*m?)/);
                data.duration = durMatch ? durMatch[1] : '';
            } else {
                data.duration = '';
            }

            // Badge (Best value, etc.)
            const badgeEl = card.querySelector('[data-testid="flight-card-badge-low-fare"]');
            data.badge = badgeEl ? badgeEl.textContent.trim() : '';

            // Departure/arrival times and airports
            const headerEl = card.querySelector('[class*="flight-info-container"], .flight-header');
            if (headerEl) {
                const headerText = headerEl.textContent.trim();
                const timeMatch = headerText.match(
                    /(\d{1,2}:\d{2}\s*[ap]m)\s*([A-Z]{3})\s*(\d{1,2}:\d{2}\s*[ap]m)\s*([A-Z]{3})/
                );
                if (timeMatch) {
                    data.departure_time = timeMatch[1];
                    data.origin = timeMatch[2];
                    data.arrival_time = timeMatch[3];
                    data.dest = timeMatch[4];
                }
            }

            // Stops
            const cardText = card.textContent || '';
            const stopMatch = cardText.match(/(\d+)\s*stop/i);
            data.stops = stopMatch ? parseInt(stopMatch[1]) : 0;

            // Parse fares from full card text
            const farePattern = /(Main|First|Saver|Premium)\s*([\d,.]+k?)\s*points\s*pts\s*\+\s*\$(\d+)/gi;
            let fareMatch;
            while ((fareMatch = farePattern.exec(cardText)) !== null) {
                const fare = {
                    cabin: fareMatch[1],
                    miles: fareMatch[2],
                    taxes: '$' + fareMatch[3],
                    seats_left: '',
                };
                data.fares.push(fare);
            }

            // Check for seats info
            const seatsMatch = cardText.match(/(last\s*\d+\s*seats?)/i);
            if (seatsMatch && data.fares.length > 0) {
                data.fares[data.fares.length - 1].seats_left = seatsMatch[1];
            }

            // Reduced fare badge
            if (cardText.includes('Reduced fare') || cardText.includes('Best value')) {
                if (!data.badge) {
                    data.badge = cardText.includes('Best value') ? 'Best value' : 'Reduced fare';
                }
            }

            cards.push(data);
            idx++;
            if (idx >= 50) break;
        }

        // Total results count
        const countEl = document.querySelector('.results-count, [class*="results-count"]');
        const countText = countEl ? countEl.textContent.trim() : '';
        const countMatch = countText.match(/(\d+)/);
        const totalResults = countMatch ? parseInt(countMatch[1]) : cards.length;

        return { cards, totalResults };
    }""")

    total_results = cards_data.get("totalResults", 0)
    all_flights = []

    for card_data in cards_data.get("cards", []):
        card_data.setdefault("origin", origin)
        card_data.setdefault("dest", dest)
        card_data.setdefault("stops", 0)
        card_data.setdefault("badge", "")

        parsed = parse_flight_card(card_data)
        all_flights.extend(parsed)

    # Sort by miles ascending
    all_flights.sort(key=lambda f: f.miles_int)

    if top > 0:
        all_flights = all_flights[:top]

    return AwardSearchResult(
        origin=origin, dest=dest, date=depart_date,
        return_date=return_date, total_results=total_results,
        flights=all_flights, error=None,
    )


def search_calendar(
    origin: str,
    dest: str,
    year: int,
    month: int,
    headed: bool = True,
) -> CalendarResult:
    """Fetch monthly award calendar by reading shoulder-dates data.

    Alaska's results page includes a <shoulder-dates> web component with a
    JSON 'dates' attribute containing ~31 days of lowest award prices centered
    on the search date. Searching for the 15th of the month covers most of
    the month in a single request.
    """
    from patchright.sync_api import sync_playwright

    # Search the 15th to center the ±15 day window on the month
    search_date = f"{year}-{month:02d}-15"
    url = build_search_url(origin, dest, search_date)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(12000)  # shoulder-dates loads after results

            # Dismiss popups
            for btn_text in ["Accept all", "No, thanks"]:
                try:
                    el = page.get_by_text(btn_text, exact=True)
                    if el.count() > 0 and el.first.is_visible():
                        el.first.click()
                        page.wait_for_timeout(500)
                except Exception:
                    pass

            page.wait_for_timeout(3000)

            # Extract dates JSON from shoulder-dates component
            dates_json = page.evaluate(r"""() => {
                const el = document.querySelector('shoulder-dates');
                if (!el) return null;
                const attr = el.getAttribute('dates');
                if (!attr) return null;
                try { return JSON.parse(attr); }
                catch (e) { return null; }
            }""")

            if not dates_json:
                return CalendarResult(
                    origin=origin, dest=dest, year=year, month=month,
                    days=[], error="Could not find shoulder-dates data",
                )

            # Filter to target month
            month_prefix = f"{year}-{month:02d}-"
            days = []
            for entry in dates_json:
                if entry["date"].startswith(month_prefix):
                    pts = entry.get("awardPoints") or 0
                    if not pts:
                        continue
                    pts_k = pts / 1000
                    if pts_k == int(pts_k):
                        miles_str = f"{int(pts_k)}k"
                    else:
                        miles_str = f"{pts_k:.1f}k"
                    days.append(CalendarDay(
                        date=entry["date"],
                        miles=pts,
                        miles_str=miles_str,
                        taxes=entry.get("price", 0),
                        is_discounted=entry.get("isDiscounted", False),
                    ))

            days.sort(key=lambda d: d.date)

            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                days=days, error=None,
            )

        except Exception as e:
            return CalendarResult(
                origin=origin, dest=dest, year=year, month=month,
                days=[], error=str(e),
            )
        finally:
            browser.close()


def format_calendar_table(result: CalendarResult) -> str:
    """Format calendar results as a monthly grid."""
    month_name = calendar.month_name[result.month]
    lines = []
    lines.append(f"\n  {month_name} {result.year} — {result.origin} → {result.dest} (Award Miles)")
    lines.append(f"  {'='*42}")

    if result.error:
        lines.append(f"  Error: {result.error}")
        return "\n".join(lines)

    if not result.days:
        lines.append("  No calendar data available")
        return "\n".join(lines)

    # Build lookup: day_of_month -> CalendarDay
    day_map = {}
    for d in result.days:
        dom = int(d.date.split("-")[2])
        day_map[dom] = d

    # Header
    lines.append(f"  {'Mon':>6} {'Tue':>6} {'Wed':>6} {'Thu':>6} {'Fri':>6} {'Sat':>6} {'Sun':>6}")
    lines.append(f"  {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

    # Calendar grid (Monday=0)
    _, num_days = calendar.monthrange(result.year, result.month)
    first_weekday = calendar.weekday(result.year, result.month, 1)  # 0=Mon

    # Find the lowest miles for highlighting
    all_miles = [d.miles for d in result.days if d.miles > 0]
    min_miles = min(all_miles) if all_miles else 0

    # Day numbers row + miles row, week by week
    day = 1
    week_num = 0
    while day <= num_days:
        day_row = "  "
        miles_row = "  "

        for weekday in range(7):
            if week_num == 0 and weekday < first_weekday:
                day_row += f"{'':>6} "
                miles_row += f"{'':>6} "
            elif day > num_days:
                day_row += f"{'':>6} "
                miles_row += f"{'':>6} "
            else:
                day_row += f"{day:>6} "
                if day in day_map and day_map[day].miles > 0:
                    m = day_map[day].miles_str
                    marker = "*" if day_map[day].miles == min_miles else ""
                    miles_row += f"{m + marker:>6} "
                else:
                    miles_row += f"{'--':>6} "
                day += 1

        lines.append(day_row.rstrip())
        lines.append(miles_row.rstrip())
        week_num += 1

    # Summary
    if all_miles:
        min_m = min(all_miles)
        min_dates = [d.date for d in result.days if d.miles == min_m]
        min_str = f"{min_m/1000:.1f}k" if min_m % 1000 else f"{min_m//1000}k"
        lines.append(f"\n  Lowest: {min_str} miles on {', '.join(d.split('-')[2] for d in min_dates)}")
        lines.append(f"  * = lowest fare")

    return "\n".join(lines)


def format_calendar_json(result: CalendarResult) -> str:
    """Format calendar results as JSON."""
    data = {
        "origin": result.origin,
        "dest": result.dest,
        "year": result.year,
        "month": result.month,
        "month_name": calendar.month_name[result.month],
        "error": result.error,
        "days": [asdict(d) for d in result.days],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_table(result: AwardSearchResult) -> str:
    """Format results as a human-readable table."""
    lines = []
    lines.append(f"\n{'='*80}")
    trip = f"{result.origin}→{result.dest} {result.date}"
    if result.return_date:
        trip += f" ~ {result.return_date}"
    lines.append(f"  Alaska Award Search: {trip}")
    lines.append(f"  {result.total_results} total results")
    lines.append(f"{'='*80}")

    if result.error:
        lines.append(f"  Error: {result.error}")
        return "\n".join(lines)

    if not result.flights:
        lines.append("  No award flights found")
        return "\n".join(lines)

    lines.append(
        f"  {'#':<3} {'Flight':<10} {'Cabin':<8} {'Miles':>8} {'Tax':>5} "
        f"{'Duration':<8} {'Depart':>8} {'Arrive':>8} {'Stops':>5} {'Note'}"
    )
    lines.append(
        f"  {'-'*3} {'-'*10} {'-'*8} {'-'*8} {'-'*5} "
        f"{'-'*8} {'-'*8} {'-'*8} {'-'*5} {'-'*15}"
    )

    for i, f in enumerate(result.flights, 1):
        note = f.seats_left or f.badge or ""
        lines.append(
            f"  {i:<3} {f.flight_number:<10} {f.cabin:<8} {f.miles:>8} {f.taxes:>5} "
            f"{f.duration:<8} {f.departure_time:>8} {f.arrival_time:>8} {f.stops:>5} {note}"
        )

    return "\n".join(lines)


def format_json(result: AwardSearchResult) -> str:
    """Format results as JSON."""
    data = {
        "origin": result.origin,
        "dest": result.dest,
        "date": result.date,
        "return_date": result.return_date,
        "total_results": result.total_results,
        "error": result.error,
        "flights": [asdict(f) for f in result.flights],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Alaska Airlines award (mileage) flight search"
    )
    parser.add_argument("origin", help="Origin IATA code (e.g. SEA)")
    parser.add_argument("dest", help="Destination IATA code (e.g. LAX)")
    parser.add_argument("date", nargs="?", help="Departure date YYYY-MM-DD")
    parser.add_argument(
        "--return-date", help="Return date YYYY-MM-DD (makes it round-trip)"
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
        help="Show monthly calendar view with lowest miles per day",
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

    for date in dates:
        result = search_awards(
            origin=args.origin,
            dest=args.dest,
            depart_date=date,
            return_date=args.return_date,
            headed=not args.headless,
            top=args.top,
        )

        if args.format == "json":
            print(format_json(result))
        else:
            print(format_table(result))


if __name__ == "__main__":
    main()
