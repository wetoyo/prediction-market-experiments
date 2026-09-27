"""Account-wide allocation guard: stops runners on one Kalshi account from
together claiming more money than the account holds.

Each runner sizes off its own ledger (sim_bankroll.py) and nothing sits
between the runners in the order path, on purpose -- see
resolution_alpha/live/SIM_BANKROLL_PLAN.md, "Why". So the only thing that
stops three $5 allocations on a $13 account is Kalshi rejecting whichever
orders come last. This guard closes that gap once, at the moment it opens:
when a ledger takes a fresh allocation. Added 2026-09-27.

How:
  - Every live runner with a ledger registers itself at startup: one small
    JSON file per order tag in the registry directory (REGISTRY_DIR_ENV,
    default ./runners.d/), naming its status file and configured allocation.
  - Before a ledger takes a fresh allocation, it adds up every *other*
    registered runner's claim on the account, plus its own, and refuses
    (sizes $0, retries every sync, logs an ERROR) if the total is more than
    the real balance minus MIN_UNALLOCATED_ENV dollars.
  - A peer's claim is its status file's `sim_cash + reserve` (the
    available cash + profit-skim reserve that account_reconciler.py sums
    against the real balance). A peer that hasn't initialized yet, or whose
    status file is missing, claims its configured allocation -- fixed
    dollars, or its fraction of the balance.
  - A runner in single-runner mode (not SHARED_ACCOUNT) claims the whole
    balance, so a second runner can't start beside it: resolution_alpha
    must switch into shared mode first, as the plan's go-live order says.
  - A resumed ledger is checked too, but only warned about: its money is
    already its own, and refusing it would strand its open positions.

A stopped runner stays registered on purpose -- its ledger resumes with its
cash, so its claim still stands. To retire a runner for good, remove its
entry: `python allocation_guard.py remove <tag>` (from this directory).
`python allocation_guard.py list` shows every registered runner and claim.

Off the order path: it reads a few small files when a ledger initializes.
Set ENABLED_ENV=false to turn it off (then nothing is registered either).
"""

import argparse
import json
import os
import time
from pathlib import Path

REGISTRY_DIR_ENV = "KALSHI_RUNNER_REGISTRY_DIR"
MIN_UNALLOCATED_ENV = "KALSHI_MIN_UNALLOCATED_DOLLARS"
ENABLED_ENV = "KALSHI_ALLOCATION_GUARD"
DEFAULT_REGISTRY_DIR = Path(__file__).resolve().parent / "runners.d"


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "true").strip().lower() not in ("0", "false", "no", "off")


def registry_dir() -> Path:
    return Path(os.environ.get(REGISTRY_DIR_ENV) or DEFAULT_REGISTRY_DIR)


def min_unallocated() -> float:
    try:
        return max(0.0, float(os.environ.get(MIN_UNALLOCATED_ENV) or 0.0))
    except ValueError:
        return 0.0


def _read_json(path) -> dict | None:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def entry_claim(entry: dict, real_balance: float) -> tuple[float, str]:
    """(claim in dollars, where it came from) for one registry entry."""
    status = _read_json(entry.get("status_path") or "")
    if status and status.get("initialized") and "sim_cash" in status:
        return float(status["sim_cash"]) + float(status.get("reserve") or 0.0), "ledger"
    allocation = float(entry.get("allocation_dollars") or 0.0)
    if allocation > 0:
        return allocation, "configured allocation (ledger not initialized)"
    return real_balance * float(entry.get("allocation_fraction") or 1.0), \
        "configured fraction of the balance (ledger not initialized)"


def list_entries(directory: Path | None = None) -> list[dict]:
    directory = directory or registry_dir()
    entries = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        entry = _read_json(path)
        if entry and entry.get("order_tag"):
            entries.append(entry)
    return entries


class AllocationGuard:
    def __init__(
        self,
        order_tag: str,
        status_path: str,
        allocation_dollars: float | None,
        allocation_fraction: float,
        shared_account: bool,
        registry: Path | None = None,
        min_unallocated_dollars: float | None = None,
        tolerance_dollars: float = 0.01,
    ):
        self.order_tag = order_tag
        self.status_path = os.path.abspath(status_path)
        self.allocation_dollars = allocation_dollars if allocation_dollars and allocation_dollars > 0 else None
        self.allocation_fraction = allocation_fraction
        self.shared_account = shared_account
        self.registry = Path(registry) if registry is not None else registry_dir()
        self.min_unallocated = min_unallocated() if min_unallocated_dollars is None else min_unallocated_dollars
        self.tolerance_dollars = tolerance_dollars

    def register(self) -> str | None:
        """Write this runner's registry entry. Returns a warning if another
        entry under the same tag pointed at a different status file."""
        path = self.registry / f"{self.order_tag}.json"
        previous = _read_json(path)
        warning = None
        if previous and os.path.abspath(previous.get("status_path") or "") != self.status_path:
            warning = (f"order tag {self.order_tag!r} was registered with status file {previous.get('status_path')}; "
                       f"now {self.status_path} -- every runner on the account needs its own tag")
        entry = {
            "order_tag": self.order_tag, "status_path": self.status_path,
            "allocation_dollars": self.allocation_dollars,
            # single-runner mode: the ledger takes (a fraction of) the whole balance
            "allocation_fraction": self.allocation_fraction,
            "shared_account": self.shared_account, "pid": os.getpid(), "registered_ts": time.time(),
        }
        self.registry.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(entry, fh, indent=1)
        os.replace(tmp, path)
        return warning

    def check(self, own_claim: float, real_balance: float) -> tuple[bool, str]:
        """Would this runner claiming `own_claim` (allocation + its reserve)
        over-commit the account? Returns (ok, one-line breakdown)."""
        peers = []
        for entry in list_entries(self.registry):
            if entry["order_tag"] == self.order_tag:
                continue
            claim, source = entry_claim(entry, real_balance)
            peers.append((entry["order_tag"], claim, source))
        total = own_claim + sum(c for _, c, _ in peers)
        limit = real_balance - self.min_unallocated
        breakdown = (
            f"{self.order_tag} ${own_claim:.2f}"
            + "".join(f" + {tag} ${claim:.2f} ({source})" for tag, claim, source in peers)
            + f" = ${total:.2f} vs real balance ${real_balance:.2f}"
            + (f" - ${self.min_unallocated:.2f} kept unallocated" if self.min_unallocated else "")
        )
        return total <= limit + self.tolerance_dollars, breakdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runners registered on the Kalshi account (allocation_guard.py).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="registered runners and their claims")
    rm = sub.add_parser("remove", help="retire a runner: drop its registry entry")
    rm.add_argument("tag")
    args = parser.parse_args(argv)
    directory = registry_dir()

    if args.cmd == "remove":
        path = directory / f"{args.tag}.json"
        if not path.exists():
            print(f"no entry for {args.tag!r} in {directory}")
            return 1
        path.unlink()
        print(f"removed {args.tag!r}; its claim no longer counts against the account")
        return 0

    entries = list_entries(directory)
    print(f"registry: {directory}")
    if not entries:
        print("  (no runners registered)")
        return 0
    # the most recent real balance any ledger saw, for the fraction-based claims
    balances = [(float(s.get("updated_ts") or 0), s["last_check"].get("real_balance"))
                for s in (_read_json(e.get("status_path") or "") for e in entries)
                if s and isinstance(s.get("last_check"), dict) and s["last_check"].get("real_balance") is not None]
    real = float(max(balances)[1]) if balances else 0.0
    total = 0.0
    for entry in entries:
        claim, source = entry_claim(entry, real)
        total += claim
        mode = "shared" if entry.get("shared_account") else "SINGLE-RUNNER (claims the whole balance)"
        print(f"  {entry['order_tag']}: ${claim:.4f} from {source}; {mode}; status {entry.get('status_path')}; "
              f"registered {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.get('registered_ts') or 0))}")
    if balances:
        print(f"total claims ${total:.4f} vs last seen real balance ${real:.4f}: "
              + ("OK" if total <= real - min_unallocated() + 0.01 else "OVER-COMMITTED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
