"""Trading wrapper for Polymarket's CLOB (the Execution Loop's trading side)
-- placing/cancelling orders and viewing balance and open orders, plus
positions via Polymarket's public Data API (positions aren't a CLOB concept).

Order signing (EIP-712) and API-credential derivation are delegated to the
official py-clob-client SDK rather than hand-rolled, since a signing bug
here risks real funds. Set POLYMARKET_PRIVATE_KEY (0x-prefixed wallet key)
in the environment to use this. If you trade through a Polymarket proxy/Safe
wallet (email/Magic login) rather than a raw EOA, also set
POLYMARKET_FUNDER to that wallet's address and POLYMARKET_SIGNATURE_TYPE
(1 for email/Magic, 2 for browser wallet proxy) -- positions are read from
the funder address when set, since that's the wallet that actually holds
the funds.
"""

import os

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OpenOrderParams, OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

CLOB_HOST = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137


class PolymarketTradingClient:
    """Thin wrapper around py-clob-client for order execution plus a positions helper."""

    def __init__(
        self,
        private_key: str | None = None,
        funder: str | None = None,
        signature_type: int | None = None,
    ):
        private_key = private_key or os.environ["POLYMARKET_PRIVATE_KEY"]
        funder = funder or os.environ.get("POLYMARKET_FUNDER")
        if signature_type is None and (env_sig_type := os.environ.get("POLYMARKET_SIGNATURE_TYPE")):
            signature_type = int(env_sig_type)

        self.client = ClobClient(
            CLOB_HOST,
            chain_id=POLYGON_CHAIN_ID,
            key=private_key,
            funder=funder,
            signature_type=signature_type,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.wallet_address = funder or self.client.get_address()

    def place_order(self, token_id: str, price: float, size: float, side: str = BUY) -> dict:
        """Places a limit order. side is BUY or SELL from py_clob_client.order_builder.constants."""
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order)

    def cancel_order(self, order_id: str) -> dict:
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> dict:
        return self.client.cancel_all()

    def get_open_orders(self, market: str | None = None, asset_id: str | None = None) -> list[dict]:
        """Lists this account's resting orders, optionally filtered by condition id
        (market) or token id (asset_id).
        """
        params = OpenOrderParams(market=market, asset_id=asset_id) if (market or asset_id) else None
        return self.client.get_orders(params)

    def get_balance(self) -> dict:
        """Returns available USDC collateral balance and allowance."""
        return self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

    def get_positions(self) -> list[dict]:
        """Fetches current positions for this wallet via Polymarket's public Data
        API -- the CLOB itself has no notion of aggregate positions, only orders/trades.
        """
        response = requests.get(f"{DATA_API_URL}/positions", params={"user": self.wallet_address}, timeout=20)
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    client = PolymarketTradingClient()
    print("Wallet:", client.wallet_address)
    print("Balance:", client.get_balance())
    print("Positions:", client.get_positions())
    print("Open orders:", client.get_open_orders())
