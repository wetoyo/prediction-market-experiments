"""Gateway to the Kalshi client code living in the prediction_market_scraper
submodule (Clients/Kalshi). That package uses bare same-directory imports
(e.g. `from live_datastream import ...` inside live_execution.py), so we add
its directory to sys.path rather than importing it as a proper package.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KALSHI_CLIENT_DIR = _REPO_ROOT / "prediction_market_scraper" / "Clients" / "Kalshi"

if str(_KALSHI_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_KALSHI_CLIENT_DIR))

from fetch_historical import BASE_URL, fetch_markets, fetch_orderbook, fetch_series  # noqa: E402
from live_execution import KalshiTradingClient  # noqa: E402
from live_datastream import WS_URL, _auth_headers, _load_private_key  # noqa: E402

import requests  # noqa: E402


def fetch_market(ticker: str) -> dict:
    """Single-market lookup (fetch_historical.py only has the plural,
    filtered/paginated fetch_markets) -- used by check_resolutions.py to
    check one sampled ticker's settlement status at a time. Public endpoint,
    no auth needed.
    """
    response = requests.get(f"{BASE_URL}/markets/{ticker}", timeout=20)
    response.raise_for_status()
    return response.json()["market"]


__all__ = [
    "fetch_series", "fetch_markets", "fetch_orderbook", "fetch_market", "KalshiTradingClient",
    "WS_URL", "_auth_headers", "_load_private_key",
]
