# Architecture and design decisions

## Why three separate modules

`derbit` splits cleanly along transport/protocol boundaries rather than by feature:

- **`fetch_historical.py`** talks HTTP, synchronously, to REST endpoints. It knows nothing about SQLite or WebSockets.
- **`live_datastream.py`** talks WebSockets, asynchronously. It also knows nothing about persistence.
- **`clean_historical.py`** knows nothing about the network at all — it only shapes dicts and writes them to SQLite.

This keeps the sync/async boundary contained: `fetch_historical` can be used from any plain script, while `live_datastream` requires an event loop. Persistence (`clean_historical`) is decoupled from both, so the same `save_order_books` function works whether the order book dict came from a REST call or (in principle) a WebSocket `book` message — nothing about it is REST-specific beyond the shape of the payload.

`__init__.py` re-exports everything flat at the package level (`from derbit import get_order_book, save_order_books, stream_deribit`) so callers don't need to know which submodule a given function lives in.

## Why the DB stores latest state, not a time series

`order_books` uses `instrument_name` as a primary key with `ON CONFLICT ... DO UPDATE`. Every call to `save_order_books` for a given instrument overwrites its row rather than appending a new one.

This is a deliberate simplification for a workspace focused on live snapshots (e.g. "what's the current order book for every active option") rather than time-series backtesting. If you need historical order book snapshots over time, don't fight this schema — either:

- Add a non-unique row per fetch (drop the `PRIMARY KEY` on `instrument_name`, add a fetch/observation id), or
- Rely on the fact that `raw_json` plus `fetched_at` gives you a full point-in-time snapshot per write, and persist those separately (e.g. one file/row per fetch batch) if you need to reconstruct history.

## Why `raw_json` is kept alongside flattened columns

`clean_order_book` only promotes a fixed subset of fields to columns — the ones useful for querying/filtering (`mark_price`, `delta`, `mark_iv`, etc.). Deribit's order book payload has more fields than that (full bid/ask depth arrays, `state`, `settlement_price`, and so on), and the set of fields Deribit returns can change over time.

Rather than adding a column per field or dropping unused data, the entire original payload is serialized into `raw_json` on every write. This means:

- Queries filtering/sorting on common fields (`mark_iv`, `delta`, ...) can use plain SQL against real columns.
- Nothing is ever lost — any field not promoted to a column is still recoverable by parsing `raw_json` (see [How to query the local database](../how-to-guides/query-the-database.md#recover-fields-that-werent-flattened-into-columns)).
- Schema changes (adding a new column) don't require backfilling — old rows already have the data in `raw_json`.

## Why REST is sync and streaming is async

Deribit's REST endpoints are simple request/response — there's no benefit to async here for a script that's fetching a handful of instruments sequentially, and keeping `fetch_historical` sync means it can be called from anywhere without an event loop.

WebSocket streaming is inherently long-lived and message-driven, which is a natural fit for `async for`. `live_datastream` uses async generators (`stream_deribit`, `stream_instrument_updates`) so callers can consume updates with a simple loop and control their own exit condition (message count, timeout, filter match) rather than the library imposing one.

## Data flow

```
                 ┌─────────────────────┐
   REST calls    │  fetch_historical.py │
  ───────────────▶  (sync, requests)    │
                 └──────────┬───────────┘
                            │ raw order book dict
                            ▼
                 ┌─────────────────────┐
                 │  clean_historical.py │──▶ Data/derbit.db (order_books table)
                 │  (flatten + upsert)  │
                 └─────────────────────┘

                 ┌─────────────────────┐
  WebSocket      │  live_datastream.py  │
  ───────────────▶  (async, websockets) │──▶ yielded to caller (not auto-persisted)
                 └─────────────────────┘
```

Note that `live_datastream` does not write to the database itself — streamed updates are handed back to the caller as plain dicts. If you want to persist live updates, pass them through `clean_historical.save_order_books` yourself (this works as long as the message shape matches what `clean_order_book` expects; Deribit's WebSocket `book`/`ticker` payloads differ slightly in shape from the REST `get_order_book` response, so check field names before wiring this up).
