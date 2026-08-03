# How to stream live order books

Both platforms expose a websocket feed for live order-book updates. These
are the "fast path" described in
[System architecture](../explanation/architecture.md) — no LLM calls or
database matching happens in this loop, only ID-based streaming.

## Polymarket (no auth required)

The CLOB `market` channel is public. Pass it the CLOB token (asset) ids you
want to watch — these come from the `clob_token_ids` column populated by
[fetching Polymarket markets](fetch-polymarket-markets.md):

```python
import asyncio
from Clients.Polymarket.live_datastream import stream_order_books

async def main():
    asset_ids = ["21742633143463906290569050155826241533067272736897614950488156847949938836455"]
    async for update in stream_order_books(asset_ids):
        print(update)

asyncio.run(main())
```

Each yielded item is a single `book` or `price_change` event (the client
flattens Polymarket's occasional list-of-events payloads for you).

## Kalshi (requires a signed handshake)

> **Status:** wired up but untested against the live socket — Kalshi API
> credentials are not yet available in this project. Verify behavior before
> relying on it.

Every Kalshi websocket subscription, even for public market data, requires
request signing with an RSA private key. Before streaming:

1. Generate an API key in your Kalshi account and download its private key
   (PEM, RSA).
2. Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` — see
   [Configuration](../reference/configuration.md).

```python
import asyncio
from Clients.Kalshi.live_datastream import stream_orderbook

async def main():
    async for update in stream_orderbook(["KXFED-26JUL-T4.25"]):
        print(update)

asyncio.run(main())
```

Under the hood, `stream_orderbook` signs a `GET /trade-api/ws/v2` request
with `RSA-PSS`/SHA-256, attaches the `KALSHI-ACCESS-*` headers, and
subscribes to the `orderbook_delta` channel for the given tickers.
