"""Parse source-reported TFT business-class offers without external dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import logging
import re
import urllib.request
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

LOGGER = logging.getLogger(__name__)
FEED_URL = 'https://www.the-frequent-traveler.com.tw/feed/'
UA = 'Mozilla/5.0 (compatible; TFTFareReader/1.0)'
ARTICLE_RE = re.compile(r'/asia-biz-deals-\d{8}/?$')
IATA_LEG = re.compile(r'^\s*(.*?)\s+([A-Z]{3})\s*→\s*(.*?)\s+([A-Z]{3})\s*$')
DATE_RE = re.compile(r'(\d{4})/(\d{1,2})/(\d{1,2})\s*[–—~-]\s*(?:(\d{4})/)?(\d{1,2})/(\d{1,2})')
PRICE_RE = re.compile(r'NT\$\s*([\d,]+)')
ROUTE_RE = re.compile(r'([^\s　→]+)\s*→\s*([^\s　→]+)')
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return ''.join(child.text() if isinstance(child, Node) else child for child in self.children).strip()

    def has(self, name):
        return name in self.attrs.get('class', '').split()

    def descendants(self, name):
        return [n for n in self.walk() if n is not self and n.has(name)]


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _first(node, klass):
    return next(iter(node.descendants(klass)), None)


def _date_range(value):
    match = DATE_RE.search(value)
    if not match:
        raise ValueError('missing date range')
    year, month, day, end_year, end_month, end_day = match.groups()
    start = date(int(year), int(month), int(day))
    end = date(int(end_year or year), int(end_month), int(end_day))
    if not end_year and end < start:
        end = date(start.year + 1, int(end_month), int(end_day))
    if end < start or (end - start).days > 366:
        raise ValueError('invalid date order')
    return start.isoformat(), end.isoformat()


def _price(text):
    match = PRICE_RE.search(text)
    if not match or int(match.group(1).replace(',', '')) <= 0:
        raise ValueError('missing price')
    return 'NT$' + match.group(1), int(match.group(1).replace(',', ''))


def _link(node, url):
    for element in node.walk():
        if element.tag == 'a' and element.attrs.get('href', '').startswith(('https://', 'http://')):
            return urljoin(url, element.attrs['href'])
    raise ValueError('missing booking link')


def _card(node, url):
    meta_node = _first(node, 'meta')
    price_node = _first(node, 'prc')
    date_node = _first(node, 'dbox')
    cta_node = _first(node, 'cta')
    if not all((meta_node, price_node, date_node, cta_node)):
        raise ValueError('missing card fields')
    meta = meta_node.text()
    carrier = re.search(r'([^・]+?)\s*（([A-Z0-9]{2,3})）', meta)
    if not carrier:
        raise ValueError('missing carrier')
    airline = carrier.group(1).strip() + '（' + carrier.group(2) + '）'
    outbound, inbound = [], []
    direction = None
    city_pairs = []
    for box in node.descendants('sbox'):
        label = box.text()
        if '【去程】' in label:
            direction = 'outbound'
        elif '【回程】' in label:
            direction = 'inbound'
        if not direction:
            raise ValueError('unlabeled segment')
        spans = [n.text() for n in box.walk() if n.tag == 'span']
        if not spans:
            raise ValueError('empty segment')
        leg = IATA_LEG.match(spans[0])
        if not leg:
            raise ValueError('invalid IATA segment')
        city_a, orig, city_b, dest = leg.groups()
        (outbound if direction == 'outbound' else inbound).append({
            'origin': orig, 'destination': dest, 'origin_city': city_a.strip(),
            'destination_city': city_b.strip(), 'flight': spans[1] if len(spans) > 1 else '',
            'aircraft': spans[2] if len(spans) > 2 else '',
        })
        city_pairs.extend(((city_a.strip(), orig), (city_b.strip(), dest)))
    if not outbound or not inbound or outbound[-1]['destination'] != inbound[0]['origin'] or inbound[-1]['destination'] != outbound[0]['origin']:
        raise ValueError('incomplete roundtrip')
    for legs in (outbound, inbound):
        if any(a['destination'] != b['origin'] for a, b in zip(legs, legs[1:])):
            raise ValueError('disconnected segments')
    return dict(origin=outbound[0]['origin'], destination=outbound[-1]['destination'],
                carrier=carrier.group(2), airline=airline, price=_price(price_node.text()),
                dates=_date_range(date_node.text()), booking_url=_link(cta_node, url),
                outbound_segments=outbound, inbound_segments=inbound,
                city_pairs=city_pairs, summary=meta)


def _xbox(node, url, cities):
    spans = [n.text() for n in node.walk() if n.tag == 'span']
    if len(spans) < 2:
        raise ValueError('missing summary')
    route = ROUTE_RE.search(spans[0])
    carrier = re.search(r'([^\s　]+航空)\s+([A-Z0-9]{2,3})\b', spans[0])
    if not route or not carrier:
        raise ValueError('missing route or carrier')
    orig, dest = (cities.get(city, set()) for city in route.groups())
    if len(orig) != 1 or len(dest) != 1:
        raise ValueError('unknown or ambiguous city')
    return dict(origin=next(iter(orig)), destination=next(iter(dest)), carrier=carrier.group(2),
                airline=carrier.group(1) + '（' + carrier.group(2) + '）',
                price=_price(spans[0]), dates=_date_range(spans[1]), booking_url=_link(node, url),
                outbound_segments=[], inbound_segments=[], city_pairs=[], summary=spans[0])


def parse_article(html, url, pub_date):
    """Return deduplicated source claims; raise ValueError if nothing is usable."""
    tree = Tree()
    tree.feed(html)
    sections = [node for node in tree.root.walk() if node.tag == 'div' and (node.has('card') or node.has('xbox'))]
    cities, records = {}, []
    skipped = 0
    for node in sections:
        if not node.has('card'):
            continue
        try:
            rec = _card(node, url)
            records.append((node, rec))
            for city, code in rec['city_pairs']:
                cities.setdefault(city, set()).add(code)
        except (ValueError, TypeError) as exc:
            skipped += 1
            LOGGER.debug('TFT card %s skipped: %s', node.attrs.get('id'), exc)
    cards = {id(node): rec for node, rec in records}
    results, seen = [], set()
    # Detailed cards take precedence when an earlier summary repeats the same fare.
    for node in sorted(sections, key=lambda section: not section.has('card')):
        try:
            rec = cards.get(id(node)) if node.has('card') else _xbox(node, url, cities)
            if rec is None:
                continue
        except (ValueError, TypeError) as exc:
            skipped += 1
            LOGGER.debug('TFT xbox %s skipped: %s', node.attrs.get('id'), exc)
            continue
        start, end = rec['dates']
        origin, dest = rec['origin'], rec['destination']
        guid = sha256('|'.join((origin, dest, rec['carrier'], start, end, 'business')).encode()).hexdigest()
        if guid in seen:
            continue
        seen.add(guid)
        section = node.attrs.get('id', '')
        deal_url = url.split('#')[0] + ('#' + section if section else '')
        price, amount = rec['price']
        parsed = dict(is_fare=True, route=f'{origin}->{dest}', cabin='business', trip_type='roundtrip',
                      price_original=price, price_original_currency='TWD', price_original_amount=amount,
                      price_twd=amount, travel_range={'start': start, 'end': end}, dates=f'{start}/{end}',
                      airline=rec['airline'], summary_zh=rec['summary'], deal_url=deal_url,
                      booking_url=rec['booking_url'], source_claims_unverified=True,
                      outbound_segments=rec['outbound_segments'], inbound_segments=rec['inbound_segments'])
        results.append(dict(guid=guid, pub_date=pub_date, title=f'{origin} → {dest} {rec["airline"]} {price}',
                            description=rec['summary'], images=[], link=deal_url, parsed=parsed))
    if skipped:
        LOGGER.warning('TFT article skipped=%d sections; valid=%d', skipped, len(results))
    if not results:
        raise ValueError(f'TFT article contained no valid deals (skipped={skipped})')
    return results


def _get(url):
    request = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode('utf-8-sig')


def fetch_items():
    """Fetch only the latest already-published TFT daily business-deals article."""
    feed = ET.fromstring(_get(FEED_URL))
    candidates = []
    now = datetime.now(timezone.utc)
    for item in feed.findall('.//channel/item'):
        link = (item.findtext('link') or '').strip()
        pub = (item.findtext('pubDate') or '').strip()
        article_match = ARTICLE_RE.search(urlsplit(link).path)
        if not article_match:
            continue
        try:
            published = parsedate_to_datetime(pub)
            slug_date = datetime.strptime(article_match.group().strip('/').rsplit('-', 1)[-1], '%Y%m%d').date()
            if (published.tzinfo is None or published.astimezone(timezone.utc) > now or
                    slug_date > now.astimezone(published.tzinfo).date()):
                continue
        except (ValueError, TypeError):
            continue
        candidates.append((published, link, pub))
    if not candidates:
        raise ValueError('TFT feed contains no published asia-biz-deals article')
    _, link, pub = max(candidates, key=lambda entry: entry[0])
    return parse_article(_get(link), link, pub)
