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

Funding (live): the perp trades on Kalshi's margin instance, which has its
own cash. Each tick the hedger reads that instance's equity and keeps it
between HEDGE_MARGIN_TOPUP_RATIO and HEDGE_MARGIN_SWEEP_RATIO times the
initial margin the hedge needs (notional / the market's leverage estimate),
transferring back to HEDGE_MARGIN_TARGET_RATIO when it drifts outside --
out of this runner's own ledger cash (order_manager.transfer_margin books it
as `margin`, see ../shared/sim_bankroll.py), never the rest of the account.
With nothing to hedge the whole margin balance goes back. One transfer at a
time: the next waits until the last one shows on the margin side (or
HEDGE_TRANSFER_TIMEOUT_SECONDS passes). A target the margin on hand can't
carry is clipped to what it can.

Entry pricing (hedge_cost_per_contract, used by strategy._evaluate): what
hedging one binary contract is expected to cost -- perp taker fee plus half
the spread on HEDGE_TURNOVER times its delta notional, plus funding at the
current estimated rate over the time it'd be hedged -- comes off the edge,
and the margin it needs (hedge_margin_per_contract) is set aside from the
cash its entry is sized against.

What this doesn't do: hedge gamma or vol (a binary near its strike close to
expiry can't be delta-hedged well -- that's what the stop window and the
contract cap bound), or correct for the Deribit forward vs. the perp's
BRTI-based mark.
"""

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import deribit_iv
from kalshi_gateway import fetch_margin_funding_estimate, fetch_margin_market
from order_manager import OrderManager

logger = logging.getLogger("btc_implied_prob.hedger")

FUNDING_PERIOD_SECONDS = 8 * 3600  # Kalshi funds perps three times a day


@dataclass
class PerpQuote:
    """One tick's view of the perp, shared by entry pricing and the hedge."""
    ticker: str
    contract_btc: float
    bid: float
    ask: float
    tick: float
    long_leverage: float
    short_leverage: float
    taker_fee_rate: float
    funding_rate: float  # this period's estimate, fraction of notional per FUNDING_PERIOD_SECONDS

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def btc_price(self) -> float:
        return self.mid / self.contract_btc


def _leverage(market: dict, key: str) -> float:
    estimates = market.get(key) or {}
    value = estimates.get("1000") or market.get("leverage_estimate")
    return float(value) if value else 1.0  # unknown: assume no leverage, the conservative end


def fetch_perp_quote(ticker: str, taker_fee_rate: float) -> PerpQuote:
    market = fetch_margin_market(ticker)
    try:
        funding_rate = float(fetch_margin_funding_estimate(ticker).get("funding_rate") or 0.0)
    except Exception:
        logger.warning("%s: no funding estimate this tick, pricing funding at 0", ticker)
        funding_rate = 0.0
    bid, ask = float(market.get("bid") or 0.0), float(market.get("ask") or 0.0)
    if not (0.0 < bid <= ask):
        raise ValueError(f"{ticker}: no two-sided quote (bid {bid}, ask {ask})")
    return PerpQuote(
        ticker=ticker,
        contract_btc=float(market["contract_size"]) * float(market.get("underlying_multiplier") or 1.0),
        bid=bid, ask=ask, tick=float(market["tick_size"]),
        long_leverage=_leverage(market, "long_leverage_estimates"),
        short_leverage=_leverage(market, "short_leverage_estimates"),
        taker_fee_rate=taker_fee_rate, funding_rate=funding_rate,
    )


def hedged_seconds(seconds_to_close: float) -> float:
    return max(0.0, seconds_to_close - config.HEDGE_STOP_SECONDS_BEFORE_CLOSE)


def hedge_cost_per_contract(abs_delta_btc: float, seconds_to_close: float, perp: PerpQuote) -> float:
    """Expected dollars to delta-hedge one binary contract over its life."""
    hedged = hedged_seconds(seconds_to_close)
    if hedged <= 0.0:
        return 0.0  # entered inside the stop window: never hedged
    notional = abs_delta_btc * perp.btc_price
    half_spread = (perp.ask - perp.bid) / 2.0 / perp.mid
    trading = notional * (perp.taker_fee_rate + half_spread) * config.HEDGE_TURNOVER
    funding = notional * abs(perp.funding_rate) * hedged / FUNDING_PERIOD_SECONDS
    return trading + funding


def hedge_margin_per_contract(abs_delta_btc: float, seconds_to_close: float, perp: PerpQuote) -> float:
    """Margin cash one binary contract's hedge keeps on the margin instance."""
    if hedged_seconds(seconds_to_close) <= 0.0:
        return 0.0
    leverage = min(perp.long_leverage, perp.short_leverage)
    return abs_delta_btc * perp.btc_price / leverage * config.HEDGE_MARGIN_TARGET_RATIO


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


def _initial_margin(contracts: float, perp: PerpQuote) -> float:
    leverage = perp.long_leverage if contracts > 0 else perp.short_leverage
    return abs(contracts) * perp.mid / leverage


class PerpHedger:
    def __init__(self, manager: OrderManager, ticker: str = config.PERP_HEDGE_TICKER):
        self.manager = manager
        self.ticker = ticker
        # transfer id -> {"dollars": + to margin / - back, "equity_before": float | None, "ts"}
        self._pending: dict[str, dict] = {}

    def rebalance(self, open_positions: dict, surface: deribit_iv.Surface, perp: PerpQuote) -> None:
        """Best-effort: anything failing here is logged and retried next tick."""
        delta_btc = portfolio_delta_btc(open_positions, surface, datetime.now(timezone.utc))
        target = target_perp_contracts(delta_btc, perp.contract_btc)
        try:
            held = round(self.manager.get_perp_position(self.ticker))
        except Exception:
            logger.exception("%s: failed to read the perp position, skipping hedge this tick", self.ticker)
            return
        logger.info(
            "hedge: portfolio delta %+.6f BTC across %d position(s) -> target %+d %s, holding %+d",
            delta_btc, len(open_positions), target, self.ticker, held,
        )

        if not self.manager.dry_run:
            try:
                target = self._fund(target, held, perp)
            except Exception:
                logger.exception("%s: margin funding failed, holding the hedge where it is this tick", self.ticker)
                return

        diff = target - held
        if diff == 0 or (target != 0 and abs(diff) < config.HEDGE_REBALANCE_CONTRACTS):
            return
        if diff > 0:
            limit = _round_to_tick(perp.ask * (1.0 + config.HEDGE_MAX_SLIPPAGE), perp.tick, up=True)
        else:
            limit = _round_to_tick(perp.bid * (1.0 - config.HEDGE_MAX_SLIPPAGE), perp.tick, up=False)
        try:
            self.manager.trade_perp(self.ticker, diff, limit)
        except Exception:
            logger.exception("%s: hedge order (%+d @ %.4f) failed, will retry next tick", self.ticker, diff, limit)

    # -- margin funding ----------------------------------------------------------

    def _settle_transfers(self, equity: float) -> None:
        """Drops transfers that have shown up on the margin side (equity moved
        at least half the amount their way) or timed out. Picks up ones a
        previous process left in the ledger, which only time out."""
        for transfer_id in self.manager.margin_transfers_in_flight():
            self._pending.setdefault(transfer_id, {"dollars": 0.0, "equity_before": None, "ts": time.time()})
        now = time.time()
        for transfer_id, t in list(self._pending.items()):
            moved = None if t["equity_before"] is None else equity - t["equity_before"]
            landed = moved is not None and moved * t["dollars"] > 0 and abs(moved) >= abs(t["dollars"]) / 2
            if landed:
                logger.info("margin transfer %s ($%+.4f) landed", transfer_id, t["dollars"])
            elif now - t["ts"] >= config.HEDGE_TRANSFER_TIMEOUT_SECONDS:
                logger.error(
                    "margin transfer %s ($%+.4f) not seen on the margin side after %.0fs -- no longer waiting on it; "
                    "if it never happened, the ledger's balance check will flag the gap",
                    transfer_id, t["dollars"], now - t["ts"],
                )
            else:
                continue
            del self._pending[transfer_id]
            self.manager.margin_transfer_landed(transfer_id)

    def _fund(self, target: int, held: int, perp: PerpQuote) -> int:
        """Keeps the margin instance funded for the hedge; returns the target,
        clipped to what the margin on hand (plus what's on its way) carries."""
        equity = self.manager.get_margin_equity()
        self._settle_transfers(equity)
        if not self._pending:
            self.manager.mark_margin(equity)

        # Margin for whatever ends up held -- an unwinding position still needs its margin until it's gone.
        need = max(_initial_margin(target, perp), _initial_margin(held, perp) if held else 0.0)
        want = need * config.HEDGE_MARGIN_TARGET_RATIO
        if not self._pending:
            if equity < need * config.HEDGE_MARGIN_TOPUP_RATIO and want - equity >= config.HEDGE_MIN_TRANSFER_DOLLARS:
                self._transfer(want - equity, equity)
            elif equity > need * config.HEDGE_MARGIN_SWEEP_RATIO and equity - want >= config.HEDGE_MIN_TRANSFER_DOLLARS:
                self._transfer(-(equity - want), equity)  # with nothing hedged, want is 0: all of it

        if target == 0:
            return 0
        incoming = sum(t["dollars"] for t in self._pending.values() if t["dollars"] > 0)
        leverage = perp.long_leverage if target > 0 else perp.short_leverage
        supported = int((equity + incoming) * leverage / (perp.mid * config.HEDGE_MARGIN_TOPUP_RATIO))
        if abs(target) > supported:
            logger.warning(
                "hedge target %+d needs $%.2f margin, $%.2f on hand/incoming -- hedging %+d for now",
                target, _initial_margin(target, perp) * config.HEDGE_MARGIN_TOPUP_RATIO, equity + incoming,
                int(math.copysign(supported, target)),
            )
            target = int(math.copysign(supported, target))
        return target

    def _transfer(self, dollars: float, equity_before: float) -> None:
        transfer_id, booked = self.manager.transfer_margin(dollars)
        if transfer_id is not None:
            self._pending[transfer_id] = {"dollars": booked, "equity_before": equity_before, "ts": time.time()}
