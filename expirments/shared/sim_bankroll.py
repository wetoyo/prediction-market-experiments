"""Per-OrderManager simulated bankroll -- a local cash ledger this runner
keeps for itself, so that (eventually) several models can share one Kalshi
account, each sizing off its own allocation instead of the whole real
balance. See resolution_alpha/live/SIM_BANKROLL_PLAN.md for the full rollout
plan.

Shared by all three experiments since 2026-09-26 (moved here from
resolution_alpha/). Each experiment's order_manager.py puts this directory
on sys.path. resolution_alpha feeds it from POST responses (IOC orders fill
at once, see below); btc_implied_prob and golf_field_alpha place GTC orders
that can rest and fill later, so they feed it through tagged_ledger.py,
which books exact fills straight from the order records (`book_fill`) and
reserves the cash a resting order holds (`set_hold`).

Added 2026-09-22 in shadow mode: tracked alongside the real balance and
checked against it on every bankroll refresh, to prove it stays in lockstep
before anything trusts it. With one runner on the account, "in lockstep" is
exact: every change in the real balance must be explained by this runner's
own fills and settlements. Phase 1 (2026-09-22..26: 69 settled trades, 0
divergences) passed; from 2026-09-26 OrderManager can size off it
(`sizing_cash`) behind config.SIZE_FROM_SIM_BANKROLL, default off.

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

Holds: Kalshi's `balance` is the cash available to trade, so a resting buy
order's reserved collateral is already out of it. `holds` (order id ->
dollars) mirrors that: the ledger's *available* cash is `cash - sum(holds)`,
and that is what check(), sizing_cash() and the status file's `sim_cash`
use. `cash` itself still includes held money (it's spent only on a fill).
With no holds (resolution_alpha: IOC only) the two are the same number.

Divergence is measured on *changes*, not levels: `offset` = (available
cash - real balance) at the last sync, and the ledger has diverged when
available != real + offset. That makes the check correct for any allocation
(fixed dollars or a fraction) while this is the only runner on the account.
A divergence must show on two consecutive checks before it's counted and
resynced: a settlement landing between the balance fetch and the settlement
fetch is a legitimate one-poll transient, not drift.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("sim_bankroll")

# Booked order ids are kept this long, for account_reconciler.py to tell
# which tagged account orders this ledger has (and hasn't) seen.
RECENT_ORDER_ID_TTL_SECONDS = 24 * 3600


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

        self.instance_id = uuid.uuid4().hex[:12]
        # Set on a fresh allocation, carried through resume(): a new epoch
        # tells account_reconciler.py to re-baseline, a resumed one doesn't.
        self.allocation_epoch: str | None = None
        self.resumed_from: str | None = None
        self.initialized = False
        self.cash = 0.0
        self.offset = 0.0  # cash - real_balance at the last sync
        self.positions: dict[str, _Position] = {}
        self.pending_exact: dict[str, dict] = {}  # order_id -> {"ticker", "side", "cost", "count"}
        self.fill_seq = 0  # bumped on every booked fill; lets check() spot a fill racing the balance snapshot
        self.recent_order_ids: dict[str, float] = {}  # order_id -> booked ts
        self.recovered_orders = 0  # fills resume() booked from the account's order history
        self.holds: dict[str, float] = {}  # order_id -> dollars a resting order keeps out of the balance

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
        holds: dict[str, float] | None = None,
    ) -> float | None:
        """First sync: size the allocation off the real balance and adopt any
        positions already open on the account (a restart mid-window), so
        their settlements are expected rather than read as divergence.
        `open_positions`: ticker -> ("yes"|"no", contracts). `holds`: order
        id -> dollars reserved by adopted resting orders; the real balance
        already excludes them, so the allocation is the *available* cash and
        the held money sits on top of it. Returns the available cash, or
        None (retry next sync) if a fill landed after `real_balance` was
        fetched."""
        with self._lock:
            if self.fill_seq != fill_seq_at_balance:
                return None
            if self.allocation_dollars is not None and self.allocation_dollars > real_balance:
                logger.warning(
                    "[sim-bankroll] fixed allocation $%.4f exceeds the real balance $%.4f",
                    self.allocation_dollars, real_balance,
                )
            self.holds = {i: d for i, d in (holds or {}).items() if d > 0}
            self.cash = self._allocation(real_balance) + sum(self.holds.values())
            self.offset = self._available() - real_balance
            for ticker, (side, contracts) in (open_positions or {}).items():
                pos = self.positions.setdefault(ticker, _Position())
                setattr(pos, side, getattr(pos, side) + contracts)
            self.allocation_epoch = self.instance_id
            self.initialized = True
            return self._available()

    def resume(
        self,
        state: dict,
        real_balance: float,
        fill_seq_at_balance: int,
        recovered_orders: list[tuple[dict, str]] = (),
    ) -> float | None:
        """Shared-account restart: carry on from this ledger's own last
        snapshot() (cash, positions, pending exact costs, allocation epoch)
        instead of re-allocating off the real balance and adopting every
        position on the account -- other runners' positions included.
        Settlements that landed while the process was down are picked up by
        the next sync (positions keep their opened_ts).

        `recovered_orders`: (GET order record, outcome side) for this
        runner's own filled orders since the snapshot was written. A fill
        booked in memory but not yet written out when the process died is
        in there; orders the snapshot already knows are skipped.

        Returns the available cash, or None (retry next sync) if a fill
        landed after `real_balance` was fetched."""
        with self._lock:
            if self.fill_seq != fill_seq_at_balance:
                return None
            # `ledger_cash` (cash incl. holds) is written since holds were
            # added; older files only have `sim_cash`, which had no holds.
            self.cash = float(state.get("ledger_cash", state["sim_cash"]))
            self.holds = {i: float(d) for i, d in (state.get("holds") or {}).items()}
            self.offset = self._available() - real_balance
            for ticker, row in (state.get("open_positions") or {}).items():
                self.positions[ticker] = _Position(
                    yes=float(row.get("yes", 0.0)), no=float(row.get("no", 0.0)),
                    opened_ts=float(row.get("opened_ts") or time.time()),
                )
            self.pending_exact = dict(state.get("pending_exact") or {})
            self.recent_order_ids = dict(state.get("recent_order_ids") or {})
            self.allocation_epoch = state.get("allocation_epoch") or self.instance_id
            self.resumed_from = state.get("instance_id")
            opened_ts = float(state.get("updated_ts") or time.time())
            self.recovered_orders = sum(
                self._book_order_record(order, side, opened_ts) for order, side in recovered_orders
            )
            self.initialized = True
            return self._available()

    def _book_order_record(self, order: dict, side: str, opened_ts: float) -> bool:
        """Book a filled order straight from its GET order record (exact
        cost), unless this ledger already has it. Caller holds the lock."""
        order_id = order.get("order_id")
        if not order_id or order_id in self.recent_order_ids or order_id in self.pending_exact:
            return False
        count = _f(order.get("fill_count_fp"))
        if count <= 0:
            return False
        self.cash -= sum(_f(order.get(k)) for k in (
            "taker_fill_cost_dollars", "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars",
        ))
        ticker = order["ticker"]
        is_new = ticker not in self.positions
        self._add_contracts(ticker, side, count)
        if is_new and ticker in self.positions:
            # settlement lookups start from opened_ts; the fill was after the snapshot
            self.positions[ticker].opened_ts = opened_ts
        self.recent_order_ids[order_id] = time.time()
        self.fill_seq += 1
        return True

    def _allocation(self, real_balance: float) -> float:
        if self.allocation_dollars is not None:
            return self.allocation_dollars
        return real_balance * self.allocation_fraction

    def _available(self) -> float:
        """Cash not reserved by a resting order. Caller holds the lock."""
        return self.cash - sum(self.holds.values())

    def available_cash(self) -> float:
        with self._lock:
            return self._available()

    def held_cash(self) -> float:
        with self._lock:
            return sum(self.holds.values())

    def sizing_cash(self, real_balance: float) -> float:
        """What the runner may size off: this ledger's own cash, never more
        than the account really holds, never below 0. Before the first sync
        has initialized the ledger, the allocation it is about to get.

        The min() never lets a ledger bug size past what the account holds.
        A fill the ledger missed (cash too high) is caught by the divergence
        check and resynced within two syncs. A settlement it hasn't applied
        yet (cash too low) only sizes smaller until the next sync."""
        with self._lock:
            cash = self._available() if self.initialized else self._allocation(real_balance)
        return max(0.0, min(cash, real_balance))

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
                now = time.time()
                self.recent_order_ids[order_id] = now
                cutoff = now - RECENT_ORDER_ID_TTL_SECONDS
                for old_id in [i for i, ts in self.recent_order_ids.items() if ts < cutoff]:
                    del self.recent_order_ids[old_id]
        return count

    def book_fill(
        self, ticker: str, side: str, count: float, cost: float,
        order_id: str | None = None, opened_ts: float | None = None,
    ) -> None:
        """Book an exact fill (or the newly filled part of an order already
        partly booked): `count` more contracts of `side` for `cost` dollars,
        fees included. tagged_ledger.py calls this with the change in an
        order record's fill_count_fp / cost fields since its last sync.
        `count` may be 0 with a nonzero `cost`: a correction to a
        provisional booking. `opened_ts` (when the order was placed) starts
        a new position's settlement lookup early enough for a fill that is
        only booked late, e.g. one recovered after a restart."""
        with self._lock:
            self.fill_seq += 1
            self.cash -= cost
            if count:
                is_new = ticker not in self.positions
                self._add_contracts(ticker, side, count)
                if opened_ts is not None and is_new and ticker in self.positions:
                    self.positions[ticker].opened_ts = opened_ts
            if order_id:
                self.recent_order_ids[order_id] = time.time()

    def set_hold(self, order_id: str, dollars: float) -> None:
        """Set what a resting order currently keeps out of the balance (0
        once it's filled, canceled or no longer resting)."""
        with self._lock:
            if dollars > 0:
                self.holds[order_id] = dollars
            else:
                self.holds.pop(order_id, None)

    def prune_recent_order_ids(self) -> None:
        with self._lock:
            cutoff = time.time() - RECENT_ORDER_ID_TTL_SECONDS
            for old_id in [i for i, ts in self.recent_order_ids.items() if ts < cutoff]:
                del self.recent_order_ids[old_id]

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

    def open_positions(self) -> dict[str, tuple[float, float]]:
        """ticker -> (yes, no) contracts this ledger holds."""
        with self._lock:
            return {t: (p.yes, p.no) for t, p in self.positions.items()}

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
            available = self._available()
            gap = available - expected
            base = dict(sim_cash=available, real_balance=real_balance, expected_cash=expected, gap=gap)
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
                "ts": time.time(), "sim_cash": available, "real_balance": real_balance,
                "expected_cash": expected, "gap": gap, "open_positions": len(self.positions),
                "holds": round(sum(self.holds.values()), 6),
            }
            self.cash = expected + sum(self.holds.values())
            return CheckResult("diverged", **base)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "initialized": self.initialized,
                "instance_id": self.instance_id,
                "allocation_epoch": self.allocation_epoch,
                "resumed_from": self.resumed_from,
                "allocation_dollars": self.allocation_dollars,
                "allocation_fraction": self.allocation_fraction,
                # sim_cash is the *available* cash (what account_reconciler.py
                # sums against the real balance); ledger_cash includes holds.
                "sim_cash": round(self._available(), 6),
                "ledger_cash": round(self.cash, 6),
                "holds": dict(self.holds),
                "offset": round(self.offset, 6),
                "fill_seq": self.fill_seq,
                "open_positions": {
                    t: {"yes": p.yes, "no": p.no, "opened_ts": p.opened_ts} for t, p in self.positions.items()
                },
                "pending_exact_orders": len(self.pending_exact),
                "pending_exact": {i: dict(p) for i, p in self.pending_exact.items()},
                "recent_order_ids": dict(self.recent_order_ids),
                "checks": self.checks,
                "inconclusive_checks": self.inconclusive_checks,
                "divergence_count": self.divergence_count,
                "last_divergence": self.last_divergence,
            }
