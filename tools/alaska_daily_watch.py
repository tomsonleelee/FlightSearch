#!/usr/bin/env python3
"""Daily Alaska Atmos / Mileage Plan award sweep for the 2-pax business wishlist.

Wraps ``tools/alaska_award_watch.py`` (imported, not shelled out) and scans a
fixed route list day-by-day across every month touched by the next 330 days,
then writes one Traditional-Chinese Markdown report to ``results/``.

    python3 tools/alaska_daily_watch.py                       # full sweep
    python3 tools/alaska_daily_watch.py --routes BKK-FRA      # subset
    python3 tools/alaska_daily_watch.py --horizon-days 60     # shorter window

Read-only: it searches and reports. It never logs in, transfers points, or
books. Always re-check a hit on alaskaair.com before transferring or booking.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from alaska_award_watch import (  # noqa: E402  (path set above)
    AwardHit,
    ScanFailure,
    SearchRequest,
    duration_label,
    scan_request,
)

TAIPEI = dt.timezone(dt.timedelta(hours=8))

# Route wishlist, grouped exactly as requested so the report stays readable.
ROUTE_GROUPS: list[tuple[str, list[str]]] = [
    ("Finnair / oneworld 重點", ["NRT-HEL", "HND-HEL", "ICN-HEL"]),
    (
        "BKK 出發往歐洲",
        [
            "BKK-HEL", "BKK-LHR", "BKK-FRA", "BKK-MUC", "BKK-CDG", "BKK-AMS",
            "BKK-MAD", "BKK-MXP", "BKK-FCO", "BKK-ZRH", "BKK-VIE", "BKK-IST",
        ],
    ),
    ("TPE 出發長程", ["TPE-LHR", "TPE-CDG", "TPE-FRA", "TPE-AMS", "TPE-HEL", "TPE-DOH"]),
    (
        "鄰近機場出發歐洲",
        # HKG-HEL added 2026-08-01 at tomson's request. Alaska does sell awards
        # on this route (Finnair AY 100, economy from 30,000 pts, 4-8 days a
        # month) but a 2-pax business search returned 0 across all 330 days —
        # and so did a 1-pax business search on every day that had any award,
        # so Finnair appears to release no business space here at all. Kept on
        # the daily list anyway so the digest reports it the day that changes.
        ["HKG-LHR", "HKG-CDG", "HKG-FRA", "HKG-AMS", "HKG-MAD", "HKG-ZRH", "HKG-HEL", "KIX-HEL"],
    ),
    ("TPE 北美長程", ["TPE-SEA", "TPE-SFO", "TPE-LAX"]),
]

PASSENGERS = 2
CABIN = "business"
# 2, not 4, since 2026-08-02. tomson asked for a gentler scan that still has
# results on his phone by 08:00 — halving concurrency roughly doubles the
# sweep (4 workers did 16,957 requests in 100 minutes on 08-02), which is why
# the timer moved to 03:00 and gained margin rather than staying at 07:00.
# A hoped-for side benefit — that being gentler would let the shoulderDates
# pre-filter recover — did NOT materialise: at 2 workers it still failed
# 561/572 calls (98%), versus 556/572 at 4. The throttling is not a function of
# our request rate, so do not slow down further expecting it to help.
WORKERS = 2
MAX_POINTS_PER_PERSON = 75_000

# Second pass, added 2026-08-01, revised the same day. First cut derived it
# from the TPE-* prefix, which swept in London/Seattle/LAX — tomson wants
# economy only for SHORT-HAUL ASIA, no transoceanic legs, so the routes are
# now listed explicitly rather than inferred.
#
# Probing these on 2026-08-01 first produced the confident conclusion that only
# NRT and HND had any award space. That was WRONG: the calendar endpoint had
# started rate-limiting after a burst of manual probes, and the probe counted a
# failed call as "no availability" — NRT and HND simply happened to be first
# and second in the list, before the throttling kicked in. A real day-by-day
# search found most of these routes do have space, with TPE-HKG and TPE-BKK at
# 7,500 points/person. Failure is not absence; the pre-filter now reports its
# failed-call count for exactly this reason.
ECONOMY_CABIN = "economy"
ECONOMY_ROUTES = [
    # 日本
    "TPE-NRT", "TPE-HND", "TPE-KIX", "TPE-CTS", "TPE-FUK", "TPE-OKA",
    # 韓國
    "TPE-ICN", "TPE-PUS",
    # 港澳・東南亞
    "TPE-HKG", "TPE-BKK", "TPE-SIN", "TPE-KUL", "TPE-MNL",
    "TPE-SGN", "TPE-HAN", "TPE-DPS", "TPE-HKT",
    # 中國
    "TPE-PVG", "TPE-PEK", "TPE-CAN",
]
# Observed 2026-08-01: TPE-NRT 7,500/person on Starlux direct (+US$35), and
# TPE-HND 25,000 on a Philippine Airlines routing that backtracks via Manila.
# 30,000 keeps both plus headroom, and still excludes long-haul-priced noise.
ECONOMY_MAX_POINTS_PER_PERSON = 30_000
HORIZON_DAYS = 330

# Telegram delivery. The sweep used to only write a 1.7MB file into results/ —
# tomson never saw it (2026-08-01: "今天沒有看到啊"). A scheduled job nobody
# reads is the same as no job, so the digest now ships with the run.
TG_ENV = Path.home() / ".claude/channels/telegram/.env"
TG_ACCESS = Path.home() / ".claude/channels/telegram/access.json"
# Yesterday's per-route best, so the message can lead with what *changed*
# instead of repeating an identical wall of routes every morning.
STATE_PATH = REPO_ROOT / "data" / "alaska_award_state.json"


def group_of(route: str) -> str:
    for name, routes in ROUTE_GROUPS:
        if route in routes:
            return name
    return "其他"


def months_in_window(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    """Every (year, month) touched by [start, end]."""
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def dates_of_month(year: int, month: int, start: dt.date, end: dt.date) -> list[dt.date]:
    """Days of that month clipped to the horizon (first/last month are partial)."""
    day = dt.date(year, month, 1)
    out: list[dt.date] = []
    while day.month == month and day.year == year:
        if start <= day <= end:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def money(value: float) -> str:
    return f"US${value:,.2f}"


# --- Stage 1: cheap month-at-a-time pre-filter -------------------------------
#
# `POST /search/api/shoulderDates` answers one call with 31 days (target ±15),
# each carrying the *lowest* award points for that date — or null when the date
# has no award seat in any cabin. It carries no cabin dimension, so it cannot
# find business seats by itself; what it can do is tell us which dates are not
# worth a full per-day search at all.
#
# Safe direction: lowest-across-cabins is ≤ business, so a date with a business
# seat always reports a number. null therefore implies "no award at all", and
# skipping it cannot hide a business seat. Spot-checked 2026-07-31 both ways:
# BKK-FRA 10-20 (business 60k) reports 30k (kept); TPE-HEL 09-03 reports null
# and the full search finds nothing.
#
# OFF BY DEFAULT since 2026-08-04 (tomson: "預篩關掉"). The endpoint began
# rejecting almost everything and never recovered: 561 of 572 calls failed on
# 08-04, and halving our request rate the day before changed nothing (556/572),
# so the throttling is not ours to fix by backing off. What survived bought a
# 1.1% reduction in day-searches — not worth 572 doomed requests per sweep,
# especially when the whole point of the slower schedule is to be gentler.
# Pass --prefilter to try it again if Alaska ever loosens up; the logic itself
# was never wrong, only unusable.

SHOULDER_URL = "https://www.alaskaair.com/search/api/shoulderDates"
SHOULDER_SPAN = 31  # days returned per call
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def shoulder_payload(origin: str, destination: str, date: dt.date, passengers: int) -> str:
    return json.dumps(
        {
            "origins": [origin],
            "destinations": [destination],
            "dates": [date.isoformat()],
            "onba": False,
            "dnba": False,
            "numADTs": passengers,
            "numCHDs": 0,
            "isAddingToAdultRes": False,
            "sliceToSearch": 0,
            "sliceSelections": [],
            "selectedSegments": [],
            "fareView": "as_awards",
            "discount": {
                "code": "",
                "status": 0,
                "expirationDate": dt.datetime.now(dt.timezone.utc).isoformat(),
                "message": "",
                "memo": "",
                "type": 0,
                "searchContainsDiscountedFare": False,
                "campaignName": "",
                "campaignCode": "",
            },
            "isAlaska": False,
            "isWholeTripPricing": True,
        }
    )


def shoulder_dates(origin: str, destination: str, anchor: dt.date, passengers: int) -> dict[dt.date, int | None] | None:
    """Lowest award points per date around `anchor`; None when the call fails."""
    command = [
        "curl", "--silent", "--show-error", "--compressed", "--max-time", "45",
        "-X", "POST", SHOULDER_URL,
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {BROWSER_UA}",
        "-H", "Referer: https://www.alaskaair.com/search/results",
        "--data", shoulder_payload(origin, destination, anchor, passengers),
    ]
    done = subprocess.run(command, check=False, capture_output=True, text=True)
    if done.returncode:
        return None
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        return None
    out: dict[dt.date, int | None] = {}
    for entry in payload.get("shoulderDates") or []:
        try:
            day = dt.date.fromisoformat(entry["date"])
        except (KeyError, ValueError):
            continue
        out[day] = entry.get("awardPoints")
    return out or None


def prefilter_dates(
    routes: list[str], start: dt.date, end: dt.date, passengers: int
) -> tuple[dict[str, set[dt.date]], int, int, int]:
    """Per-route dates worth a full search, plus (calls, skipped, failed_calls).

    `failed_calls` matters: the endpoint rate-limits under heavy use, and a
    failure is NOT "no availability". On 2026-08-01 every call started failing
    after a burst of manual probing, and reading those failures as empty
    results produced a confidently wrong conclusion ("only NRT and HND have
    award space") that was the opposite of the truth. The sweep itself is
    unaffected — a failed call keeps every date — but the count has to surface
    so a silently degraded pre-filter is visible instead of looking like a
    quiet, well-behaved run.
    """
    anchors: list[dt.date] = []
    cursor = start + dt.timedelta(days=SHOULDER_SPAN // 2)
    while cursor - dt.timedelta(days=SHOULDER_SPAN // 2) <= end:
        anchors.append(cursor)
        cursor += dt.timedelta(days=SHOULDER_SPAN)

    keep: dict[str, set[dt.date]] = {r: set() for r in routes}
    calls = 0
    skipped = 0
    failed = 0
    jobs = [(r, a) for r in routes for a in anchors]

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {
            pool.submit(shoulder_dates, r.split("-", 1)[0], r.split("-", 1)[1], a, passengers): (r, a)
            for r, a in jobs
        }
        for fut in futures.as_completed(futs):
            route, anchor = futs[fut]
            calls += 1
            try:
                table = fut.result()
            except Exception:  # noqa: BLE001 — a failed pre-filter must not lose dates
                table = None
            if table is None:
                failed += 1
            window = [
                anchor + dt.timedelta(days=offset)
                for offset in range(-(SHOULDER_SPAN // 2), SHOULDER_SPAN // 2 + 1)
            ]
            for day in window:
                if not (start <= day <= end):
                    continue
                if table is None:
                    keep[route].add(day)  # fail open: never drop a date on error
                elif day not in table:
                    keep[route].add(day)  # unknown -> search it
                elif table[day] is None:
                    skipped += 1
                else:
                    keep[route].add(day)
    return keep, calls, skipped, failed


def scan(
    routes: list[str],
    start: dt.date,
    end: dt.date,
    max_points: int | None,
    candidates: dict[str, set[dt.date]] | None = None,
    cabin: str = CABIN,
) -> tuple[dict[tuple[str, str], list[AwardHit]], list[ScanFailure], int]:
    """Scan every route × date in the window. Failures never abort the sweep."""
    hits_by_route_month: dict[tuple[str, str], list[AwardHit]] = defaultdict(list)
    failures: list[ScanFailure] = []
    requests: list[SearchRequest] = []

    for route in routes:
        origin, destination = route.split("-", 1)
        allowed = candidates.get(route) if candidates is not None else None
        for year, month in months_in_window(start, end):
            for day in dates_of_month(year, month, start, end):
                if allowed is not None and day not in allowed:
                    continue
                requests.append(SearchRequest(origin, destination, day))

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {
            pool.submit(scan_request, req, PASSENGERS, cabin, max_points, None): req
            for req in requests
        }
        for fut in futures.as_completed(futs):
            req = futs[fut]
            try:
                hits, failure = fut.result()
            except Exception as error:  # noqa: BLE001 — one bad date must not kill the run
                failures.append(ScanFailure(req, f"unexpected: {error}"))
                continue
            if failure is not None:
                failures.append(failure)
                continue
            key = (f"{req.origin}-{req.destination}", req.date.strftime("%Y-%m"))
            hits_by_route_month[key].extend(hits)

    return hits_by_route_month, failures, len(requests)


def build_report(
    hits_by_route_month: dict[tuple[str, str], list[AwardHit]],
    failures: list[ScanFailure],
    routes: list[str],
    start: dt.date,
    end: dt.date,
    total_requests: int,
    max_points: int | None,
    prefilter: tuple[int, int, int] | None = None,
    cabin: str = CABIN,
) -> str:
    now = dt.datetime.now(TAIPEI)
    total_hits = sum(len(v) for v in hits_by_route_month.values())
    cap = f"每人 ≤ {max_points:,} 點" if max_points else "無點數上限"
    cabin_label = "經濟艙" if cabin == ECONOMY_CABIN else "商務艙"

    lines = [
        "# Alaska Atmos Rewards／Mileage Plan 每日獎勵票掃描",
        "",
        f"- 搜尋時間：{now:%Y-%m-%d %H:%M} (Asia/Taipei)",
        f"- 查詢日期範圍：{start:%Y-%m-%d} ～ {end:%Y-%m-%d}（未來 {(end - start).days} 天）",
        f"- 條件：{PASSENGERS} 人・{cabin_label}・{cap}・並行 {WORKERS}",
        f"- 掃描航線：{len(routes)} 條／逐日查詢次數：{total_requests:,}",
        f"- 命中總數：{total_hits}",
    ]
    if prefilter is not None:
        calls, skipped, failed = prefilter
        saved = skipped + total_requests
        pct = (skipped / saved * 100) if saved else 0.0
        lines.append(
            f"- 預篩（月曆端點）：{calls:,} 次呼叫，刷掉 {skipped:,} 個「完全無獎勵位」的日期"
            f"（省下 {pct:.0f}% 逐日查詢）"
        )
        if failed:
            lines.append(
                f"- ⚠️ 預篩端點 {failed:,}/{calls:,} 次呼叫失敗（多半是被限流）。"
                "失敗一律當「可能有位」處理、該航線全日期照樣逐日查，**結果不會漏**，"
                "只是省不到時間。**失敗 ≠ 沒有獎勵位**。"
            )
    lines += [
        "",
        "> ⚠️ 本報告僅為查詢結果，未登入、未轉點、未訂位。"
        "命中結果請務必回 alaskaair.com 人工確認後再轉點或訂票。",
        "",
    ]

    if total_hits == 0:
        lines += ["## 結果", "", "本次掃描所有航線／月份**皆未找到**符合條件的商務艙獎勵票。", ""]

    for group_name, group_routes in ROUTE_GROUPS:
        active = [r for r in group_routes if r in routes]
        if not active:
            continue
        lines += [f"## {group_name}", ""]
        for route in active:
            lines += [f"### {route}", ""]
            months = [m for (r, m) in hits_by_route_month if r == route]
            any_month = False
            for year, month in months_in_window(start, end):
                label = f"{year:04d}-{month:02d}"
                hits = sorted(
                    hits_by_route_month.get((route, label), []),
                    key=lambda h: (h.points_per_person, h.request.date),
                )
                if not hits:
                    lines.append(f"- **{label}**：未找到符合條件的結果")
                    continue
                any_month = True
                best = hits[0]
                lines += [
                    f"- **{label}**：{len(hits)} 筆　"
                    f"🏆 最低 {best.points_per_person:,} 點/人 @ {best.request.date:%m-%d}",
                    "",
                    "| 日期 | 航班 | 航段/航空 | 飛行時間 | 點數/人 | 現金/人 | 席位 | 連結 |",
                    "|---|---|---|---:|---:|---:|---:|---|",
                ]
                for h in hits:
                    star = "🏆 " if h is best else ""
                    lines.append(
                        f"| {star}{h.request.date:%Y-%m-%d} | {h.flights} | {h.carriers} | "
                        f"{duration_label(h.duration_minutes)} | {h.points_per_person:,} | "
                        f"{money(h.cash_per_person)} | {h.seats_remaining} | "
                        f"[查詢]({h.url}) |"
                    )
                lines.append("")
            if not any_month and months:
                lines.append("")
        lines.append("")

    lines += ["## 失敗清單", ""]
    if not failures:
        lines.append("本次無查詢失敗。")
    else:
        lines += [
            f"共 {len(failures)} 筆查詢失敗（其餘航線不受影響）：",
            "",
            "| 航線 | 日期 | 錯誤 |",
            "|---|---|---|",
        ]
        for f in sorted(failures, key=lambda x: (x.request.origin, x.request.date)):
            msg = f.message.replace("|", "/")[:120]
            lines.append(
                f"| {f.request.origin}-{f.request.destination} | {f.request.date:%Y-%m-%d} | {msg} |"
            )
    lines.append("")
    return "\n".join(lines)


def route_stats(
    hits_by_route_month: dict[tuple[str, str], list[AwardHit]],
) -> dict[str, dict]:
    """route -> cheapest per-person points, earliest date at that price, month spread."""
    stats: dict[str, dict] = {}
    for (route, _month), hits in hits_by_route_month.items():
        for hit in hits:
            cur = stats.setdefault(route, {"points": None, "date": None, "months": set()})
            cur["months"].add(hit.request.date.strftime("%Y-%m"))
            if cur["points"] is None or hit.points_per_person < cur["points"]:
                cur["points"] = hit.points_per_person
                cur["date"] = hit.request.date.isoformat()
            elif hit.points_per_person == cur["points"] and hit.request.date.isoformat() < cur["date"]:
                cur["date"] = hit.request.date.isoformat()
    return {r: {**v, "months": sorted(v["months"])} for r, v in stats.items()}


def load_prev_state() -> dict[str, dict[str, dict]]:
    """{"business": {route: ...}, "economy": {...}} — older files were flat."""
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt state file must not stop the sweep
        return {}
    return {
        # "routes" is the pre-2026-08-01 key and only ever held business.
        "business": raw.get("business") or raw.get("routes") or {},
        "economy": raw.get("economy") or {},
    }


def save_state(by_cabin: dict[str, dict[str, dict]], when: dt.date) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": when.isoformat(),
        "business": by_cabin.get("business", {}),
        "economy": by_cabin.get("economy", {}),
        # Kept so an older build of this script can still read the baseline.
        "routes": by_cabin.get("business", {}),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def digest_section(
    label: str,
    stats: dict[str, dict],
    prev: dict[str, dict],
    routes: list[str],
) -> list[str]:
    """One cabin's block: what changed first, then the current tiers."""
    out = [f"\n<b>【{label}】</b>{len(stats)}/{len(routes)} 條有位"]

    gained = [r for r in stats if r not in prev]
    lost = [r for r in prev if r not in stats]
    cheaper = [
        r for r in stats
        if r in prev and prev[r].get("points") and stats[r]["points"] < prev[r]["points"]
    ]
    if prev:
        if gained or lost or cheaper:
            out.append("🔔 <b>與上次相比的變化</b>")
            for r in gained:
                out.append(f"➕ 新出現 {r} — {stats[r]['points']:,} 點/人（{stats[r]['date']}）")
            for r in cheaper:
                out.append(f"📉 變便宜 {r} — {prev[r]['points']:,} → {stats[r]['points']:,} 點/人")
            for r in lost:
                out.append(f"➖ 消失 {r}（上次 {prev[r].get('points') or '?':,} 點/人）")
        else:
            out.append("✅ 與上次相同")

    tiers: dict[int, list[str]] = defaultdict(list)
    for route, info in stats.items():
        tiers[info["points"]].append(route)
    for points in sorted(tiers):
        marker = "🏆 " if points == min(tiers) else ""
        out.append(f"\n{marker}<b>{points:,} 點/人</b>")
        for route in sorted(tiers[points], key=lambda r: stats[r]["date"]):
            info = stats[route]
            out.append(f"• {route} — 最早 {info['date']}、{len(info['months'])} 個月有位")

    empty = [r for r in routes if r not in stats]
    if empty:
        shown = "、".join(empty[:6]) + ("…" if len(empty) > 6 else "")
        out.append(f"\n❌ 無位：{len(empty)} 條（{shown}）")
    return out


def build_digest(
    passes: list[dict],
    total_requests: int,
    hit_count: int,
    failures: list[ScanFailure],
    prefilter: tuple[int, int, int] | None,
    report_path: Path,
) -> str:
    now = dt.datetime.now(TAIPEI)
    warn = f"／⚠️{len(failures)} 失敗" if failures else "／0 失敗"
    pre = f"／預篩省 {prefilter[1]:,} 日" if prefilter else ""
    if prefilter and prefilter[2]:
        pre += f"／⚠️預篩端點 {prefilter[2]}/{prefilter[0]} 次失敗（已全掃、無漏）"
    out = [
        f"✈️ Alaska 獎勵票每日掃描 {now:%m/%d}",
        f"{total_requests:,} 次查詢／{hit_count:,} 命中{warn}{pre}",
    ]
    for spec in passes:
        out += digest_section(spec["label"], spec["stats"], spec["prev"], spec["routes"])

    out.append(f"\n完整明細：{report_path.name}")
    out.append("⚠️ 僅為查詢結果，請回 alaskaair.com 人工確認後再轉點或訂票。")
    return "\n".join(out)


def send_telegram(text: str) -> bool:
    import re

    token = ""
    if TG_ENV.exists():
        m = re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$", TG_ENV.read_text(), re.M)
        token = m.group(1).strip() if m else ""
    chat = ""
    if TG_ACCESS.exists():
        try:
            allow = json.loads(TG_ACCESS.read_text()).get("allowFrom") or []
            chat = str(allow[0]) if allow else ""
        except Exception:  # noqa: BLE001
            chat = ""
    if not token or not chat:
        print("[alaska-daily] Telegram token/chat 缺失，未送出", file=sys.stderr)
        return False
    done = subprocess.run(
        ["curl", "-sS", "-m", "30", "-X", "POST",
         f"https://api.telegram.org/bot{token}/sendMessage",
         "-d", f"chat_id={chat}", "-d", "parse_mode=HTML",
         "--data-urlencode", f"text={text}"],
        check=False, capture_output=True, text=True,
    )
    ok = done.returncode == 0 and '"ok":true' in done.stdout
    print(f"[alaska-daily] Telegram {'sent' if ok else 'FAILED: ' + done.stdout[:200]}")
    return ok


def run_pass(
    routes: list[str],
    start: dt.date,
    end: dt.date,
    max_points: int | None,
    cabin: str,
    use_prefilter: bool,
) -> tuple[dict, list[ScanFailure], int, tuple[int, int, int] | None, dict[str, dict]]:
    """One cabin's sweep. Returns (hits, failures, requests, prefilter, stats)."""
    prefilter_stats: tuple[int, int, int] | None = None
    candidates: dict[str, set[dt.date]] | None = None
    if use_prefilter:
        candidates, calls, skipped, failed = prefilter_dates(routes, start, end, PASSENGERS)
        prefilter_stats = (calls, skipped, failed)
    hits, failures, total = scan(routes, start, end, max_points, candidates, cabin)
    return hits, failures, total, prefilter_stats, route_stats(hits)


def main() -> int:
    global WORKERS  # --workers rebinds the module constant the scanners read
    parser = argparse.ArgumentParser(description="Daily Alaska award sweep (2 pax)")
    parser.add_argument("--routes", help="Comma-separated subset, default = full wishlist")
    parser.add_argument("--horizon-days", type=int, default=HORIZON_DAYS)
    parser.add_argument(
        "--max-points",
        type=int,
        default=MAX_POINTS_PER_PERSON,
        help="Per-person cap for the business pass; 0 disables the filter",
    )
    parser.add_argument(
        "--economy-max-points",
        type=int,
        default=ECONOMY_MAX_POINTS_PER_PERSON,
        help="Per-person cap for the TPE economy pass; 0 disables the filter",
    )
    parser.add_argument("--output", help="Report path; default results/<date>_alaska_award_watch.md")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip the Telegram digest (use for manual/subset test runs)",
    )
    parser.add_argument(
        "--prefilter",
        action="store_true",
        help="Re-enable the shoulderDates pre-filter (off since 2026-08-04; "
             "it was failing 98% of calls and saving only ~1% of searches)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=WORKERS,
        help=f"Parallel requests (default {WORKERS}); raise only for one-off manual runs",
    )
    parser.add_argument(
        "--skip-economy",
        action="store_true",
        help="Run only the business pass",
    )
    parser.add_argument(
        "--cabin",
        choices=["business", "economy"],
        help="Force a single cabin for a --routes run (default: business)",
    )
    args = parser.parse_args()
    WORKERS = max(1, args.workers)

    subset = bool(args.routes)
    routes = (
        [r.strip().upper() for r in args.routes.split(",") if r.strip()]
        if subset
        else [r for _, group in ROUTE_GROUPS for r in group]
    )
    today = dt.datetime.now(TAIPEI).date()
    start, end = today, today + dt.timedelta(days=args.horizon_days)
    use_prefilter = args.prefilter

    # A --routes run is a targeted manual check: honour --cabin and do one pass.
    # The scheduled full sweep always does both, because that is what the daily
    # report promises.
    if subset:
        cabin = args.cabin or CABIN
        cap = args.economy_max_points if cabin == ECONOMY_CABIN else args.max_points
        specs = [(cabin, routes, cap if cap > 0 else None)]
    else:
        econ_routes = list(ECONOMY_ROUTES)
        specs = [(CABIN, routes, args.max_points if args.max_points > 0 else None)]
        if not args.skip_economy and econ_routes:
            specs.append(
                (ECONOMY_CABIN, econ_routes,
                 args.economy_max_points if args.economy_max_points > 0 else None)
            )

    sections: list[str] = []
    passes: list[dict] = []
    total_requests = 0
    total_hits = 0
    all_failures: list[ScanFailure] = []
    first_prefilter: tuple[int, int, int] | None = None
    stats_by_cabin: dict[str, dict[str, dict]] = {}

    for cabin, pass_routes, cap in specs:
        hits, failures, total, prefilter_stats, stats = run_pass(
            pass_routes, start, end, cap, cabin, use_prefilter
        )
        # Only the scheduled economy pass is TPE-only; a --routes run can force
        # economy on anything, and mislabelling it "TPE 出發" would be a lie.
        scope = "・亞洲短程" if cabin == ECONOMY_CABIN and not subset else ""
        label = f"{'經濟艙' if cabin == ECONOMY_CABIN else '商務艙'} {PASSENGERS} 人{scope}"
        sections.append(
            build_report(hits, failures, pass_routes, start, end, total, cap,
                         prefilter_stats, cabin)
        )
        passes.append({"label": label, "stats": stats, "routes": pass_routes, "prev": {}})
        stats_by_cabin[cabin] = stats
        total_requests += total
        total_hits += sum(len(v) for v in hits.values())
        all_failures += failures
        if first_prefilter is None:
            first_prefilter = prefilter_stats
        elif prefilter_stats:
            first_prefilter = (first_prefilter[0] + prefilter_stats[0],
                               first_prefilter[1] + prefilter_stats[1],
                               first_prefilter[2] + prefilter_stats[2])

    out = Path(args.output) if args.output else REPO_ROOT / "results" / f"{today:%Y-%m-%d}_alaska_award_watch.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n---\n\n".join(sections), encoding="utf-8")

    pre = f"／預篩省 {first_prefilter[1]:,} 日" if first_prefilter else ""
    if first_prefilter and first_prefilter[2]:
        pre += f"／預篩失敗 {first_prefilter[2]}/{first_prefilter[0]}"
    print(
        f"[alaska-daily] {out} — {total_requests:,} 次逐日查詢／{total_hits} 命中"
        f"／{len(all_failures)} 失敗{pre}"
    )

    # A partial sweep must never become the diff baseline: routes it did not
    # scan would show up as "disappeared" tomorrow.
    full_sweep = not subset and args.horizon_days == HORIZON_DAYS
    prev = load_prev_state() if full_sweep else {}
    for spec, (cabin, _r, _c) in zip(passes, specs):
        spec["prev"] = prev.get(cabin, {})
    # Always compose it, so --no-notify can show exactly what would have been
    # sent. A digest you can only see by sending it is a digest you test on
    # tomson.
    digest = build_digest(passes, total_requests, total_hits, all_failures,
                          first_prefilter, out)
    if args.no_notify:
        print("--- 摘要（--no-notify，未送出）---")
        print(digest.replace("<b>", "").replace("</b>", ""))
    else:
        send_telegram(digest)
    if full_sweep:
        save_state(stats_by_cabin, today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
