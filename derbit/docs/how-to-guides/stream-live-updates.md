# How to stream live updates over WebSocket

**Goal:** receive a continuous feed of ticker, order book, or trade updates for one or more instruments instead of polling the REST API.

## Steps

1. Pick the instruments and update types you want. Supported update type prefixes are `ticker`, `book`, and `trades`, each with a `.100ms` or (for `book`) `.raw` suffix:

   ```python
   import asyncio
   from live_datastream import stream_instrument_updates

   instruments = ["BTC-27JUL26-58000-C"]
   ```

2. Iterate the async generator to receive messages as they arrive:

   ```python
   async def main():
       async for update in stream_instrument_updates(instruments, update_types=["ticker.100ms"]):
           print(update)

   asyncio.run(main())
   ```

3. The first message received is Deribit's subscription confirmation, not a data update — handle both cases if you're branching on message shape:

   ```python
   async def main():
       async for update in stream_instrument_updates(instruments, update_types=["ticker.100ms"]):
           if "params" in update and "data" in update["params"]:
               tick = update["params"]["data"]
               print(tick["instrument_name"], tick["mark_price"])
           else:
               print("non-data message:", update)
   ```

## Subscribing to multiple channels per instrument

Pass multiple `update_types` to subscribe to several channels for the same instruments in one connection:

```python
async for update in stream_instrument_updates(
    instruments,
    update_types=["ticker.100ms", "book.100ms", "trades.100ms"],
):
    ...
```

Each combination of instrument × update type becomes its own subscribed channel (e.g. `ticker.BTC-27JUL26-58000-C.100ms`, `book.BTC-27JUL26-58000-C.100ms`).

## Using the lower-level `stream_deribit`

If you already know the exact Deribit channel names you want (rather than building them from instrument/update-type pairs), subscribe directly:

```python
from live_datastream import stream_deribit

channels = ["ticker.BTC-27JUL26-58000-C.100ms", "trades.BTC.100ms"]

async for update in stream_deribit(channels):
    print(update)
```

## Stopping the stream

Both generators run until the connection closes or you `break` out of the `async for` loop — there's no built-in message limit, so guard long-running consumers with your own stop condition (message count, timeout, `asyncio.wait_for`, etc.).

## Related

- [`live_datastream` reference](../reference/live_datastream.md)
- [Fetch and store order books for a currency](fetch-and-store-order-books.md) — for periodic snapshots instead of a live feed
