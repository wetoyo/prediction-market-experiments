"""Delta-hedges strategy.py's open binary positions with Kalshi's BTC
perpetual (config.PERP_HEDGE_TICKER). Replaces the take-profit/trailing-stop
exits removed 2026-10-21: positions now always ride to settlement, and the
BTC exposure they carry in the meantime is offset with the perp.

Each tick: sum every open position's delta (dollars it gains per $1 BTC
move -- its BTC exposure -- from deribit_iv.estimate_delta_yes, positive for
a YES "above" or NO "below"), convert the opposite of that to perp contracts
(contract_size BTC each), clip to config.HEDGE_MAX_CONTRACTS, and trade the
difference from the held perp position with an IOC limit through the top of
book. Positions within config.HEDGE_STOP_SECONDS_BEFORE_CLOSE of close count
as zero delta, so the hedge unwinds them before settlement; with nothing
open the target is flat.

What this doesn't do: hedge gamma or vol (a binary near its strike close to
expiry can't be delta-hedged well -- that's what the stop window and the
contract cap bound), account for perp fees/funding in the entry edge, or
correct for the Deribit forward vs. the perp's BRTI-based mark.
"""

import logging
import math
from datetime import datetime, timezone

import config
import deribit_iv
from kalshi_gateway import fetch_margin_market
from order_manager import OrderManager

logger = logging.getLogger("btc_implied_prob.hedger")


def portfolio_delta_btc(open_positions: dict, surface: deribit_iv.Surface, now: datetime) -> float:
    """Net BTC exposure of every open position still outside the stop window."""
    total = 0.0
    for ticker, position in open_positions.items():
        market = position["market"]
        seconds_left = (market.close_time - now).total_seconds()
        if seconds_left <= config.HEDGE_STOP_SECONDS_BEFORE_CLOSE:
            continue
        try:
            delta_yes = deribit_iv.estimate_delta_yes(
                surface, direction=market.direction, strike=market.strike, seconds_to_expiry=seconds_left,
            )
        except ValueError:
            logger.warning("%s: no surface to price delta against this tick, leaving it out of the hedge", ticker)
            continue
        signed_contracts = position["contracts"] if position["side"] == "yes" else -position["contracts"]
        total += signed_contracts * delta_yes
    return total


def target_perp_contracts(delta_btc: float, contract_btc: float) -> int:
    """Whole perp contracts (positive = long) that offset `delta_btc`, capped."""
    target = round(-delta_btc / contract_btc)
    cap = int(config.HEDGE_MAX_CONTRACTS)
    return max(-cap, min(cap, target))


def _round_to_tick(price: float, tick: float, up: bool) -> float:
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * tick, 6)


class PerpHedger:
    def __init__(self, manager: OrderManager, ticker: str = config.PERP_HEDGE_TICKER):
        self.manager = manager
        self.ticker = ticker

    def rebalance(self, open_positions: dict, surface: deribit_iv.Surface) -> None:
        """Best-effort: anything failing here is logged and retried next tick."""
        try:
            perp = fetch_margin_market(self.ticker)
        except Exception:
            logger.exception("%s: failed to fetch perp market, skipping hedge this tick", self.ticker)
            return
        contract_btc = float(perp["contract_size"]) * float(perp.get("underlying_multiplier") or 1.0)

        delta_btc = portfolio_delta_btc(open_positions, surface, datetime.now(timezone.utc))
        target = target_perp_contracts(delta_btc, contract_btc)
        try:
            held = self.manager.get_perp_position(self.ticker)
        except Exception:
            logger.exception("%s: failed to read the perp position, skipping hedge this tick", self.ticker)
            return

        diff = target - round(held)
        logger.info(
            "hedge: portfolio delta %+.6f BTC across %d position(s) -> target %+d %s, holding %+.0f",
            delta_btc, len(open_positions), target, self.ticker, held,
        )
        if diff == 0 or (target != 0 and abs(diff) < config.HEDGE_REBALANCE_CONTRACTS):
            return

        tick = float(perp["tick_size"])
        if diff > 0:
            best = float(perp.get("ask") or 0.0)
            limit = _round_to_tick(best * (1.0 + config.HEDGE_MAX_SLIPPAGE), tick, up=True)
        else:
            best = float(perp.get("bid") or 0.0)
            limit = _round_to_tick(best * (1.0 - config.HEDGE_MAX_SLIPPAGE), tick, up=False)
        if best <= 0.0:
            logger.warning("%s: no %s on the book, skipping hedge this tick", self.ticker, "ask" if diff > 0 else "bid")
            return

        try:
            self.manager.trade_perp(self.ticker, diff, limit)
        except Exception:
            logger.exception("%s: hedge order (%+d @ %.4f) failed, will retry next tick", self.ticker, diff, limit)
