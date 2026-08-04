# Polymarket client reference

Package: `Clients.Polymarket`. Base REST URL: `https://gamma-api.polymarket.com`.

## `fetch_historical.py`

REST client for Polymarket's public, unauthenticated Gamma API.

### `fetch_markets(active=True, closed=False, limit=100, max_pages=10) -> list[dict]`

Fetches markets via offset pagination, stopping when a page comes back
shorter than `limit` or `max_pages` is reached.

### `search_events(query: str, events_status="active", limit_per_type=20) -> list[dict]`

Free-text search over events (via `/public-search`), returning events with
their nested `markets` arrays. There is no direct free-text market search —
flatten `event["markets"]` across results to get individual markets.

## `clean_historical.py`

Normalization and SQLite persistence.

### `clean_market(market: dict) -> dict`

Maps a raw Gamma API market dict to the row shape stored in the `markets`
table. Derives `status` from the raw booleans: `closed=True` → `"closed"`;
else `active=True` → `"active"`; else `"inactive"`. Also captures
`clob_token_ids` (needed to stream that market's order book) and the full
`raw_json` payload.

### `get_connection(db_path=DEFAULT_DB_PATH) -> sqlite3.Connection`

Opens (creating parent directories and the schema if needed) a connection
to `Data/polymarket.db`.

### `save_markets(markets: list[dict], conn=None) -> int`

Cleans and upserts a batch of raw market dicts, keyed on `id`. Opens and
closes its own connection if none is passed. Returns the number of markets
processed.

## `live_datastream.py`

Websocket client for Polymarket's public CLOB market channel
(`wss://ws-subscriptions-clob.polymarket.com/ws/market`). No credentials
required.

### `stream_order_books(asset_ids: list[str])`

Async generator. Sends a `{"assets_ids": asset_ids, "type": "market"}`
subscription message, then yields each `book`/`price_change` event.
Polymarket occasionally sends a batch as a JSON list rather than a single
object — the generator flattens either shape into individual events.

## `live_execution.py`

Trading wrapper around Polymarket's CLOB — the trading side of the
Execution Loop. Order signing (EIP-712) and API-credential derivation are
delegated to the official
[`py-clob-client`](https://pypi.org/project/py-clob-client/) SDK rather
than hand-rolled, since a signing bug here risks real funds. It isn't in
any `requirements.txt` yet — install it separately (`pip install
py-clob-client`). Requires `POLYMARKET_PRIVATE_KEY`; see
[Configuration](configuration.md). See
[Execute live trades](../how-to/execute-live-trades.md) for usage.

> **Status:** implemented but untested against a real wallet — no
> Polymarket trading credentials are configured in this project yet.
> Verify behavior before relying on it.

### `PolymarketTradingClient(private_key=None, funder=None, signature_type=None)`

Reads `POLYMARKET_PRIVATE_KEY`/`POLYMARKET_FUNDER`/
`POLYMARKET_SIGNATURE_TYPE` from the environment when not passed
explicitly. On construction, derives (or creates) API credentials via
`create_or_derive_api_creds()` and sets `self.wallet_address` to `funder`
if given, else the key's own address. `funder`/`signature_type` are only
needed when trading through a Polymarket proxy/Safe wallet (email/Magic
login) rather than a raw EOA.

#### `place_order(token_id, price, size, side=BUY) -> dict`

Builds and signs an `OrderArgs` order, then posts it. `side` is `BUY` or
`SELL` from `py_clob_client.order_builder.constants`.

#### `cancel_order(order_id) -> dict`

#### `cancel_all_orders() -> dict`

#### `get_open_orders(market=None, asset_id=None) -> list[dict]`

Lists this account's resting orders, optionally filtered by condition id
(`market`) or token id (`asset_id`).

#### `get_balance() -> dict`

Available USDC collateral balance and allowance, via
`get_balance_allowance`.

#### `get_positions() -> list[dict]`

Fetches current positions for `self.wallet_address` from Polymarket's
public Data API (`data-api.polymarket.com/positions`) — the CLOB itself
has no notion of aggregate positions, only orders/trades.
