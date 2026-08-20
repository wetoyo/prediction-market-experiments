"""Live market-data feed over Kalshi's authenticated websocket
(wss://api.elections.kalshi.com/trade-api/ws/v2), replacing REST polling as
the primary source for order books and (for BTC/ETH) the settlement index
itself. Schema confirmed against Kalshi's public docs on 2026-08-05
(docs.kalshi.com/websockets/*) and against a real authenticated connection
the same day (connects, signs the handshake, subscribes to both channels
below, and receives/parses real snapshot/delta/index messages). Falls back
to REST (orderbook.py callers hitting fetch_orderbook, spot_feed.py's
Coinbase poll) wherever this feed has no data yet, so the system degrades to
its previous fully-REST behavior rather than stalling if credentials are
absent or the socket is down.

Two channels:

  orderbook_delta -- per-market yes/no bid levels. An `orderbook_snapshot`
  message gives the full book on subscribe; `orderbook_delta` messages give
  signed quantity deltas per price level afterward. Reconstructed state is
  exposed via get_orderbook_fp(ticker) in the same {"yes_dollars": [[price,
  qty], ...], "no_dollars": [...]} shape fetch_orderbook() returns, so
  orderbook.walk_book works unmodified against either source. Subscribed
  market_tickers are kept in sync with the live discovery loop's active-market
  set via update_subscription add_markets/delete_markets (docs.kalshi.com/
  websockets/websocket-connection), not a fixed list at connect time.

  cfbenchmarks_value -- real-time ticks of the actual CF Benchmarks index
  Kalshi settles on (see ../README.md's "Index/oracle mechanics"), not a
  proxy. Only wired up for index ids confirmed against Kalshi's own contract
  rules text: BRTI (BTC) and ETHUSD_RTI (ETH). This is the fix for the
  "biggest open risk" flagged in ./README.md -- Coinbase-spot-as-proxy basis
  risk -- for the two assets it covers; every other underlying still relies
  on spot_feed.py's Coinbase proxy. Discovering additional index ids (SOL,
  XRP, DOGE, ...) needs the `indexlist` action against a live, authenticated
  connection, which hasn't been done here -- INDEX_ID_BY_UNDERLYING is only
  ever extended with confirmed ids, never guessed.

Both channels' history/latest-value accessors intentionally mirror
spot_feed.SpotFeed's (`latest`, `history`) so runner.py can pick whichever
source is available per-underlying without branching probability.py's model
inputs.
"""

import asyncio
import json
import logging
import time
from collections import deque

from config import SPOT_HISTORY_SECONDS
from kalshi_gateway import WS_URL, _auth_headers, _load_private_key
import os

import websockets

logger = logging.getLogger("resolution_alpha.ws_feed")

# Confirmed via live markets' rules_primary text (see ../README.md); never guessed.
INDEX_ID_BY_UNDERLYING = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
}

_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0


class KalshiWebsocketFeed:
    """Owns one websocket connection. Call `run()` as a long-lived asyncio
    task; query state from any coroutine on the same event loop via the
    accessor methods (no cross-thread locking -- everything here runs on one
    asyncio loop, so plain dict/deque mutation is safe).
    """

    def __init__(self):
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self._index_history: dict[str, deque] = {
            index_id: deque() for index_id in INDEX_ID_BY_UNDERLYING.values()
        }
        self._desired_tickers: set[str] = set()
        self._subscribed_tickers: set[str] = set()
        self._orderbook_sid: int | None = None
        self._orderbook_subscribe_sent = False
        self._connected = False
        self._credentials_missing = False
        self._next_cmd_id = 1
        self._ws = None

    # -- public accessors -------------------------------------------------

    def connected(self) -> bool:
        return self._connected

    def get_orderbook_fp(self, ticker: str) -> dict | None:
        book = self._books.get(ticker)
        if book is None:
            return None
        return {
            "yes_dollars": [[price, qty] for price, qty in sorted(book["yes"].items())],
            "no_dollars": [[price, qty] for price, qty in sorted(book["no"].items())],
        }

    def index_id_for(self, underlying: str) -> str | None:
        return INDEX_ID_BY_UNDERLYING.get(underlying)

    def latest_index_value(self, underlying: str) -> tuple[float, float] | None:
        index_id = self.index_id_for(underlying)
        if index_id is None:
            return None
        history = self._index_history.get(index_id)
        return history[-1] if history else None

    def index_history(self, underlying: str) -> list[tuple[float, float]]:
        index_id = self.index_id_for(underlying)
        if index_id is None:
            return []
        return list(self._index_history.get(index_id, ()))

    def set_desired_tickers(self, tickers: set[str]) -> None:
        """Called whenever discovery refreshes the active-market list. The
        actual add_markets/delete_markets calls are issued from the run()
        loop (needs the live websocket), so this just records intent.
        """
        self._desired_tickers = set(tickers)

    # -- connection lifecycle ----------------------------------------------

    async def run(self) -> None:
        backoff = _RECONNECT_BACKOFF_INITIAL
        while True:
            try:
                api_key_id = os.environ["KALSHI_API_KEY_ID"]
                private_key = _load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
            except KeyError:
                if not self._credentials_missing:
                    logger.warning(
                        "KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH not set -- websocket feed disabled, "
                        "falling back to REST polling for everything"
                    )
                    self._credentials_missing = True
                return  # nothing will make credentials appear later in this process; stop trying
            except Exception:
                # e.g. bad KALSHI_PRIVATE_KEY_PATH or unparseable PEM -- distinct from "not
                # configured" above, and worth surfacing loudly since it would otherwise die
                # silently (this coroutine is a fire-and-forget asyncio.create_task in
                # runner.py, so an uncaught exception here is never awaited/logged elsewhere).
                logger.exception("failed to load Kalshi credentials, websocket feed disabled")
                return

            try:
                await self._connect_and_listen(api_key_id, private_key)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("websocket feed disconnected, reconnecting in %.0fs", backoff)
            self._connected = False
            self._books.clear()
            self._subscribed_tickers.clear()
            self._orderbook_sid = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
            continue

    async def _connect_and_listen(self, api_key_id, private_key) -> None:
        headers = _auth_headers(api_key_id, private_key)
        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            self._connected = True
            logger.info("websocket connected")

            # orderbook_delta's subscribe is deferred to _subscription_sync_loop: Kalshi
            # rejects a subscribe with an empty market_tickers list ("Params required"),
            # and at connect time discovery.py may not have populated any tickers yet.
            self._orderbook_subscribe_sent = False
            index_ids = sorted(set(INDEX_ID_BY_UNDERLYING.values()))
            if index_ids:
                await self._send(ws, "subscribe", {"channels": ["cfbenchmarks_value"], "index_ids": index_ids})

            sync_task = asyncio.create_task(self._subscription_sync_loop(ws))
            try:
                async for raw in ws:
                    self._handle_message(json.loads(raw))
            finally:
                sync_task.cancel()

    async def _subscription_sync_loop(self, ws) -> None:
        """Every few seconds, diffs desired vs. subscribed tickers and issues
        update_subscription add/delete calls -- keeps the orderbook_delta
        subscription in step with discovery.py's active-market list without
        tearing down the whole connection. Also owns sending the *first*
        orderbook_delta subscribe, deferred until there's at least one
        desired ticker (see _connect_and_listen).
        """
        while True:
            if self._orderbook_sid is None:
                if self._desired_tickers and not self._orderbook_subscribe_sent:
                    await self._send(ws, "subscribe", {
                        "channels": ["orderbook_delta"], "market_tickers": sorted(self._desired_tickers),
                    })
                    self._orderbook_subscribe_sent = True
                await asyncio.sleep(2.0)
                continue

            to_add = self._desired_tickers - self._subscribed_tickers
            to_delete = self._subscribed_tickers - self._desired_tickers
            if to_add:
                await self._send(ws, "update_subscription", {
                    "sid": self._orderbook_sid, "action": "add_markets", "market_tickers": sorted(to_add),
                })
                self._subscribed_tickers |= to_add
            if to_delete:
                await self._send(ws, "update_subscription", {
                    "sid": self._orderbook_sid, "action": "delete_markets", "market_tickers": sorted(to_delete),
                })
                self._subscribed_tickers -= to_delete
                for ticker in to_delete:
                    self._books.pop(ticker, None)

            # Without this, an empty diff (the steady-state common case) loops
            # with no `await` inside the body -- a synchronous busy-spin that
            # starves the single-threaded event loop entirely (the message
            # receiver, runner.py's main loop, everything). This was the real
            # cause of a multi-minute live hang (2026-08-05), not the size of
            # the initial ticker batch as first suspected.
            await asyncio.sleep(5.0)

    async def _send(self, ws, cmd: str, params: dict) -> None:
        cmd_id = self._next_cmd_id
        self._next_cmd_id += 1
        await ws.send(json.dumps({"id": cmd_id, "cmd": cmd, "params": params}))

    # -- message handling ----------------------------------------------

    def _handle_message(self, message: dict) -> None:
        msg_type = message.get("type")
        if msg_type == "subscribed":
            channel = message.get("msg", {}).get("channel")
            sid = message.get("msg", {}).get("sid")
            if channel == "orderbook_delta":
                self._orderbook_sid = sid
            logger.info("subscribed: channel=%s sid=%s", channel, sid)
        elif msg_type == "orderbook_snapshot":
            self._apply_orderbook_snapshot(message.get("msg", {}))
        elif msg_type == "orderbook_delta":
            self._apply_orderbook_delta(message.get("msg", {}))
        elif msg_type == "cfbenchmarks_value":
            self._apply_cfbenchmarks_value(message.get("msg", {}))
        elif msg_type == "error":
            logger.warning("websocket error message: %s", message.get("msg"))

    def _apply_orderbook_snapshot(self, msg: dict) -> None:
        ticker = msg.get("market_ticker")
        if not ticker:
            return
        yes_levels = {float(p): float(q) for p, q in msg.get("yes_dollars_fp", [])}
        no_levels = {float(p): float(q) for p, q in msg.get("no_dollars_fp", [])}
        self._books[ticker] = {"yes": yes_levels, "no": no_levels}
        self._subscribed_tickers.add(ticker)

    def _apply_orderbook_delta(self, msg: dict) -> None:
        ticker = msg.get("market_ticker")
        side = msg.get("side")
        if not ticker or side not in ("yes", "no"):
            return
        try:
            price = float(msg["price_dollars"])
            delta = float(msg["delta_fp"])
        except (KeyError, TypeError, ValueError):
            return
        book = self._books.setdefault(ticker, {"yes": {}, "no": {}})
        levels = book[side]
        new_qty = levels.get(price, 0.0) + delta
        if new_qty <= 1e-9:
            levels.pop(price, None)
        else:
            levels[price] = new_qty

    def _apply_cfbenchmarks_value(self, msg: dict) -> None:
        index_id = msg.get("index_id")
        if index_id not in self._index_history:
            return
        try:
            raw = json.loads(msg["data"])
            value = float(raw["value"])
            ts_ms = float(raw.get("time", msg.get("received_at")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return

        now = time.time()
        history = self._index_history[index_id]
        history.append((ts_ms / 1000.0, value))
        cutoff = now - SPOT_HISTORY_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
