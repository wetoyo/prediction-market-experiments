"""Profit skimming: owner-defined rules that take profit out of a runner's
simulated bankroll (sim_bankroll.py) on a schedule.

Kalshi has no withdrawal endpoint, so "taking profit out" moves it from the
ledger's cash into its `reserve`: the money stays in the Kalshi account, the
runner stops sizing off it, and the owner can withdraw it on kalshi.com
whenever they like. Added 2026-09-27 for resolution_alpha; nothing here is
resolution_alpha-specific.

Rules live in a JSON file (reloaded whenever it changes -- no restart
needed). An invalid file is rejected whole and the previous rules stay in
force, so a typo never silently drops a rule:

    {"rules": [
      {"name": "daily-10pct",       # unique; its state is keyed by it
       "at": "00:05",               # local time the rule runs
       "timezone": "America/New_York",
       "days": ["mon", "tue"],      # optional; default every day
       "fraction": 0.10,            # share of the period's profit to take
       "basis": "period",           # see below
       "min_dollars": 0.01,         # optional; skip smaller skims
       "max_dollars": null,         # optional cap per run
       "max_wait_minutes": 180,     # optional; see "flat" below
       "enabled": true}             # optional
    ]}

Profit is `trading_pnl` (sim_bankroll.py): the change in ledger cash from
fills, pair redemptions and settlements, and nothing else -- so a skim, a
release, a withdrawal, a deposit absorbed by a divergence resync, or a
restart's re-allocation is never mistaken for profit. Each rule accrues it
from its previous run (or from when it was first seen) to this one.

  basis "period":        take `fraction` of the period's profit; a losing
                         period takes nothing and is forgotten -- the next
                         period starts from 0.
  basis "carry_losses":  a loss carries forward and must be made back
                         before anything is taken again (a high-water mark).

"Flat": a rule waits past its time until the ledger holds no position and
no fill is waiting on its exact cost, since money in an open position shows
as a loss until it settles. Past `max_wait_minutes` it runs anyway. Profit
earned while it waits counts toward this run; nothing is counted twice,
because every period starts where the last one ended.

Owner actions go through an append-only inbox file (see `cli`), which the
runner reads on its next sync:
  - withdrawal: "I'm about to withdraw $X on kalshi.com". The ledger
    applies it when the real balance drops by $X, out of the reserve first
    (sim_bankroll.SimulatedBankroll.check). Declare it BEFORE withdrawing.
    An undeclared withdrawal reads as a divergence, and the resync takes it
    out of trading cash, not the reserve.
  - release: move $X of the reserve back into trading cash.

State (the reserve, each rule's accrued profit and last run, which inbox
entries are done) is kept in a JSON state file, so it survives restarts.
Every skim, skip, withdrawal and release is appended to a JSONL log.
"""

import argparse
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

logger = logging.getLogger("profit_skim")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BASES = ("period", "carry_losses")
# A declared withdrawal the balance never showed is dropped after this long.
WITHDRAWAL_DECLARATION_TTL_SECONDS = 7 * 24 * 3600


class RuleError(ValueError):
    pass


def parse_rules(doc: dict) -> list[dict]:
    """Validate a rules document; returns normalized rules. Raises RuleError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise RuleError('expected {"rules": [...]}')
    rules, names = [], set()
    for i, raw in enumerate(doc["rules"]):
        where = f"rule {i}"
        if not isinstance(raw, dict):
            raise RuleError(f"{where}: not an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise RuleError(f"{where}: needs a non-empty name")
        where = f"rule {name!r}"
        if name in names:
            raise RuleError(f"{where}: duplicate name")
        names.add(name)
        unknown = set(raw) - {
            "name", "at", "timezone", "days", "fraction", "basis", "min_dollars", "max_dollars",
            "max_wait_minutes", "enabled", "note",
        }
        if unknown:
            raise RuleError(f"{where}: unknown field(s) {sorted(unknown)}")
        try:
            hour, minute = (int(x) for x in str(raw.get("at", "")).split(":"))
            at = dtime(hour, minute)
        except (TypeError, ValueError):
            raise RuleError(f"{where}: 'at' must be \"HH:MM\"") from None
        try:
            tz = ZoneInfo(raw.get("timezone", "America/New_York"))
        except Exception:
            raise RuleError(f"{where}: unknown timezone {raw.get('timezone')!r}") from None
        days = raw.get("days")
        if days is not None:
            if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
                raise RuleError(f"{where}: 'days' must be a non-empty list of {DAYS}")
            days = sorted({DAYS.index(d) for d in days})
        fraction = raw.get("fraction")
        if not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
            raise RuleError(f"{where}: 'fraction' must be in (0, 1]")
        basis = raw.get("basis", "period")
        if basis not in BASES:
            raise RuleError(f"{where}: 'basis' must be one of {BASES}")
        min_dollars = raw.get("min_dollars", 0.01)
        max_dollars = raw.get("max_dollars")
        max_wait = raw.get("max_wait_minutes", 180)
        for field_name, value in (("min_dollars", min_dollars), ("max_wait_minutes", max_wait)):
            if not isinstance(value, (int, float)) or value < 0:
                raise RuleError(f"{where}: {field_name!r} must be a number >= 0")
        if max_dollars is not None and (not isinstance(max_dollars, (int, float)) or max_dollars <= 0):
            raise RuleError(f"{where}: 'max_dollars' must be a number > 0 or null")
        rules.append({
            "name": name, "at": at, "tz": tz, "days": days, "fraction": float(fraction), "basis": basis,
            "min_dollars": float(min_dollars), "max_dollars": None if max_dollars is None else float(max_dollars),
            "max_wait_seconds": float(max_wait) * 60, "enabled": bool(raw.get("enabled", True)),
        })
    return rules


def latest_slot(rule: dict, now: float) -> datetime:
    """The most recent scheduled time of `rule` at or before `now`."""
    local = datetime.fromtimestamp(now, rule["tz"])
    for back in range(8):
        day = local.date() - timedelta(days=back)
        candidate = datetime.combine(day, rule["at"], tzinfo=rule["tz"])
        if candidate <= local and (rule["days"] is None or day.weekday() in rule["days"]):
            return candidate
    raise AssertionError("unreachable: some day in the last week matches")


def next_slot(rule: dict, now: float) -> datetime:
    local = datetime.fromtimestamp(now, rule["tz"])
    for ahead in range(8):
        day = local.date() + timedelta(days=ahead)
        candidate = datetime.combine(day, rule["at"], tzinfo=rule["tz"])
        if candidate > local and (rule["days"] is None or day.weekday() in rule["days"]):
            return candidate
    raise AssertionError("unreachable: some day in the next week matches")


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


def read_inbox(path: str) -> list[dict]:
    entries = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id") and entry.get("kind") in ("withdrawal", "release"):
                    entries.append(entry)
    except FileNotFoundError:
        pass
    return entries


class ProfitSkimmer:
    """Runs the rules against one SimulatedBankroll. The runner calls
    `step(sim)` at the end of every ledger sync (a background thread, never
    in an order path). Pure apart from its own files; the clock is a
    parameter for tests."""

    def __init__(
        self,
        rules_path: str,
        state_path: str,
        log_path: str,
        inbox_path: str,
        apply_withdrawals_immediately: bool = False,
        log=None,
    ):
        self.rules_path = rules_path
        self.state_path = state_path
        self.log_path = log_path
        self.inbox_path = inbox_path
        # Shared-account mode: the ledger runs no check of its own that
        # could match a declared withdrawal to the balance drop.
        self.apply_withdrawals_immediately = apply_withdrawals_immediately
        self._log = log or logger.log
        self.rules: list[dict] = []
        self._rules_sig = None
        self._inbox_sig = None
        self._declared: set[str] = set()
        self._waiting_logged: set[tuple[str, float]] = set()
        try:
            self.state = _read_json(state_path) or {}
        except (OSError, ValueError) as exc:
            # Refusing to start would stop trading over a bookkeeping file;
            # starting from scratch forgets the reserve. Say so loudly.
            self._log(logging.ERROR, "[profit-skim] state file %s unreadable (%r) -- starting with no reserve",
                      state_path, exc)
            self.state = {}
        self.state.setdefault("reserve", 0.0)
        self.state.setdefault("rules", {})
        self.state.setdefault("inbox_done", {})

    @property
    def saved_reserve(self) -> float:
        return float(self.state.get("reserve") or 0.0)

    # -- files ---------------------------------------------------------------

    def _reload_rules(self) -> None:
        try:
            st = os.stat(self.rules_path)
            sig = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            sig = None
        if sig == self._rules_sig:
            return
        self._rules_sig = sig
        if sig is None:
            if self.rules:
                self._log(logging.WARNING, "[profit-skim] %s is gone -- no skim rules now", self.rules_path)
            self.rules = []
            return
        try:
            with open(self.rules_path) as fh:
                rules = parse_rules(json.load(fh))
        except (OSError, ValueError) as exc:
            self._log(logging.ERROR, "[profit-skim] %s rejected (%s) -- keeping the previous %d rule(s)",
                      self.rules_path, exc, len(self.rules))
            return
        self.rules = rules
        self._log(logging.WARNING, "[profit-skim] loaded %d rule(s) from %s: %s", len(rules), self.rules_path,
                  ", ".join(f"{r['name']}{'' if r['enabled'] else ' (disabled)'}" for r in rules) or "none")

    def _save(self, sim) -> None:
        self.state["reserve"] = round(sim.reserve, 6)
        self.state["updated_ts"] = time.time()
        try:
            _write_json(self.state_path, self.state)
        except OSError:
            self._log(logging.WARNING, "[profit-skim] could not write %s", self.state_path)

    def _event(self, event: dict) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            self._log(logging.WARNING, "[profit-skim] could not append to %s", self.log_path)

    # -- the step ------------------------------------------------------------

    def step(self, sim, now: float | None = None) -> None:
        if not sim.initialized:
            return
        now = time.time() if now is None else now
        self._reload_rules()
        changed = self._process_inbox(sim, now)
        changed |= self._collect_withdrawals(sim, now)

        # Accrue trading P&L since the last step. A new process's ledger
        # starts its trading_pnl at 0.
        pnl, instance = sim.trading_pnl, sim.instance_id
        last = self.state.get("last_seen") or {}
        delta = pnl - float(last.get("trading_pnl", 0.0)) if last.get("instance_id") == instance else pnl
        if last.get("instance_id") != instance or delta:
            self.state["last_seen"] = {"instance_id": instance, "trading_pnl": pnl}
            changed = True

        active = {r["name"] for r in self.rules if r["enabled"]}
        for name in [n for n in self.state["rules"] if n not in active]:
            self._log(logging.WARNING, "[profit-skim] rule %r removed or disabled (accrued $%.4f dropped)",
                      name, self.state["rules"][name].get("accrued", 0.0))
            del self.state["rules"][name]
            changed = True
        for rule in self.rules:
            if not rule["enabled"]:
                continue
            rs = self.state["rules"].get(rule["name"])
            if rs is None:
                # New rule: it accrues from now; its first run is the next slot.
                self.state["rules"][rule["name"]] = {
                    "accrued": 0.0, "last_slot_ts": latest_slot(rule, now).timestamp(), "created_ts": now,
                }
                changed = True
                continue
            rs["accrued"] = float(rs.get("accrued", 0.0)) + delta
            changed |= self._maybe_run(rule, rs, sim, now)
        if changed:
            self._save(sim)

    def _maybe_run(self, rule: dict, rs: dict, sim, now: float) -> bool:
        slot = latest_slot(rule, now)
        slot_ts = slot.timestamp()
        if slot_ts <= float(rs.get("last_slot_ts", 0.0)):
            return False
        open_positions = sim.open_positions()
        flat = not open_positions and not sim.pending_exact
        waited = now - slot_ts
        if not flat and waited < rule["max_wait_seconds"]:
            if (rule["name"], slot_ts) not in self._waiting_logged:
                self._waiting_logged.add((rule["name"], slot_ts))
                self._log(logging.INFO, "[profit-skim] %s due (%s) -- waiting for the ledger to go flat "
                          "(%d open position(s))", rule["name"], slot.isoformat(), len(open_positions))
            return False

        profit = float(rs.get("accrued", 0.0))
        wanted = profit * rule["fraction"] if profit > 0 else 0.0
        if rule["max_dollars"] is not None:
            wanted = min(wanted, rule["max_dollars"])
        if wanted < max(rule["min_dollars"], 1e-9):
            wanted = 0.0
        moved = sim.set_aside(wanted) if wanted > 0 else 0.0
        if rule["basis"] == "period" or profit > 0:
            rs["accrued"] = 0.0
        rs["last_slot_ts"] = slot_ts
        rs["last_run"] = {"ts": now, "profit": round(profit, 6), "skimmed": round(moved, 6)}
        event = {
            "ts": now, "event": "skim" if moved > 0 else "skip", "rule": rule["name"], "slot": slot.isoformat(),
            "period_profit": round(profit, 6), "fraction": rule["fraction"], "basis": rule["basis"],
            "wanted": round(wanted, 6), "skimmed": round(moved, 6), "flat": flat,
            "reserve_after": round(sim.reserve, 6), "trading_cash_after": round(sim.available_cash(), 6),
        }
        if moved > 0:
            self.state["set_aside_total"] = round(float(self.state.get("set_aside_total", 0.0)) + moved, 6)
            self._log(logging.WARNING,
                      "[profit-skim] %s: period profit $%.4f -> set aside $%.4f (%.0f%%)%s; reserve $%.4f, "
                      "trading cash $%.4f", rule["name"], profit, moved, rule["fraction"] * 100,
                      "" if moved >= wanted - 1e-9 else f" of ${wanted:.4f} wanted (capped at the available cash)",
                      sim.reserve, sim.available_cash())
        else:
            if profit <= 0:
                why = "not profitable"
            elif wanted == 0:
                why = f"${profit * rule['fraction']:.4f} is below min_dollars"
            else:
                why = "no available cash"
            event["reason"] = why
            self._log(logging.WARNING, "[profit-skim] %s: period profit $%.4f -- nothing set aside (%s)%s",
                      rule["name"], profit, why,
                      "; loss carried forward" if rule["basis"] == "carry_losses" and profit < 0 else "")
        if not flat:
            self._log(logging.WARNING, "[profit-skim] %s ran %.0f min late without going flat; open positions' "
                      "cost counted as a loss this period (it comes back as profit next period)",
                      rule["name"], waited / 60)
        self._event(event)
        return True

    def _process_inbox(self, sim, now: float) -> bool:
        try:
            st = os.stat(self.inbox_path)
            sig = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return False
        if sig == self._inbox_sig:
            return False
        self._inbox_sig = sig
        changed = False
        done = self.state["inbox_done"]
        for entry in read_inbox(self.inbox_path):
            entry_id = str(entry["id"])
            if entry_id in done or entry_id in self._declared:
                continue
            try:
                dollars = float(entry.get("dollars"))
            except (TypeError, ValueError):
                dollars = 0.0
            if dollars <= 0:
                done[entry_id] = "invalid"
                changed = True
                continue
            if entry["kind"] == "release":
                moved = sim.release(dollars)
                done[entry_id] = "released"
                self._log(logging.WARNING, "[profit-skim] released $%.4f of the reserve back to trading (%s asked "
                          "$%.4f); reserve $%.4f", moved, entry_id, dollars, sim.reserve)
                self._event({"ts": now, "event": "release", "id": entry_id, "asked": dollars, "released": moved,
                             "reserve_after": round(sim.reserve, 6)})
                changed = True
            elif now - float(entry.get("ts") or now) > WITHDRAWAL_DECLARATION_TTL_SECONDS:
                done[entry_id] = "expired"
                self._log(logging.WARNING, "[profit-skim] withdrawal %s ($%.2f) declared over 7 days ago and never "
                          "seen -- dropped", entry_id, dollars)
                self._event({"ts": now, "event": "withdrawal_expired", "id": entry_id, "dollars": dollars})
                changed = True
            elif self.apply_withdrawals_immediately:
                sim.apply_withdrawal_now(entry_id, dollars)
            else:
                sim.declare_withdrawal(entry_id, dollars)
                self._declared.add(entry_id)
                self._log(logging.WARNING, "[profit-skim] withdrawal %s of $%.2f declared -- applied when the "
                          "real balance drops by that much", entry_id, dollars)
        return changed

    def _collect_withdrawals(self, sim, now: float) -> bool:
        matched = sim.take_matched_withdrawals()
        for m in matched:
            self.state["inbox_done"][m["id"]] = "withdrawn"
            self._declared.discard(m["id"])
            self.state["withdrawn_total"] = round(float(self.state.get("withdrawn_total", 0.0)) + m["dollars"], 6)
            beyond = m["dollars"] - m["from_reserve"]
            self._log(logging.WARNING, "[profit-skim] withdrawal %s of $%.4f seen on the account: $%.4f from the "
                      "reserve%s; reserve $%.4f", m["id"], m["dollars"], m["from_reserve"],
                      f", ${beyond:.4f} from trading cash" if beyond > 1e-9 else "", sim.reserve)
            self._event({"ts": now, "event": "withdrawal", **m, "reserve_after": round(sim.reserve, 6)})
        return bool(matched)


# -- owner CLI -----------------------------------------------------------------


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


def cli(argv: list[str] | None, *, rules_path: str, state_path: str, log_path: str, inbox_path: str,
        status_path: str, prog: str) -> int:
    parser = argparse.ArgumentParser(
        prog=prog, description="Profit-skim rules for this runner's simulated bankroll (see shared/profit_skim.py).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="reserve, rules, accrued profit, recent skims")
    sub.add_parser("check-rules", help="validate the rules file and show when each rule runs next")
    w = sub.add_parser("withdraw", help="declare a withdrawal you are ABOUT to make on kalshi.com")
    w.add_argument("dollars", type=float)
    r = sub.add_parser("release", help="move money from the reserve back into trading cash")
    r.add_argument("dollars", type=float)
    args = parser.parse_args(argv)
    now = time.time()

    if args.cmd in ("withdraw", "release"):
        if args.dollars <= 0:
            parser.error("dollars must be > 0")
        entry = {"id": f"{args.cmd[0]}-{uuid.uuid4().hex[:8]}", "kind": "withdrawal" if args.cmd == "withdraw"
                 else "release", "dollars": round(args.dollars, 2), "ts": now}
        os.makedirs(os.path.dirname(os.path.abspath(inbox_path)), exist_ok=True)
        with open(inbox_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        reserve = float((_read_json(state_path) or {}).get("reserve") or 0.0)
        print(f"queued {entry['kind']} {entry['id']} for ${entry['dollars']:.2f} (reserve now ${reserve:.2f}).")
        if args.cmd == "withdraw":
            if entry["dollars"] > reserve + 1e-9:
                print(f"NOTE: that is more than the reserve -- the extra ${entry['dollars'] - reserve:.2f} comes out "
                      "of the runner's trading cash.")
            print("The runner picks it up on its next sync (~15s). Withdraw EXACTLY that amount on kalshi.com; the "
                  "ledger matches it when the balance drops. Check with `status` (look for it under matched).")
        return 0

    rules, rules_error = [], None
    try:
        with open(rules_path) as fh:
            rules = parse_rules(json.load(fh))
    except FileNotFoundError:
        rules_error = "missing"
    except (OSError, ValueError) as exc:
        rules_error = str(exc)
    print(f"rules file: {rules_path}" + (f"  [{'INVALID: ' if rules_error != 'missing' else ''}{rules_error}]"
                                          if rules_error else ""))
    state = _read_json(state_path) or {}
    for rule in rules:
        rs = (state.get("rules") or {}).get(rule["name"], {})
        line = (f"  {rule['name']}: {rule['fraction']:.0%} of {rule['basis']} profit at "
                f"{rule['at'].strftime('%H:%M')} {rule['tz'].key}"
                f"{'' if rule['days'] is None else ' on ' + ','.join(DAYS[d] for d in rule['days'])}"
                f"{'' if rule['enabled'] else ' (DISABLED)'}; next {next_slot(rule, now).strftime('%Y-%m-%d %H:%M %Z')}")
        if rs:
            line += f"; accrued ${float(rs.get('accrued', 0.0)):.4f}"
            if rs.get("last_run"):
                lr = rs["last_run"]
                line += f"; last run {_fmt_ts(lr['ts'])} (profit ${lr['profit']:.4f}, took ${lr['skimmed']:.4f})"
        elif args.cmd == "status":
            line += "; not picked up by the runner yet"
        print(line)
    if args.cmd == "check-rules":
        return 1 if rules_error and rules_error != "missing" else 0

    ledger = _read_json(status_path) or {}
    print(f"reserve: ${float(state.get('reserve') or ledger.get('reserve') or 0.0):.4f}  "
          f"(set aside ever ${float(state.get('set_aside_total', 0.0)):.4f}, withdrawn ever "
          f"${float(state.get('withdrawn_total', 0.0)):.4f}; state saved {_fmt_ts(state.get('updated_ts'))})")
    if ledger:
        print(f"ledger: trading cash ${float(ledger.get('sim_cash', 0.0)):.4f}, reserve "
              f"${float(ledger.get('reserve', 0.0)):.4f}, this run's trading P&L "
              f"${float(ledger.get('trading_pnl', 0.0)):.4f}, open positions {len(ledger.get('open_positions') or {})}"
              f" (status {_fmt_ts(ledger.get('updated_ts'))})")
        pending = ledger.get("pending_withdrawals") or {}
        if pending:
            print("declared withdrawals waiting for the balance to drop: "
                  + ", ".join(f"{i} ${d:.2f}" for i, d in pending.items()))
    queued = [e for e in read_inbox(inbox_path) if str(e["id"]) not in (state.get("inbox_done") or {})]
    if queued:
        print("inbox entries not finished: " + ", ".join(f"{e['kind']} {e['id']} ${e.get('dollars')}" for e in queued))
    try:
        with open(log_path) as fh:
            tail = fh.readlines()[-10:]
    except FileNotFoundError:
        tail = []
    if tail:
        print("recent events:")
        for line in tail:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            detail = {k: v for k, v in e.items() if k not in ("ts", "event")}
            print(f"  {_fmt_ts(e.get('ts'))} {e.get('event')}: {json.dumps(detail)}")
    return 0
