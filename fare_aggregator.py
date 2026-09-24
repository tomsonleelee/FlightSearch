#!/usr/bin/env python3
"""Bug-fare aggregator — pulls deal posts from multiple sources, parses with a
vision-capable LLM into structured rows, dedups, and sends a Telegram digest.

Phase 1 sources:
  - theflightdeal : https://www.theflightdeal.com/feed/ (plain RSS, curl-friendly)
  - secretflying  : https://www.secretflying.com/feed/ (Cloudflare-protected,
                    fetched via headless Chromium already in venv)
  - bonbon        : @bonbon.map IG via rsshub.app — DISABLED in phase 1
                    (rsshub.app public instance is fully blocked by Cloudflare
                    managed-challenge, even via patchright stealth). Hook is
                    left in place so phase 2 can re-enable once an alternative
                    IG source is decided.

Sits alongside price_tracker.py (watchlist Z-score) — completely separate
concern, separate table (bug_fares), separate timer (flightsearch-bugfare).

Style mirrors price_alert.py: stdlib only (urllib.request for HTTP/Telegram,
sqlite3 for storage, manual load_dotenv). Playwright is reused from the
existing FlightSearch dep (no new pip install beyond what is already pinned).

Usage:
    python3 fare_aggregator.py                # one tick
    python3 fare_aggregator.py --dry-run      # fetch + parse but do not store / alert
    python3 fare_aggregator.py --no-alert     # store rows but skip Telegram
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "data" / "prices.db"
ENV_FILE = REPO_ROOT / ".env"
LOG_PATH = REPO_ROOT / "data" / "bugfare.log"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"  # multimodal, cheap, verified live

# Phase 1 source registry — keep bonbon entry for phase-2 reactivation.
SOURCES = {
    "theflightdeal": {
        "url": "https://www.theflightdeal.com/feed/",
        "fetcher": "curl",      # plain urllib.request is fine
        "emoji": "✈️",
        "enabled": True,
    },
    "secretflying": {
        # /feed/ 301-redirects to homepage (server killed public RSS),
        # and all alt WP feed paths are CF managed-challenge. The homepage
        # itself loads fine through chromium and exposes 10-15 deal cards
        # with full route+price in the <a title="..."> attr, which is the
        # only signal the LLM needs. Custom fetcher 'playwright_homepage'.
        "url": "https://www.secretflying.com/",
        "fetcher": "playwright_homepage",
        "emoji": "🤫",
        "enabled": True,
    },
    "bonbon": {
        "url": "https://rsshub.app/instagram/bonbon.map",
        "fetcher": "playwright",
        "emoji": "🍬",
        "enabled": False,  # rsshub.app blocked; revisit in phase 2
        "disabled_reason": "rsshub.app public instance is blocked by Cloudflare; revisit with self-hosted rsshub or alt IG source.",
    },
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bug_fares (
    dedup_hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    item_guid TEXT,
    pub_date TEXT,
    raw_title TEXT,
    raw_description TEXT,
    raw_image_urls TEXT,
    parsed_json TEXT,
    alerted_at TEXT,
    inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bug_fares_source ON bug_fares(source);
CREATE INDEX IF NOT EXISTS idx_bug_fares_inserted_at ON bug_fares(inserted_at);
"""


# ---------------------------------------------------------------------------
# .env loader (mirrors price_alert.load_dotenv — keeps tracker/aggregator parity)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Logging — single stdout/stderr line per event so systemd journal stays grep-able
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def dedup_exists(conn: sqlite3.Connection, dedup_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bug_fares WHERE dedup_hash = ?", (dedup_hash,)
    ).fetchone()
    return row is not None


def insert_fare(
    conn: sqlite3.Connection,
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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR IGNORE INTO bug_fares
           (dedup_hash, source, item_guid, pub_date, raw_title, raw_description,
            raw_image_urls, parsed_json, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_hash, source, item_guid, pub_date, raw_title,
            raw_description[:4000],  # keep DB sane
            json.dumps(raw_image_urls),
            json.dumps(parsed) if parsed else None,
            now if alerted else None,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# RSS fetching — two backends
# ---------------------------------------------------------------------------

def fetch_curl(url: str, timeout: int = 20) -> str:
    """Plain urllib.request fetch with a friendly UA. Raises on HTTP error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


_PLAYWRIGHT_CTX = None  # (playwright, browser, context) tuple, opened lazily


def _get_playwright_ctx():
    global _PLAYWRIGHT_CTX
    if _PLAYWRIGHT_CTX is not None:
        return _PLAYWRIGHT_CTX
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=USER_AGENT)
    _PLAYWRIGHT_CTX = (pw, browser, ctx)
    return _PLAYWRIGHT_CTX


def _close_playwright_ctx() -> None:
    global _PLAYWRIGHT_CTX
    if _PLAYWRIGHT_CTX is None:
        return
    pw, browser, _ctx = _PLAYWRIGHT_CTX
    try:
        browser.close()
    finally:
        pw.stop()
    _PLAYWRIGHT_CTX = None


def fetch_playwright(url: str, timeout: int = 45) -> str:
    """Headless-chromium fetch that defeats Cloudflare managed-challenge.

    Returns the rendered page body (still XML for /feed/ endpoints — WordPress
    serves the RSS directly once the CF JS challenge clears).
    """
    _pw, _browser, ctx = _get_playwright_ctx()
    page = ctx.new_page()
    try:
        resp = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        if resp is None or resp.status >= 400:
            raise RuntimeError(f"playwright fetch {url} status={resp.status if resp else 'None'}")
        body = page.content()
        return body
    finally:
        page.close()


def extract_rss_xml(rendered: str) -> str:
    """When chromium renders /feed/, it wraps the XML in HTML.

    The actual <rss>...</rss> document lives verbatim inside the wrapper —
    grab the first <rss ...>...</rss> block (or <feed ...>...</feed> for Atom).
    """
    for tag in ("rss", "feed"):
        # Greedy from first <rss to last </rss>
        m = re.search(rf"<{tag}\b.*?</{tag}>", rendered, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(0)
    # Fallback: assume already raw XML
    return rendered


# ---------------------------------------------------------------------------
# RSS parsing — extract title, link, guid, pubDate, description, image URLs
# ---------------------------------------------------------------------------

IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def parse_rss(xml_str: str) -> list[dict]:
    """Parse RSS 2.0 or Atom into a list of normalized item dicts."""
    items = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        log(f"  parse_rss: XML parse error: {e}")
        return items

    # RSS 2.0: //channel/item
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        pub_date = (it.findtext("pubDate") or "").strip()
        # description / content:encoded
        desc = (it.findtext("description") or "").strip()
        ns_content = it.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        if ns_content is not None and ns_content.text:
            desc_full = ns_content.text
        else:
            desc_full = desc
        images = list(set(IMG_SRC_RE.findall(desc_full)))
        items.append({
            "title": html.unescape(title),
            "link": link,
            "guid": guid,
            "pub_date": pub_date,
            "description": html.unescape(_strip_html(desc_full))[:3000],
            "raw_description_html": desc_full[:6000],
            "images": images,
        })

    # Atom fallback (only if no RSS items found)
    if not items:
        atom_ns = "{http://www.w3.org/2005/Atom}"
        for it in root.findall(f"{atom_ns}entry"):
            title = (it.findtext(f"{atom_ns}title") or "").strip()
            link_el = it.find(f"{atom_ns}link")
            link = link_el.get("href") if link_el is not None else ""
            guid = (it.findtext(f"{atom_ns}id") or link or title).strip()
            pub_date = (it.findtext(f"{atom_ns}updated") or it.findtext(f"{atom_ns}published") or "").strip()
            content_el = it.find(f"{atom_ns}content") or it.find(f"{atom_ns}summary")
            desc_full = (content_el.text if content_el is not None else "") or ""
            images = list(set(IMG_SRC_RE.findall(desc_full)))
            items.append({
                "title": html.unescape(title),
                "link": link,
                "guid": guid,
                "pub_date": pub_date,
                "description": html.unescape(_strip_html(desc_full))[:3000],
                "raw_description_html": desc_full[:6000],
                "images": images,
            })

    return items


def _strip_html(s: str) -> str:
    """Cheap HTML→text. Good enough for LLM input — keeps signal density up."""
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# LLM parser — OpenRouter / Gemini 2.5 Flash multimodal
# ---------------------------------------------------------------------------
# Delegate to structured_fares.build_prompt() so new fields (cabin, trip_type,
# ISO booking_deadline, travel_range, price_original_currency, price_original_amount,
# conditions) are first-class. Legacy fields remain accepted on read; backwards
# compatibility is verified by tests.test_structured_fares (prompt contract tests).
try:
    from structured_fares import build_prompt as _build_prompt
    PARSE_PROMPT = _build_prompt()
except ImportError:  # pragma: no cover — structured_fares is bundled in this repo
    PARSE_PROMPT = """You are extracting structured "bug fare" / mistake-fare data
from a deal post. Output a SINGLE JSON object — no prose, no markdown fence.

Schema:
{
  "is_fare": true|false,
  "route": "ORIG->DEST" or "ORIG->DEST->DEST2" (use IATA codes if visible, else city names),
  "dates": "free text travel window e.g. 'Sep 1-15, 2026' or 'fall 2026'",
  "price_twd": integer TWD (convert USD/EUR/JPY using rough rate USD=32 EUR=35 JPY=0.22) or null,
  "price_original": "raw price string from post e.g. '$903 USD' or 'NT$15,000'",
  "airline": "carrier name or null",
  "deadline": "booking-by date if mentioned, else null",
  "deal_url": "primary booking link if extractable, else null",
  "summary_zh": "ONE 繁體中文 sentence summarising the deal (<=80 chars)"
}

If the post is NOT a flight fare deal (visa news, hotel, generic blog), output:
{"is_fare": false}

Be concise. Use null (not empty string) for unknown fields."""


def call_llm(
    api_key: str,
    title: str,
    description: str,
    image_urls: list[str],
    timeout: int = 45,
) -> dict | None:
    """Send post content to OpenRouter Gemini 2.5 Flash, return parsed dict.

    Returns None on any parse failure (network, JSON, etc.) — caller treats
    None as 'unparseable, store dedup hash but skip alert'.
    """
    content_parts: list[dict] = [{
        "type": "text",
        "text": f"TITLE: {title}\n\nBODY:\n{description}",
    }]
    # Cap at 3 images to keep token cost predictable — bonbon-style carousels
    # rarely add new info past the first 3 frames.
    for url in image_urls[:3]:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": PARSE_PROMPT},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.1,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/tomsonleelee/FlightSearch",
            "X-Title": "FlightSearch bug-fare aggregator",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp_body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"  LLM HTTP {e.code}: {e.read()[:200].decode('utf-8', 'ignore')}")
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        log(f"  LLM error: {e}")
        return None

    try:
        text = resp_body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log(f"  LLM bad shape: {str(resp_body)[:300]}")
        return None

    # Some models still wrap JSON in ```json fences despite response_format
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"  LLM JSON decode failed: {e} | raw: {text[:200]!r}")
        return None

    if not isinstance(parsed, dict):
        return None
    if parsed.get("is_fare") is False:
        # Valid LLM verdict — store as {is_fare: false} so we don't re-process.
        return parsed
    return parsed


# ---------------------------------------------------------------------------
# Telegram digest
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    req = urllib.request.Request(
        url,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        log(f"  Telegram send failed: {e}")
        return False


def format_alert_line(source: str, parsed: dict, item: dict) -> str:
    """One-deal line in the digest. Falls back to raw title if LLM was weak."""
    emoji = SOURCES.get(source, {}).get("emoji", "🔥")
    parts = [f"{emoji} <b>{html.escape(_safe(parsed, 'route') or item['title'][:80])}</b>"]
    summary_zh = _safe(parsed, "summary_zh")
    if summary_zh:
        parts.append(html.escape(summary_zh))

    bits = []
    if v := _safe(parsed, "dates"):
        bits.append(f"📅 {html.escape(v)}")
    if v := _safe(parsed, "price_original"):
        bits.append(f"💰 {html.escape(v)}")
    elif (v := parsed.get("price_twd")) is not None:
        bits.append(f"💰 NT${v:,}")
    if v := _safe(parsed, "airline"):
        bits.append(f"✈ {html.escape(v)}")
    if v := _safe(parsed, "deadline"):
        bits.append(f"⏰ {html.escape(v)} 前")
    if bits:
        parts.append(" / ".join(bits))

    link = _safe(parsed, "deal_url") or item.get("link")
    if link:
        parts.append(f'<a href="{html.escape(link)}">原文連結</a>')
    return "\n".join(parts)


def _safe(d: dict | None, k: str) -> str | None:
    if not d:
        return None
    v = d.get(k)
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return str(v)


def build_digest(alerts: list[tuple[str, dict, dict]]) -> str:
    """alerts = [(source, parsed, raw_item), ...]"""
    n = len(alerts)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"🔥 <b>BUG fare 警報</b>（{n} 筆新發現 · {now}）"
    lines = [header, ""]
    for src, parsed, item in alerts:
        lines.append(format_alert_line(src, parsed, item))
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def compute_dedup_hash(source: str, item: dict) -> str:
    raw = f"{source}|{item.get('guid','')}|{item.get('pub_date','')}"
    return hashlib.sha256(raw.encode()).hexdigest()


SF_POST_LINK_RE = re.compile(
    r'<a[^>]+href="(https://www\.secretflying\.com/posts/[^"]+)"[^>]*title="([^"]+)"',
    re.IGNORECASE,
)
SF_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"', re.IGNORECASE)


def parse_secretflying_homepage(html_body: str) -> list[dict]:
    """Pull deal cards directly from the homepage HTML.

    Each card is an <a href="https://www.secretflying.com/posts/SLUG/"
    title="🔥 Route description for only €X roundtrip">. The title alone
    is sufficient signal for the LLM — no need to fetch each post body.
    """
    items: list[dict] = []
    seen_urls: set[str] = set()
    # Collect post links with titles
    for url, title in SF_POST_LINK_RE.findall(html_body):
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append({
            "title": html.unescape(title).strip(),
            "link": url,
            "guid": url,                # URL slug is stable + unique
            "pub_date": "",             # set below from datetime list, best-effort
            "description": html.unescape(title).strip(),
            "raw_description_html": "",
            "images": [],
        })
    # Best-effort: pair datetime stamps in document order with post cards.
    # Order is not guaranteed identical, but in practice WP themes interleave
    # <time> next to each <a>. If the count matches we trust it; otherwise
    # we leave pub_date blank (dedup_hash still works on guid).
    times = SF_TIME_RE.findall(html_body)
    if len(times) >= len(items):
        for it, ts in zip(items, times):
            it["pub_date"] = ts
    return items


def fetch_source(source_key: str, cfg: dict) -> list[dict]:
    """Return list of normalised items for one source. Empty on failure."""
    log(f"  fetching {source_key} ({cfg['url']}) via {cfg['fetcher']}")
    try:
        if cfg["fetcher"] == "curl":
            xml = fetch_curl(cfg["url"])
            items = parse_rss(xml)
        elif cfg["fetcher"] == "playwright":
            rendered = fetch_playwright(cfg["url"])
            xml = extract_rss_xml(rendered)
            items = parse_rss(xml)
        elif cfg["fetcher"] == "playwright_homepage":
            rendered = fetch_playwright(cfg["url"])
            items = parse_secretflying_homepage(rendered)
        else:
            log(f"  unknown fetcher: {cfg['fetcher']}")
            return []
    except Exception as e:
        log(f"  fetch failed for {source_key}: {e}")
        return []

    log(f"  {source_key}: parsed {len(items)} item(s)")
    return items


def run_tick(dry_run: bool = False, send_alert: bool = True) -> int:
    """One aggregator tick. Returns number of new alerts emitted."""
    log("=" * 60)
    log(f"fare_aggregator tick start (dry_run={dry_run}, send_alert={send_alert})")

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        log("FATAL: OPENROUTER_API_KEY missing from environment (.env)")
        return 0

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    conn = init_db(DB_PATH) if not dry_run else None
    new_alerts: list[tuple[str, dict, dict]] = []
    stats = {"sources": 0, "items_seen": 0, "items_new": 0, "parsed": 0, "fare": 0, "skipped_disabled": 0}

    try:
        for key, cfg in SOURCES.items():
            if not cfg.get("enabled", True):
                log(f"  {key}: SKIPPED — {cfg.get('disabled_reason','disabled')}")
                stats["skipped_disabled"] += 1
                continue
            stats["sources"] += 1
            items = fetch_source(key, cfg)
            for item in items:
                stats["items_seen"] += 1
                dh = compute_dedup_hash(key, item)
                if conn is not None and dedup_exists(conn, dh):
                    continue
                stats["items_new"] += 1
                log(f"  parsing: {item['title'][:80]}")
                parsed = call_llm(
                    api_key,
                    item["title"],
                    item["description"],
                    item["images"],
                )
                if parsed is not None:
                    stats["parsed"] += 1

                is_fare = bool(parsed and parsed.get("is_fare"))
                if is_fare:
                    stats["fare"] += 1
                    new_alerts.append((key, parsed, item))

                if not dry_run and conn is not None:
                    from structured_fares import insert_fare as insert_structured_fare
                    insert_structured_fare(
                        conn,
                        dedup_hash=dh,
                        source=key,
                        item_guid=item.get("guid"),
                        pub_date=item.get("pub_date"),
                        raw_title=item["title"],
                        raw_description=item["description"],
                        raw_image_urls=item["images"],
                        parsed=parsed,
                        alerted=is_fare and send_alert and bool(tg_token and tg_chat),
                    )

        # Telegram digest — collapse all new fares into 1 message
        if new_alerts and send_alert and not dry_run:
            if tg_token and tg_chat:
                digest = build_digest(new_alerts)
                # Telegram hard limit 4096 chars; chunk if needed
                for chunk in _chunk_message(digest, 3900):
                    ok = send_telegram(tg_token, tg_chat, chunk)
                    log(f"  Telegram digest send: {'OK' if ok else 'FAILED'} ({len(chunk)} chars)")
            else:
                log("  WARN: Telegram credentials missing — digest NOT sent")

    finally:
        if conn is not None:
            conn.close()
        _close_playwright_ctx()

    log(
        f"tick done | sources={stats['sources']} seen={stats['items_seen']} "
        f"new={stats['items_new']} parsed={stats['parsed']} fare={stats['fare']} "
        f"skipped_disabled={stats['skipped_disabled']} alerts_sent={len(new_alerts) if send_alert else 0}"
    )
    return len(new_alerts)


def _chunk_message(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    out, buf = [], ""
    for block in s.split("\n\n"):
        nxt = (buf + "\n\n" + block) if buf else block
        if len(nxt) > limit:
            if buf:
                out.append(buf)
            buf = block
        else:
            buf = nxt
    if buf:
        out.append(buf)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Bug-fare aggregator (FlightSearch phase 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + parse but do NOT write DB or send Telegram")
    parser.add_argument("--no-alert", action="store_true",
                        help="Write DB but skip Telegram (useful for first backfill)")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    try:
        new_count = run_tick(dry_run=args.dry_run, send_alert=not args.no_alert)
        return 0 if new_count >= 0 else 1
    except Exception as e:
        log(f"FATAL unhandled: {e}")
        log(traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(main())
