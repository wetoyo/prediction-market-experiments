"""Gateway to the Kalshi client code living in the prediction_market_scraper
submodule (Clients/Kalshi). That package uses bare same-directory imports
(e.g. `from live_datastream import ...` inside live_execution.py), so we add
its directory to sys.path rather than importing it as a proper package.
Mirrors ../resolution_alpha/live/kalshi_gateway.py's approach.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KALSHI_CLIENT_DIR = _REPO_ROOT / "prediction_market_scraper" / "Clients" / "Kalshi"

if str(_KALSHI_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_KALSHI_CLIENT_DIR))

from fetch_historical import BASE_URL, fetch_markets, fetch_orderbook, fetch_series, fetch_trades  # noqa: E402
from live_execution import KalshiTradingClient  # noqa: E402

import requests  # noqa: E402


def fetch_market(ticker: str) -> dict:
    """Single-market lookup (fetch_historical.py only has the plural,
    filtered/paginated fetch_markets). Public endpoint, no auth needed.
    """
    response = requests.get(f"{BASE_URL}/markets/{ticker}", timeout=20)
    response.raise_for_status()
    return response.json()["market"]


def fetch_margin_market(ticker: str) -> dict:
    """A perps (margin) market, e.g. KXBTCPERP: contract_size, tick_size and
    the live top-of-book `bid`/`ask`. Public endpoint, no auth needed. Uses
    `bid`/`ask` from here rather than /orderbook, whose levels came back
    best-last on 2026-10-21 despite the spec saying best-first.
    """
    response = requests.get(f"{BASE_URL}/margin/markets/{ticker}", timeout=20)
    response.raise_for_status()
    return response.json()["market"]


def fetch_margin_funding_estimate(ticker: str) -> dict:
    """The perp's in-progress funding period: `funding_rate` (fraction of
    notional, longs pay shorts when positive), `next_funding_time`. Public."""
    response = requests.get(f"{BASE_URL}/margin/funding_rates/estimate", params={"ticker": ticker}, timeout=20)
    response.raise_for_status()
    return response.json()


__all__ = [
    "fetch_series", "fetch_markets", "fetch_orderbook", "fetch_market", "fetch_margin_market",
    "fetch_margin_funding_estimate", "fetch_trades",
    "KalshiTradingClient",
]
