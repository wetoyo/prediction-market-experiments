"""Per-OrderManager simulated bankroll -- a local cash ledger this runner
keeps for itself, so that (eventually) several models can share one Kalshi
account, each sizing off its own allocation instead of the whole real
balance. See live/SIM_BANKROLL_PLAN.md for the full rollout plan.

SHADOW MODE ONLY (added 2026-09-22): nothing reads `cash` for sizing yet.
The runner still sizes off the real account balance exactly as before; this
ledger is tracked alongside it and checked against the real balance on every
bankroll refresh, to prove it stays in lockstep before anything trusts it.
With one runner on the account, "in lockstep" is exact: every change in the
real balance must be explained by this runner's own fills and settlements.

This module is pure bookkeeping -- no network. OrderManager feeds it:
  - record_fill: from the POST order response, right after an order returns.
    That response only carries 4-decimal per-contract *averages*
    (average_fill_price, average_fee_paid), so the booked cost can be off by
    up to ~$0.0001 x contracts. The order_id is kept as "pending exact" until
  - apply_exact_cost: the GET /portfolio/orders/{id} record's exact
    taker/maker fill cost + fees replaces the approximation (fetched off the
    order path, during the balance-refresh sync).
  - apply_settlement: a /portfolio/settlements record for a held ticker pays
    out this ledger's OWN contracts on the winning side (not the record's
    account-wide `revenue`, which would double-count once two models hold the
    same ticker).
  - check: compares against a real balance snapshot and resyncs on a
    confirmed divergence.

Cash model (matches reconstruct_trade_history.py's verified P&L formula):
every fill, either side, costs price*count + fee; a matched yes+no pair on
one ticker redeems for $1 (credited here as soon as the pair exists -- if
Kalshi only credits it at settlement the divergence check will show it);
settlement pays $1 per contract held on the winning side.

Divergence is measured on *changes*, not levels: `offset` = (ledger cash -
real balance) at the last sync, and the ledger has diverged when
cash != real + offset. That makes the check correct for any allocation
(fixed dollars or a fraction) while this is the only runner on the account.
A divergence must show on two consecutive checks before it's counted and
resynced: a settlement landing between the balance fetch and the settlement
fetch is a legitimate one-poll transient, not drift.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("resolution_alpha.sim_bankroll")


@dataclass
class _Position:
    yes: float = 0.0
    no: float = 0.0
    opened_ts: float = field(default_factory=time.time)


@dataclass
class CheckResult:
    status: str  # "ok" | "suspect" | "diverged" | "inconclusive" | "uninitialized"
    sim_cash: float | None = None
    real_balance: float | None = None
    expected_cash: float | None = None
    gap: float | None = None
    reason: str | None = None


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SimulatedBankroll:
    def __init__(
        self,
        allocation_dollars: float | None = None,
        allocation_fraction: float = 1.0,
        tolerance_dollars: float = 0.01,
    ):
        self.allocation_dollars = allocation_dollars if allocation_dollars and allocation_dollars > 0 else None
        self.allocation_fraction = allocation_fraction
        self.tolerance_dollars = tolerance_dollars
        self._lock = threading.Lock()

        self.initialized = False
        self.cash = 0.0
        self.offset = 0.0  # cash - real_balance at the last sync
        self.positions: dict[str, _Position] = {}
        self.pending_exact: dict[str, dict] = {}  # order_id -> {"ticker", "side", "cost", "count"}
        self.fill_seq = 0  # bumped on every booked fill; lets check() spot a fill racing the balance snapshot

        self.checks = 0
        self.inconclusive_checks = 0
        self.divergence_count = 0
        self.last_divergence: dict | None = None
        self._suspect_gap: float | None = None

    # -- setup ---------------------------------------------------------------

    def initialize(
        self,
        real_balance: float,
        fill_seq_at_balance: int,
        open_positions: dict[str, tuple[str, float]] | None = None,
    ) -> float | None:
        """First sync: size the allocation off the real balance and adopt any
        positions already open on the account (a restart mid-window), so
        their settlements are expected rather than read as divergence.
        `open_positions`: ticker -> ("yes"|"no", contracts). Returns None
        (retry next sync) if a fill landed after `real_balance` was fetched."""
        with self._lock:
            if self.fill_seq != fill_seq_at_balance:
                return None
            if self.allocation_dollars is not None:
                self.cash = self.allocation_dollars
                if self.allocation_dollars > real_balance:
                    logger.warning(
                        "[sim-bankroll] fixed allocation $%.4f exceeds the real balance $%.4f",
                        self.allocation_dollars, real_balance,
                    )
            else:
                self.cash = real_balance * self.allocation_fraction
            self.offset = self.cash - real_balance
            for ticker, (side, contracts) in (open_positions or {}).items():
                pos = self.positions.setdefault(ticker, _Position())
                setattr(pos, side, getattr(pos, side) + contracts)
            self.initialized = True
            return self.cash

    # -- events --------------------------------------------------------------

    def record_fill(self, ticker: str, side: str, order_response: dict) -> float:
        """Book a live fill from the POST /portfolio/events/orders response.
        `side` is the outcome side bought ("yes"/"no"). Returns contracts
        booked (0 for a zero fill)."""
        count = _f(order_response.get("fill_count"))
        if count <= 0:
            return 0.0
        yes_price = _f(order_response.get("average_fill_price"), default=float("nan"))
        if 0.0 < yes_price < 1.0:
            price = yes_price if side == "yes" else 1.0 - yes_price  # response prices are YES-denominated
        else:
            # Still book the contracts; apply_exact_cost fills in the real
            # cost from the GET order record at the next sync.
            logger.warning(
                "[sim-bankroll] %s: unusable average_fill_price %r -- booking cost 0 pending the exact order record",
                ticker, order_response.get("average_fill_price"),
            )
            price = 0.0
        fee = _f(order_response.get("average_fee_paid")) * count
        cost = price * count + fee
        with self._lock:
            self.fill_seq += 1
            if not self.initialized:
                # Nothing to book against yet; the fill_seq bump alone stops
                # initialize() sizing off a balance fetched before this fill.
                return 0.0
            self.cash -= cost
            self._add_contracts(ticker, side, count)
            order_id = order_response.get("order_id")
            if order_id:
                self.pending_exact[order_id] = {"ticker": ticker, "side": side, "cost": cost, "count": count}
        return count

    def apply_exact_cost(self, order_id: str, order: dict) -> None:
        """Replace a fill's approximate cost with the GET order record's exact
        one (taker+maker fill cost + fees, all outcome-side dollars)."""
        with self._lock:
            pending = self.pending_exact.pop(order_id, None)
            if pending is None:
                return
            exact_cost = sum(
                _f(order.get(k)) for k in (
                    "taker_fill_cost_dollars", "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars",
                )
            )
            self.cash += pending["cost"] - exact_cost
            exact_count = _f(order.get("fill_count_fp"), default=pending["count"])
            if exact_count != pending["count"]:
                self._add_contracts(pending["ticker"], pending["side"], exact_count - pending["count"])

    def open_tickers(self) -> dict[str, float]:
        """ticker -> opened_ts, for the settlement fetch's min_ts."""
        with self._lock:
            return {t: p.opened_ts for t, p in self.positions.items()}

    def apply_settlement(self, settlement: dict) -> float | None:
        """Pay out this ledger's own contracts on a settled ticker. Returns
        the payout, or None if the ticker isn't held here."""
        ticker = settlement.get("ticker")
        with self._lock:
            pos = self.positions.pop(ticker, None)
            if pos is None:
                return None
            result = settlement.get("market_result")
            if result == "yes":
                payout = pos.yes
            elif result == "no":
                payout = pos.no
            elif settlement.get("value") is not None:
                yes_value = _f(settlement.get("value")) / 100.0  # cents per YES contract
                payout = pos.yes * yes_value + pos.no * (1.0 - yes_value)
            else:
                logger.warning("[sim-bankroll] %s settled with result %r and no value -- crediting 0", ticker, result)
                payout = 0.0
            self.cash += payout
            return payout

    def _add_contracts(self, ticker: str, side: str, count: float) -> None:
        pos = self.positions.setdefault(ticker, _Position())
        setattr(pos, side, getattr(pos, side) + count)
        pairs = min(pos.yes, pos.no)
        if pairs > 0:  # a matched yes+no pair redeems for $1
            self.cash += pairs
            pos.yes -= pairs
            pos.no -= pairs
        if pos.yes <= 1e-9 and pos.no <= 1e-9:
            self.positions.pop(ticker, None)

    # -- check ---------------------------------------------------------------

    def check(self, real_balance: float, fill_seq_at_balance: int) -> CheckResult:
        """Compare against a real balance snapshot. `fill_seq_at_balance` is
        self.fill_seq as of when that balance was fetched -- a fill booked
        since then makes this check inconclusive rather than a false alarm."""
        with self._lock:
            if not self.initialized:
                return CheckResult("uninitialized")
            self.checks += 1
            expected = real_balance + self.offset
            gap = self.cash - expected
            base = dict(sim_cash=self.cash, real_balance=real_balance, expected_cash=expected, gap=gap)
            if self.fill_seq != fill_seq_at_balance:
                self.inconclusive_checks += 1
                return CheckResult("inconclusive", reason="fill booked after the balance snapshot", **base)
            if self.pending_exact:
                self.inconclusive_checks += 1
                return CheckResult("inconclusive", reason="exact fill cost not fetched yet", **base)
            if abs(gap) <= self.tolerance_dollars:
                self._suspect_gap = None
                return CheckResult("ok", **base)
            if self._suspect_gap is None:
                self._suspect_gap = gap
                return CheckResult("suspect", **base)
            # Confirmed on a second consecutive check: count it and resync.
            self.divergence_count += 1
            self._suspect_gap = None
            self.last_divergence = {
                "ts": time.time(), "sim_cash": self.cash, "real_balance": real_balance,
                "expected_cash": expected, "gap": gap, "open_positions": len(self.positions),
            }
            self.cash = expected
            return CheckResult("diverged", **base)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "initialized": self.initialized,
                "allocation_dollars": self.allocation_dollars,
                "allocation_fraction": self.allocation_fraction,
                "sim_cash": round(self.cash, 6),
                "offset": round(self.offset, 6),
                "open_positions": {t: {"yes": p.yes, "no": p.no} for t, p in self.positions.items()},
                "pending_exact_orders": len(self.pending_exact),
                "checks": self.checks,
                "inconclusive_checks": self.inconclusive_checks,
                "divergence_count": self.divergence_count,
                "last_divergence": self.last_divergence,
            }
