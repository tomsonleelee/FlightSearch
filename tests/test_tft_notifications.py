import unittest,sqlite3,copy
from unittest.mock import Mock
import fare_aggregator as fa
import tft_notifications as tn

def item(origin='TPE',price=50000,guid='trip-a'):
    return {'guid':guid,'pub_date':'2026-09-28','title':origin+'->CDG','description':'fixture','images':[], 'link':'https://example.com/article', 'parsed':{'is_fare':True,'route':origin+'->CDG','airline':'CX','cabin':'business','trip_type':'roundtrip','price_original':f'NT${price}','price_original_currency':'TWD','price_original_amount':price,'price_twd':price,'travel_range':{'start':'2027-01-01','end':'2027-01-09'},'dates':'2027-01-01–2027-01-09','booking_url':'https://example.com/book'}}

class Notify(unittest.TestCase):
 def setUp(self):
    self.c=sqlite3.connect(':memory:');self.c.executescript(fa.SCHEMA);self.send=Mock(return_value=True)
 def tearDown(self):self.c.close()
 def run_items(self,items,enabled=True):
    return tn.process(self.c,items,lambda p:fa.is_tpe_reachable_origin(fa._extract_origin(p)),self.send if enabled else None)
 def test_persist_filter_notify_repeat_and_drop(self):
    a=item();b=item('LAX',30000,'trip-b')
    r=self.run_items([a,b]);self.assertEqual(r['sent'],1);self.assertEqual(r['filtered'],1)
    self.assertEqual(self.c.execute('select count(*) from fares').fetchone()[0],2)
    self.assertEqual(self.c.execute('select count(*) from bug_fares where alerted_at is not null').fetchone()[0],1)
    self.assertIn('未即時驗價',self.send.call_args[0][0]);self.assertEqual(self.run_items([a,b])['sent'],0)
    more=item(price=55000);self.assertEqual(self.run_items([more])['sent'],0)
    less=item(price=49000);self.assertEqual(self.run_items([less])['sent'],1)
    self.assertEqual(self.run_items([less])['sent'],0)
 def test_failed_send_retries_without_false_sent(self):
    self.send.return_value=False
    self.assertEqual(self.run_items([item()])['sent'],0)
    self.assertEqual(self.c.execute('select count(*) from bug_fares where alerted_at is not null').fetchone()[0],0)
    self.send.return_value=True;self.assertEqual(self.run_items([item()])['sent'],1)
 def test_changed_dates_new_id_and_non_tpe_note(self):
    a=item('HKG');self.run_items([a]);self.assertIn('需另買 TPE 接駁票',self.send.call_args[0][0])
    a['guid']='new-dates';a['parsed']['travel_range']['start']='2027-02-01'
    self.assertEqual(self.run_items([a])['sent'],1)
 def test_no_alert_and_expired(self):
    self.assertEqual(self.run_items([item()],False)['sent'],0);self.send.assert_not_called()
    a=item(guid='expired');a['parsed']['travel_range']['start']='2000-01-01'
    self.assertEqual(self.run_items([a])['sent'],0)
 def test_tpe_first_and_html_safety(self):
    a=item('HKG',guid='hkg');a['parsed']['airline']='<unsafe>'
    b=item('TPE',guid='tpe')
    self.run_items([a,b]);text=self.send.call_args[0][0]
    self.assertLess(text.index('TPE-&gt;CDG'),text.index('HKG-&gt;CDG'))
    self.assertIn('&lt;unsafe&gt;',text)
 def test_chunks_only_mark_successful_groups(self):
    batch=[item(guid=f'long-{i}') for i in range(18)]
    self.send.side_effect=[True]+[False]*20
    r=self.run_items(batch)
    self.assertGreater(self.send.call_count,1)
    self.assertEqual(r['sent']+r['failed'],18)
    self.assertEqual(self.c.execute('select count(*) from bug_fares where alerted_at is not null').fetchone()[0],r['sent'])
    for call in self.send.call_args_list:self.assertLessEqual(len(call[0][0]),3500)
 def test_duplicate_in_same_batch(self):
    a=item();self.assertEqual(self.run_items([a,copy.deepcopy(a)])['sent'],1)
