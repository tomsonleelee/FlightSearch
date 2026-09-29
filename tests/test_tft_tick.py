import unittest,tempfile,sqlite3,sys,types
from pathlib import Path
from unittest.mock import Mock,patch
import fare_aggregator as fa
from tests.test_tft_notifications import item

class TFTTick(unittest.TestCase):
 def test_real_tick_bypasses_llm_filters_stores_and_dedups(self):
    fake=types.ModuleType('tft_source');fake.fetch_items=Mock(return_value=[item(),item('LAX',30000,'lax')])
    with tempfile.TemporaryDirectory() as t,patch.dict(sys.modules,{'tft_source':fake}),patch.object(fa,'DB_PATH',Path(t)/'db.sqlite'),patch.object(fa,'SOURCES',{'tft_business':{'enabled':True,'fetcher':'tft','url':'https://example.com'}}),patch.dict('os.environ',{'OPENROUTER_API_KEY':'','TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}),patch.object(fa,'call_llm',side_effect=AssertionError('TFT must not use LLM')),patch.object(fa,'send_telegram',return_value=True) as send:
      self.assertEqual(fa.run_tick(),1)
      self.assertEqual(fa.run_tick(),0)
      self.assertEqual(send.call_count,1)
      c=sqlite3.connect(fa.DB_PATH)
      self.assertEqual(c.execute('select count(*) from fares').fetchone()[0],2)
      self.assertEqual(c.execute('select count(*) from bug_fares where alerted_at is not null').fetchone()[0],1)
      c.close()
 def test_tft_fetch_failure_surfaces_nonzero(self):
    fake=types.ModuleType('tft_source');fake.fetch_items=Mock(side_effect=ValueError('layout changed'))
    with tempfile.TemporaryDirectory() as t,patch.dict(sys.modules,{'tft_source':fake}),patch.object(fa,'DB_PATH',Path(t)/'db.sqlite'),patch.object(fa,'SOURCES',{'tft_business':{'enabled':True,'fetcher':'tft','url':'https://example.com'}}),patch.dict('os.environ',{'OPENROUTER_API_KEY':''}):
      with self.assertRaisesRegex(ValueError,'layout changed'):fa.run_tick()
