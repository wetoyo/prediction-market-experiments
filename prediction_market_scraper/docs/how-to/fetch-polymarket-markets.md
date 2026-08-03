# How to fetch and store Polymarket markets

Polymarket's Gamma API (`markets`, `events`, `public-search`) is public and
needs no authentication. Use `Clients.Polymarket` to pull markets and
persist them to `Data/polymarket.db`.

## Fetch active markets

```python
from Clients.Polymarket import fetch_markets, save_markets

markets = fetch_markets(active=True, closed=False)
written = save_markets(markets)
print(f"Wrote {written} markets")
```

`fetch_markets` pages through the API using offset pagination (100 markets
per page by default) until a short page signals the end, or `max_pages` is
hit.

## Search for markets by topic

Polymarket's listing endpoint doesn't support free-text search — use
`search_events` and flatten the nested markets:

```python
from Clients.Polymarket import search_events, save_markets

events = search_events("bitcoin price")
markets = [m for event in events for m in event.get("markets", [])]
written = save_markets(markets)
```

`events_status` defaults to `"active"`; pass `events_status="closed"` to
search resolved events instead.

## Run it as a script

`clean_historical.py` has a `__main__` block that searches for "bitcoin
price" and writes matching markets to `Data/polymarket.db`:

```bash
./venv/Scripts/python.exe -m Clients.Polymarket.clean_historical
```

## What gets stored

Each market is upserted into the `markets` table keyed by `id`. Status is
derived from the raw `closed`/`active` booleans into `"closed"`,
`"active"`, or `"inactive"`. `clob_token_ids` holds the JSON-encoded CLOB
token ids you'll need to stream that market's order book — see
[Stream live order books](stream-live-order-books.md). Full column list in
[Database schemas](../reference/database-schemas.md#polymarketdb).
