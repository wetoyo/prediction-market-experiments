"""Websocket client for Polymarket's live order-book feed (the Execution Loop's fast path).

The CLOB "market" channel is public -- no API credentials needed to stream
order-book updates for a given set of asset (CLOB token) ids.
"""

import asyncio
import json

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def stream_order_books(asset_ids: list[str]):
    """Yields book/price_change messages for the given CLOB token ids."""
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))
        async for message in ws:
            payload = json.loads(message)
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                yield event


if __name__ == "__main__":
    async def main():
        # Example token id for a live BTC threshold market -- swap in ids pulled
        # from fetch_historical.fetch_markets()'s clobTokenIds field.
        asset_ids = ["21742633143463906290569050155826241533067272736897614950488156847949938836455"]
        async for update in stream_order_books(asset_ids):
            print(update)

    asyncio.run(main())
