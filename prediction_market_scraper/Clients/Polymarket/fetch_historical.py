"""REST client for Polymarket's public Gamma API.

These endpoints (markets/events/public-search) are read-only and require no
authentication.
"""

import json

import requests

BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


def fetch_markets(
    active: bool = True,
    closed: bool = False,
    limit: int = 100,
    max_pages: int = 10,
) -> list[dict]:
    """Fetches markets via offset pagination."""
    markets: list[dict] = []
    offset = 0

    for _ in range(max_pages):
        params = {"active": active, "closed": closed, "limit": limit, "offset": offset}
        response = requests.get(f"{BASE_URL}/markets", params=params, timeout=20)
        response.raise_for_status()
        page = response.json()

        markets.extend(page)
        if len(page) < limit:
            break
        offset += limit

    return markets


def search_events(query: str, events_status: str = "active", limit_per_type: int = 20) -> list[dict]:
    """Searches events (and their nested markets) by free-text query."""
    params = {"q": query, "events_status": events_status, "limit_per_type": limit_per_type}
    response = requests.get(f"{BASE_URL}/public-search", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("events", [])


def fetch_order_book(token_id: str) -> dict:
    """Fetches the current order book (bids/asks) for a single CLOB token."""
    response = requests.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id}, timeout=20)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    events = search_events("bitcoin price")
    print(f"Found {len(events)} events")
    for event in events[:5]:
        print(event["title"], "|", event.get("slug"))

    markets = [m for event in events for m in event.get("markets", [])]
    if markets:
        token_ids = json.loads(markets[0].get("clobTokenIds") or "[]")
        if token_ids:
            book = fetch_order_book(token_ids[0])
            print(f"Orderbook for token {token_ids[0]}: {book}")
