"""Places (or, by default, simulates) YES orders on golf player markets.
Defaults to dry-run: real order placement requires both
GOLF_FIELD_ALPHA_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).

Kalshi's order `side` field is always "bid" (buy YES) or "ask" (sell YES,
i.e. economically buy NO at 1 - price) -- "For event markets, this refers
to the YES leg only." This strategy only ever buys YES, so it always sends
`side="bid"`. `_to_api_order` is kept in the same shape as the crypto
experiments' order managers (which validated the yes/no <-> bid/ask
translation against a real live order on 2026-08-06) in case a NO leg is
ever added.

`limit_price` should be a tick-aligned price already resting on the book
(the `yes_ask` being crossed), not a blended average -- Kalshi rejects
prices that aren't whole-cent aligned (`price_level_structure:
"linear_cent"`). This module does NOT walk order-book depth: a basket leg
is a small marketable limit order at the displayed ask, and a partial fill
is left partial (same simplification as ../btc_implied_prob/order_manager.py;
golf player books are thin, so real slippage/partial-fill behaviour is a
known unmodelled risk -- see ../README.md Limitations).

Per-runner bankroll (2026-09-26): with config.SIM_BANKROLL_ENABLED and a
live run, every order carries client_order_id "<ORDER_TAG>-<y|n>-<hex>" and
is tracked by a ../shared/tagged_ledger.py ledger until it's done -- a
partly filled leg's remainder rests and can fill later -- so this runner can
share the Kalshi account with the others. strategy.py starts its background
sync (start_ledger). See config.py's "Per-runner bankroll".
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

logger = logging.getLogger("golf_field_alpha.order_manager")


class OrderRefused(Exception):
    """The ledger refused an order (SIZE_FROM_SIM_BANKROLL on): it would
    spend more than this runner's available cash. Nothing was sent."""


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
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
                    env_prefix="GOLF_FIELD_ALPHA_",
                    logger=logging.getLogger("golf_field_alpha.ledger"),
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
        """Bankroll for Kelly sizing (selection.plan_basket): the real
        account balance, or with SIZE_FROM_SIM_BANKROLL on, this runner's
        ledger cash capped at the real balance (0 in shared-account mode
        until the ledger has resumed). Only call when dry_run is False.

        Reads `balance_dollars` (sub-cent precision, the unit the ledger
        books in), falling back to the cents `balance` field. Both are in
        the payload as of 2026-09-26; an older note here said
        `balance_dollars` wasn't, which is no longer true.
        """
        balance = real_balance_dollars(self._client.get_balance())
        if self.ledger is not None and self.ledger.size_from_sim:
            return self.ledger.sizing_cash(balance)
        return balance

    def spendable_dollars(self) -> float | None:
        """What a new basket may cost in total, when the ledger limits it
        (SIZE_FROM_SIM_BANKROLL on): its available cash, 0 before it has
        initialized. None when nothing but the exchange limits it."""
        if self.ledger is None or not self.ledger.size_from_sim:
            return None
        return self.ledger.sim.available_cash() if self.ledger.sim.initialized else 0.0

    def get_positions(self) -> dict:
        """Real open positions, for positions_store.reconcile_with_kalshi.
        Only call when dry_run is False. market_positions entries carry a
        signed `position_fp` (whole contracts, +YES / -NO) and
        `total_traded_dollars` (cumulative cost basis).

        In shared-account mode the account's positions include other
        runners', so this returns only this runner's ledger positions (same
        shape, total_traded_dollars 0 -- the ledger keeps no cost basis).
        """
        if self.ledger is not None and self.ledger.shared_account:
            return {"market_positions": self.ledger.own_market_positions(), "event_positions": []}
        return self._client.get_positions()

    def buy_favored_side(self, ticker: str, side: str, contracts: float, limit_price: float) -> dict:
        """Places a marketable limit order on `side` ("yes"/"no") for
        `contracts` at `limit_price`. In dry-run, only logs and returns a
        synthetic response.

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
