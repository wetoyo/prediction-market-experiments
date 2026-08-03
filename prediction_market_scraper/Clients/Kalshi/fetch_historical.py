"""REST client for Kalshi's public trade-api v2 market listing endpoints.

These endpoints (series/markets) are read-only and require no authentication.
"""

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def fetch_series(category: str | None = None, limit: int = 200) -> list[dict]:
    """Lists market series (e.g. KXFED, KXBTCD), optionally filtered by category."""
    params = {"limit": limit}
    if category:
        params["category"] = category
    response = requests.get(f"{BASE_URL}/series", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("series", [])


def fetch_markets(
    series_ticker: str | None = None,
    status: str | None = "open",
    limit: int = 200,
    max_pages: int = 10,
) -> list[dict]:
    """Fetches markets, following the cursor until exhausted or max_pages is hit."""
    markets: list[dict] = []
    cursor = None

    for _ in range(max_pages):
        params = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor

        response = requests.get(f"{BASE_URL}/markets", params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()

        markets.extend(payload.get("markets", []))
        cursor = payload.get("cursor")
        if not cursor:
            break

    return markets


def fetch_orderbook(ticker: str, depth: int | None = None) -> dict:
    """Fetches the current order book (yes/no price levels) for a single market."""
    params = {}
    if depth:
        params["depth"] = depth
    response = requests.get(f"{BASE_URL}/markets/{ticker}/orderbook", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("orderbook_fp", {})


if __name__ == "__main__":
    fed_markets = fetch_markets(series_ticker="KXFED")
    print(f"Fetched {len(fed_markets)} KXFED markets")
    for market in fed_markets[:5]:
        print(market["ticker"], "|", market["title"])

    if fed_markets:
        orderbook = fetch_orderbook(fed_markets[0]["ticker"])
        print(f"Orderbook for {fed_markets[0]['ticker']}: {orderbook}")
