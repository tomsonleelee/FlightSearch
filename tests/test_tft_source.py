"""Offline contract tests for the TFT article parser and RSS selection."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tft_source

ARTICLE = "https://www.the-frequent-traveler.com.tw/asia-biz-deals-20260928/"
SNAPSHOT = pathlib.Path('/home/tomson/.hermes/profiles/work/workspace/tft-live.html')


def card(section='first', date='2026/12/29–01/04', origin='台北 TPE', destination='東京成田 NRT', price='NT$40,178', airline='星宇航空（JX）'):
    return f'''<div class="card" id="{section}"><div class="chd">台北 → 東京</div>
    <div class="meta">台北 → 東京 ・ {airline} ・ A350</div>
    <span class="prc">{price}</span><div class="dbox">適用優惠日期 <span>{date}</span></div>
    <div class="sbox"><b>【去程】</b><span>{origin} → {destination}</span><span>JX800</span></div>
    <div class="sbox"><b>【回程】</b><span>{destination} → {origin}</span><span>JX801</span></div>
    <div class="cta"><a href="https://booking.example/deal">查看</a></div></div>'''


class TFTSourceTests(unittest.TestCase):
    def test_explicit_card_rollover_and_segments(self):
        item, = tft_source.parse_article(card(), ARTICLE, '2026-09-28T22:54:27+08:00')
        p = item['parsed']
        self.assertEqual(p['route'], 'TPE->NRT')
        self.assertEqual(p['travel_range'], {'start': '2026-12-29', 'end': '2027-01-04'})
        self.assertEqual(p['dates'], '2026-12-29/2027-01-04')
        self.assertEqual(p['outbound_segments'][0]['origin'], 'TPE')
        self.assertEqual(p['inbound_segments'][0]['destination'], 'TPE')
        self.assertEqual(p['booking_url'], 'https://booking.example/deal')
        self.assertEqual(p['deal_url'], ARTICLE + '#first')
        self.assertEqual(p['price_original_amount'], 40178)
        self.assertEqual(p['price_original'], 'NT$40,178')
        self.assertEqual(p['price_original_currency'], 'TWD')
        self.assertEqual(p['price_twd'], 40178)
        self.assertEqual(p['airline'], '星宇航空（JX）')
        self.assertTrue(p['source_claims_unverified'])
        self.assertTrue(p['is_fare'])
        self.assertEqual(p['cabin'], 'business')
        self.assertEqual(p['trip_type'], 'roundtrip')
        self.assertEqual(item['images'], [])
        self.assertEqual(item['link'], ARTICLE + '#first')

    def test_malformed_rows_skip_and_no_valid_fail_loudly(self):
        with self.assertLogs('tft_source', level='WARNING') as logs:
            items = tft_source.parse_article(card(section='bad', date='2026/02/30–03/04') + card(), ARTICLE, '2026-09-28')
        self.assertEqual(len(items), 1)
        self.assertTrue(any('skipped=1' in msg for msg in logs.output))
        with self.assertRaises(ValueError):
            tft_source.parse_article(card(date='2026/02/30–03/04'), ARTICLE, '2026-09-28')

    def test_duplicate_sections_same_canonical_guid_regardless_price(self):
        first = tft_source.parse_article(card(), ARTICLE, '2026-09-28')[0]
        repeated = card(section='repeat', price='NT$35,000')
        self.assertEqual(len(tft_source.parse_article(card() + repeated, ARTICLE, '2026-09-28')), 1)
        self.assertEqual(first['guid'], tft_source.parse_article(repeated, ARTICLE, '2026-09-29')[0]['guid'])

    def test_xbox_city_mapping_only_from_same_article_and_unambiguous(self):
        xbox = '''<div class="xbox" id="other"><span>台北 → 東京成田　星宇航空 JX　NT$40,178　CP 3</span><span>適用優惠日期：2026/12/29–01/04</span><a href="https://booking.example/x">查看</a></div>'''
        result = tft_source.parse_article(card() + xbox, ARTICLE, '2026-09-28')
        self.assertEqual(len(result), 1)  # same deal as explicit card
        unknown = xbox.replace('東京成田', '巴黎')
        self.assertEqual(len(tft_source.parse_article(card() + unknown, ARTICLE, '2026-09-28')), 1)
        ambiguous = card(section='another', destination='東京成田 HND', price='NT$30,000')
        self.assertEqual(len(tft_source.parse_article(card() + ambiguous + xbox, ARTICLE, '2026-09-28')), 2)

    def test_snapshot_parses_real_cards_and_xboxes(self):
        items = tft_source.parse_article(SNAPSHOT.read_text(), ARTICLE, '2026-09-28T22:54:27+08:00')
        self.assertGreater(len(items), 15)
        self.assertEqual(len({i['guid'] for i in items}), len(items))
        self.assertTrue(any(i['parsed']['route'] == 'ICN->WAW' for i in items))
        self.assertTrue(any(i['parsed']['route'] == 'BKK->ZRH' for i in items))
        syd = next(i for i in items if i['parsed']['route'] == 'TPE->SYD')
        self.assertTrue(syd['parsed']['outbound_segments'])  # card detail must win over earlier xbox
        self.assertTrue(all(i['parsed']['outbound_segments'] for i in items if i['parsed']['inbound_segments']))

    def test_feed_selects_latest_published_matching_article_only(self):
        feed = '''<rss><channel>
        <item><link>https://www.the-frequent-traveler.com.tw/asia-biz-deals-20990101/</link><pubDate>Tue, 01 Jan 2099 00:00:00 +0800</pubDate></item>
        <item><link>https://www.the-frequent-traveler.com.tw/asia-biz-deals-20990102/</link><pubDate>Mon, 28 Sep 2026 14:00:00 +0800</pubDate></item>
        <item><link>https://www.the-frequent-traveler.com.tw/other/</link><pubDate>Tue, 29 Sep 2026 12:00:00 +0800</pubDate></item>
        <item><link>https://www.the-frequent-traveler.com.tw/asia-biz-deals-20260927/</link><pubDate>Sun, 27 Sep 2026 12:00:00 +0800</pubDate></item>
        <item><link>https://www.the-frequent-traveler.com.tw/asia-biz-deals-20260928/</link><pubDate>Mon, 28 Sep 2026 12:00:00 +0800</pubDate></item>
        </channel></rss>'''
        seen = []
        class Response:
            def __init__(self, body): self.body = body.encode()
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return self.body
        def fake_open(request, timeout=None):
            seen.append(request)
            return Response(feed if len(seen) == 1 else card())
        with patch('tft_source.urllib.request.urlopen', side_effect=fake_open):
            result = tft_source.fetch_items()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['link'], ARTICLE + '#first')
        self.assertEqual(len(seen), 2)
        self.assertTrue(all('Mozilla' in r.get_header('User-agent') for r in seen))
        self.assertEqual(seen[1].full_url, ARTICLE)


if __name__ == '__main__':
    unittest.main()
