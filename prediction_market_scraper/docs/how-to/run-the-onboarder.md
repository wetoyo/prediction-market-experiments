# How to match markets across platforms with the UME

The Universal Market Embedder (UME) turns a raw market title from either
platform into a structured record, and links it to the same `event_id` as
any equivalent contract already onboarded from the other platform. See
[The two-stage matching algorithm](../explanation/matching-algorithm.md)
for how the match decision is made.

Prerequisites: `GROQ_API_KEY` set (see
[Configuration](../reference/configuration.md)), and the UME's
dependencies installed (`pip install -r
Clients/Utils/universal_market_embedder/requirements.txt`).

## Onboard a single market

```python
from Clients.Utils.universal_market_embedder import database, onboard_market

conn = database.get_connection()
database.init_db(conn)

event_id, parsed = onboard_market(
    conn,
    market_id="KALSHI-FED-25BPS-SEP26",
    platform="kalshi",
    raw_title="Will the Fed raise rates by 25 basis points in September 2026?",
)
print(parsed.model_dump_json(indent=2))
print(event_id)

conn.close()
```

`onboard_market`:

1. Parses `raw_title` into a `MarketSchema` via Groq.
2. Embeds the extracted `underlying_asset` with a local sentence-transformer.
3. Looks for an existing event that matches (exact fingerprint, or
   soft+hard match against every stored event) and reuses its `event_id`,
   or creates a new event if none matches.
4. Upserts the market row under that `event_id`.

Call it once per raw title, from either platform, in any order — the
matcher doesn't care which platform "arrives first."

## Onboard everything you've fetched

Combine this with the fetch/store steps from
[Fetch Kalshi markets](fetch-kalshi-markets.md) and
[Fetch Polymarket markets](fetch-polymarket-markets.md): iterate the rows
in `Data/kalshi.db` / `Data/polymarket.db` and call `onboard_market` for
each `raw_title`, tagging `platform` accordingly.

```python
import sqlite3
from Clients.Utils.universal_market_embedder import database, onboard_market

ume_conn = database.get_connection()
database.init_db(ume_conn)

kalshi_conn = sqlite3.connect("Data/kalshi.db")
for ticker, raw_title in kalshi_conn.execute("SELECT ticker, raw_title FROM markets"):
    onboard_market(ume_conn, market_id=ticker, platform="kalshi", raw_title=raw_title)
```

Do the equivalent for `Data/polymarket.db` (`id`, `raw_title` columns).
Note this calls Groq once per market — mind Groq's free-tier rate limits
(30 requests/minute) if onboarding a large batch. See
[System architecture](../explanation/architecture.md) for why this is
meant to run as a slow, offline batch job rather than inline with trading.

## Inspect matched events

```python
conn = database.get_connection()
for event in database.get_all_events(conn):
    markets = database.get_markets_by_event(conn, event["id"])
    if len(markets) > 1:
        print(event["event_fingerprint"], [m["platform"] for m in markets])
```

## Run the built-in demo

```bash
./venv/Scripts/python.exe -m Clients.Utils.universal_market_embedder.onboarder
```

This onboards one hardcoded Kalshi title and one hardcoded Polymarket
title describing the same Fed decision, and prints whether they matched.
