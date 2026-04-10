#!/usr/bin/env python3
"""Generate Google Flights search URLs by constructing the tfs protobuf parameter.

Usage:
    python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business
    python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business --stops 1
    python3 tools/build_url.py TPE ATH 2026-09-01 --cabin economy   # one-way

Batch mode (multiple date combos):
    python3 tools/build_url.py TPE ATH --cabin business --batch \\
        2026-09-01,2026-09-11 \\
        2026-09-02,2026-09-11 \\
        2026-09-04,2026-09-14

Multi-city mode (arbitrary legs, for open-jaw / split tickets):
    python3 tools/build_url.py --multi --cabin business \\
        TPE,ATH,2026-09-01 \\
        ROM,TPE,2026-09-11
"""

import argparse
import base64
import sys


CABIN_MAP = {
    "economy": 1,
    "premium": 2,
    "business": 3,
    "first": 4,
}


def encode_varint(n: int) -> bytes:
    result = b""
    while True:
        bits = n & 0x7F
        n >>= 7
        result += bytes([bits | (0x80 if n else 0)])
        if not n:
            break
    return result


def encode_field_varint(field: int, value: int) -> bytes:
    return encode_varint((field << 3) | 0) + encode_varint(value)


def encode_field_bytes(field: int, data: bytes) -> bytes:
    return encode_varint((field << 3) | 2) + encode_varint(len(data)) + data


def encode_airport(iata: str) -> bytes:
    return encode_field_varint(1, 1) + encode_field_bytes(2, iata.encode())


def encode_leg(date: str, origin: str, dest: str) -> bytes:
    return (
        encode_field_bytes(2, date.encode())
        + encode_field_bytes(13, encode_airport(origin))
        + encode_field_bytes(14, encode_airport(dest))
    )


def build_url(
    origin: str,
    dest: str,
    depart_date: str,
    return_date: str | None = None,
    passengers: int = 1,
    cabin: int = 1,
    stops: int = 0,
    hl: str = "zh-TW",
    curr: str = "TWD",
    gl: str | None = None,
) -> str:
    proto = (
        encode_field_varint(1, 28)
        + encode_field_varint(2, passengers)
        + encode_field_bytes(3, encode_leg(depart_date, origin, dest))
    )
    if return_date:
        proto += encode_field_bytes(3, encode_leg(return_date, dest, origin))
    proto += (
        encode_field_varint(8, stops)
        + encode_field_varint(9, cabin)
        + encode_field_varint(14, 1)
        + bytes.fromhex("82010b08ffffffffffffffffff01980101")
    )
    tfs = base64.urlsafe_b64encode(proto).decode().rstrip("=")
    url = f"https://www.google.com/travel/flights/search?tfs={tfs}&tfu=KgIIAw&hl={hl}&curr={curr}"
    if gl:
        url += f"&gl={gl}"
    return url


def build_url_multi(
    legs: list[tuple[str, str, str]],
    passengers: int = 1,
    cabin: int = 1,
    stops: int = 0,
    hl: str = "zh-TW",
    curr: str = "TWD",
) -> str:
    """Build a multi-city Google Flights URL.

    Args:
        legs: List of (origin, dest, date) tuples, e.g.
              [("TPE", "ATH", "2026-09-01"), ("ROM", "TPE", "2026-09-11")]
    """
    proto = encode_field_varint(1, 28) + encode_field_varint(2, passengers)
    for origin, dest, date in legs:
        proto += encode_field_bytes(3, encode_leg(date, origin, dest))
    proto += (
        encode_field_varint(8, stops)
        + encode_field_varint(9, cabin)
        + encode_field_varint(14, 1)
        + bytes.fromhex("82010b08ffffffffffffffffff01980101")
    )
    tfs = base64.urlsafe_b64encode(proto).decode().rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&tfu=KgIIAw&hl={hl}&curr={curr}"


def main():
    parser = argparse.ArgumentParser(description="Generate Google Flights search URLs")
    parser.add_argument("origin", nargs="?", help="Origin IATA code (e.g. TPE)")
    parser.add_argument("dest", nargs="?", help="Destination IATA code (e.g. ATH)")
    parser.add_argument("depart", nargs="?", help="Departure date YYYY-MM-DD")
    parser.add_argument("return_date", nargs="?", help="Return date YYYY-MM-DD")
    parser.add_argument(
        "--cabin",
        choices=["economy", "premium", "business", "first"],
        default="economy",
    )
    parser.add_argument(
        "--stops",
        type=int,
        default=0,
        help="0=any, 1=nonstop, 2=max 1 stop",
    )
    parser.add_argument("--passengers", type=int, default=1)
    parser.add_argument("--hl", default="zh-TW")
    parser.add_argument("--curr", default="TWD")
    parser.add_argument("--gl", default=None, help="Country code for POS (e.g. tw, th, tr)")
    parser.add_argument(
        "--batch",
        nargs="+",
        metavar="DEPART,RETURN",
        help="Batch mode: multiple date pairs",
    )
    parser.add_argument(
        "--multi",
        nargs="+",
        metavar="ORIGIN,DEST,DATE",
        help="Multi-city mode: arbitrary legs (e.g. TPE,ATH,2026-09-01 ROM,TPE,2026-09-11)",
    )

    args = parser.parse_args()
    cabin = CABIN_MAP[args.cabin]

    if args.multi:
        legs = []
        for leg_str in args.multi:
            parts = leg_str.split(",")
            if len(parts) != 3:
                parser.error(f"Each --multi leg must be ORIGIN,DEST,DATE, got: {leg_str}")
            legs.append((parts[0], parts[1], parts[2]))
        label = " → ".join(f"{o}→{d} {dt}" for o, d, dt in legs)
        url = build_url_multi(legs, args.passengers, cabin, args.stops, args.hl, args.curr)
        if args.gl:
            url += f"&gl={args.gl}"
        print(f"{label}: {url}")
    elif args.batch:
        for pair in args.batch:
            parts = pair.split(",")
            depart = parts[0]
            ret = parts[1] if len(parts) > 1 else None
            url = build_url(
                args.origin, args.dest, depart, ret,
                args.passengers, cabin, args.stops, args.hl, args.curr, args.gl,
            )
            label = f"{depart} → {ret}" if ret else depart
            print(f"{label}: {url}")
    elif args.depart:
        url = build_url(
            args.origin, args.dest, args.depart, args.return_date,
            args.passengers, cabin, args.stops, args.hl, args.curr, args.gl,
        )
        print(url)
    else:
        parser.error("Either provide depart date, use --batch, or use --multi")


if __name__ == "__main__":
    main()
