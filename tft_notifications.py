"""TFT storage and delivery: historical quotes retained; notify new trips/new lows.
No booking or live-fare verification is performed. Caller owns the DB connection.
"""
import hashlib
import html
import json
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from structured_fares import insert_fare

SOURCE = 'tft_business'


def safe_link(url):
    return isinstance(url, str) and urlsplit(url).scheme in ('http', 'https')


def format_offer(item, reduced=False):
    p = item['parsed']
    esc = lambda x: html.escape(str(x))
    route = p['route']
    lines = [f"✈️ <b>{esc(route)}</b> {'⬇️ 降價' if reduced else '新行程'}",
             f"{esc(p.get('airline', ''))}｜來源標示商務艙、來回含稅",
             f"💰 NT${p['price_original_amount']:,.0f}｜📅 {esc(p.get('dates', '未註明'))}"]
    if route.split('->')[0] != 'TPE':
        lines.append('ℹ️ 需另買 TPE 接駁票；接駁成本未包含')
    lines.append('⚠️ 來源報價，未即時驗價；艙等／座椅及可訂日期請至訂票頁確認')
    article = p.get('deal_url') or item.get('link')
    if safe_link(article):lines.append(f'<a href="{esc(article)}">TFT 原文</a>')
    booking = p.get('booking_url')
    if safe_link(booking):lines.append(f'<a href="{esc(booking)}">來源提供的訂票連結（可能含聯盟追蹤）</a>')
    return '\n'.join(lines)


def process(conn, items, qualifies, send=None):
    """Return counts. `send(text)->bool` is None for storage-only runs.

    Same itinerary/price is one row across articles. Only lower prices than
    any successfully notified quote trigger a price alert. Failed sends leave
    alerted_at NULL and retry when the offer is present again. Dates/carrier/
    cabin are part of parser's itinerary guid. All raw quotes remain stored.
    """
    stats = dict(seen=len(items), stored=0, filtered=0, unchanged=0, eligible=0, sent=0, failed=0)
    pending=[];seen=set()
    # Stable itinerary id => lowest current quote, independent of section order.
    unique={}
    for item in items:
        key=item['guid'];amount=item['parsed']['price_original_amount']
        if key not in unique or amount < unique[key]['parsed']['price_original_amount']:
            unique[key]=item
    ordered = sorted(unique.values(), key=lambda i: (
        i['parsed']['route'].split('->')[0] != 'TPE',
        i['parsed']['price_original_amount']))
    for item in ordered:
        p=item['parsed'];amount=p['price_original_amount']
        if isinstance(amount,bool) or not isinstance(amount,(int,float)) or not 0 < amount < float('inf'):
            raise ValueError('Invalid TFT price')
        if p.get('price_original_currency')!='TWD':raise ValueError('Unexpected TFT currency')
        key=hashlib.sha256(f'{SOURCE}|{item["guid"]}|TWD|{amount:g}'.encode()).hexdigest()
        previous=conn.execute('SELECT parsed_json,alerted_at FROM bug_fares WHERE source=? AND item_guid=?', (SOURCE,item['guid'])).fetchall()
        notified=[json.loads(raw)['price_original_amount'] for raw,at in previous if at and raw]
        exists=conn.execute('SELECT 1 FROM bug_fares WHERE dedup_hash=?',(key,)).fetchone()
        if not exists:
            insert_fare(conn,dedup_hash=key,source=SOURCE,item_guid=item['guid'],pub_date=item.get('pub_date'),raw_title=item['title'],raw_description=item['description'],raw_image_urls=item.get('images',[]),parsed=p,alerted=False)
            stats['stored']+=1
        start=p.get('travel_range',{}).get('start')
        if not start or date.fromisoformat(start)<date.today() or not qualifies(p):
            stats['filtered']+=1;continue
        if notified and amount>=min(notified):
            stats['unchanged']+=1;continue
        if key in seen:continue
        seen.add(key);stats['eligible']+=1
        pending.append((key,format_offer(item,bool(notified))))
    # Mark only IDs in a successfully delivered bounded chunk. Do not touch
    # prior global alert flags. Network timeouts are logged by the transport;
    # delivery is at-least-once (an ambiguous timeout may duplicate on retry).
    header='💼 <b>TFT 商務艙優惠</b>\n\n'
    groups=[];keys=[];text=header
    for key,block in pending:
        if len(text)+len(block)+2>3500 and keys:
            groups.append((keys,text.rstrip()));keys=[];text=header
        keys.append(key);text+=block+'\n\n'
    if keys:groups.append((keys,text.rstrip()))
    if send:
        for keys,text in groups:
            if send(text):
                now=datetime.now(timezone.utc).isoformat()
                with conn:conn.executemany('UPDATE bug_fares SET alerted_at=? WHERE dedup_hash=?',[(now,k) for k in keys])
                stats['sent']+=len(keys)
            else:stats['failed']+=len(keys)
    return stats
