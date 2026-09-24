import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
from pathlib import Path
from unittest.mock import patch
import fare_aggregator as fa
import structured_fares as sf

TPE_REACHABLE_3_5H = frozenset({
    "TPE","TSA","KHH",
    "HKG","MFM",
    "ISG","SHI","OKA","KOJ","KMJ","KMI","HSG","FUK","OIT","HIJ","YGJ","MYJ","TAK","OKJ","KCZ","UKB","KIX",
    "CJU","PUS","TAE","CJJ","ICN","HND","NRT","NGO","KMQ",
    "CEB","CRK","MNL","DAD","HAN",
    "FOC","XMN","NGB","HGH","PVG","NKG","CAN","SZX","WUH","TAO",
})


class TpeFilterRules(unittest.TestCase):
    def setUp(self):
        fa.TPE_REACHABLE = TPE_REACHABLE_3_5H
        fa.REBUILD_TPE_FILTER = lambda: TPE_REACHABLE_3_5H

    def test_tpe_origin_is_in_set(self):
        self.assertTrue(fa.is_tpe_reachable_origin("TPE"))

    def test_known_tpe_reachable_origin(self):
        self.assertTrue(fa.is_tpe_reachable_origin("ICN"))
        self.assertTrue(fa.is_tpe_reachable_origin("HKG"))
        self.assertTrue(fa.is_tpe_reachable_origin("DAD"))

    def test_non_tpe_reachable_origin(self):
        self.assertFalse(fa.is_tpe_reachable_origin("LAX"))
        self.assertFalse(fa.is_tpe_reachable_origin("BKK"))
        self.assertFalse(fa.is_tpe_reachable_origin("ARN"))

    def test_lowercase_and_unknown_filtered(self):
        self.assertFalse(fa.is_tpe_reachable_origin("lax"))
        self.assertFalse(fa.is_tpe_reachable_origin(None))
        self.assertFalse(fa.is_tpe_reachable_origin(""))

    def test_alert_line_for_non_tpe_origin_marks_connection(self):
        parsed = {"route": "ICN->DAD", "price_original": "$153 USD", "summary_zh": "首爾飛峴港來回機票"}
        line = fa.format_alert_line("theflightdeal", parsed, {"title": "ICN->DAD", "link": "https://x"})
        self.assertIn("需另買 TPE 接駁票", line)

    def test_alert_line_for_tpe_origin_no_connection_note(self):
        parsed = {"route": "TPE->DAD", "price_original": "$200 USD", "summary_zh": "台北飛峴港"}
        line = fa.format_alert_line("theflightdeal", parsed, {"title": "TPE->DAD", "link": "https://x"})
        self.assertNotIn("需另買 TPE 接駁票", line)


class LiveTickFilter(unittest.TestCase):
    def setUp(self):
        fa.TPE_REACHABLE = TPE_REACHABLE_3_5H

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / "prices.db"
            conn = fa.init_db(db)
            sf.migrate(conn)
            conn.close()
            item = {"guid": "fixture", "pub_date": "2026-09-24",
                    "title": payload.get("route", "fixture"),
                    "description": payload.get("summary_zh", "fixture"),
                    "images": []}
            env = {
                "OPENROUTER_API_KEY": "fixture",
                "TELEGRAM_BOT_TOKEN": "fixture-token",
                "TELEGRAM_CHAT_ID": "fixture-chat",
            }
            with patch.object(fa, "DB_PATH", db), \
                 patch.object(fa, "SOURCES", {"fixture": {"enabled": True}}), \
                 patch.object(fa, "fetch_source", return_value=[item]), \
                 patch.object(fa, "call_llm", return_value=payload), \
                 patch.dict("os.environ", env), \
                 patch.object(fa, "send_telegram", return_value=True) as sent:
                n = fa.run_tick(send_alert=True)
            c = __import__("sqlite3").connect(db)
            row = c.execute(
                "SELECT alerted_at FROM bug_fares WHERE dedup_hash=?",
                (fa.compute_dedup_hash("fixture", item),),
            ).fetchone()
            try:
                origin = payload["route"].split("->")[0].strip()
            except Exception:
                origin = ""
            return n, bool(row and row[0] and row[0] not in (0, "")), sent.called, origin

    def test_icn_origin_records_alerted_and_triggers_telegram(self):
        n, alerted, sent, origin = self._run({
            "is_fare": True, "route": "ICN->DAD",
            "price_original": "$153 USD", "summary_zh": "首爾→峴港",
        })
        self.assertTrue(origin in fa.TPE_REACHABLE)
        self.assertEqual(n, 1)
        self.assertTrue(alerted)
        self.assertTrue(sent)

    def test_lax_origin_not_recorded_as_alerted(self):
        n, alerted, sent, origin = self._run({
            "is_fare": True, "route": "LAX->BKK",
            "price_original": "$400 USD", "summary_zh": "LAX→BKK",
        })
        self.assertFalse(origin in fa.TPE_REACHABLE)
        self.assertEqual(n, 0)
        self.assertFalse(alerted)
        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()