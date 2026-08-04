"""Smoke test for KalshiTradingClient against a real account -- exercises
only the read-only endpoints (get_balance, get_positions, get_orders).
Never calls place_order/cancel_order, since those move real money.

Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH to be set; see
docs/reference/configuration.md. Run with:

    ./venv/Scripts/python.exe -m Clients.Kalshi.test_live_execution
"""

from live_execution import KalshiTradingClient


def main():
    client = KalshiTradingClient()

    balance = client.get_balance()
    assert "balance" in balance, f"unexpected get_balance response: {balance}"
    print(f"get_balance OK -- balance: {balance['balance']} cents")

    positions = client.get_positions()
    assert "market_positions" in positions and "event_positions" in positions, (
        f"unexpected get_positions response: {positions}"
    )
    print(
        f"get_positions OK -- {len(positions['market_positions'])} market positions, "
        f"{len(positions['event_positions'])} event positions"
    )

    orders = client.get_orders(status="resting")
    assert "orders" in orders, f"unexpected get_orders response: {orders}"
    print(f"get_orders OK -- {len(orders['orders'])} resting orders")

    print("All read-only endpoints responded with expected shapes.")


if __name__ == "__main__":
    main()
