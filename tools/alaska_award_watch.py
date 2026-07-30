#!/usr/bin/env python3
"""Scan Alaska Atmos Rewards award results without launching a browser.

Examples:
    python3 tools/alaska_award_watch.py \
      --route BKK-FRA --route HKG-LHR \
      --passengers 2 --cabin business --max-points 75000 \
      --date 2026-10-20 --date 2026-11-17

    python3 tools/alaska_award_watch.py \
      --route NRT-HEL --route ICN-HEL \
      --passengers 2 --cabin business --max-points 60000 \
      --start 2026-08-01 --end 2027-06-30 \
      --day-of-month 3,10,17,24 --workers 4 \
      --output results/alaska_monthly_watch.md

The Alaska result page includes a serialized search result object. This script
fetches that page with curl, then extracts the fare and segment data locally.
The page format is not a public API, so always verify a hit on alaskaair.com
before transferring points or booking.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode


SEARCH_URL = "https://www.alaskaair.com/search/results"
CURL_TIMEOUT_SECONDS = 25
CABIN_COLUMNS = {
    "economy": "REFUNDABLE_MAIN",
    "premium": "REFUNDABLE_PARTNER_PREMIUM",
    "business": "REFUNDABLE_BUSINESS",
    "first": "REFUNDABLE_FIRST",
}


@dataclass(frozen=True)
class SearchRequest:
    origin: str
    destination: str
    date: dt.date


@dataclass(frozen=True)
class AwardHit:
    request: SearchRequest
    carriers: str
    flights: str
    aircraft: str
    duration_minutes: int | None
    points_per_person: int
    points_total: int
    cash_per_person: float
    cash_total: float
    seats_remaining: int
    mixed_cabin: bool
    url: str


@dataclass(frozen=True)
class ScanFailure:
    request: SearchRequest
    message: str


@dataclass(frozen=True)
class CalendarDay:
    request: SearchRequest
    hit: AwardHit | None


def parse_route(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Za-z]{3})[-:]([A-Za-z]{3})", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("route must use ORIGIN-DEST, e.g. BKK-FRA")
    return match.group(1).upper(), match.group(2).upper()


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def parse_days(value: str) -> list[int]:
    try:
        days = sorted({int(item) for item in value.split(",")})
    except ValueError as error:
        raise argparse.ArgumentTypeError("day-of-month must be comma-separated integers") from error
    if not days or any(day < 1 or day > 31 for day in days):
        raise argparse.ArgumentTypeError("day-of-month values must be between 1 and 31")
    return days


def parse_month(value: str) -> tuple[int, int]:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m")
    except ValueError as error:
        raise argparse.ArgumentTypeError("calendar must use YYYY-MM") from error
    return parsed.year, parsed.month


def month_scan_dates(start: dt.date, end: dt.date, days: list[int]) -> list[dt.date]:
    dates: list[dt.date] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        last_day = (next_month - dt.timedelta(days=1)).day
        for day in days:
            if day <= last_day:
                candidate = cursor.replace(day=day)
                if start <= candidate <= end:
                    dates.append(candidate)
        cursor = next_month
    return dates


def all_month_dates(year: int, month: int) -> list[dt.date]:
    return [
        dt.date(year, month, day)
        for day in range(1, calendar.monthrange(year, month)[1] + 1)
    ]


def build_url(request: SearchRequest, passengers: int) -> str:
    query = urlencode(
        {
            "A": passengers,
            "C": 0,
            "D": request.destination,
            "L": 0,
            "O": request.origin,
            "OD": request.date.isoformat(),
            "OT": "Anytime",
            "RT": "false",
            "ShoppingMethod": "onlineaward",
            "UPG": "none",
            "awardType": "MilesOnly",
        }
    )
    return f"{SEARCH_URL}?{query}"


def fetch_html(url: str) -> str:
    command = [
        "curl",
        "--location",
        "--compressed",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        str(CURL_TIMEOUT_SECONDS),
        "--user-agent",
        "Mozilla/5.0 (compatible; AtmosAwardWatch/1.0)",
        url,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        message = completed.stderr.strip() or f"curl exited with {completed.returncode}"
        raise RuntimeError(message)
    return completed.stdout


def find_number(text: str, name: str, default: int | None = None) -> int | None:
    match = re.search(rf"{re.escape(name)}:(\d+)", text)
    return int(match.group(1)) if match else default


def find_decimal(text: str, name: str, default: float | None = None) -> float | None:
    match = re.search(rf"{re.escape(name)}:(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else default


def find_value(text: str, name: str, default: str = "") -> str:
    match = re.search(rf'{re.escape(name)}:"([^"]*)"', text)
    return match.group(1) if match else default


def route_records(html: str, request: SearchRequest) -> Iterable[str]:
    pattern = re.compile(
        rf'\{{id:\d+,origin:"{request.origin}",destination:"{request.destination}".*?version:"v2\.0"\}}',
        re.DOTALL,
    )
    yield from (match.group(0) for match in pattern.finditer(html))


def parse_record(record: str, request: SearchRequest, cabin: str, url: str) -> AwardHit | None:
    column = CABIN_COLUMNS[cabin]
    solution_match = re.search(
        rf"{column}:\{{(.*?)\}}(?=,REFUNDABLE_|,version:)", record, re.DOTALL
    )
    if not solution_match:
        return None

    solution = solution_match.group(1)
    points_per_person = find_number(solution, "atmosPoints")
    points_total = find_number(solution, "allPaxPoints")
    cash_per_person = find_decimal(solution, "grandTotal")
    cash_total = find_decimal(solution, "allPaxTotal")
    seats_remaining = find_number(solution, "seatsRemaining")
    if None in (points_per_person, points_total, cash_per_person, cash_total, seats_remaining):
        return None

    disclosures = re.findall(r'disclosures:\["([^"]+)"\]', record)
    carriers = "; ".join(disclosures) or "Unknown carrier"
    segments = re.findall(
        r'displayCarrier:\{carrierCode:"([^"]+)",flightNumber:(\d+),carrierFullName:"([^"]+)"\}',
        record,
    )
    flights = ", ".join(dict.fromkeys(f"{code}{number}" for code, number, _ in segments))
    aircraft = ", ".join(dict.fromkeys(re.findall(r'aircraft:"([^"]+)"', record)))

    return AwardHit(
        request=request,
        carriers=carriers,
        flights=flights,
        aircraft=aircraft,
        duration_minutes=find_number(record, "duration"),
        points_per_person=points_per_person,
        points_total=points_total,
        cash_per_person=cash_per_person,
        cash_total=cash_total,
        seats_remaining=seats_remaining,
        mixed_cabin="mixedCabin:true" in solution,
        url=url,
    )


def scan_request(
    request: SearchRequest,
    passengers: int,
    cabin: str,
    max_points: int | None,
    max_total_points: int | None,
) -> tuple[list[AwardHit], ScanFailure | None]:
    url = build_url(request, passengers)
    try:
        html = fetch_html(url)
    except RuntimeError as error:
        return [], ScanFailure(request, str(error))

    hits: list[AwardHit] = []
    for record in route_records(html, request):
        hit = parse_record(record, request, cabin, url)
        if hit is None:
            continue
        if hit.seats_remaining < passengers:
            continue
        if max_points is not None and hit.points_per_person > max_points:
            continue
        if max_total_points is not None and hit.points_total > max_total_points:
            continue
        hits.append(hit)
    return hits, None


def duration_label(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def markdown_report(
    requests: list[SearchRequest],
    hits: list[AwardHit],
    failures: list[ScanFailure],
    args: argparse.Namespace,
) -> str:
    lines = [
        "# Alaska Atmos Rewards Award Watch",
        "",
        f"Scanned: {dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        "## Search Settings",
        "",
        f"- Routes: {', '.join(args.route)}",
        f"- Dates: {len({request.date for request in requests})} dates / {len(requests)} searches",
        f"- Passengers: {args.passengers}",
        f"- Cabin: {args.cabin}",
        f"- Per-person cap: {args.max_points if args.max_points is not None else 'none'}",
        f"- Total cap: {args.max_total_points if args.max_total_points is not None else 'none'}",
        "",
        "## Matches",
        "",
    ]
    if not hits:
        lines.append("No matching awards found. Verify hits on alaskaair.com before assuming availability.")
    else:
        lines.extend(
            [
                "| Date | Route | Carrier | Flights | Duration | Points/person | Points total | Cash total | Seats |",
                "|---|---|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for hit in sorted(hits, key=lambda item: (item.points_total, item.cash_total, item.request.date)):
            lines.append(
                "| "
                f"{hit.request.date} | {hit.request.origin}-{hit.request.destination} | "
                f"{hit.carriers} | {hit.flights or '-'} | {duration_label(hit.duration_minutes)} | "
                f"{hit.points_per_person:,} | {hit.points_total:,} | ${hit.cash_total:,.2f} | "
                f"{hit.seats_remaining} |"
            )
            lines.append(f"  Search: {hit.url}")
            if hit.aircraft:
                lines.append(f"  Aircraft: {hit.aircraft}")
    if failures:
        lines.extend(["", "## Fetch Failures", ""])
        lines.extend(f"- {item.request.date} {item.request.origin}-{item.request.destination}: {item.message}" for item in failures)
    return "\n".join(lines) + "\n"


def calendar_report(
    requests: list[SearchRequest],
    hits: list[AwardHit],
    failures: list[ScanFailure],
    args: argparse.Namespace,
) -> str:
    year, month = args.calendar
    by_day: dict[tuple[str, str, dt.date], AwardHit] = {}
    for hit in hits:
        key = (hit.request.origin, hit.request.destination, hit.request.date)
        existing = by_day.get(key)
        if existing is None or (hit.points_per_person, hit.cash_per_person) < (
            existing.points_per_person,
            existing.cash_per_person,
        ):
            by_day[key] = hit

    lines = [
        "# Alaska Atmos Rewards Award Calendar",
        "",
        f"Scanned: {dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"- Month: {year}-{month:02d}",
        f"- Passengers: {args.passengers}",
        f"- Cabin: {args.cabin}",
        f"- Per-person cap: {args.max_points if args.max_points is not None else 'none'}",
        f"- Total cap: {args.max_total_points if args.max_total_points is not None else 'none'}",
    ]

    for origin, destination in args.routes:
        route_days = [
            CalendarDay(
                SearchRequest(origin, destination, date),
                by_day.get((origin, destination, date)),
            )
            for date in all_month_dates(year, month)
        ]
        available = [item.hit for item in route_days if item.hit is not None]
        lowest = min(available, key=lambda item: (item.points_per_person, item.cash_per_person), default=None)

        lines.extend([
            "",
            f"## {origin}-{destination}",
            "",
            "| Mon | Tue | Wed | Thu | Fri | Sat | Sun |",
            "|---|---|---|---|---|---|---|",
        ])
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
        for week in weeks:
            cells: list[str] = []
            for date in week:
                if date.month != month:
                    cells.append("")
                    continue
                hit = by_day.get((origin, destination, date))
                if hit is None:
                    cells.append(f"{date.day}<br>--")
                    continue
                points = f"{hit.points_per_person / 1000:g}k"
                marker = "*" if (
                    hit.points_per_person,
                    hit.cash_per_person,
                ) == (
                    lowest.points_per_person,
                    lowest.cash_per_person,
                ) else ""
                cells.append(f"{date.day}<br>{points}{marker}")
            lines.append("| " + " | ".join(cells) + " |")

        if lowest is None:
            lines.append("\nNo matching awards found for this month.")
            continue
        lowest_dates = [
            item.request.date.isoformat()
            for item in route_days
            if item.hit is not None
            and item.hit.points_per_person == lowest.points_per_person
            and item.hit.cash_per_person == lowest.cash_per_person
        ]
        lines.extend([
            "",
            f"Lowest: {lowest.points_per_person:,} points/person on {', '.join(lowest_dates)} (*).",
            "",
            "| Date | Points/person | Cash/person | Seats | Flights | Search |",
            "|---|---:|---:|---:|---|---|",
        ])
        for item in route_days:
            if item.hit is None:
                continue
            hit = item.hit
            lines.append(
                f"| {hit.request.date} | {hit.points_per_person:,} | ${hit.cash_per_person:,.2f} | "
                f"{hit.seats_remaining} | {hit.flights or '-'} | [Open]({hit.url}) |"
            )

    if failures:
        lines.extend(["", "## Fetch Failures", ""])
        lines.extend(
            f"- {item.request.date} {item.request.origin}-{item.request.destination}: {item.message}"
            for item in failures
        )
    return "\n".join(lines) + "\n"


def build_requests(args: argparse.Namespace) -> list[SearchRequest]:
    selected_dates = list(args.date or [])
    if args.calendar:
        if selected_dates or args.start or args.end or args.day_of_month:
            raise ValueError("--calendar cannot be combined with --date or fixed-date scan options")
        selected_dates = all_month_dates(*args.calendar)
    if args.start or args.end or args.day_of_month:
        if not (args.start and args.end and args.day_of_month):
            raise ValueError("--start, --end, and --day-of-month must be used together")
        if args.start > args.end:
            raise ValueError("--start must not be after --end")
        selected_dates.extend(month_scan_dates(args.start, args.end, args.day_of_month))
    if not selected_dates:
        raise ValueError("provide at least one --date or a --start/--end/--day-of-month scan")
    unique_dates = sorted(set(selected_dates))
    return [SearchRequest(origin, destination, date) for origin, destination in args.routes for date in unique_dates]


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight Alaska Atmos award scanner using curl")
    parser.add_argument("--route", action="append", required=True, help="Repeatable route: BKK-FRA")
    parser.add_argument("--passengers", type=int, default=1)
    parser.add_argument("--cabin", choices=sorted(CABIN_COLUMNS), default="business")
    parser.add_argument("--max-points", type=int, help="Maximum points per person")
    parser.add_argument("--max-total-points", type=int, help="Maximum total points for all passengers")
    parser.add_argument("--date", action="append", type=parse_date, help="Repeatable date: YYYY-MM-DD")
    parser.add_argument("--start", type=parse_date, help="Start date for fixed monthly scan")
    parser.add_argument("--end", type=parse_date, help="End date for fixed monthly scan")
    parser.add_argument("--day-of-month", type=parse_days, help="Fixed days, e.g. 3,10,17,24")
    parser.add_argument("--calendar", type=parse_month, metavar="YYYY-MM", help="Full monthly lowest-points calendar")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent curl requests (1-6)")
    parser.add_argument("--output", type=Path, help="Optional Markdown report path")
    args = parser.parse_args()

    try:
        args.routes = [parse_route(route) for route in args.route]
        if args.passengers < 1 or args.passengers > 9:
            raise ValueError("--passengers must be between 1 and 9")
        if args.workers < 1 or args.workers > 6:
            raise ValueError("--workers must be between 1 and 6")
        if args.max_points is not None and args.max_points < 1:
            raise ValueError("--max-points must be positive")
        if args.max_total_points is not None and args.max_total_points < 1:
            raise ValueError("--max-total-points must be positive")
        requests = build_requests(args)
    except ValueError as error:
        parser.error(str(error))

    hits: list[AwardHit] = []
    failures: list[ScanFailure] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                scan_request,
                request,
                args.passengers,
                args.cabin,
                args.max_points,
                args.max_total_points,
            )
            for request in requests
        ]
        for future in concurrent.futures.as_completed(futures):
            request_hits, failure = future.result()
            hits.extend(request_hits)
            if failure:
                failures.append(failure)

    report = calendar_report(requests, hits, failures, args) if args.calendar else markdown_report(requests, hits, failures, args)
    print(report, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
