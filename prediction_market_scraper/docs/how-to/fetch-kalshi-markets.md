# How to fetch and store Kalshi markets

Kalshi's series/markets listing endpoints are public and need no
authentication. Use `Clients.Kalshi` to pull markets and persist them to
`Data/kalshi.db`.

## Fetch markets for a specific series

```python
from Clients.Kalshi import fetch_markets, save_markets

fed_markets = fetch_markets(series_ticker="KXFED")
written = save_markets(fed_markets)
print(f"Wrote {written} markets")
```

`fetch_markets` follows Kalshi's pagination cursor until it runs out of
pages or hits `max_pages` (default 10, 200 markets per page). Pass
`status=None` to include closed/settled markets as well as open ones.

## Discover available series

If you don't know a series ticker, list series by category first:

```python
from Clients.Kalshi import fetch_series

crypto_series = fetch_series(category="Crypto")
for series in crypto_series:
    print(series["ticker"], series["title"])
```

## Run it as a script

`clean_historical.py` has a `__main__` block that fetches the `KXFED`
series and writes it to `Data/kalshi.db`:

```bash
./venv/Scripts/python.exe -m Clients.Kalshi.clean_historical
```

Edit the `series_ticker` argument in that file's `if __name__ ==
"__main__":` block to pull a different series.

## What gets stored

Each market is upserted into the `markets` table keyed by `ticker`. See
[Database schemas](../reference/database-schemas.md#kalshidb) for the full
column list, and
[Kalshi client reference](../reference/kalshi-client.md#build_raw_title)
for why `raw_title` concatenates `title` and `yes_sub_title`.

Calling `save_markets` again with the same tickers updates the existing
rows (`status`, `close_time`, `raw_json`, `fetched_at`) instead of
duplicating them — safe to re-run on a schedule.
