"""Per-runner ledger wiring for runners whose orders can REST and fill
later -- btc_implied_prob and golf_field_alpha, which place GTC orders
(btc_implied_prob also rests take-profit orders). Added 2026-09-26 so they
can share the Kalshi account with resolution_alpha: see
resolution_alpha/live/SIM_BANKROLL_PLAN.md ("Phase 3") for the design.

resolution_alpha's own wiring (its order_manager.py) books each fill from
the POST response, because its orders are IOC and are done when the POST
returns. That misses a GTC order's later fills, so this module tracks
every order it places until the order is done:

  - record_order (after each POST): books whatever filled at once from the
    response's 4-decimal averages (provisional), and holds the cash the
    unfilled remainder reserves while it rests.
  - sync (every `interval_seconds`, in a background thread, plus once
    inline at start): re-reads each tracked order's record (one resting-
    orders listing, plus a GET for each order that has left it) and books
    the change in `fill_count_fp` and the exact cost fields since the last
    sync -- which also corrects the provisional booking. A done order
    (`executed`/`canceled`) is dropped. Then settlements for held tickers,
    then the check against the real balance, then the status file.

Everything else matches resolution_alpha's wiring and uses the same pure
ledger (sim_bankroll.py): the per-runner check, `sizing_cash`, order tags
(`<tag>-<y|n>-<28 hex>`), shared-account mode (resume from this runner's
own status file, fail closed when that state is lost), and a status file
account_reconciler.py can sum with the others.

Collateral on resting orders (UNVERIFIED on this account -- no runner has
rested an order since the ledger exists): Kalshi's `balance` is the cash
available to trade, so a resting buy is assumed to hold remaining x price
out of it, and an order that closes contracts this ledger holds (a
take-profit sell of the held side) is assumed to hold nothing. If that's
wrong, the per-runner check (or account_reconciler.py in shared mode) shows
a gap of about the resting order's value while it rests.
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from sim_bankroll import CheckResult, SimulatedBankroll

COST_FIELDS = ("taker_fill_cost_dollars", "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars")
TERMINAL_STATUSES = ("canceled", "executed")
# "<tag>-<y|n>-<28 hex>" stays within the 36 characters of the bare uuid4()
# Kalshi got before tagging, for tags up to this long.
MAX_TAG_LENGTH = 5
_STATUS_LOG_EVERY = 60  # syncs (~15 min at 15s)


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def order_cost(order: dict) -> float:
    """Exact dollars an order record has cost so far: fills plus fees."""
    return sum(_f(order.get(k)) for k in COST_FIELDS)


def split_client_order_id(client_order_id: str | None) -> tuple[str | None, str | None]:
    """"<tag>-<y|n>-<hex>" -> (tag, "yes"/"no"). An untagged uuid4 gives
    (None, None); resolution_alpha's older "<tag>-<32 hex>" gives (tag, None)."""
    parts = (client_order_id or "").split("-", 2)
    if len(parts) == 3 and parts[1] in ("y", "n"):
        return parts[0], ("yes" if parts[1] == "y" else "no")
    if len(parts) == 2:
        return parts[0], None
    return None, None


def real_balance_dollars(balance_response: dict) -> float:
    """GET /portfolio/balance -> dollars. `balance_dollars` carries sub-cent
    precision (the ledger books exact 4-6dp costs); `balance` is cents."""
    if balance_response.get("balance_dollars") is not None:
        return float(balance_response["balance_dollars"])
    return float(balance_response["balance"]) / 100.0


def _created_ts(order: dict) -> float:
    try:
        return datetime.fromisoformat(str(order["created_time"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return time.time()


def _outcome_side(order: dict) -> str | None:
    side = order.get("outcome_side")
    if side in ("yes", "no"):
        return side
    return split_client_order_id(order.get("client_order_id"))[1]


class TaggedLedger:
    def __init__(
        self,
        client,
        *,
        order_tag: str,
        status_path: str,
        divergence_path: str,
        allocation_dollars: float | None = None,
        allocation_fraction: float = 1.0,
        size_from_sim: bool = False,
        shared_account: bool = False,
        tolerance_dollars: float = 0.01,
        allow_fresh_allocation: bool = False,
        fresh_allocation_lookback_seconds: float = 14 * 86400,
        env_prefix: str = "",
        logger: logging.Logger | None = None,
    ):
        """`client`: a KalshiTradingClient. `env_prefix` only makes log lines
        name the right env vars (e.g. "GOLF_FIELD_ALPHA_")."""
        if not order_tag or "-" in order_tag or len(order_tag) > MAX_TAG_LENGTH:
            raise ValueError(
                f"ORDER_TAG {order_tag!r} must be 1-{MAX_TAG_LENGTH} characters with no '-' "
                "(it's split off at the first '-')"
            )
        if shared_account and not (allocation_dollars and allocation_dollars > 0):
            # A fraction of the real balance at startup would include the
            # other runners' cash.
            raise ValueError(f"{env_prefix}SIM_BANKROLL_SHARED_ACCOUNT needs a fixed "
                             f"{env_prefix}SIM_BANKROLL_ALLOCATION_DOLLARS > 0")
        self._client = client
        self.order_tag = order_tag
        self.status_path = status_path
        self.divergence_path = divergence_path
        self.size_from_sim = size_from_sim
        self.shared_account = shared_account
        self.allow_fresh_allocation = allow_fresh_allocation
        self.fresh_allocation_lookback_seconds = fresh_allocation_lookback_seconds
        self.env_prefix = env_prefix
        self.log = logger or logging.getLogger("tagged_ledger")
        self.sim = SimulatedBankroll(allocation_dollars, allocation_fraction, tolerance_dollars)

        # order_id -> {"ticker", "side", "count", "cost", "price", "placed_ts"}:
        # what's booked for each order that may still fill.
        self.tracked: dict[str, dict] = {}
        self._lock = threading.Lock()  # guards self.tracked
        self._sync_lock = threading.Lock()  # one sync at a time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._syncs_since_status_log = 0
        self._refusal_logged_at = 0.0
        self._collision_logged_at = 0.0

    # -- order path (the strategy's thread) ------------------------------------

    def new_client_order_id(self, side: str) -> str:
        return f"{self.order_tag}-{side[0]}-{uuid.uuid4().hex[:28]}"

    def is_own(self, order: dict) -> bool:
        return split_client_order_id(order.get("client_order_id"))[0] == self.order_tag

    def sizing_cash(self, real_balance: float) -> float:
        """What the strategy may size off (SIZE_FROM_SIM_BANKROLL on)."""
        if self.shared_account and not self.sim.initialized:
            # Not resumed yet (or refusing a fresh allocation): the real
            # balance is other runners' money too -- don't trade.
            return 0.0
        return self.sim.sizing_cash(real_balance)

    def order_allowed(self, ticker: str, side: str, contracts: float, worst_cost: float) -> tuple[bool, str]:
        """With SIZE_FROM_SIM_BANKROLL on, an order must fit in this ledger's
        available cash -- the strategies size several orders off one balance
        read, and nothing else stops them spending past the allocation. An
        order that only closes contracts this ledger holds is always allowed:
        the matched pair redeems for $1, so it raises cash."""
        if not self.size_from_sim:
            return True, ""
        if not self.sim.initialized:
            return False, "the ledger isn't initialized yet"
        yes, no = self.sim.open_positions().get(ticker, (0.0, 0.0))
        if (no if side == "yes" else yes) >= contracts - 1e-9:
            return True, ""
        available = self.sim.available_cash()
        if worst_cost > available + 1e-9:
            return False, f"costs up to ${worst_cost:.4f} but the ledger has ${available:.4f} available"
        return True, ""

    def record_order(self, ticker: str, side: str, contracts: float, limit_price: float, response: dict) -> None:
        """After a POST returns. Never raises: the order is already placed."""
        try:
            self._record_order(ticker, side, contracts, limit_price, response)
        except Exception:
            self.log.exception("[sim-bankroll] failed to book order on %s (the order itself is unaffected)", ticker)

    def _record_order(self, ticker: str, side: str, contracts: float, limit_price: float, response: dict) -> None:
        order_id = response.get("order_id")
        if not order_id:
            self.log.warning("[sim-bankroll] %s: order response has no order_id -- can't track it", ticker)
            return
        if not self.sim.initialized:
            # The first sync hasn't allocated yet; this order's effect is in
            # the balance that sync will allocate from.
            self.log.warning("[sim-bankroll] %s: order placed before the ledger initialized -- not tracked", ticker)
            return
        count = _f(response.get("fill_count"))
        cost = 0.0
        if count > 0:
            yes_price = _f(response.get("average_fill_price"), default=float("nan"))
            if 0.0 < yes_price < 1.0:
                price = yes_price if side == "yes" else 1.0 - yes_price  # response prices are YES-denominated
            else:
                price = limit_price
            cost = (price + _f(response.get("average_fee_paid"))) * count
        now = time.time()
        with self._lock:
            self.tracked[order_id] = {
                "ticker": ticker, "side": side, "count": count, "cost": cost,
                "price": limit_price, "placed_ts": now,
            }
        if count > 0:
            self.sim.book_fill(ticker, side, count, cost, order_id, opened_ts=now)
        remaining = _f(response.get("remaining_count"), default=max(0.0, contracts - count))
        self.sim.set_hold(order_id, self._hold_dollars(ticker, side, remaining, limit_price))

    def _hold_dollars(self, ticker: str, side: str, remaining: float, price: float,
                      positions: dict[str, tuple[float, float]] | None = None) -> float:
        """Cash a resting order on `side` keeps out of the balance: nothing
        for the part that closes contracts this ledger holds on the other
        side, remaining x price for the rest. Unverified -- module docstring."""
        if remaining <= 0:
            return 0.0
        yes, no = (positions if positions is not None else self.sim.open_positions()).get(ticker, (0.0, 0.0))
        covered = min(remaining, no if side == "yes" else yes)
        return (remaining - covered) * price

    def own_market_positions(self) -> list[dict]:
        """This ledger's positions in GET /portfolio/positions' shape, for
        the strategies' startup reconciliation in shared-account mode (the
        account's own list has other runners' positions in it). No cost
        basis is kept per ticker, so total_traded_dollars is 0."""
        return [
            {"ticker": ticker, "position_fp": yes - no, "total_traded_dollars": 0.0}
            for ticker, (yes, no) in self.sim.open_positions().items() if abs(yes - no) > 1e-9
        ]

    # -- sync ------------------------------------------------------------------

    def start(self, interval_seconds: float = 15.0) -> None:
        """First sync inline (so the ledger exists before the first order),
        then keep syncing in a daemon thread."""
        self.sync()
        if interval_seconds > 0 and self._thread is None:
            self._thread = threading.Thread(
                target=self._run, args=(interval_seconds,), name=f"ledger-{self.order_tag}", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, interval_seconds: float) -> None:
        while not self._stop.wait(interval_seconds):
            self.sync()

    def sync(self) -> None:
        """Never raises."""
        with self._sync_lock:
            try:
                self._sync()
            except Exception:
                self.log.exception("[sim-bankroll] sync failed (orders unaffected; the ledger catches up next sync)")

    def _real_balance(self) -> float:
        return real_balance_dollars(self._client.get_balance())

    def _sync(self) -> None:
        sim = self.sim
        if not sim.initialized:
            self._initialize()
            return
        self._update_tracked_orders()
        self._apply_settlements()
        sim.prune_recent_order_ids()

        # Balance last: a fill or settlement landing after the reads above
        # shows as a one-sync gap ("suspect"), confirmed only if it persists.
        fill_seq = sim.fill_seq
        real_balance = self._real_balance()
        if self.shared_account:
            # Other runners move the real balance too; account_reconciler.py
            # checks the sum of every runner's ledger against it instead.
            result = CheckResult(
                "shared", sim_cash=sim.available_cash(), real_balance=real_balance,
                reason="per-runner check off in shared-account mode; see account_reconciler.py",
            )
        else:
            result = sim.check(real_balance, fill_seq)
        if result.status == "diverged":
            self.log.warning(
                "[sim-bankroll] DIVERGED (#%d this run): sim $%.4f vs expected $%.4f (real $%.4f), gap %+.4f "
                "-- resynced to the real balance", sim.divergence_count, result.sim_cash, result.expected_cash,
                result.real_balance, result.gap,
            )
            self._append_divergence(sim.last_divergence)
        elif result.status == "suspect":
            self.log.info(
                "[sim-bankroll] gap %+.4f (sim $%.4f vs expected $%.4f) -- confirming on the next check",
                result.gap, result.sim_cash, result.expected_cash,
            )
        self._syncs_since_status_log += 1
        if self._syncs_since_status_log >= _STATUS_LOG_EVERY:
            self._syncs_since_status_log = 0
            self.log.info(
                "[sim-bankroll] %s: sim $%.4f (holds $%.4f), real $%.4f, %d tracked order(s), %d checks "
                "(%d inconclusive), %d divergence(s) this run", result.status, sim.available_cash(),
                sim.held_cash(), real_balance, len(self.tracked), sim.checks, sim.inconclusive_checks,
                sim.divergence_count,
            )
        self._write_status(result)

    def _list_orders(self, **params) -> list[dict]:
        orders, cursor = [], None
        for _ in range(10):
            page_params = {"limit": 200, **params}
            if cursor:
                page_params["cursor"] = cursor
            page = self._client._request("GET", "/portfolio/orders", params=page_params)
            orders.extend(page.get("orders", []))
            cursor = page.get("cursor")
            if not cursor:
                break
        return orders

    def _update_tracked_orders(self) -> None:
        with self._lock:
            order_ids = list(self.tracked)
        if not order_ids:
            return
        resting = {o.get("order_id"): o for o in self._list_orders(status="resting")}
        for order_id in order_ids:
            order = resting.get(order_id)
            if order is None:  # done, or not listed yet: read it directly
                try:
                    order = self._client._request("GET", f"/portfolio/orders/{order_id}")["order"]
                except Exception:
                    self.log.warning("[sim-bankroll] could not fetch order %s, retrying next sync", order_id)
                    continue
            self._apply_order_record(order_id, order)

    def _apply_order_record(self, order_id: str, order: dict) -> None:
        with self._lock:
            entry = self.tracked.get(order_id)
            if entry is None:
                return
            count = _f(order.get("fill_count_fp"), default=entry["count"])
            cost = order_cost(order)
            d_count, d_cost = count - entry["count"], cost - entry["cost"]
            entry["count"], entry["cost"] = count, cost
            done = order.get("status") in TERMINAL_STATUSES
            if done:
                del self.tracked[order_id]
        if abs(d_count) > 1e-9 or abs(d_cost) > 1e-9:
            self.sim.book_fill(entry["ticker"], entry["side"], d_count, d_cost, order_id, opened_ts=entry["placed_ts"])
        remaining = 0.0 if done else _f(order.get("remaining_count_fp"))
        price = _f(order.get(f"{entry['side']}_price_dollars"), default=entry["price"])
        self.sim.set_hold(order_id, self._hold_dollars(entry["ticker"], entry["side"], remaining, price))

    def _apply_settlements(self) -> None:
        held = self.sim.open_tickers()
        if not held:
            return
        min_ts = int(min(held.values())) - 300
        cursor = None
        while True:
            params = {"limit": 200, "min_ts": min_ts}
            if cursor:
                params["cursor"] = cursor
            page = self._client._request("GET", "/portfolio/settlements", params=params)
            for settlement in page.get("settlements", []):
                if settlement.get("ticker") in held:
                    self.sim.apply_settlement(settlement)
            cursor = page.get("cursor")
            if not cursor:
                break

    # -- first sync --------------------------------------------------------------

    def _track_record(self, order: dict, booked: bool) -> dict | None:
        """A tracked-order entry from an order record: `booked` means its
        fills so far are already in the ledger (e.g. in the balance a fresh
        allocation was taken from), so only later fills get booked."""
        side = _outcome_side(order)
        if side not in ("yes", "no") or not order.get("ticker"):
            return None
        return {
            "ticker": order["ticker"], "side": side,
            "count": _f(order.get("fill_count_fp")) if booked else 0.0,
            "cost": order_cost(order) if booked else 0.0,
            "price": _f(order.get(f"{side}_price_dollars")), "placed_ts": _created_ts(order),
        }

    def _initialize(self) -> None:
        mode = "SIZING off it" if self.size_from_sim else "shadow only"
        if self.shared_account:
            previous, why_not = self._read_previous_status()
            if previous is not None:
                self._resume(previous, mode)
                return
            if not self._fresh_allocation_allowed(why_not):
                return  # stays uninitialized: sizing_cash is 0, order_allowed refuses

        fill_seq = self.sim.fill_seq
        real_balance = self._real_balance()
        adopted: dict[str, tuple[str, float]] = {}
        account_positions = 0
        for row in self._client.get_positions().get("market_positions", []):
            position = _f(row.get("position_fp"))
            if position:
                account_positions += 1
                adopted[row["ticker"]] = ("yes" if position > 0 else "no", abs(position))
        if self.shared_account and adopted:
            # The account's positions may be other runners'. A fresh ledger on
            # a shared account starts flat -- switch into shared mode while flat.
            self.log.warning(
                "[sim-bankroll] fresh ledger on a shared account: NOT adopting %d open account position(s) "
                "(they may be another runner's)", len(adopted),
            )
            adopted = {}
        # Resting orders fill later. Alone on the account every one is ours
        # (including untagged ones from before tagging); shared, only our tag.
        resting = [o for o in self._list_orders(status="resting") if not self.shared_account or self.is_own(o)]
        entries, holds = {}, {}
        positions = {t: ((c, 0.0) if s == "yes" else (0.0, c)) for t, (s, c) in adopted.items()}
        for order in resting:
            entry = self._track_record(order, booked=True)
            if entry is None:
                continue
            entries[order["order_id"]] = entry
            holds[order["order_id"]] = self._hold_dollars(
                entry["ticker"], entry["side"], _f(order.get("remaining_count_fp")), entry["price"], positions,
            )
        cash = self.sim.initialize(real_balance, fill_seq, adopted, holds)
        if cash is None:
            return  # a fill raced the balance snapshot; retry on the next sync
        with self._lock:
            self.tracked.update(entries)
        self.log.info(
            "[sim-bankroll] initialized (%s%s): sim $%.4f of real $%.4f, adopted %d of %d open position(s) "
            "and %d resting order(s)", mode, f", shared account, tag {self.order_tag!r}" if self.shared_account else "",
            cash, real_balance, len(adopted), account_positions, len(entries),
        )
        self._write_status(None)

    def _resume(self, previous: dict, mode: str) -> None:
        fill_seq = self.sim.fill_seq
        real_balance = self._real_balance()
        # Orders placed after the status file's last write died with the old
        # process; the account's order history has them.
        since = float(previous.get("updated_ts") or 0.0) - 120
        recent = self._list_orders(min_ts=int(since))
        cash = self.sim.resume(previous, real_balance, fill_seq)
        if cash is None:
            return  # a fill raced the balance snapshot; retry on the next sync
        recovered = 0
        with self._lock:
            for order_id, entry in (previous.get("tracked_orders") or {}).items():
                self.tracked.setdefault(order_id, dict(entry))
            known = set(self.tracked) | set(self.sim.recent_order_ids)
            for order in recent:
                if self.is_own(order) and order.get("order_id") not in known:
                    entry = self._track_record(order, booked=False)
                    if entry is not None:
                        self.tracked[order["order_id"]] = entry
                        recovered += 1
        self.log.log(
            logging.WARNING if recovered else logging.INFO,
            "[sim-bankroll] resumed (%s, shared account, tag %r) from instance %s: sim $%.4f, %d open position(s), "
            "%d tracked order(s), %d order(s) recovered from the order history, real account $%.4f",
            mode, self.order_tag, self.sim.resumed_from, cash, len(self.sim.positions), len(self.tracked),
            recovered, real_balance,
        )
        # Recovered orders' fills are booked right away, not a sync later.
        self._update_tracked_orders()
        self._write_status(None)

    def _read_previous_status(self) -> tuple[dict | None, str]:
        try:
            with open(self.status_path) as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return None, "is missing"
        except (OSError, ValueError) as exc:
            return None, f"is unreadable ({exc!r})"
        if state.get("order_tag") != self.order_tag:
            return None, f"belongs to tag {state.get('order_tag')!r}"
        if not state.get("initialized") or "sim_cash" not in state:
            return None, "holds no initialized ledger"
        return state, ""

    def _fresh_allocation_allowed(self, why_not: str) -> bool:
        """Shared account, nothing to resume from. A brand-new runner (no
        fills under its tag lately) starts at its allocation. One whose
        ledger state was lost must not: that would forget its P&L and every
        position it holds. Fail closed until the operator says otherwise."""
        if self.allow_fresh_allocation:
            self.log.warning(
                "[sim-bankroll] status file %s; starting a fresh allocation because "
                "%sSIM_BANKROLL_ALLOW_FRESH_ALLOCATION is set -- unset it after this restart",
                why_not, self.env_prefix,
            )
            return True
        orders = self._list_orders(min_ts=int(time.time() - self.fresh_allocation_lookback_seconds))
        if orders and not any("client_order_id" in o for o in orders):
            self.log.warning(
                "[sim-bankroll] status file %s and the order records carry no client_order_id -- can't tell "
                "whether tag %r traded before; starting a fresh allocation", why_not, self.order_tag,
            )
            return True
        own_filled = [o for o in orders if self.is_own(o) and _f(o.get("fill_count_fp")) > 0]
        if not own_filled:
            return True
        now = time.time()
        if now - self._refusal_logged_at >= 900:
            self._refusal_logged_at = now
            self.log.error(
                "[sim-bankroll] NOT TRADING: status file %s (%s), but tag %r has %d filled order(s) in the last "
                "%d days -- its ledger state is lost. Restore the status file, or set "
                "%sSIM_BANKROLL_ALLOW_FRESH_ALLOCATION=true for one restart to start over.",
                why_not, self.status_path, self.order_tag, len(own_filled),
                int(self.fresh_allocation_lookback_seconds // 86400), self.env_prefix,
            )
        return False

    # -- status files ----------------------------------------------------------

    def _status_file_taken(self) -> bool:
        """Shared account: another live runner is writing this status file
        (two runners on one log dir). Overwriting it would destroy that
        runner's resume state, so the caller doesn't."""
        try:
            with open(self.status_path) as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            return False
        owner = existing.get("instance_id")
        if owner in (None, self.sim.instance_id, self.sim.resumed_from):
            return False
        if time.time() - float(existing.get("updated_ts") or 0.0) > 60:
            return False  # a stale file from a stopped runner
        now = time.time()
        if now - self._collision_logged_at >= 900:
            self._collision_logged_at = now
            self.log.error(
                "[sim-bankroll] %s is being written by another runner (tag %r, instance %s) -- not overwriting it. "
                "Every runner needs its own %sLOG_DIR.", self.status_path, existing.get("order_tag"), owner,
                self.env_prefix,
            )
        return True

    def _write_status(self, result: CheckResult | None) -> None:
        if self.shared_account and self._status_file_taken():
            return
        status = self.sim.snapshot()
        status["order_tag"] = self.order_tag
        status["shared_account"] = self.shared_account
        status["updated_ts"] = time.time()
        with self._lock:
            status["tracked_orders"] = {i: dict(e) for i, e in self.tracked.items()}
        if result is not None:
            status["last_check"] = {
                "status": result.status, "reason": result.reason, "real_balance": result.real_balance,
                "expected_cash": result.expected_cash, "gap": result.gap,
            }
        try:
            os.makedirs(os.path.dirname(self.status_path) or ".", exist_ok=True)
            tmp = self.status_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(status, fh, indent=1)
            os.replace(tmp, self.status_path)
        except OSError:
            self.log.warning("[sim-bankroll] could not write %s", self.status_path)

    def _append_divergence(self, event: dict | None) -> None:
        try:
            os.makedirs(os.path.dirname(self.divergence_path) or ".", exist_ok=True)
            with open(self.divergence_path, "a") as fh:
                fh.write(json.dumps({**(event or {}), "divergence_count_this_run": self.sim.divergence_count}) + "\n")
        except OSError:
            self.log.warning("[sim-bankroll] could not append to %s", self.divergence_path)
