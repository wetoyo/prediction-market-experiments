# Reference: `live_datastream`

Async WebSocket client for Deribit's live data streams.

`WS_URL = "wss://www.deribit.com/ws/api/v2"`

Requires the `websockets` package.

## Functions

### `async stream_deribit(channels: list[str])`

Opens a WebSocket connection to `WS_URL`, sends a `public/subscribe` JSON-RPC request for the given `channels`, and yields every incoming message (parsed from JSON) as it arrives, including the initial subscription confirmation.

```python
async for message in stream_deribit(["ticker.BTC-27JUL26-58000-C.100ms"]):
    ...
```

The connection stays open for the lifetime of the `async for` loop; breaking out of the loop or letting the generator be garbage-collected closes it (via the `async with websockets.connect(...)` context manager).

### `async stream_instrument_updates(instruments: list[str], update_types: list[str] | None = None)`

Convenience wrapper that builds channel names from instrument/update-type pairs and delegates to `stream_deribit`.

- `instruments`: e.g. `["BTC-27JUL26-58000-C"]`
- `update_types`: e.g. `["book.100ms", "ticker.100ms", "trades.100ms"]`. Defaults to `["ticker.100ms"]` if omitted or empty.

For each `(instrument, update_type)` pair, if `update_type` starts with `book.`, `ticker.`, or `trades.`, the channel is built as `{prefix}.{instrument}.{suffix}` (e.g. `book.BTC-27JUL26-58000-C.100ms`). Any other `update_type` string falls back to `{update_type}.{instrument}.100ms`.

## Channel format reference

Deribit channel names this module targets:

| Pattern | Description |
|---|---|
| `book.{instrument}.100ms` | Order book updates, throttled to 100ms |
| `book.{instrument}.raw` | Order book updates, unthrottled |
| `ticker.{instrument}.100ms` | Ticker updates, throttled to 100ms |
| `trades.{instrument}.100ms` | Trade updates, throttled to 100ms |

## Self-test

Running `python live_datastream.py` directly fetches a live BTC option instrument via `fetch_historical.get_instruments`, subscribes to its `ticker.100ms` channel, prints the first 3 messages received (typically: subscription confirmation + 2 ticks), then exits.
