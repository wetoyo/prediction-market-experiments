# Kalshi client reference

Package: `Clients.Kalshi`. Base REST URL: `https://api.elections.kalshi.com/trade-api/v2`.

## `fetch_historical.py`

REST client for Kalshi's public, unauthenticated market-listing endpoints.

### `fetch_series(category=None, limit=200) -> list[dict]`

Lists market series (e.g. `KXFED`, `KXBTCD`). Filters by `category` when
given. Single request, not paginated.

### `fetch_markets(series_ticker=None, status="open", limit=200, max_pages=10) -> list[dict]`

Fetches markets, following the response's `cursor` field until it's empty
or `max_pages` requests have been made. `status="open"` by default; pass
`None` to fetch all statuses.

## `clean_historical.py`

Normalization and SQLite persistence.

### `build_raw_title(market: dict) -> str`

Kalshi frequently puts the strike/threshold in `yes_sub_title` rather than
`title` (e.g. `title="Bitcoin price on Jul 19, 2026?"`,
`yes_sub_title="$72,000 or above"`). Concatenates both (skipping
`yes_sub_title` if it's already a substring of `title`) so downstream
parsing sees the full contract terms.

### `clean_market(market: dict) -> dict`

Maps a raw API market dict to the row shape stored in the `markets` table:
`ticker`, `event_ticker`, `raw_title` (via `build_raw_title`), `status`,
`close_time`, `raw_json` (the original payload, JSON-encoded).

### `get_connection(db_path=DEFAULT_DB_PATH) -> sqlite3.Connection`

Opens (creating parent directories and the schema if needed) a connection
to `Data/kalshi.db`.

### `save_markets(markets: list[dict], conn=None) -> int`

Cleans and upserts a batch of raw market dicts, keyed on `ticker`. Opens
and closes its own connection if none is passed. Returns the number of
markets processed.

## `live_datastream.py`

Websocket client for Kalshi's `orderbook_delta` channel
(`wss://api.elections.kalshi.com/trade-api/ws/v2`). Every subscription —
including public market data — requires a signed request; see
[Configuration](configuration.md) for the required environment variables
and [Stream live order books](../how-to/stream-live-order-books.md) for
usage.

### `stream_orderbook(market_tickers: list[str])`

Async generator. Signs the websocket handshake with the RSA private key at
`KALSHI_PRIVATE_KEY_PATH`, subscribes to `orderbook_delta` for the given
tickers, and yields each decoded JSON message as it arrives.

Internal helpers: `_load_private_key`, `_sign_request` (RSA-PSS/SHA-256 over
`{timestamp_ms}{method}{path}`), `_auth_headers` (assembles the
`KALSHI-ACCESS-*` headers).
