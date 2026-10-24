"""Gateway to the Kalshi client code living in the prediction_market_scraper
submodule (Clients/Kalshi). That package uses bare same-directory imports
(e.g. `from live_datastream import ...` inside live_execution.py), so we add
its directory to sys.path rather than importing it as a proper package.
Mirrors ../btc_implied_prob/kalshi_gateway.py and ../golf_field_alpha/kalshi_gateway.py.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KALSHI_CLIENT_DIR = _REPO_ROOT / "prediction_market_scraper" / "Clients" / "Kalshi"

if str(_KALSHI_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_KALSHI_CLIENT_DIR))

from fetch_historical import BASE_URL, fetch_markets, fetch_orderbook, fetch_series  # noqa: E402
import live_execution  # noqa: E402
from live_datastream import WS_URL, _auth_headers, _load_private_key  # noqa: E402

import requests  # noqa: E402


class KalshiTradingClient(live_execution.KalshiTradingClient):
    """live_execution.KalshiTradingClient over one pooled keep-alive HTTPS
    session (added 2026-10-24). The upstream _request calls bare
    `requests.request`, so every order POST paid a fresh DNS + TCP + TLS
    handshake to external-api.kalshi.com (~2-3 extra RTTs, plus the TLS
    crypto on the Pi 3) before Kalshi even saw it -- in the last seconds of
    a window, where the fill is a race. The bankroll refresh (GET
    /portfolio/balance every BANKROLL_REFRESH_INTERVAL_SECONDS) runs through
    this same session, so the connection stays warm between orders.
    requests.Session's urllib3 pool is thread-safe for the to_thread calls
    here (order placement and sync_sim_bankroll can overlap; each gets its
    own pooled connection). Same signing, URL, timeout and error shape as
    upstream -- only the transport changes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = requests.Session()

    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        headers = live_execution._auth_headers(
            self.api_key_id, self.private_key, method, f"{live_execution.API_PATH_PREFIX}{path}"
        )
        response = self._session.request(
            method, f"{live_execution.BASE_URL}{path}", headers=headers, params=params, json=json_body, timeout=20
        )
        if not response.ok:
            # Same body-preserving HTTPError as upstream (see live_execution._request).
            raise requests.exceptions.HTTPError(
                f"{response.status_code} {response.reason} for {method} {path}: {response.text[:600]}",
                response=response,
            )
        return response.json()


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
