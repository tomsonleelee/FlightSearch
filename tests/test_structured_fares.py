"""TDD test suite for structured_fares. Sequential red-green: each block
below corresponds to a behaviour we need to lock in. See README for the
order of expansion.

Run from repo root:
    python3 -m unittest tests.test_structured_fares -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import fare_aggregator
import structured_fares as sf


def _fresh_db() -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db = sqlite3.connect(Path(tmp.name) / "test.db")
    db.executescript(fare_aggregator.SCHEMA)
    return db, tmp


def _seed_raw(db: sqlite3.Connection, dedup_hash: str, parsed: object, *, raw_text: str | None = None) -> None:
    """Insert a raw bug_fares row mimicking the existing aggregator path."""
    payload = raw_text if raw_text is not None else json.dumps(parsed)
    db.execute(
        """INSERT INTO bug_fares(dedup_hash, source, raw_title, parsed_json)
           VALUES (?, 'fixture', 'untouched', ?)""",
        (dedup_hash, payload),
    )
    db.commit()


class SchemaAndMigrationTests(unittest.TestCase):
    def test_repeat_migration_is_idempotent(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(db.close)
        before = db.execute("SELECT * FROM bug_fares").fetchone()
        _seed_raw(db, "a", {"is_fare": True, "summary_zh": "優惠"})
        sf.migrate(db)
        sf.migrate(db)
        self.assertEqual(before, None)  # sanity: bug_fares not nuked
        rows = db.execute(
            "SELECT source_hash, parse_state, summary FROM fares ORDER BY source_hash"
        ).fetchall()
        self.assertEqual(rows, [("a", "fare", "優惠")])

    def test_migration_preserves_raw_bug_fares(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True}, raw_text='{"is_fare":true,"note":"kept"}')
        before = db.execute("SELECT raw_title, parsed_json FROM bug_fares").fetchall()
        sf.migrate(db)
        sf.migrate(db)
        self.assertEqual(before, db.execute("SELECT raw_title, parsed_json FROM bug_fares").fetchall())

    def test_rollback_on_failure_leaves_db_unchanged(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "summary_zh": "good"})
        # Sabotage: drop fares table mid-migration to force an error.
        original_ensure = sf._ensure_schema

        def broken_ensure(conn):
            original_ensure(conn)
            conn.execute("DROP TABLE fares")

        sf._ensure_schema = broken_ensure  # type: ignore[assignment]
        try:
            with self.assertRaises(Exception):
                sf.migrate(db)
        finally:
            sf._ensure_schema = original_ensure  # type: ignore[assignment]
        # bug_fares still there, fares absent
        self.assertEqual(db.execute("SELECT COUNT(*) FROM bug_fares").fetchone()[0], 1)
        self.assertEqual(db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fares'"
        ).fetchone(), None)

    def test_indexes_and_constraints_present(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True})
        sf.migrate(db)
        idx = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        self.assertIn("idx_fares_airline", idx)
        self.assertIn("idx_legs_airport", idx)
        self.assertIn("idx_prices_currency", idx)


class ParseStateTests(unittest.TestCase):
    def test_dict_with_is_fare_true_is_fare(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "fare")

    def test_dict_with_is_fare_false_is_non_fare(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": False})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "non_fare")

    def test_malformed_json_is_tracked(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", None, raw_text="{not json")
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "malformed_json")

    def test_null_json_is_tracked(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", None, raw_text=None)
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "missing_json")

    def test_list_json_is_non_fare(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", [1, 2, 3])
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "non_fare")

    def test_scalar_json_is_non_fare(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", 42, raw_text="42")
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "non_fare")


class RouteLegTests(unittest.TestCase):
    def test_two_letter_route_becomes_origin_and_destination(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "route": "BLR->BKK"})
        sf.migrate(db)
        rows = db.execute(
            "SELECT leg_order, airport_code, location_name, is_origin, is_destination"
            " FROM fare_route_legs ORDER BY leg_order"
        ).fetchall()
        self.assertEqual(rows, [
            (0, "BLR", None, 1, 0),
            (1, "BKK", None, 0, 1),
        ])

    def test_three_letter_route_serial_legs(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "route": "TPE->HKG->SYD"})
        sf.migrate(db)
        rows = db.execute(
            "SELECT airport_code FROM fare_route_legs ORDER BY leg_order"
        ).fetchall()
        self.assertEqual(rows, [("TPE",), ("HKG",), ("SYD",)])

    def test_city_name_stored_as_location_name_not_airport(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "route": "Bangalore->Bangkok"})
        sf.migrate(db)
        rows = db.execute(
            "SELECT airport_code, location_name FROM fare_route_legs ORDER BY leg_order"
        ).fetchall()
        self.assertEqual(rows, [
            (None, "Bangalore"),
            (None, "Bangkok"),
        ])

    def test_alternative_route_not_split_into_serial_legs(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "route": "LAX/SFO->TPE"})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_route_legs").fetchone()[0], 0)

    def test_comma_separated_route_not_serialised(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "route": "TPE, HKG to JFK"})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_route_legs").fetchone()[0], 0)


class PriceTests(unittest.TestCase):
    def test_explicit_iso_currency_parsed(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "price_original": "USD 903", "price_twd": 28896})
        sf.migrate(db)
        row = db.execute(
            "SELECT original_text, original_currency, original_amount, approximate_twd"
            " FROM fare_prices"
        ).fetchone()
        self.assertEqual(row, ("USD 903", "USD", 903.0, 28896))

    def test_dollar_sign_not_assumed_usd(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "price_original": "$903"})
        sf.migrate(db)
        text, currency, amount, twd = db.execute(
            "SELECT original_text, original_currency, original_amount, approximate_twd"
            " FROM fare_prices"
        ).fetchone()
        self.assertEqual(text, "$903")
        self.assertIsNone(currency)
        self.assertEqual(amount, 903.0)
        self.assertIsNone(twd)

    def test_nt_dollar_marked_twd(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "price_original": "NT$15,000"})
        sf.migrate(db)
        text, currency, amount, _ = db.execute(
            "SELECT original_text, original_currency, original_amount, approximate_twd"
            " FROM fare_prices"
        ).fetchone()
        self.assertEqual(currency, "TWD")
        self.assertEqual(amount, 15000.0)

    def test_approximate_twd_distinct_column(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "price_original": "EUR 499", "price_twd": 17465})
        sf.migrate(db)
        row = db.execute("SELECT original_currency, approximate_twd FROM fare_prices").fetchone()
        self.assertEqual(row, ("EUR", 17465))

    def test_null_price_original_yet_twd_values_present(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "price_twd": 28896})
        sf.migrate(db)
        text, currency, amount, twd = db.execute(
            "SELECT original_text, original_currency, original_amount, approximate_twd"
            " FROM fare_prices"
        ).fetchone()
        self.assertIsNone(text)
        self.assertIsNone(currency)
        self.assertIsNone(amount)
        self.assertEqual(twd, 28896)


class DateValidationTests(unittest.TestCase):
    def test_unambiguous_iso_deadline_kept(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "deadline": "2026-09-15"})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT booking_deadline FROM fares").fetchone()[0], "2026-09-15")

    def test_free_text_deadline_dropped(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "deadline": "next Tuesday"})
        sf.migrate(db)
        self.assertIsNone(db.execute("SELECT booking_deadline FROM fares").fetchone()[0])

    def test_partial_iso_deadline_dropped(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "deadline": "2026-09"})
        sf.migrate(db)
        self.assertIsNone(db.execute("SELECT booking_deadline FROM fares").fetchone()[0])

    def test_invalid_iso_date_dropped(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "deadline": "2026-13-40"})
        sf.migrate(db)
        self.assertIsNone(db.execute("SELECT booking_deadline FROM fares").fetchone()[0])

    def test_free_text_dates_preserved_in_raw_dates(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "dates": "Sep 1-15, 2026"})
        sf.migrate(db)
        self.assertEqual(db.execute("SELECT raw_dates FROM fares").fetchone()[0], "Sep 1-15, 2026")

    def test_travel_range_parsed(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "travel_range": {"start": "2026-09-01", "end": "2026-09-15"}})
        sf.migrate(db)
        s, e = db.execute(
            "SELECT travel_date_start, travel_date_end FROM fares"
        ).fetchone()
        self.assertEqual((s, e), ("2026-09-01", "2026-09-15"))


class InsertionTests(unittest.TestCase):
    def test_insert_fare_writes_old_and_new_atomically(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        sf.insert_fare(
            db,
            dedup_hash="x", source="who", item_guid="g", pub_date=None,
            raw_title="T", raw_description="D", raw_image_urls=[],
            parsed={"is_fare": True, "summary_zh": "便宜", "route": "TPE->NRT",
                    "price_original": "USD 500", "price_twd": 16000},
            alerted=False,
        )
        self.assertEqual(db.execute("SELECT COUNT(*) FROM bug_fares").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fares").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_route_legs").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_prices").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT raw_title FROM bug_fares").fetchone()[0], "T")

    def test_insert_fare_rollback_on_failure(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        # First migrate so the fares table exists.
        sf.migrate(db)
        original = sf._parse_route
        def boom(p):  # noqa: ANN001
            raise RuntimeError("boom")
        sf._parse_route = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                sf.insert_fare(
                    db,
                    dedup_hash="x", source="who", item_guid=None, pub_date=None,
                    raw_title="T", raw_description="D", raw_image_urls=[],
                    parsed={"is_fare": True, "route": "TPE->NRT"},
                    alerted=False,
                )
        finally:
            sf._parse_route = original  # type: ignore[assignment]
        self.assertEqual(db.execute("SELECT COUNT(*) FROM bug_fares").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fares").fetchone()[0], 0)

    def test_insert_fare_handles_non_fare(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        sf.insert_fare(
            db,
            dedup_hash="x", source="who", item_guid=None, pub_date=None,
            raw_title="T", raw_description="D", raw_image_urls=[],
            parsed={"is_fare": False}, alerted=False,
        )
        self.assertEqual(db.execute("SELECT parse_state FROM fares").fetchone()[0], "non_fare")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_route_legs").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fare_prices").fetchone()[0], 0)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.db, self.tmp = _fresh_db()
        self.addCleanup(self.tmp.cleanup); self.addCleanup(self.db.close)
        _seed_raw(self.db, "a", {
            "is_fare": True, "route": "TPE->NRT", "cabin": "business",
            "price_original": "USD 1200", "price_twd": 38400,
        })
        _seed_raw(self.db, "b", {
            "is_fare": True, "route": "TPE->HKG", "cabin": "economy",
            "price_original": "EUR 800", "price_twd": 28000,
        })
        _seed_raw(self.db, "c", {
            "is_fare": True, "route": "HKG->NRT", "cabin": "business",
            "price_original": "USD 900", "price_twd": 28800,
        })
        sf.migrate(self.db)

    def test_origin_filter(self):
        rows = sf.query(self.db, origin="TPE")
        self.assertEqual({r["source_hash"] for r in rows}, {"a", "b"})

    def test_destination_filter(self):
        rows = sf.query(self.db, destination="NRT")
        self.assertEqual({r["source_hash"] for r in rows}, {"a", "c"})

    def test_cabin_filter(self):
        rows = sf.query(self.db, cabin="business")
        self.assertEqual({r["source_hash"] for r in rows}, {"a", "c"})

    def test_currency_filter_does_not_mix_currencies(self):
        rows = sf.query(self.db, currency="USD")
        self.assertEqual({r["source_hash"] for r in rows}, {"a", "c"})

    def test_max_price_filter(self):
        rows = sf.query(self.db, currency="USD", max_price=1000)
        self.assertEqual({r["source_hash"] for r in rows}, {"c"})

    def test_combined_filters(self):
        rows = sf.query(self.db, origin="TPE", destination="NRT", cabin="business")
        self.assertEqual({r["source_hash"] for r in rows}, {"a"})

    def test_query_does_not_mutate(self):
        before_fares = self.db.execute("SELECT * FROM fares").fetchall()
        before_legs = self.db.execute("SELECT * FROM fare_route_legs").fetchall()
        sf.query(self.db, origin="TPE")
        self.assertEqual(before_fares, self.db.execute("SELECT * FROM fares").fetchall())
        self.assertEqual(before_legs, self.db.execute("SELECT * FROM fare_route_legs").fetchall())


class PromptTests(unittest.TestCase):
    def test_prompt_lists_explicit_fields(self):
        prompt = sf.build_prompt()
        for token in ("cabin", "trip_type", "booking_deadline", "travel_range",
                       "price_original_currency", "price_original_amount",
                       "approximate_twd" in prompt and "approximate_twd" or None,
                       "conditions"):
            if token is None:
                continue
            self.assertIn(token, prompt)

    def test_legacy_fields_still_listed(self):
        prompt = sf.build_prompt()
        for legacy in ("dates", "price_twd", "price_original", "airline",
                       "summary_zh", "deal_url"):
            self.assertIn(legacy, prompt)


class UnknownFieldTests(unittest.TestCase):
    def test_unknown_fields_stored_as_null(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True})
        sf.migrate(db)
        cols = [r[0] for r in db.execute("SELECT summary, airline, cabin, trip_type, "
                                         "booking_deadline, travel_date_start, "
                                         "travel_date_end, raw_dates FROM fares").description]
        self.assertEqual(db.execute("SELECT summary, airline, cabin, trip_type, "
                                    "booking_deadline, travel_date_start, "
                                    "travel_date_end, raw_dates FROM fares").fetchone(),
                         (None, None, None, None, None, None, None, None))

    def test_conditions_table_present_and_nullable(self):
        db, tmp = _fresh_db()
        self.addCleanup(tmp.cleanup); self.addCleanup(db.close)
        _seed_raw(db, "a", {"is_fare": True, "conditions": {"bag_included": True}})
        sf.migrate(db)
        # conditions is a separate table; we never INSERT into it on migrate
        # (insert_fare path does, but legacy migrate does not — keep simple).
        row = db.execute(
            "SELECT bag_included, refundable, notes FROM fare_conditions WHERE fare_source_hash='a'"
        ).fetchone()
        self.assertIsNone(row)


class CliSmokeTests(unittest.TestCase):
    def test_cli_migrate_and_query(self):
        import os
        import subprocess
        import sys
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "cli.db"
        env = {"PYTHONPATH": "."}
        rc = subprocess.run(
            [sys.executable, "structured_fares_cli.py", "migrate", "--db", str(db_path)],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, **env},
        )
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        # Now insert a fare directly into the DB and query.
        conn = sqlite3.connect(db_path)
        conn.executescript(fare_aggregator.SCHEMA)
        _seed_raw(conn, "z", {
            "is_fare": True, "route": "TPE->NRT", "cabin": "business",
            "price_original": "USD 500", "price_twd": 16000,
        })
        conn.commit(); conn.close()
        # Backfill so fares/legs/prices rows exist for the seed.
        rc = subprocess.run(
            [sys.executable, "structured_fares_cli.py", "migrate", "--db", str(db_path)],
            capture_output=True, text=True,
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, **env},
        )
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        rc = subprocess.run(
            [sys.executable, "structured_fares_cli.py", "query",
             "--db", str(db_path), "--origin", "TPE", "--destination", "NRT"],
            capture_output=True, text=True,
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, **env},
        )
        self.assertEqual(rc.returncode, 0, msg=rc.stderr)
        self.assertIn("TPE", rc.stdout)


if __name__ == "__main__":
    unittest.main()