#!/usr/bin/env python3
"""Generate combo ticket search strategies for finding cheaper flights.

Given an origin, destination, and travel dates, generates multiple search
strategies including:
  - Baseline: standard round-trip
  - Open Jaw: fly into one city, return from a nearby city
  - Reverse Ticket: round-trip originating from destination + one-way to get there
  - Split Ticket: break the journey via cheap hub cities

Usage:
    python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business
    python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin economy --json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_url import CABIN_MAP, build_url


# Nearby airports for open-jaw: destination -> list of alternative return cities
# Each entry: (IATA, city name, typical transport from destination)
NEARBY_AIRPORTS = {
    # Europe - Mediterranean / Southern
    "ATH": [("IST", "伊斯坦堡", "2h flight"), ("SOF", "索菲亞", "bus/flight"), ("ROM", "羅馬", "2.5h flight"), ("MIL", "米蘭", "2.5h flight")],
    "ROM": [("MIL", "米蘭", "3h train"), ("FCO", "羅馬", "same city"), ("NAP", "那不勒斯", "1h train"), ("VCE", "威尼斯", "3.5h train"), ("ATH", "雅典", "2.5h flight")],
    "IST": [("ATH", "雅典", "2h flight"), ("SOF", "索菲亞", "bus/flight"), ("AYT", "安塔利亞", "1h flight")],
    "BCN": [("MAD", "馬德里", "2.5h train"), ("LIS", "里斯本", "flight"), ("MRS", "馬賽", "4h train")],
    "PAR": [("LON", "倫敦", "2.5h train"), ("AMS", "阿姆斯特丹", "3h train"), ("BRU", "布魯塞爾", "1.5h train"), ("FRA", "法蘭克福", "4h train")],
    "LON": [("PAR", "巴黎", "2.5h train"), ("AMS", "阿姆斯特丹", "1h flight"), ("BRU", "布魯塞爾", "2h train")],
    "VIE": [("PRG", "布拉格", "4h train"), ("BUD", "布達佩斯", "2.5h train"), ("MUC", "慕尼黑", "4h train")],
    "PRG": [("VIE", "維也納", "4h train"), ("BUD", "布達佩斯", "train"), ("MUC", "慕尼黑", "5h train/bus")],
    # Europe - Northern / Western
    "AMS": [("PAR", "巴黎", "3h train"), ("LON", "倫敦", "1h flight"), ("BRU", "布魯塞爾", "2h train"), ("FRA", "法蘭克福", "4h train")],
    "FRA": [("MUC", "慕尼黑", "3.5h train"), ("AMS", "阿姆斯特丹", "4h train"), ("ZRH", "蘇黎世", "4h train")],
    # Asia
    "NRT": [("KIX", "大阪", "shinkansen"), ("HND", "東京羽田", "same city"), ("ICN", "首爾", "2.5h flight")],
    "KIX": [("NRT", "東京", "shinkansen"), ("ICN", "首爾", "2h flight"), ("FUK", "福岡", "shinkansen")],
    "ICN": [("NRT", "東京", "2.5h flight"), ("KIX", "大阪", "2h flight"), ("PUS", "釜山", "KTX")],
    "BKK": [("SGN", "胡志明市", "1.5h flight"), ("KUL", "吉隆坡", "2h flight"), ("SIN", "新加坡", "2.5h flight")],
    "SIN": [("KUL", "吉隆坡", "1h flight"), ("BKK", "曼谷", "2.5h flight"), ("CGK", "雅加達", "2h flight")],
    # Americas
    "JFK": [("EWR", "紐瓦克", "same area"), ("BOS", "波士頓", "train"), ("IAD", "華盛頓", "train")],
    "LAX": [("SFO", "舊金山", "1h flight"), ("SAN", "聖地牙哥", "2h drive"), ("LAS", "拉斯維加斯", "1h flight")],
}

# Known cheap hub cities for split tickets (from Asia perspective)
# These are cities where connecting flights tend to be cheap
SPLIT_HUBS = {
    "asia_to_europe": [
        ("BKK", "曼谷"),
        ("KUL", "吉隆坡"),
        ("SIN", "新加坡"),
        ("DOH", "杜哈"),
        ("IST", "伊斯坦堡"),
        ("DXB", "杜拜"),
    ],
    "asia_to_americas": [
        ("NRT", "東京"),
        ("ICN", "首爾"),
        ("HKG", "香港"),
        ("TPE", "台北"),
    ],
    "americas_to_europe": [
        ("KEF", "雷克雅維克"),
        ("DUB", "都柏林"),
        ("LON", "倫敦"),
    ],
}


def detect_region(iata: str) -> str:
    asia = {"TPE", "NRT", "HND", "KIX", "ICN", "BKK", "SIN", "KUL", "HKG", "SGN", "CGK", "MNL", "PUS", "FUK", "DEL", "BOM", "PEK", "PVG", "CAN"}
    europe = {"ATH", "ROM", "FCO", "IST", "BCN", "MAD", "PAR", "CDG", "LON", "LHR", "AMS", "FRA", "MUC", "VIE", "PRG", "BUD", "SOF", "LIS", "MIL", "MXP", "VCE", "NAP", "ZRH", "BRU", "MRS", "AYT", "DUB", "KEF", "CPH", "ARN", "HEL", "OSL"}
    americas = {"JFK", "EWR", "LAX", "SFO", "ORD", "IAD", "BOS", "SAN", "LAS", "MIA", "ATL", "SEA", "YYZ", "YVR", "MEX", "GRU", "EZE", "SCL", "LIM", "BOG"}
    middle_east = {"DOH", "DXB", "AUH", "AMM", "TLV"}

    if iata in asia:
        return "asia"
    elif iata in europe:
        return "europe"
    elif iata in americas:
        return "americas"
    elif iata in middle_east:
        return "middle_east"
    return "unknown"


def get_split_hubs(origin: str, dest: str) -> list[tuple[str, str]]:
    """Get relevant hub cities for splitting a journey."""
    orig_region = detect_region(origin)
    dest_region = detect_region(dest)

    route_key = f"{orig_region}_to_{dest_region}"
    hubs = SPLIT_HUBS.get(route_key, [])

    # Filter out origin/dest themselves
    return [(code, name) for code, name in hubs if code != origin and code != dest]


def get_nearby(iata: str) -> list[tuple[str, str, str]]:
    """Get nearby airports for open-jaw."""
    return NEARBY_AIRPORTS.get(iata, [])


def generate_strategies(
    origin: str,
    dest: str,
    depart_date: str,
    return_date: str,
    cabin: int = 1,
    stops: int = 0,
    passengers: int = 1,
    hl: str = "zh-TW",
    curr: str = "TWD",
) -> list[dict]:
    """Generate all combo ticket strategies with URLs."""
    common = dict(passengers=passengers, cabin=cabin, stops=stops, hl=hl, curr=curr)
    strategies = []

    # === 1. Baseline: standard round-trip ===
    strategies.append({
        "type": "baseline",
        "name": "直接來回票",
        "desc": f"{origin}↔{dest}",
        "segments": [
            {"label": f"{origin}→{dest} 來回", "url": build_url(origin, dest, depart_date, return_date, **common)},
        ],
    })

    # === 2. Open Jaw: fly in to dest, return from nearby city ===
    # Google Flights does not support multi-city URLs via protobuf encoding
    # (the /search endpoint silently rewrites them to round-trip).
    # Instead, we split into separate one-way searches.
    for alt_iata, alt_name, transport in get_nearby(dest):
        # Leg 1: origin → dest (one-way outbound)
        outbound_url = build_url(origin, dest, depart_date, **common)
        # Leg 2: alt_city → origin (one-way return from nearby city)
        return_url = build_url(alt_iata, origin, return_date, **common)
        # Supplement: dest → alt (one-way transfer between cities)
        supplement_url = build_url(dest, alt_iata, return_date, **common)

        strategies.append({
            "type": "open_jaw",
            "name": f"Open Jaw 經{alt_name}",
            "desc": f"{origin}→{dest} ... {alt_iata}→{origin}，中段 {dest}→{alt_iata}（{transport}）自補",
            "segments": [
                {"label": f"去程 {origin}→{dest}（單程）", "url": outbound_url},
                {"label": f"回程 {alt_iata}→{origin}（單程）", "url": return_url},
                {"label": f"補票 {dest}→{alt_iata}（單程，需另選日期）", "url": supplement_url},
            ],
        })

    # === 3. Reverse Ticket: round-trip from dest + one-way to get there ===
    # The reverse round-trip: dest→origin→dest
    reverse_url = build_url(dest, origin, return_date, depart_date, **common)
    # One-way to get there: origin→dest
    oneway_url = build_url(origin, dest, depart_date, **common)

    strategies.append({
        "type": "reverse",
        "name": "反向票",
        "desc": f"反向來回 {dest}↔{origin} + 單程 {origin}→{dest}",
        "segments": [
            {"label": f"反向來回 {dest}→{origin}→{dest}", "url": reverse_url},
            {"label": f"補去程 {origin}→{dest}（單程）", "url": oneway_url},
        ],
    })

    # === 4. Split Ticket via hubs ===
    hubs = get_split_hubs(origin, dest)
    for hub_iata, hub_name in hubs:
        # Leg 1: origin → hub (one-way)
        leg1_url = build_url(origin, hub_iata, depart_date, **common)
        # Leg 2: hub → dest (one-way)
        leg2_url = build_url(hub_iata, dest, depart_date, **common)
        # Leg 3: dest → origin (one-way)
        leg3_url = build_url(dest, origin, return_date, **common)

        strategies.append({
            "type": "split",
            "name": f"拆票經{hub_name}",
            "desc": f"{origin}→{hub_iata}→{dest}（去程拆）+ {dest}→{origin}（回程）",
            "segments": [
                {"label": f"{origin}→{hub_iata}（單程，需選中轉日期）", "url": leg1_url},
                {"label": f"{hub_iata}→{dest}（單程，需選中轉日期）", "url": leg2_url},
                {"label": f"{dest}→{origin}（單程回程）", "url": leg3_url},
            ],
        })

    return strategies


def print_strategies(strategies: list[dict]) -> None:
    for i, s in enumerate(strategies, 1):
        print(f"\n{'='*60}")
        print(f"策略 {i}: [{s['type']}] {s['name']}")
        print(f"  {s['desc']}")
        for seg in s["segments"]:
            print(f"  📎 {seg['label']}")
            print(f"     {seg['url']}")


def main():
    parser = argparse.ArgumentParser(description="Generate combo ticket search strategies")
    parser.add_argument("origin", help="Origin IATA code (e.g. TPE)")
    parser.add_argument("dest", help="Destination IATA code (e.g. ATH)")
    parser.add_argument("depart", help="Departure date YYYY-MM-DD")
    parser.add_argument("return_date", help="Return date YYYY-MM-DD")
    parser.add_argument("--cabin", choices=["economy", "premium", "business", "first"], default="economy")
    parser.add_argument("--stops", type=int, default=0)
    parser.add_argument("--passengers", type=int, default=1)
    parser.add_argument("--hl", default="zh-TW")
    parser.add_argument("--curr", default="TWD")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--types", nargs="+", choices=["baseline", "open_jaw", "reverse", "split"],
                        help="Only generate specific strategy types")

    args = parser.parse_args()
    cabin = CABIN_MAP[args.cabin]

    strategies = generate_strategies(
        args.origin, args.dest, args.depart, args.return_date,
        cabin, args.stops, args.passengers, args.hl, args.curr,
    )

    if args.types:
        strategies = [s for s in strategies if s["type"] in args.types]

    if args.json:
        print(json.dumps(strategies, ensure_ascii=False, indent=2))
    else:
        print(f"🔍 {args.origin} → {args.dest}  {args.depart} ~ {args.return_date}  {args.cabin}")
        print(f"共生成 {len(strategies)} 個搜尋策略")
        print_strategies(strategies)


if __name__ == "__main__":
    main()
