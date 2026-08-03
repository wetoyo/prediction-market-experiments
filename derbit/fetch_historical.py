"""REST client for Deribit's public API.

All functions communicate with Deribit's public v2 REST API (https://www.deribit.com/api/v2).
No authentication is required for these public endpoints.
"""

import requests

BASE_URL = "https://www.deribit.com/api/v2"


def get_index_price(index_name: str = "btc_usd") -> dict:
    """Retrieves the current index price for the specified index."""
    params = {"index_name": index_name}
    response = requests.get(f"{BASE_URL}/public/get_index_price", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_instruments(currency: str = "BTC", kind: str = "option", expired: bool = False) -> list[dict]:
    """Retrieves all active or expired instruments for a given currency and kind."""
    params = {"currency": currency, "kind": kind, "expired": str(expired).lower()}
    response = requests.get(f"{BASE_URL}/public/get_instruments", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_order_book(instrument_name: str) -> dict:
    """Retrieves the order book for a given instrument."""
    params = {"instrument_name": instrument_name}
    response = requests.get(f"{BASE_URL}/public/get_order_book", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_book_summary_by_currency(currency: str = "BTC", kind: str = "option") -> list[dict]:
    """Retrieves the book summary for all instruments of a given currency."""
    params = {"currency": currency, "kind": kind}
    response = requests.get(f"{BASE_URL}/public/get_book_summary_by_currency", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_book_summary_by_instrument(instrument_name: str) -> list[dict]:
    """Retrieves the book summary for a single instrument."""
    params = {"instrument_name": instrument_name}
    response = requests.get(f"{BASE_URL}/public/get_book_summary_by_instrument", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_ticker(instrument_name: str) -> dict:
    """Retrieves the ticker info for a single instrument."""
    params = {"instrument_name": instrument_name}
    response = requests.get(f"{BASE_URL}/public/ticker", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_last_trades_by_instrument(instrument_name: str, count: int = 10) -> dict:
    """Retrieves the last trades for a given instrument."""
    params = {"instrument_name": instrument_name, "count": count}
    response = requests.get(f"{BASE_URL}/public/get_last_trades_by_instrument", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_last_trades_by_currency(currency: str = "BTC", count: int = 10) -> dict:
    """Retrieves the last trades for all instruments of a given currency."""
    params = {"currency": currency, "count": count}
    response = requests.get(f"{BASE_URL}/public/get_last_trades_by_currency", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_funding_rate_value(instrument_name: str, start_timestamp: int, end_timestamp: int) -> float:
    """Retrieves funding rate value for a given instrument between start and end timestamps."""
    params = {
        "instrument_name": instrument_name,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
    }
    response = requests.get(f"{BASE_URL}/public/get_funding_rate_value", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", 0.0)


def get_index(index_name: str = "btc_usd") -> dict:
    """Retrieves current values and constituents of an index."""
    params = {"index_name": index_name}
    response = requests.get(f"{BASE_URL}/public/get_index", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_delivery_prices(index_name: str = "btc_usd") -> dict:
    """Retrieves historical settlement/delivery prices for an index."""
    params = {"index_name": index_name}
    response = requests.get(f"{BASE_URL}/public/get_delivery_prices", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_historical_volatility(currency: str = "BTC") -> list:
    """Retrieves historical realized volatility for a currency."""
    params = {"currency": currency}
    response = requests.get(f"{BASE_URL}/public/get_historical_volatility", params=params, timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_time() -> int:
    """Retrieves exchange system time (milliseconds since epoch)."""
    response = requests.get(f"{BASE_URL}/public/get_time", timeout=20)
    response.raise_for_status()
    return response.json().get("result", 0)


def test() -> dict:
    """Checks connection and returns response from the exchange test endpoint."""
    response = requests.get(f"{BASE_URL}/public/test", timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def status() -> dict:
    """Checks the overall status of the exchange."""
    response = requests.get(f"{BASE_URL}/public/status", timeout=20)
    response.raise_for_status()
    return response.json().get("result", {})


def get_supported_index_names() -> list[str]:
    """Retrieves supported index names."""
    response = requests.get(f"{BASE_URL}/public/get_supported_index_names", timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_trade_volumes() -> list[dict]:
    """Retrieves 24h trade volumes for all supported currencies."""
    response = requests.get(f"{BASE_URL}/public/get_trade_volumes", timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


def get_currencies() -> list[dict]:
    """Retrieves metadata of all currencies supported by Deribit."""
    response = requests.get(f"{BASE_URL}/public/get_currencies", timeout=20)
    response.raise_for_status()
    return response.json().get("result", [])


if __name__ == "__main__":
    print("Testing get_index_price...")
    try:
        print("BTC Index Price:", get_index_price("btc_usd"))
        print("Instruments count (BTC option):", len(get_instruments("BTC", "option")))
        print("Historical Volatility count:", len(get_historical_volatility("BTC")))
    except Exception as e:
        print("Error during test run:", e)
