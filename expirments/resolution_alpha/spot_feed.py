"""Public, no-auth spot price feed used as a proxy for Kalshi's actual
settlement source (CF Benchmarks' Real Time Index, not freely available --
see the "Spot proxy" note in live/README.md for the basis risk this introduces).

Keeps a short rolling (timestamp, price) history per underlying symbol so
the probability model can estimate realized volatility and reconstruct the
realized portion of Kalshi's trailing settlement-average window.
"""

import threading
import time
from collections import deque

import requests

from config import KALSHI_UNDERLYING_TO_COINBASE_PRODUCT, SPOT_HISTORY_SECONDS

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{product_id}/spot"


class SpotFeed:
    """Polls Coinbase spot prices on demand and keeps a rolling history per
    underlying symbol for the configured window.
    """

    def __init__(self, history_seconds: float = SPOT_HISTORY_SECONDS):
        self._history_seconds = history_seconds
        self._history: dict[str, deque] = {}
        self._lock = threading.Lock()

    def poll(self, underlying: str) -> float | None:
        """Fetches the current spot price for `underlying` and records it.
        Returns None if the symbol has no known Coinbase product or the
        request fails -- callers should skip that market rather than crash,
        since this runs continuously for every discovered underlying.
        """
        product_id = KALSHI_UNDERLYING_TO_COINBASE_PRODUCT.get(underlying)
        if not product_id:
            return None
        try:
            response = requests.get(COINBASE_SPOT_URL.format(product_id=product_id), timeout=5)
            response.raise_for_status()
            price = float(response.json()["data"]["amount"])
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return None

        now = time.time()
        with self._lock:
            history = self._history.setdefault(underlying, deque())
            history.append((now, price))
            cutoff = now - self._history_seconds
            while history and history[0][0] < cutoff:
                history.popleft()
        return price

    def latest(self, underlying: str) -> tuple[float, float] | None:
        """Most recently recorded (timestamp, price) for `underlying`, with
        no network call. None if nothing has been polled yet.
        """
        with self._lock:
            history = self._history.get(underlying)
            return history[-1] if history else None

    def history(self, underlying: str) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._history.get(underlying, ()))
