"""Places (or, by default, simulates) an order on the favored side of a
market. Defaults to dry-run: real order placement requires both
BTC_IMPLIED_PROB_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).

Kalshi's order `side` field is always "bid" (buy YES) or "ask" (sell YES,
i.e. economically buy NO at 1 - price) -- "For event markets, this refers
to the YES leg only." This module's `side` param is the model's favored
side, "yes" or "no"; `_to_api_order` does the translation. Same convention
as ../resolution_alpha/live/order_manager.py, which validated it against a
real live order on 2026-08-06.
"""

import logging

from config import DRY_RUN
from kalshi_gateway import KalshiTradingClient

logger = logging.getLogger("btc_implied_prob.order_manager")


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
    """Translates a favored-side ("yes"/"no") + its quoted price into
    Kalshi's API terms: (api_side "bid"/"ask", price always in YES-dollar
    terms). Buying NO at price P == selling YES at (1 - P). Rounds to the
    cent -- Kalshi rejects prices that aren't tick-aligned to whole cents.
    """
    if side == "yes":
        return "bid", round(limit_price, 2)
    if side == "no":
        return "ask", round(1.0 - limit_price, 2)
    raise ValueError(f"unknown side {side!r}, expected 'yes' or 'no'")


class OrderManager:
    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run
        self._client = None
        if not dry_run:
            self._client = KalshiTradingClient()

    def get_balance_dollars(self) -> float:
        """Real account balance, for Kelly sizing (strategy.py's
        _kelly_contracts). Only call when dry_run is False -- there's no
        client to query otherwise; strategy.py uses
        config.DRY_RUN_SIMULATED_BALANCE_DOLLARS in that case instead.

        Kalshi's /portfolio/balance returns `balance` in cents (confirmed
        against a real account by
        ../../prediction_market_scraper/Clients/Kalshi/test_live_execution.py)
        -- NOT `balance_dollars`. ../resolution_alpha/live/order_manager.py's
        get_balance_dollars reads the latter key, which that endpoint doesn't
        actually return; this deliberately doesn't copy that.
        """
        return float(self._client.get_balance()["balance"]) / 100.0

    def get_positions(self) -> dict:
        """Real open positions (market_positions/event_positions), for
        positions_store.reconcile_with_kalshi. Only call when dry_run is
        False -- there's no client to query otherwise. Confirmed live
        2026-08-15: market_positions entries carry a signed `position_fp`
        (whole contracts, positive=YES held, negative=NO held, despite the
        "_fp" suffix -- not fixed-point/scaled, verified against
        total_traded_dollars / price for a real position) and
        `total_traded_dollars` (cumulative cost basis, usable as an average
        entry price when reconciling a position this process has no local
        record of).
        """
        return self._client.get_positions()

    def get_resting_orders(self, ticker: str) -> list[dict]:
        """Currently-resting (unfilled, not yet canceled) orders for a single
        ticker, for maintaining a take-profit resting order (see strategy.py's
        _maintain_take_profit_order). Confirmed live 2026-08-15 (place+list+
        cancel of a real 1-contract order, priced to never fill): each entry
        has `order_id`, `ticker`, `side` ("yes"/"no" -- this module's own
        convention, not the api "bid"/"ask" translation _to_api_order does),
        `remaining_count_fp`, `yes_price_dollars`/`no_price_dollars` (both
        present regardless of side, complementary: no_price = 1-yes_price).
        Cancel-to-listing has a couple seconds of lag -- don't assume a
        just-canceled order is already absent from the very next call.
        Empty in dry-run (no real orders to list).
        """
        if self.dry_run:
            return []
        return self._client.get_orders(status="resting", ticker=ticker).get("orders", [])

    def cancel_order(self, order_id: str) -> dict:
        """No-op in dry-run. Live behavior confirmed 2026-08-15."""
        if self.dry_run:
            return {"dry_run": True}
        return self._client.cancel_order(order_id)

    def buy_favored_side(self, ticker: str, side: str, contracts: float, limit_price: float) -> dict:
        """Places a marketable limit order on `side` ("yes"/"no") for
        `contracts` at `limit_price` (dollars, 0-1, quoted in that side's own
        terms). `limit_price` should be a tick-aligned quote already resting
        on the book (e.g. the ask you're crossing), not a blended average. In
        dry-run mode, only logs the intended trade and returns a synthetic
        response.
        """
        api_side, api_price = _to_api_order(side, limit_price)
        price_str = f"{api_price:.4f}"
        count_str = f"{contracts:.2f}"

        if self.dry_run:
            logger.info(
                "[DRY RUN] would BUY %s %s %s @ %s (api side=%s price=%s)",
                count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
            )
            return {"dry_run": True, "ticker": ticker, "side": side, "count": count_str, "price": price_str}

        logger.warning(
            "LIVE ORDER: BUY %s %s %s @ %s (api side=%s price=%s)",
            count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
        )
        return self._client.place_order(ticker=ticker, side=api_side, count=count_str, price=price_str)
