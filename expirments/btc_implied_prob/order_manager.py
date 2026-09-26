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

Per-runner bankroll (2026-09-26): with config.SIM_BANKROLL_ENABLED and a
live run, every order carries client_order_id "<ORDER_TAG>-<y|n>-<hex>" and
is tracked by a ../shared/tagged_ledger.py ledger until it's done, so this
runner can share the Kalshi account with the others. strategy.py starts its
background sync (start_ledger). See config.py's "Per-runner bankroll".
"""

import logging
import sys
from pathlib import Path

import config
import fees
from config import DRY_RUN
from kalshi_gateway import KalshiTradingClient

# sim_bankroll.py / tagged_ledger.py are shared by all three experiments.
_SHARED_DIR = str(Path(__file__).resolve().parents[1] / "shared")
if _SHARED_DIR not in sys.path:
    sys.path.append(_SHARED_DIR)
from tagged_ledger import TaggedLedger, real_balance_dollars  # noqa: E402

logger = logging.getLogger("btc_implied_prob.order_manager")


class OrderRefused(Exception):
    """The ledger refused an order (SIZE_FROM_SIM_BANKROLL on): it would
    spend more than this runner's available cash. Nothing was sent."""


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
    ledger: TaggedLedger | None = None  # class-level: test fakes built via __new__ have none

    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run
        self._client = None
        if not dry_run:
            self._client = KalshiTradingClient()
            if config.SIM_BANKROLL_ENABLED:
                self.ledger = TaggedLedger(
                    self._client,
                    order_tag=config.ORDER_TAG,
                    status_path=config.SIM_BANKROLL_STATUS_PATH,
                    divergence_path=config.SIM_BANKROLL_DIVERGENCE_LOG_PATH,
                    allocation_dollars=config.SIM_BANKROLL_ALLOCATION_DOLLARS,
                    allocation_fraction=config.SIM_BANKROLL_ALLOCATION_FRACTION,
                    size_from_sim=config.SIZE_FROM_SIM_BANKROLL,
                    shared_account=config.SIM_BANKROLL_SHARED_ACCOUNT,
                    tolerance_dollars=config.SIM_BANKROLL_TOLERANCE_DOLLARS,
                    allow_fresh_allocation=config.SIM_BANKROLL_ALLOW_FRESH_ALLOCATION,
                    env_prefix="BTC_IMPLIED_PROB_",
                    logger=logging.getLogger("btc_implied_prob.ledger"),
                )
            elif config.SIZE_FROM_SIM_BANKROLL:
                logger.warning(
                    "SIZE_FROM_SIM_BANKROLL is on but SIM_BANKROLL_ENABLED is off -- sizing off the real balance"
                )

    def start_ledger(self) -> None:
        """First ledger sync now, then every SIM_BANKROLL_SYNC_SECONDS in a
        background thread. Call once, before the first order."""
        if self.ledger is not None:
            self.ledger.start(config.SIM_BANKROLL_SYNC_SECONDS)

    def get_balance_dollars(self) -> float:
        """Bankroll for Kelly sizing (strategy.py's _kelly_contracts): the
        real account balance, or with SIZE_FROM_SIM_BANKROLL on, this
        runner's ledger cash capped at the real balance (0 in shared-account
        mode until the ledger has resumed). Only call when dry_run is False --
        there's no client to query otherwise; strategy.py uses
        config.DRY_RUN_SIMULATED_BALANCE_DOLLARS in that case instead.

        Reads `balance_dollars` (sub-cent precision, the unit the ledger
        books in), falling back to the cents `balance` field. Both are in
        the payload as of 2026-09-26; an older note here said
        `balance_dollars` wasn't, which is no longer true.
        """
        balance = real_balance_dollars(self._client.get_balance())
        if self.ledger is not None and self.ledger.size_from_sim:
            return self.ledger.sizing_cash(balance)
        return balance

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

        In shared-account mode the account's positions include other
        runners', so this returns only this runner's ledger positions (same
        shape, total_traded_dollars 0 -- the ledger keeps no cost basis).
        """
        if self.ledger is not None and self.ledger.shared_account:
            return {"market_positions": self.ledger.own_market_positions(), "event_positions": []}
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
        Empty in dry-run (no real orders to list). In shared-account mode,
        only this runner's own (tagged) orders -- strategy.py cancels
        whatever this returns.
        """
        if self.dry_run:
            return []
        orders = self._client.get_orders(status="resting", ticker=ticker).get("orders", [])
        if self.ledger is not None and self.ledger.shared_account:
            orders = [o for o in orders if self.ledger.is_own(o)]
        return orders

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

        With the ledger on, raises OrderRefused (nothing sent) when
        SIZE_FROM_SIM_BANKROLL is on and the order could cost more than the
        ledger's available cash; otherwise tags the order and books it.
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

        client_order_id = None
        if self.ledger is not None:
            worst_cost = contracts * limit_price + fees.estimate_fee_dollars(limit_price, contracts)
            allowed, why = self.ledger.order_allowed(ticker, side, contracts, worst_cost)
            if not allowed:
                logger.warning("NOT PLACED: BUY %s %s %s @ %.4f -- %s", count_str, side.upper(), ticker, limit_price, why)
                raise OrderRefused(why)
            client_order_id = self.ledger.new_client_order_id(side)

        logger.warning(
            "LIVE ORDER: BUY %s %s %s @ %s (api side=%s price=%s)",
            count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
        )
        response = self._client.place_order(
            ticker=ticker, side=api_side, count=count_str, price=price_str, client_order_id=client_order_id,
        )
        if self.ledger is not None:
            # The limit actually sent, in `side`'s own terms (after the cent rounding).
            self.ledger.record_order(ticker, side, contracts, api_price if side == "yes" else 1.0 - api_price, response)
        return response
