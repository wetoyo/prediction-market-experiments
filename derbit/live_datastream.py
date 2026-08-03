"""Websocket client for Deribit's live data streams.

Connects to wss://www.deribit.com/ws/api/v2 and streams real-time updates for specified instruments and channels.
Channels include order books, tickers, and trades.
"""

import asyncio
import json

import websockets

WS_URL = "wss://www.deribit.com/ws/api/v2"


async def stream_deribit(channels: list[str]):
    """Connects to Deribit WebSocket and yields incoming messages for subscribed channels."""
    async with websockets.connect(WS_URL) as ws:
        subscribe_payload = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "public/subscribe",
            "params": {"channels": channels},
        }
        await ws.send(json.dumps(subscribe_payload))

        async for message in ws:
            payload = json.loads(message)
            # The first message might be a subscription confirmation response
            yield payload


async def stream_instrument_updates(instruments: list[str], update_types: list[str] = None):
    """Convenience helper to construct channels and yield updates for list of instruments.

    Args:
        instruments: List of instrument names, e.g. ["BTC-27JUL26-58000-C"]
        update_types: List of suffix styles, e.g. ["book.100ms", "ticker.100ms", "trades.100ms"]
                      Defaults to ["ticker.100ms"]
    """
    if not update_types:
        update_types = ["ticker.100ms"]

    channels = []
    for inst in instruments:
        for ut in update_types:
            # Map type to channel prefix correctly
            if ut.startswith("book.") or ut.startswith("ticker.") or ut.startswith("trades."):
                # If fully qualified suffix is passed (e.g. 'book.100ms'), format as: {channel_prefix}.{instrument}.{suffix}
                # For Deribit, channel format is:
                #   - book.{instrument}.100ms
                #   - book.{instrument}.raw
                #   - ticker.{instrument}.100ms
                #   - trades.{instrument}.100ms
                parts = ut.split(".")
                prefix = parts[0]
                suffix = ".".join(parts[1:])
                channels.append(f"{prefix}.{inst}.{suffix}")
            else:
                # Fallback default configuration
                channels.append(f"{ut}.{inst}.100ms")

    async for update in stream_deribit(channels):
        yield update


if __name__ == "__main__":
    async def main():
        from fetch_historical import get_instruments

        print("Fetching a live instrument to test WebSocket streaming...")
        try:
            instruments = get_instruments(currency="BTC", kind="option")
            if not instruments:
                print("No instruments available. Exiting.")
                return

            test_inst = instruments[0]["instrument_name"]
            print(f"Subscribing to ticker updates for: {test_inst}")

            # Subscribing to ticker updates
            channels = [f"ticker.{test_inst}.100ms"]
            counter = 0

            async for update in stream_deribit(channels):
                # Print output
                print(json.dumps(update, indent=2))
                counter += 1
                if counter >= 3:  # Stop after receiving 3 updates (usually sub confirm + 2 ticks)
                    break

        except Exception as e:
            print("WebSocket connection test failed:", e)

    asyncio.run(main())
