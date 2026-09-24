import unittest, tempfile, sqlite3, json
from pathlib import Path
from unittest.mock import patch
import fare_aggregator as fa
import structured_fares as sf

class LiveStorage(unittest.TestCase):
    def test_new_prompt_fields(self):
        p={'is_fare':True,'price_original':'$200','price_original_currency':'USD','price_original_amount':200,'booking_deadline':'2026-10-01','dates_free_text':'Christmas'}
        self.assertEqual(sf._parse_price(p)[1:3],('USD',200))
        row=sf._build_fare_row('x',p)
        self.assertEqual(row['booking_deadline'],'2026-10-01')
        self.assertEqual(row['raw_dates'],'Christmas')

    def test_incremental_migration(self):
        c=sqlite3.connect(':memory:');c.executescript(fa.SCHEMA)
        c.execute("insert into bug_fares(dedup_hash,source,parsed_json) values ('a','test','{\"is_fare\":false}')");c.commit()
        self.assertEqual(sf.migrate(c),1)
        c.execute("insert into bug_fares(dedup_hash,source,parsed_json) values ('b','test','{\"is_fare\":false}')");c.commit()
        self.assertEqual(sf.migrate(c),1)
        self.assertEqual(sf.migrate(c),0)
        c.close()

    def test_actual_tick_writes_structured_tables(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'prices.db'
            c=fa.init_db(db); sf.migrate(c); c.close()
            item={'guid':'fixture','pub_date':'2026-09-24','title':'fixture','description':'fixture','images':[]}
            payload={'is_fare':True,'route':'TPE->NRT','cabin':'economy','trip_type':'roundtrip','price_original':'USD 200','price_twd':6400}
            with patch.object(fa,'DB_PATH',db),patch.object(fa,'SOURCES',{'fixture':{'enabled':True}}),patch.object(fa,'fetch_source',return_value=[item]),patch.object(fa,'call_llm',return_value=payload),patch.dict('os.environ',{'OPENROUTER_API_KEY':'fixture'}):
                fa.run_tick(send_alert=False)
            c=sqlite3.connect(db)
            self.assertEqual(c.execute('select count(*) from fares').fetchone()[0],1)
            self.assertEqual(c.execute('select count(*) from fare_route_legs').fetchone()[0],2)
            self.assertEqual(c.execute('select count(*) from fare_prices').fetchone()[0],1)
            c.close()
