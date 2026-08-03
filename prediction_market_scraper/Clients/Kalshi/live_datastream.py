"""Websocket client for Kalshi's live order-book feed (the Execution Loop's fast path).

Every Kalshi websocket subscription -- even for public market data -- requires
a signed handshake. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (PEM RSA
key) in the environment to use this. Neither is available in this project yet,
so this module is wired up but untested against the live socket.
"""

import asyncio
import base64
import json
import os
import time

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

WS_HOST = "api.elections.kalshi.com"
WS_PATH = "/trade-api/ws/v2"
WS_URL = f"wss://{WS_HOST}{WS_PATH}"


def _load_private_key(private_key_path: str):
    with open(private_key_path, "rb") as key_file:
        return serialization.load_pem_private_key(key_file.read(), password=None)


def _sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _auth_headers(api_key_id: str, private_key) -> dict:
    timestamp_ms = str(int(time.time() * 1000))
    signature = _sign_request(private_key, timestamp_ms, "GET", WS_PATH)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


async def stream_orderbook(market_tickers: list[str]):
    """Yields orderbook_delta messages for the given market tickers."""
    api_key_id = os.environ["KALSHI_API_KEY_ID"]
    private_key = _load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    headers = _auth_headers(api_key_id, private_key)

    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": market_tickers},
        }))
        async for message in ws:
            yield json.loads(message)


if __name__ == "__main__":
    async def main():
        async for update in stream_orderbook(["KXFED-26JUL-T4.25"]):
            print(update)

    asyncio.run(main())
