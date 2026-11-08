"""Places (or, by default, simulates) IOC buys of a locked side.

Real orders need WEATHER_LOCK_DRY_RUN=false *and* Kalshi credentials.
Kalshi's order `side` is always the YES leg: "bid" buys YES, "ask" sells YES
(economically buys NO at 1 - price). Same translation as the other
experiments, which checked it against live orders.

Orders are immediate-or-cancel at the worst price worth paying
(lock.plan_entry): Kalshi fills every level up to that price and drops the
rest, so nothing is left resting on a market that is about to be repriced.

With config.SIM_BANKROLL_ENABLED and a live run, every order carries
client_order_id "<ORDER_TAG>-<y|n>-<hex>" and is tracked by a
../shared/tagged_ledger.py ledger, so this runner can share the account with
the others (see ../MULTI_RUNNER_HANDOFF.md).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import config
import fees
from kalshi_gateway import KalshiTradingClient

_SHARED_DIR = str(Path(__file__).resolve().parents[1] / "shared")
if _SHARED_DIR not in sys.path:
    sys.path.append(_SHARED_DIR)
import allocation_guard  # noqa: E402
from tagged_ledger import TaggedLedger, real_balance_dollars  # noqa: E402

logger = logging.getLogger("weather_lock.order_manager")


class OrderRefused(Exception):
    """The ledger refused the order (over this runner's available cash). Nothing was sent."""


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
    if side == "yes":
        return "bid", round(limit_price, 4)
    if side == "no":
        return "ask", round(1.0 - limit_price, 4)
    raise ValueError(f"unknown side {side!r}, expected 'yes' or 'no'")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def filled_contracts(response: dict) -> float:
    """Contracts filled, from a POST /portfolio/events/orders response (or
    its nested "order" record)."""
    for rec in (response, response.get("order") or {}):
        for key in ("fill_count", "fill_count_fp"):
            if rec.get(key) is not None:
                return _num(rec.get(key))
    return 0.0


class OrderManager:
    ledger: TaggedLedger | None = None

    def __init__(self, dry_run: bool = config.DRY_RUN):
        self.dry_run = dry_run
        self._client = None
        if dry_run:
            return
        self._client = KalshiTradingClient()
        if not config.SIM_BANKROLL_ENABLED:
            if config.SIZE_FROM_SIM_BANKROLL:
                logger.warning("SIZE_FROM_SIM_BANKROLL is on but SIM_BANKROLL_ENABLED is off -- sizing off the real balance")
            return
        guard = None
        if allocation_guard.enabled():
            guard = allocation_guard.AllocationGuard(
                config.ORDER_TAG, config.SIM_BANKROLL_STATUS_PATH, config.SIM_BANKROLL_ALLOCATION_DOLLARS,
                config.SIM_BANKROLL_ALLOCATION_FRACTION, config.SIM_BANKROLL_SHARED_ACCOUNT,
                tolerance_dollars=config.SIM_BANKROLL_TOLERANCE_DOLLARS,
            )
            try:
                warning = guard.register()
                if warning:
                    logger.error("[allocation-guard] %s", warning)
            except OSError:
                logger.error("[allocation-guard] could not register in %s", guard.registry)
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
            env_prefix="WEATHER_LOCK_",
            guard=guard,
            logger=logging.getLogger("weather_lock.ledger"),
        )

    def start_ledger(self) -> None:
        if self.ledger is not None:
            self.ledger.start(config.SIM_BANKROLL_SYNC_SECONDS)

    def bankroll_dollars(self) -> float:
        """What sizing may use: the simulated balance in dry-run, else the real
        balance, or this runner's ledger cash with SIZE_FROM_SIM_BANKROLL on."""
        if self.dry_run:
            return config.DRY_RUN_SIMULATED_BALANCE_DOLLARS
        balance = real_balance_dollars(self._client.get_balance())
        if self.ledger is not None and self.ledger.trading_blocked():
            return 0.0
        if self.ledger is not None and self.ledger.size_from_sim:
            cash = self.ledger.sizing_cash(balance)
            return min(cash, self.ledger.sim.available_cash() if self.ledger.sim.initialized else 0.0)
        return balance

    def get_positions(self) -> dict:
        if self.ledger is not None and self.ledger.shared_account:
            return {"market_positions": self.ledger.own_market_positions(), "event_positions": []}
        return self._client.get_positions()

    def buy(self, ticker: str, side: str, contracts: float, limit_price: float,
            exchange_index: int | None = None) -> float:
        """IOC buy of `side` up to `limit_price`. Returns contracts filled
        (dry-run assumes a full fill)."""
        api_side, api_price = _to_api_order(side, limit_price)
        price_str = f"{api_price:.4f}"
        count_str = f"{contracts:.2f}"

        if self.dry_run:
            logger.info("[DRY RUN] would BUY %s %s %s @ <=%.4f (api side=%s price=%s IOC)",
                        count_str, side.upper(), ticker, limit_price, api_side, price_str)
            return contracts

        client_order_id = None
        if self.ledger is not None:
            worst_cost = contracts * limit_price + fees.estimate_fee_dollars(limit_price, contracts)
            allowed, why = self.ledger.order_allowed(ticker, side, contracts, worst_cost)
            if not allowed:
                logger.warning("NOT PLACED: BUY %s %s %s @ %.4f -- %s", count_str, side.upper(), ticker, limit_price, why)
                raise OrderRefused(why)
            client_order_id = self.ledger.new_client_order_id(side)

        logger.warning("LIVE ORDER: BUY %s %s %s @ <=%.4f (api side=%s price=%s IOC)",
                       count_str, side.upper(), ticker, limit_price, api_side, price_str)
        response = self._client.place_order(
            ticker=ticker, side=api_side, count=count_str, price=price_str,
            time_in_force="immediate_or_cancel", client_order_id=client_order_id,
            exchange_index=exchange_index,
        )
        if self.ledger is not None:
            self.ledger.record_order(ticker, side, contracts, api_price if side == "yes" else 1.0 - api_price, response)
        filled = filled_contracts(response)
        logger.warning("FILLED %.2f/%s %s %s", filled, count_str, side.upper(), ticker)
        return filled
