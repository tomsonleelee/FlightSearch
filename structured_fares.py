"""Structured fare storage: normalized SQLite tables alongside ``bug_fares``.

Split the opaque ``parsed_json`` blob into queryable, typed tables without
losing provenance. Backwards compatible: ``bug_fares`` rows are never
modified, only linked to via foreign keys.

Public API:
    migrate(conn)         -- idempotent transactional schema + backfill
    insert_fare(conn, ...) -- atomic old+new row insert with rollback
    query(conn, ...)       -- structured filters
    build_prompt()         -- future-LLM extraction prompt

All JSON shapes are tolerated: dict / list / str / number / bool / null.
Unparseable blobs are tracked with explicit ``non_fare`` / ``malformed_json``
parse states so we never silently drop data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
PARSE_STATES = (
    "fare",            # parsed_json.is_fare == true
    "non_fare",        # parsed_json.is_fare == false (LLM verdict)
    "malformed_json",  # parsed_json was not valid JSON / not a dict
    "missing_json",    # parsed_json was null/empty in source
)
VERIFICATION_STATE = "unverified"

DDL = [
    # fares: one row per source record (FK to bug_fares, never overwrite raw)
    """
    CREATE TABLE IF NOT EXISTS fares (
        source_hash TEXT PRIMARY KEY REFERENCES bug_fares(dedup_hash),
        parse_state TEXT NOT NULL,
        parse_version INTEGER NOT NULL,
        summary TEXT,
        airline TEXT,
        cabin TEXT,
        trip_type TEXT,
        booking_deadline TEXT,
        travel_date_start TEXT,
        travel_date_end TEXT,
        raw_dates TEXT,
        verification_state TEXT NOT NULL DEFAULT 'unverified',
        inserted_at TEXT NOT NULL
    )""",
    # route legs: ordered IATA-shaped codes only. Ambiguous alternatives NOT
    # stored as serial legs — they belong in raw_description.
    """
    CREATE TABLE IF NOT EXISTS fare_route_legs (
        fare_source_hash TEXT NOT NULL REFERENCES fares(source_hash),
        leg_order INTEGER NOT NULL,
        airport_code TEXT,
        location_name TEXT,
        is_origin INTEGER NOT NULL,
        is_destination INTEGER NOT NULL,
        PRIMARY KEY (fare_source_hash, leg_order)
    )""",
    # prices: keep raw string + parsed numeric + ISO currency (only when
    # explicit/unambiguous). Approximate TWD lives in its own column —
    # never conflated with the original currency.
    """
    CREATE TABLE IF NOT EXISTS fare_prices (
        fare_source_hash TEXT PRIMARY KEY REFERENCES fares(source_hash),
        original_text TEXT,
        original_currency TEXT,
        original_amount REAL,
        approximate_twd INTEGER
    )""",
    # conditions: nullable bag of soft fields.
    """
    CREATE TABLE IF NOT EXISTS fare_conditions (
        fare_source_hash TEXT PRIMARY KEY REFERENCES fares(source_hash),
        bag_included INTEGER,
        refundable TEXT,
        notes TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fares_airline ON fares(airline)",
    "CREATE INDEX IF NOT EXISTS idx_fares_trip_type ON fares(trip_type)",
    "CREATE INDEX IF NOT EXISTS idx_fares_deadline ON fares(booking_deadline)",
    "CREATE INDEX IF NOT EXISTS idx_legs_airport ON fare_route_legs(airport_code)",
    "CREATE INDEX IF NOT EXISTS idx_prices_currency ON fare_prices(original_currency)",
]

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IATA_CODE_RE = re.compile(r"^[A-Z]{3}$")
_CURRENCY_TOKENS = {"USD", "EUR", "JPY", "TWD", "GBP", "AUD", "CAD", "HKD", "SGD", "KRW", "CNY", "NT$"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(raw: str | None) -> tuple[Any, str]:
    """Return (parsed, parse_state) for a raw parsed_json column value.

    Tolerates every JSON shape; never raises.
    """
    if raw is None or raw == "":
        return None, "missing_json"
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None, "malformed_json"
    return v, ""


def _safe_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v)


def _iso_date(v: Any) -> str | None:
    """Return ISO yyyy-mm-dd iff unambiguous. Free-text or partial → None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if ISO_DATE_RE.match(s):
        # Cheap sanity: month/day within range.
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        return s
    return None


def _parse_price(parsed: dict) -> tuple[str | None, str | None, float | None, int | None]:
    """Extract (original_text, currency, amount, approximate_twd) without
    guessing. ``$`` without currency code is NOT USD — left in original_text
    with currency=None so callers don't silently treat it as USD."""
    orig = _safe_str(parsed.get("price_original"))
    twd = parsed.get("price_twd")
    if isinstance(twd, bool) or not isinstance(twd, (int, float)):
        twd_int: int | None = None
    else:
        twd_int = int(twd)
    if orig is None:
        return None, None, None, twd_int

    text = orig
    # Currency hint scan (NT$ handled separately because of the symbol).
    cur: str | None = None
    if "NT$" in text or "TWD" in text.upper():
        cur = "TWD"
    else:
        for tok in ("USD", "EUR", "JPY", "GBP", "AUD", "CAD", "HKD", "SGD", "KRW", "CNY"):
            if tok in text.upper():
                cur = tok
                break
    amt: float | None = None
    # Numeric: tolerate commas + thousands separators. Strip currency words.
    num = re.search(r"([0-9][0-9.,]*)", text)
    if num:
        raw_num = num.group(1).replace(",", "")
        try:
            amt = float(raw_num)
        except ValueError:
            amt = None
    explicit_currency = parsed.get("price_original_currency")
    explicit_amount = parsed.get("price_original_amount")
    if isinstance(explicit_currency, str) and explicit_currency.upper() in _CURRENCY_TOKENS:
        cur = explicit_currency.upper()
    if isinstance(explicit_amount, (int, float)) and not isinstance(explicit_amount, bool) and 0 <= explicit_amount < float("inf"):
        amt = float(explicit_amount)
    return orig, cur, amt, twd_int


def _airport_like(token: str) -> bool:
    """Shape-validate ONLY. Does not claim the token is a real airport."""
    if not isinstance(token, str):
        return False
    return bool(IATA_CODE_RE.match(token.strip().upper()))


def _parse_route(parsed: dict) -> list[dict]:
    """Return ordered legs from route string.

    Format: 'A->B' or 'A->B->C'. Alternatives like 'A/B->C' or comma lists
    are AMBIGUOUS — caller records raw_dates for them, we do NOT create
    serial legs. Tokens that don't look like 3-letter IATA codes are stored
    as location_name, not airport_code.
    """
    raw = _safe_str(parsed.get("route"))
    if raw is None:
        return []
    # Ambiguity markers: '/', ',', ' or ', ' and ', '|'
    if any(sep in raw for sep in ("/", ",", " or ", " and ", "|", "（", "(")):
        return []
    tokens = [t.strip() for t in raw.split("->") if t.strip()]
    legs: list[dict] = []
    for idx, tok in enumerate(tokens):
        up = tok.upper()
        if _airport_like(up):
            legs.append({
                "leg_order": idx,
                "airport_code": up,
                "location_name": None,
                "is_origin": 1 if idx == 0 else 0,
                "is_destination": 1 if idx == len(tokens) - 1 else 0,
            })
        else:
            legs.append({
                "leg_order": idx,
                "airport_code": None,
                "location_name": tok,
                "is_origin": 1 if idx == 0 else 0,
                "is_destination": 1 if idx == len(tokens) - 1 else 0,
            })
    return legs


def _classify(parsed: Any) -> str:
    """Map a loaded-JSON value to a final parse_state."""
    if parsed is None:
        return "missing_json"
    if isinstance(parsed, dict):
        if parsed.get("is_fare") is True:
            return "fare"
        if parsed.get("is_fare") is False:
            return "non_fare"
        # dict but no explicit verdict → store as fare with nulls.
        return "fare"
    # lists/scalars → not a fare.
    return "non_fare"


def _build_fare_row(source_hash: str, parsed: Any) -> dict:
    """Materialise fare row dict from parsed payload."""
    p = parsed if isinstance(parsed, dict) else {}
    bd = _iso_date(p.get("booking_deadline", p.get("deadline")))
    raw_dates = _safe_str(p.get("dates_free_text", p.get("dates")))
    travel_start = travel_end = None
    # travel_range is the explicit form we expect from the new prompt; for
    # legacy free-text we deliberately leave dates null.
    tr = p.get("travel_range")
    if isinstance(tr, dict):
        travel_start = _iso_date(tr.get("start"))
        travel_end = _iso_date(tr.get("end"))
    elif isinstance(tr, (list, tuple)) and len(tr) == 2:
        travel_start = _iso_date(tr[0])
        travel_end = _iso_date(tr[1])

    return {
        "source_hash": source_hash,
        "parse_state": _classify(parsed),
        "parse_version": SCHEMA_VERSION,
        "summary": _safe_str(p.get("summary_zh")),
        "airline": _safe_str(p.get("airline")),
        "cabin": _safe_str(p.get("cabin")),
        "trip_type": _safe_str(p.get("trip_type")),
        "booking_deadline": bd,
        "travel_date_start": travel_start,
        "travel_date_end": travel_end,
        "raw_dates": raw_dates,
        "verification_state": VERIFICATION_STATE,
        "inserted_at": _now(),
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in DDL:
        conn.execute(stmt)


def migrate(conn: sqlite3.Connection) -> int:
    """Idempotent schema creation + backfill from bug_fares. Returns row count
    inserted into fares (0 if already migrated). Wrapped in a single
    transaction; safe to call repeatedly."""
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        _ensure_schema(conn)
        rows = cur.execute(
            "SELECT b.dedup_hash, b.parsed_json FROM bug_fares b "
            "WHERE NOT EXISTS (SELECT 1 FROM fares f WHERE f.source_hash=b.dedup_hash)"
        ).fetchall()
        inserted = 0
        for source_hash, raw_json in rows:
            parsed, malformed_state = _load_json(raw_json)
            state = malformed_state or _classify(parsed)
            fare_row = _build_fare_row(source_hash, parsed if parsed is not None else {})
            fare_row["parse_state"] = state
            cur.execute(
                """INSERT INTO fares
                   (source_hash, parse_state, parse_version, summary, airline,
                    cabin, trip_type, booking_deadline, travel_date_start,
                    travel_date_end, raw_dates, verification_state, inserted_at)
                   VALUES (:source_hash,:parse_state,:parse_version,:summary,
                           :airline,:cabin,:trip_type,:booking_deadline,
                           :travel_date_start,:travel_date_end,:raw_dates,
                           :verification_state,:inserted_at)""",
                fare_row,
            )
            if state == "fare":
                legs = _parse_route(parsed if isinstance(parsed, dict) else {})
                for leg in legs:
                    cur.execute(
                        """INSERT INTO fare_route_legs
                           (fare_source_hash, leg_order, airport_code,
                            location_name, is_origin, is_destination)
                           VALUES (?,?,?,?,?,?)""",
                        (source_hash, leg["leg_order"], leg["airport_code"],
                         leg["location_name"], leg["is_origin"],
                         leg["is_destination"]),
                    )
                orig, cur_code, amt, twd = _parse_price(parsed if isinstance(parsed, dict) else {})
                cur.execute(
                    """INSERT INTO fare_prices
                       (fare_source_hash, original_text, original_currency,
                        original_amount, approximate_twd)
                       VALUES (?,?,?,?,?)""",
                    (source_hash, orig, cur_code, amt, twd),
                )
            inserted += 1
        cur.execute("COMMIT")
        return inserted
    except Exception:
        cur.execute("ROLLBACK")
        raise


def insert_fare(
    conn: sqlite3.Connection,
    *,
    dedup_hash: str,
    source: str,
    item_guid: str | None,
    pub_date: str | None,
    raw_title: str,
    raw_description: str,
    raw_image_urls: list[str],
    parsed: dict | None,
    alerted: bool,
) -> None:
    """Atomic old + new insert. Rolls back BOTH tables on failure so bug_fares
    never gets a row that lacks its structured counterpart.

    We re-implement the bug_fares INSERT here (instead of calling
    fare_aggregator.insert_fare) because the original commits immediately,
    which would defeat the transaction. The aggregator's own call path is
    untouched and continues to work as the production hard-warning code path.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        _ensure_schema(conn)
        cur.execute(
            """INSERT OR IGNORE INTO bug_fares
               (dedup_hash, source, item_guid, pub_date, raw_title, raw_description,
                raw_image_urls, parsed_json, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_hash, source, item_guid, pub_date, raw_title,
                raw_description[:4000],
                json.dumps(raw_image_urls),
                json.dumps(parsed) if parsed else None,
                now if alerted else None,
            ),
        )
        state = _classify(parsed)
        fare_row = _build_fare_row(dedup_hash, parsed if parsed is not None else {})
        fare_row["parse_state"] = state
        cur.execute(
            """INSERT OR REPLACE INTO fares
               (source_hash, parse_state, parse_version, summary, airline,
                cabin, trip_type, booking_deadline, travel_date_start,
                travel_date_end, raw_dates, verification_state, inserted_at)
               VALUES (:source_hash,:parse_state,:parse_version,:summary,
                       :airline,:cabin,:trip_type,:booking_deadline,
                       :travel_date_start,:travel_date_end,:raw_dates,
                       :verification_state,:inserted_at)""",
            fare_row,
        )
        cur.execute("DELETE FROM fare_route_legs WHERE fare_source_hash=?", (dedup_hash,))
        cur.execute("DELETE FROM fare_prices WHERE fare_source_hash=?", (dedup_hash,))
        if state == "fare":
            for leg in _parse_route(parsed if isinstance(parsed, dict) else {}):
                cur.execute(
                    """INSERT INTO fare_route_legs
                       (fare_source_hash, leg_order, airport_code,
                        location_name, is_origin, is_destination)
                       VALUES (?,?,?,?,?,?)""",
                    (dedup_hash, leg["leg_order"], leg["airport_code"],
                     leg["location_name"], leg["is_origin"],
                     leg["is_destination"]),
                )
            orig, cur_code, amt, twd = _parse_price(parsed if isinstance(parsed, dict) else {})
            cur.execute(
                """INSERT INTO fare_prices
                   (fare_source_hash, original_text, original_currency,
                    original_amount, approximate_twd)
                   VALUES (?,?,?,?,?)""",
                (dedup_hash, orig, cur_code, amt, twd),
            )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def query(
    conn: sqlite3.Connection,
    *,
    origin: str | None = None,
    destination: str | None = None,
    cabin: str | None = None,
    currency: str | None = None,
    max_price: float | None = None,
) -> list[dict]:
    """Read-only filter. Refuses to mix currencies — passing currency=USD and
    filtering origin/destination only returns USD-priced fares (no fallback to
    approximate_twd).
    """
    sql = [
        "SELECT f.source_hash, f.summary, f.airline, f.cabin, f.trip_type,"
        " f.booking_deadline, f.travel_date_start, f.travel_date_end,"
        " (SELECT airport_code FROM fare_route_legs WHERE fare_source_hash=f.source_hash"
        "   AND is_origin=1) AS origin_code,"
        " (SELECT airport_code FROM fare_route_legs WHERE fare_source_hash=f.source_hash"
        "   AND is_destination=1) AS destination_code,"
        " p.original_text, p.original_currency, p.original_amount,"
        " p.approximate_twd FROM fares f"
        " LEFT JOIN fare_prices p ON p.fare_source_hash = f.source_hash"
        " WHERE f.parse_state = 'fare'"
    ]
    args: list[Any] = []
    if origin:
        sql.append("""AND EXISTS (
            SELECT 1 FROM fare_route_legs l
            WHERE l.fare_source_hash = f.source_hash
              AND l.is_origin = 1 AND l.airport_code = ?)""")
        args.append(origin.upper())
    if destination:
        sql.append("""AND EXISTS (
            SELECT 1 FROM fare_route_legs l
            WHERE l.fare_source_hash = f.source_hash
              AND l.is_destination = 1 AND l.airport_code = ?)""")
        args.append(destination.upper())
    if cabin:
        sql.append("AND f.cabin = ?")
        args.append(cabin)
    if currency:
        sql.append("AND p.original_currency = ?")
        args.append(currency)
    if max_price is not None:
        sql.append("AND p.original_amount IS NOT NULL AND p.original_amount <= ?")
        args.append(max_price)
    cur = conn.cursor()
    cur.execute(" ".join(sql), args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_prompt() -> str:
    """Future-LLM extraction prompt: explicit structured fields. Backwards
    compatible — code accepts the legacy fields below too."""
    return """You are extracting a BUG fare / mistake-fare JSON object (no prose).

Required fields (use null when unknown, never empty string):
  is_fare: bool                                 — true if this is a flight deal
  summary_zh: str (<=80 chars)                  — 繁體中文 one-liner
  airline: str | null                           — carrier name
  cabin: "economy" | "premium_economy" | "business" | "first" | null
  trip_type: "oneway" | "roundtrip" | "multi_city" | null
  booking_deadline: "YYYY-MM-DD" | null         — ISO ONLY if unambiguous
  travel_range: { start: "YYYY-MM-DD" | null, end: "YYYY-MM-DD" | null } | null
  route: "ORIG->DEST" | "ORIG->DEST->DEST2"     — IATA codes if visible,
                                                  city names otherwise.
                                                  DO NOT split alternatives
                                                  like 'LAX/SFO' into legs.
  price_original: str | null                    — verbatim e.g. "$903 USD"
  price_original_currency: "USD"|"EUR"|"JPY"|"TWD"|"GBP"|"HKD"|"SGD"|"KRW"|"CNY"|null
  price_original_amount: number | null          — parsed numeric
  price_twd: integer | null                     — APPROXIMATE TWD only;
                                                  NEVER assume $ means USD
  conditions: { bag_included: bool|null, refundable: "yes"|"no"|"partial"|null,
                notes: str|null } | null
  deal_url: str | null

Rules:
  - For ambiguous dates ("Sep-Oct", "fall 2026"), set booking_deadline and
    travel_range both to null; keep raw text in `dates_free_text`.
  - For alternative routes ("LAX/SFO to TPE"), set route=null and put the
    raw text in `dates_free_text`.
  - `dates_free_text` is OPTIONAL but recommended for vague strings.
  - If this is NOT a flight fare (visa news, hotel, generic blog), output
    only `{"is_fare": false}`.
  - Legacy fields still accepted on read: dates, price_twd, price_original,
    airline, summary_zh, deal_url.

Output a single JSON object."""