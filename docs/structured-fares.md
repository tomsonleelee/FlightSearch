# Structured Fares Storage — additive normalization for `bug_fares`

Splits the opaque `parsed_json` blob into queryable, typed SQLite tables.
**Additive only** — `bug_fares` rows are never modified or deleted.

## Schema (additive, alongside `bug_fares`)

- `fares(source_hash PK→bug_fares, parse_state, parse_version, summary,
        airline, cabin, trip_type, booking_deadline, travel_date_start,
        travel_date_end, raw_dates, verification_state='unverified',
        inserted_at)`
- `fare_route_legs(fare_source_hash, leg_order, airport_code, location_name,
                   is_origin, is_destination)` — IATA-shaped only; ambiguous
  alternatives are NOT stored as serial legs.
- `fare_prices(fare_source_hash PK, original_text, original_currency,
               original_amount, approximate_twd)` — `original_currency` is
  NULL when currency is not explicit (e.g. `$903` ≠ USD).
- `fare_conditions(fare_source_hash PK, bag_included, refundable, notes)`

Indexes: `idx_fares_airline`, `idx_fares_trip_type`, `idx_fares_deadline`,
`idx_legs_airport`, `idx_prices_currency`. PK + FK constraints via
`source_hash → bug_fares.dedup_hash`.

`verification_state` is fixed at `'unverified'` — we do not pretend
3-letter tokens are real airports or that hand-extracted prices are
guaranteed accurate. Anything that needs verification should be flagged
downstream.

## Commands

Always pass `--db`. There is **no default** to `data/prices.db`.

### Backfill (idempotent, transactional)

    python3 structured_fares_cli.py migrate --db /path/to/prices.db

- Safe to re-run: backfills only `bug_fares` rows with no matching `fares`
  row, even when older structured rows already exist.
- Wrapped in a single `BEGIN…COMMIT`; any failure rolls everything back
  so `bug_fares` stays untouched.

### Read-only query

    python3 structured_fares_cli.py query \
        --db /path/to/prices.db \
        --origin TPE --destination NRT \
        --cabin business --currency USD --max-price 1500

- Refuses to mix currencies: passing `--currency USD` returns only
  USD-priced fares (no fallback to `approximate_twd`).
- `--max-price` is in the SAME currency as `--currency`. Without
  `--currency` it filters on whatever `original_currency` happens to be,
  which is rarely what you want — pass both together.

## Backup / rollback

**Before migrating**, copy the live DB:

    cp data/prices.db data/prices-$(date +%F-%H%M).bak.db

Migration only writes to new tables (`fares`, `fare_route_legs`,
`fare_prices`, `fare_conditions`) and never touches `bug_fares`. To roll
back completely:

    # Restore from backup (preferred — also reverts anything else that
    # ran between backup and now).
    cp data/prices-2026-09-24-1830.bak.db data/prices.db

    # Or drop ONLY the new tables (keeps any new rows added since the
    # migration). Note: `fares` rows added after the migration would be
    # orphaned here — restore-from-backup is safer.
    sqlite3 data/prices.db \
        "DROP TABLE IF EXISTS fare_conditions;
         DROP TABLE IF EXISTS fare_prices;
         DROP TABLE IF EXISTS fare_route_legs;
         DROP TABLE IF EXISTS fares;"

**Caveats:**

1. **Atomicity on insert path.** `structured_fares.insert_fare` wraps
   `bug_fares` + structured writes in a single transaction. If the
   structured side fails (e.g. parse error), `bug_fares` is rolled back
   too. No half-broken rows.
2. **Aggregator's own insert path is UNTOUCHED.** `fare_aggregator.py`'s
   `run_tick` now routes through `structured_fares.insert_fare` so the
   atomic guarantee applies in production. If the new module is removed,
   revert `fare_aggregator.py` to call `insert_fare` directly (the old
   `fare_aggregator.insert_fare` function is unchanged and still works).
3. **`--max-price` interpretation.** It applies to
   `original_amount` in the row's `original_currency`. Do NOT use it to
   filter by approximate `twd` — those are tracked separately and the
   query CLI never mixes currencies.
4. **Route alternatives are intentionally dropped from legs.** Strings
   like `LAX/SFO->TPE` or `TPE, HKG to JFK` are flagged ambiguous and
   produce zero `fare_route_legs` rows. The raw text stays in
   `bug_fares.raw_description` for downstream inspection.

## Tests

    python3 -m unittest tests.test_structured_fares -v

41 tests covering: schema migration idempotency, rollback, raw
preservation, parse states (fare / non_fare / malformed_json /
missing_json), route legs (codes vs names, alternatives, comma lists),
price currency / `$-is-not-USD`, date validation (ISO only),
`insert_fare` atomicity, query filters, and CLI smoke.

## Future LLM prompt

`structured_fares.build_prompt()` returns the new extraction prompt with
explicit `cabin`, `trip_type`, `booking_deadline` (ISO), `travel_range`,
`price_original_currency`, `price_original_amount`, `approximate_twd`,
`conditions`. The legacy fields (`dates`, `price_twd`, `price_original`,
`airline`, `summary_zh`, `deal_url`) are still listed so old JSON
parsed by the older prompt keeps parsing correctly — both shapes
feed the same `fares` table.

Drop the prompt into `PARSE_PROMPT` in `fare_aggregator.py` whenever the
LLM should produce the richer schema. The storage code does not care
which prompt produced the JSON.

## Telegram notifications are origin-filtered

Live ticks only emit a Telegram digest when the parsed route's **origin
airport** is in `TPE_REACHABLE_CODES` — i.e. a non-stop flight from
Taoyuan (TPE) within roughly 3.5 hours, plus TSA and KHH. TPE itself is
always in. Filtered items still land in `bug_fares` / `fares`, but
`alerted_at` stays null and the digest line is suppressed (the item is
counted as `fare_filtered` in the tick log).

For items that pass the filter from a non-TPE origin, the digest line
appends `ℹ️ 出發地 ICN：需另買 TPE 接駁票`. Edit
`TPE_REACHABLE_CODES` in `fare_aggregator.py` to widen or tighten the
allowed origins.