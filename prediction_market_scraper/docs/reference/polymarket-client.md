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
