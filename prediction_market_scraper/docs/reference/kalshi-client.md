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

## `live_execution.py`

REST client for Kalshi's authenticated portfolio/order endpoints — the
trading side of the Execution Loop. Signs every request the same way as
`live_datastream.py`'s websocket handshake (it imports
`_load_private_key`/`_sign_request` from that module rather than
duplicating them). Requires `KALSHI_API_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH`; see [Configuration](configuration.md). See
[Execute live trades](../how-to/execute-live-trades.md) for usage.

> **Status:** verified against a real account — `get_balance`,
> `get_positions`, `get_orders`, `place_order`, and `cancel_order` have all
> been exercised live (order placement tested with a 1¢, 1-contract order
> that was placed and canceled). `live_datastream.py`'s websocket client is
> still untested, since that's a separate signing path.

### `KalshiTradingClient(api_key_id=None, private_key_path=None)`

Reads `KALSHI_API_KEY_ID`/`KALSHI_PRIVATE_KEY_PATH` from the environment
when not passed explicitly.

#### `get_balance() -> dict`

Available balance and portfolio value, in cents.

#### `get_positions(ticker=None, event_ticker=None) -> dict`

Open `market_positions`/`event_positions` (non-zero position or
total_traded), optionally filtered.

#### `get_orders(status=None, ticker=None) -> dict`

Lists orders. `status` filters to `"resting"`, `"canceled"`, or
`"executed"`.

#### `place_order(ticker, side, count, price, time_in_force="good_till_canceled", client_order_id=None) -> dict`

Submits a limit order via `POST /portfolio/events/orders` — the current,
non-deprecated order-creation endpoint (the legacy `POST
/portfolio/orders` is being phased out). `side` is `"bid"` or `"ask"`;
`count`/`price` are fixed-point dollar strings (e.g. `count="10.00"`,
`price="0.5600"`). Generates a random `client_order_id` (UUID4) if none is
given.

#### `cancel_order(order_id) -> dict`

`DELETE /portfolio/events/orders/{order_id}`.
