#!/usr/bin/env python3
"""Migration + query CLI for the structured fares storage layer.

Usage:
    # Create / verify schema + backfill from bug_fares (idempotent).
    # ALWAYS pass --db explicitly — no implicit default to data/prices.db.
    python3 structured_fares_cli.py migrate --db /path/to/prices.db

    # Query by origin / destination / cabin / currency / max price.
    # Currencies are NEVER mixed — passing --currency USD returns only
    # USD-priced fares (no fallback to approximate_twd).
    python3 structured_fares_cli.py query --db /path/to/prices.db \\
        --origin TPE --destination NRT --cabin business --currency USD \\
        --max-price 1500

Both commands are read-only against the schema (migrate writes; query is
strictly read). Run migrate from cron or one-shot before any query.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import fare_aggregator
import structured_fares as sf


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(fare_aggregator.SCHEMA)
    return conn


def cmd_migrate(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        n = sf.migrate(conn)
        print(f"migrate: schema ready, backfilled {n} fare row(s) (0 = already migrated)")
    finally:
        conn.close()
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    try:
        rows = sf.query(
            conn,
            origin=args.origin,
            destination=args.destination,
            cabin=args.cabin,
            currency=args.currency,
            max_price=args.max_price,
        )
        if not rows:
            print("(no matching fares)")
            return 0
        cols = list(rows[0].keys())
        print("\t".join(cols))
        for r in rows:
            print("\t".join("" if r[c] is None else str(r[c]) for c in cols))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    doc = __doc__.split("\n", 1)[0] if __doc__ else ""
    p = argparse.ArgumentParser(description=doc)
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="Create schema + backfill from bug_fares.")
    m.add_argument("--db", type=Path, required=True,
                   help="Path to SQLite DB (REQUIRED — never default to data/prices.db).")
    m.set_defaults(func=cmd_migrate)

    q = sub.add_parser("query", help="Read-only structured query.")
    q.add_argument("--db", type=Path, required=True,
                   help="Path to SQLite DB (REQUIRED — never default to data/prices.db).")
    q.add_argument("--origin", help="IATA origin code, e.g. TPE")
    q.add_argument("--destination", help="IATA destination code, e.g. NRT")
    q.add_argument("--cabin", choices=["economy", "premium_economy", "business", "first"])
    q.add_argument("--currency", help="ISO currency, e.g. USD. Refuses to mix currencies.")
    q.add_argument("--max-price", type=float, help="Max original_amount (in --currency).")
    q.set_defaults(func=cmd_query)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())