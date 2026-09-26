"""Account-level check for several runners sharing one Kalshi account
(Phase 3 of live/SIM_BANKROLL_PLAN.md).

Each runner's OrderManager keeps its own simulated bankroll and writes it to
a status file (config.SIM_BANKROLL_STATUS_PATH, one per runner). Once more
than one runner trades the account, a runner can no longer check its own
ledger against the real balance -- the other runners move it too -- so
SIM_BANKROLL_SHARED_ACCOUNT turns that per-runner check off and this process
does the check for the whole account instead:

    sum over runners of (change in ledger cash) == change in real balance

It is the same change-based check as SimulatedBankroll.check, summed:
`offset` = sum(ledger cash) - real balance at the baseline, and the account
has diverged when sum(cash) != real + offset. That holds for any mix of
allocations, with any unallocated reserve left in the account.

Separate process, read-only: it only reads the status files and issues GET
requests, so it's never in a trading loop's path. It works for a single
runner too (then it duplicates that runner's own check), which is how to
validate it before a second runner exists.

A check is skipped (`inconclusive`) when a ledger's status file is missing,
not yet initialized, stale (the runner is down or asleep), or waiting on an
exact fill cost. A new or freshly re-allocated ledger (a new
`allocation_epoch`), or one that stops appearing, re-baselines the offset. A
ledger resumed after a restart keeps its epoch, so it doesn't.

A gap must persist, unchanged, over two consecutive checks with no new
fills in between before it counts: a fill or settlement landing between a
runner's status write and this process's balance fetch is a legitimate
one-check transient. A confirmed divergence is counted, logged, and
appended to the divergence log with an attribution (see `attribute`), then
the offset re-baselines. It does NOT correct any runner's ledger -- that's
a cross-process write this v1 leaves to a human; see the plan.

Usage (on the Pi, from this directory, with the runner venv):
    python account_reconciler.py                        # this runner's ledger, every 30s
    python account_reconciler.py --ledger A.json --ledger B.json --interval 30
    python account_reconciler.py --count 2 --interval 20   # baseline + one real check, print, exit

Outputs, in --state-dir (default config.LOG_DIR):
    account_reconciler.json               current state, rewritten every check
    account_reconciler_divergences.jsonl  one line per confirmed divergence
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / "live" / ".env"

logger = logging.getLogger("resolution_alpha.account_reconciler")

DEFAULT_TOLERANCE_DOLLARS = 0.01
# A runner writes its status after every bankroll refresh (15s by default).
DEFAULT_MAX_STATUS_AGE_SECONDS = 90.0


@dataclass
class ReconcileResult:
    status: str  # "ok" | "suspect" | "diverged" | "inconclusive" | "rebaselined"
    reason: str | None = None
    ledger_cash: float | None = None
    real_balance: float | None = None
    expected_cash: float | None = None
    gap: float | None = None
    ledgers: list[str] = field(default_factory=list)
    overlapping_tickers: dict[str, list[str]] = field(default_factory=dict)


def _ledger_name(status: dict, path: str) -> str:
    return status.get("order_tag") or path


def overlapping_tickers(statuses: dict[str, dict]) -> dict[str, list[str]]:
    """Tickers held by more than one ledger. Kalshi nets positions per
    account, so two runners on one ticker is the cross-model netting hazard
    (plan, Phase 3 item 3): the account may have redeemed the pair already,
    and a runner's reduce_only exit can fail against the account's net
    position."""
    holders: dict[str, list[str]] = {}
    for name, status in statuses.items():
        for ticker in status.get("open_positions") or {}:
            holders.setdefault(ticker, []).append(name)
    return {t: sorted(names) for t, names in holders.items() if len(names) > 1}


class AccountReconciler:
    """Pure check -- no network, no files. Feed it the runners' status dicts
    (name -> parsed status JSON, or None if unreadable) and a real balance."""

    def __init__(
        self,
        tolerance_dollars: float = DEFAULT_TOLERANCE_DOLLARS,
        max_status_age_seconds: float = DEFAULT_MAX_STATUS_AGE_SECONDS,
    ):
        self.tolerance_dollars = tolerance_dollars
        self.max_status_age_seconds = max_status_age_seconds
        self.baseline_key: tuple | None = None
        self.offset = 0.0
        self.checks = 0
        self.inconclusive_checks = 0
        self.divergence_count = 0
        self.last_ok_ts: float | None = None
        self.last_divergence: dict | None = None
        self._suspect: tuple[float, tuple] | None = None  # (gap, fill_seqs)

    def check(self, statuses: dict[str, dict | None], real_balance: float, now: float) -> ReconcileResult:
        self.checks += 1
        names = sorted(statuses)
        base = {"ledgers": names, "real_balance": real_balance}

        problems = []
        for name in names:
            status = statuses[name]
            if status is None:
                problems.append(f"{name}: no status file")
            elif not status.get("initialized"):
                problems.append(f"{name}: not initialized")
            elif now - float(status.get("updated_ts") or 0.0) > self.max_status_age_seconds:
                problems.append(f"{name}: status {now - float(status.get('updated_ts') or 0.0):.0f}s old")
            elif status.get("pending_exact_orders"):
                problems.append(f"{name}: exact fill cost not fetched yet")
        if problems:
            self.inconclusive_checks += 1
            self._suspect = None
            return ReconcileResult("inconclusive", reason="; ".join(problems), **base)

        base["overlapping_tickers"] = overlapping_tickers(statuses)
        ledger_cash = sum(float(statuses[n]["sim_cash"]) for n in names)
        base["ledger_cash"] = ledger_cash
        key = tuple((n, statuses[n].get("allocation_epoch")) for n in names)
        if key != self.baseline_key:
            reason = "first check" if self.baseline_key is None else self._describe_change(key)
            self._rebaseline(key, ledger_cash, real_balance, now)
            return ReconcileResult("rebaselined", reason=reason, expected_cash=ledger_cash, gap=0.0, **base)

        expected = real_balance + self.offset
        gap = ledger_cash - expected
        base.update(expected_cash=expected, gap=gap)
        fill_seqs = tuple(statuses[n].get("fill_seq") for n in names)
        if abs(gap) <= self.tolerance_dollars:
            self._suspect = None
            self.last_ok_ts = now
            return ReconcileResult("ok", **base)
        if (
            self._suspect is None
            or self._suspect[1] != fill_seqs
            or abs(gap - self._suspect[0]) > self.tolerance_dollars
        ):
            self._suspect = (gap, fill_seqs)
            return ReconcileResult("suspect", **base)
        # The same gap, twice, with no fill in between: count it and re-baseline.
        self.divergence_count += 1
        self.last_divergence = {
            "ts": now, "ledger_cash": ledger_cash, "real_balance": real_balance, "expected_cash": expected,
            "gap": gap, "since_last_ok_ts": self.last_ok_ts,
            "ledgers": {n: float(statuses[n]["sim_cash"]) for n in names},
        }
        self._rebaseline(key, ledger_cash, real_balance, now)
        return ReconcileResult("diverged", **base)

    def _describe_change(self, key: tuple) -> str:
        before, after = dict(self.baseline_key), dict(key)
        changes = []
        for name in sorted(set(before) | set(after)):
            if name not in before:
                changes.append(f"{name} joined")
            elif name not in after:
                changes.append(f"{name} left")
            elif before[name] != after[name]:
                # A restart that resumed keeps its epoch; a new one means the
                # ledger started over and its P&L before this point is gone.
                changes.append(f"{name} FRESHLY ALLOCATED (epoch {before[name]} -> {after[name]})")
        return "; ".join(changes)

    def _rebaseline(self, key: tuple, ledger_cash: float, real_balance: float, now: float) -> None:
        self.baseline_key = key
        self.offset = ledger_cash - real_balance
        self._suspect = None
        self.last_ok_ts = now

    def snapshot(self) -> dict:
        return {
            "offset": round(self.offset, 6),
            "checks": self.checks,
            "inconclusive_checks": self.inconclusive_checks,
            "divergence_count": self.divergence_count,
            "last_ok_ts": self.last_ok_ts,
            "last_divergence": self.last_divergence,
        }


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def attribute(statuses: dict[str, dict], orders: list[dict], settlements: list[dict]) -> dict:
    """Best-effort explanation of a confirmed divergence, from the account's
    orders and settlements since the last ok check. Pure; the caller fetches.

    - A filled order whose client_order_id tag matches a ledger, but whose
      order_id that ledger never booked: that ledger missed a fill.
    - A filled order with no known tag: something outside the runners
      traded (a manual trade on kalshi.com, or an untagged process).
    - A settled ticker, with the ledgers holding it: a settlement one of
      them missed or double-applied.
    No gap in any of these points at a deposit or withdrawal.

    Tags are the first '-'-separated field of client_order_id
    ("<tag>-<y|n>-<hex>", see order_manager.buy_favored_side).

    NOTE: relies on GET /portfolio/orders records carrying `client_order_id`
    (a documented Order field, not yet seen on this account's own payloads)
    -- which is why this only reports, and nothing acts on it."""
    known = {name: set((status.get("recent_order_ids") or {})) for name, status in statuses.items()}
    missed: dict[str, list[dict]] = {}
    untagged = []
    for order in orders:
        count = _f(order.get("fill_count_fp") or order.get("fill_count"))
        if count <= 0:
            continue
        cost = sum(_f(order.get(k)) for k in (
            "taker_fill_cost_dollars", "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars",
        ))
        row = {"order_id": order.get("order_id"), "ticker": order.get("ticker"), "count": count, "cost": round(cost, 6),
               "client_order_id": order.get("client_order_id")}
        tag = (order.get("client_order_id") or "").split("-", 1)[0]
        if tag in known:
            if order.get("order_id") not in known[tag]:
                missed.setdefault(tag, []).append(row)
        else:
            untagged.append(row)
    held = {name: set(status.get("open_positions") or {}) for name, status in statuses.items()}
    settled = []
    for settlement in settlements:
        ticker = settlement.get("ticker")
        settled.append({
            "ticker": ticker, "market_result": settlement.get("market_result"),
            "held_by": sorted(n for n, tickers in held.items() if ticker in tickers),
        })
    return {"missed_by_ledger": missed, "untagged_filled_orders": untagged, "settlements_in_window": settled}


# -- CLI ---------------------------------------------------------------------


def _load_env(path: Path) -> None:
    """Same parsing as reconstruct_trade_history.py: live/.env isn't
    shell-sourceable, and existing environment variables win."""
    if not path.exists():
        raise SystemExit(f"missing .env at {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _read_status(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _paginate(client, path: str, item_key: str, params: dict, max_pages: int = 20) -> list[dict]:
    items, cursor = [], None
    for _ in range(max_pages):
        page = client._request("GET", path, params={**params, **({"cursor": cursor} if cursor else {})})
        items.extend(page.get(item_key, []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


def _write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ledger", action="append", help="a runner's sim_bankroll.json (repeat per runner)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between checks")
    parser.add_argument("--count", type=int, default=0,
                        help="stop after N checks and print each (default 0: run forever). The first check only "
                             "sets the baseline, so use 2+.")
    parser.add_argument("--state-dir", help="where to write the status/divergence files (default: config.LOG_DIR)")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_DOLLARS)
    parser.add_argument("--max-status-age", type=float, default=DEFAULT_MAX_STATUS_AGE_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_env(ENV_PATH)
    import config  # after the .env, so its env overrides apply
    from kalshi_gateway import KalshiTradingClient

    ledger_paths = args.ledger or [config.SIM_BANKROLL_STATUS_PATH]
    state_dir = args.state_dir or config.LOG_DIR
    os.makedirs(state_dir, exist_ok=True)
    status_path = os.path.join(state_dir, "account_reconciler.json")
    divergence_path = os.path.join(state_dir, "account_reconciler_divergences.jsonl")

    client = KalshiTradingClient()
    reconciler = AccountReconciler(args.tolerance, args.max_status_age)
    logger.info("account reconciler starting: %d ledger(s): %s", len(ledger_paths), ", ".join(ledger_paths))
    while True:
        now = time.time()
        raw = {path: _read_status(path) for path in ledger_paths}
        statuses = {(_ledger_name(s, p) if s else p): s for p, s in raw.items()}
        if len(statuses) != len(raw):
            logger.warning("two ledger files share an order tag -- every runner on the account needs its own")
        try:
            real_balance = float(client.get_balance()["balance_dollars"])
        except Exception:
            logger.exception("balance fetch failed, retrying next interval")
            real_balance = None
        if real_balance is not None:
            result = reconciler.check(statuses, real_balance, now)
            event = None
            if result.status == "diverged":
                event = dict(reconciler.last_divergence)
                since = int((event.get("since_last_ok_ts") or now) - 60)
                try:
                    orders = _paginate(client, "/portfolio/orders", "orders", {"limit": 200, "min_ts": since})
                    settlements = _paginate(
                        client, "/portfolio/settlements", "settlements", {"limit": 200, "min_ts": since},
                    )
                    event["attribution"] = attribute({n: s for n, s in statuses.items() if s}, orders, settlements)
                except Exception as exc:
                    event["attribution_error"] = repr(exc)
                logger.warning(
                    "DIVERGED (#%d): ledgers $%.4f vs expected $%.4f (real $%.4f), gap %+.4f -- re-baselined",
                    reconciler.divergence_count, result.ledger_cash, result.expected_cash, result.real_balance,
                    result.gap,
                )
                with open(divergence_path, "a") as fh:
                    fh.write(json.dumps(event) + "\n")
            elif result.status in ("suspect", "rebaselined"):
                level = logging.WARNING if "FRESHLY ALLOCATED" in (result.reason or "") else logging.INFO
                logger.log(level, "%s: %s gap=%s", result.status, result.reason or "", result.gap)
            if result.overlapping_tickers:
                logger.warning("tickers held by more than one runner: %s", result.overlapping_tickers)
            _write_json(status_path, {**reconciler.snapshot(), "updated_ts": now, "last_check": asdict(result)})
            if args.count:
                print(json.dumps({**reconciler.snapshot(), "last_check": asdict(result)}, indent=1))
        if args.count and reconciler.checks >= args.count:
            return
        time.sleep(max(1.0, args.interval - (time.time() - now)))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
