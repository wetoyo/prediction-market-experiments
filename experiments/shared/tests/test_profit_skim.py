"""profit_skim.ProfitSkimmer + the reserve in sim_bankroll.SimulatedBankroll."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from profit_skim import ProfitSkimmer, RuleError, cli, latest_slot, next_slot, parse_rules
from sim_bankroll import SimulatedBankroll

NY = ZoneInfo("America/New_York")
TICKER = "KXBTCD-26SEP2616-T83999.99"
DAILY = {"name": "daily-10pct", "at": "00:05", "timezone": "America/New_York", "fraction": 0.10, "basis": "period"}


def ts(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=NY).timestamp()


def _settlement(ticker=TICKER, result="yes"):
    return {"ticker": ticker, "market_result": result}


class Account:
    """The real balance, moved the way Kalshi moves it."""

    def __init__(self, balance=10.0):
        self.balance = balance


def _trade(sim, account, cost, payout, ticker=TICKER):
    """One round trip: buy for `cost`, settle paying `payout`."""
    sim.book_fill(ticker, "yes", 1.0, cost)
    account.balance -= cost
    sim.apply_settlement(_settlement(ticker, "yes" if payout else "no"))
    account.balance += payout


def _check(sim, account):
    return sim.check(account.balance, sim.fill_seq)


@pytest.fixture
def files(tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [DAILY]}))
    return {
        "rules_path": str(rules), "state_path": str(tmp_path / "state.json"),
        "log_path": str(tmp_path / "skims.jsonl"), "inbox_path": str(tmp_path / "inbox.jsonl"),
    }


def _setup(files, balance=10.0, start=None):
    account = Account(balance)
    skimmer = ProfitSkimmer(**files)
    sim = SimulatedBankroll()
    sim.restore_reserve(skimmer.saved_reserve)
    sim.initialize(account.balance, sim.fill_seq)
    skimmer.step(sim, now=start or ts(26, 12))  # rule first seen: accrues from here
    return account, skimmer, sim


def _events(files):
    try:
        with open(files["log_path"]) as fh:
            return [json.loads(line) for line in fh]
    except FileNotFoundError:
        return []


class TestRules:
    def test_valid_rule_normalizes(self):
        (rule,) = parse_rules({"rules": [DAILY]})
        assert rule["fraction"] == 0.10 and rule["basis"] == "period" and rule["max_wait_seconds"] == 180 * 60

    @pytest.mark.parametrize("bad", [
        {"at": "24:05"}, {"at": "5"}, {"fraction": 0}, {"fraction": 1.5}, {"basis": "weekly"},
        {"timezone": "Mars/Base"}, {"days": ["monday"]}, {"days": []}, {"max_dollars": 0}, {"typo_field": 1},
        {"name": ""}, {"at": []}, {"at": ["06:05", "25:00"]}, {"at": 6},
    ])
    def test_invalid_rules_are_rejected(self, bad):
        with pytest.raises(RuleError):
            parse_rules({"rules": [{**DAILY, **bad}]})

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(RuleError):
            parse_rules({"rules": [DAILY, DAILY]})

    def test_latest_slot_is_just_after_midnight(self):
        (rule,) = parse_rules({"rules": [DAILY]})
        assert latest_slot(rule, ts(27, 0, 4)) == datetime(2026, 9, 26, 0, 5, tzinfo=NY)
        assert latest_slot(rule, ts(27, 0, 5)) == datetime(2026, 9, 27, 0, 5, tzinfo=NY)

    def test_days_filter(self):
        (rule,) = parse_rules({"rules": [{**DAILY, "days": ["mon"]}]})
        # 2026-09-27 is a Sunday; the last Monday was the 21st.
        assert latest_slot(rule, ts(27, 12)) == datetime(2026, 9, 21, 0, 5, tzinfo=NY)

    def test_several_times_a_day(self):
        (rule,) = parse_rules({"rules": [{**DAILY, "at": ["18:05", "00:05", "12:05", "06:05"]}]})
        assert latest_slot(rule, ts(27, 0, 4)) == datetime(2026, 9, 26, 18, 5, tzinfo=NY)
        assert latest_slot(rule, ts(27, 6, 5)) == datetime(2026, 9, 27, 6, 5, tzinfo=NY)
        assert latest_slot(rule, ts(27, 17)) == datetime(2026, 9, 27, 12, 5, tzinfo=NY)
        assert next_slot(rule, ts(27, 17)) == datetime(2026, 9, 27, 18, 5, tzinfo=NY)
        assert next_slot(rule, ts(27, 19)) == datetime(2026, 9, 28, 0, 5, tzinfo=NY)

    def test_several_times_with_days_filter(self):
        (rule,) = parse_rules({"rules": [{**DAILY, "at": ["00:05", "12:05"], "days": ["mon"]}]})
        assert latest_slot(rule, ts(27, 12)) == datetime(2026, 9, 21, 12, 5, tzinfo=NY)
        assert next_slot(rule, ts(27, 12)) == datetime(2026, 9, 28, 0, 5, tzinfo=NY)


class TestSkim:
    def test_profitable_day_sets_aside_ten_percent(self, files):
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.90, 1.0)  # +0.10
        _trade(sim, account, 0.80, 1.0)  # +0.20
        skimmer.step(sim, now=ts(26, 20))
        assert sim.reserve == 0.0  # not due until 00:05
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == pytest.approx(0.03)
        assert sim.available_cash() == pytest.approx(10.30 - 0.03)
        assert sim.sizing_cash(account.balance) == pytest.approx(10.27)
        (event,) = _events(files)
        assert event["event"] == "skim" and event["period_profit"] == pytest.approx(0.30)
        # the real balance never moved for the skim: the check stays clean
        assert _check(sim, account).status == "ok"
        # runs once per slot
        skimmer.step(sim, now=ts(27, 0, 6))
        assert sim.reserve == pytest.approx(0.03) and len(_events(files)) == 1

    def test_every_six_hours_skims_each_period_separately(self, files):
        with open(files["rules_path"], "w") as fh:
            json.dump({"rules": [{**DAILY, "at": ["00:05", "06:05", "12:05", "18:05"]}]}, fh)
        account, skimmer, sim = _setup(files, start=ts(26, 12, 10))
        _trade(sim, account, 0.80, 1.0)  # +0.20
        skimmer.step(sim, now=ts(26, 18, 5))
        assert sim.reserve == pytest.approx(0.02)
        _trade(sim, account, 0.95, 0.0)  # -0.95: this period takes nothing
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == pytest.approx(0.02)
        _trade(sim, account, 0.50, 1.0)  # +0.50, not netted against the last period
        skimmer.step(sim, now=ts(27, 6, 5))
        assert sim.reserve == pytest.approx(0.07)
        assert [e["event"] for e in _events(files)] == ["skim", "skip", "skim"]

    def test_losing_day_takes_nothing_and_is_forgotten(self, files):
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.95, 0.0)  # -0.95
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == 0.0
        assert _events(files)[-1]["event"] == "skip"
        _trade(sim, account, 0.50, 1.0)  # +0.50 the next day
        skimmer.step(sim, now=ts(28, 0, 5))
        assert sim.reserve == pytest.approx(0.05)  # the loss isn't netted against it

    def test_carry_losses_needs_the_loss_made_back(self, files):
        with open(files["rules_path"], "w") as fh:
            json.dump({"rules": [{**DAILY, "basis": "carry_losses"}]}, fh)
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.95, 0.0)  # -0.95
        skimmer.step(sim, now=ts(27, 0, 5))
        _trade(sim, account, 0.50, 1.0)  # +0.50: still -0.45 overall
        skimmer.step(sim, now=ts(28, 0, 5))
        assert sim.reserve == 0.0
        for _ in range(3):
            _trade(sim, account, 0.50, 1.0)  # +1.50: +1.05 overall
        skimmer.step(sim, now=ts(29, 0, 5))
        assert sim.reserve == pytest.approx(0.105)

    def test_profit_before_the_rule_existed_is_not_taken(self, files):
        account = Account(10.0)
        skimmer = ProfitSkimmer(**files)
        sim = SimulatedBankroll()
        sim.initialize(account.balance, sim.fill_seq)
        _trade(sim, account, 0.5, 1.0)
        skimmer.step(sim, now=ts(26, 12))  # first sight of the rule
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == 0.0

    def test_waits_for_flat_then_counts_the_settlement(self, files):
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.80, 1.0)  # +0.20
        sim.book_fill("KXBTC15M-OPEN", "yes", 1.0, 0.90)  # open over midnight
        account.balance -= 0.90
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == 0.0 and not _events(files)
        sim.apply_settlement(_settlement("KXBTC15M-OPEN", "yes"))
        account.balance += 1.0
        skimmer.step(sim, now=ts(27, 0, 16))
        assert sim.reserve == pytest.approx(0.03)  # 10% of 0.20 + 0.10
        assert _check(sim, account).status == "ok"

    def test_runs_anyway_after_max_wait(self, files):
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.50, 1.0)  # +0.50
        sim.book_fill("STUCK", "yes", 1.0, 0.10)
        skimmer.step(sim, now=ts(27, 3, 6))  # 181 min after 00:05
        assert sim.reserve == pytest.approx(0.04)  # 10% of (0.50 - 0.10)
        assert _events(files)[-1]["flat"] is False

    def test_min_and_max_dollars(self, files):
        with open(files["rules_path"], "w") as fh:
            json.dump({"rules": [{**DAILY, "fraction": 0.5, "min_dollars": 0.10, "max_dollars": 0.25}]}, fh)
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.90, 1.0)  # +0.10 -> 0.05 < min
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == 0.0
        for _ in range(10):
            _trade(sim, account, 0.90, 1.0)  # +1.00 -> 0.50, capped
        skimmer.step(sim, now=ts(28, 0, 5))
        assert sim.reserve == pytest.approx(0.25)

    def test_restart_keeps_the_reserve_and_the_accrued_profit(self, files):
        account, skimmer, sim = _setup(files)
        _trade(sim, account, 0.50, 1.0)  # +0.50
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == pytest.approx(0.05)
        _trade(sim, account, 0.60, 1.0)  # +0.40 on the 27th, before the restart
        skimmer.step(sim, now=ts(27, 12))

        skimmer2 = ProfitSkimmer(**files)  # new process
        sim2 = SimulatedBankroll()
        sim2.restore_reserve(skimmer2.saved_reserve)
        sim2.initialize(account.balance, sim2.fill_seq)
        assert sim2.available_cash() == pytest.approx(account.balance - 0.05)  # reserve not re-absorbed
        skimmer2.step(sim2, now=ts(27, 13))
        _trade(sim2, account, 0.90, 1.0)  # +0.10 after the restart
        skimmer2.step(sim2, now=ts(28, 0, 5))
        assert sim2.reserve == pytest.approx(0.05 + 0.05)  # 10% of 0.40 + 0.10
        assert _check(sim2, account).status == "ok"

    def test_rules_reload_and_a_bad_edit_keeps_the_old_rules(self, files):
        account, skimmer, sim = _setup(files)
        with open(files["rules_path"], "w") as fh:
            fh.write('{"rules": [ {"name": "oops", ')  # half-written edit
        _trade(sim, account, 0.50, 1.0)
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.reserve == pytest.approx(0.05)  # the old rule still ran
        with open(files["rules_path"], "w") as fh:
            json.dump({"rules": [{**DAILY, "fraction": 0.5}]}, fh)
        skimmer.step(sim, now=ts(27, 1))
        assert skimmer.rules[0]["fraction"] == 0.5

    def test_skim_is_capped_at_available_cash(self, files):
        account, skimmer, sim = _setup(files, balance=1.0)
        with open(files["rules_path"], "w") as fh:
            json.dump({"rules": [{**DAILY, "fraction": 1.0}]}, fh)
        _trade(sim, account, 0.50, 1.0)  # +0.50
        sim.set_aside(1.3)  # (as if an earlier skim took almost everything)
        skimmer.step(sim, now=ts(27, 0, 5))
        assert sim.available_cash() == pytest.approx(0.0)


class TestWithdrawals:
    def _declare(self, files, dollars, kind="withdrawal"):
        with open(files["inbox_path"], "a") as fh:
            fh.write(json.dumps({"id": f"{kind[0]}-{dollars}", "kind": kind, "dollars": dollars, "ts": ts(27, 9)}) + "\n")

    def test_declared_withdrawal_comes_out_of_the_reserve(self, files):
        account, skimmer, sim = _setup(files)
        sim.set_aside(2.0)
        self._declare(files, 1.5)
        skimmer.step(sim, now=ts(27, 10))
        assert sim.pending_withdrawals == {"w-1.5": 1.5}
        assert _check(sim, account).status == "ok"  # not withdrawn yet: nothing happens
        account.balance -= 1.5  # owner withdraws on kalshi.com
        assert _check(sim, account).status == "withdrawal"
        assert sim.reserve == pytest.approx(0.5) and sim.available_cash() == pytest.approx(8.0)
        assert _check(sim, account).status == "ok"
        skimmer.step(sim, now=ts(27, 10, 1))
        state = json.loads(open(files["state_path"]).read())
        assert state["inbox_done"]["w-1.5"] == "withdrawn" and state["reserve"] == pytest.approx(0.5)
        assert _events(files)[-1]["event"] == "withdrawal"
        # a new process doesn't declare it again
        skimmer2 = ProfitSkimmer(**files)
        sim2 = SimulatedBankroll()
        sim2.restore_reserve(skimmer2.saved_reserve)
        sim2.initialize(account.balance, sim2.fill_seq)
        skimmer2.step(sim2, now=ts(27, 11))
        assert not sim2.pending_withdrawals and sim2.available_cash() == pytest.approx(8.0)

    def test_withdrawal_beyond_the_reserve_takes_trading_cash(self, files):
        account, skimmer, sim = _setup(files)
        sim.set_aside(1.0)
        self._declare(files, 3.0)
        skimmer.step(sim, now=ts(27, 10))
        account.balance -= 3.0
        assert _check(sim, account).status == "withdrawal"
        assert sim.reserve == 0.0 and sim.available_cash() == pytest.approx(7.0)

    def test_undeclared_withdrawal_diverges_and_resyncs_trading_cash(self, files):
        account, skimmer, sim = _setup(files)
        sim.set_aside(1.0)
        account.balance -= 1.0
        assert _check(sim, account).status == "suspect"
        assert _check(sim, account).status == "diverged"
        assert sim.reserve == pytest.approx(1.0) and sim.available_cash() == pytest.approx(8.0)
        assert _check(sim, account).status == "ok"

    def test_release_returns_reserve_to_trading(self, files):
        account, skimmer, sim = _setup(files)
        sim.set_aside(2.0)
        self._declare(files, 0.5, kind="release")
        skimmer.step(sim, now=ts(27, 10))
        assert sim.reserve == pytest.approx(1.5) and sim.available_cash() == pytest.approx(8.5)
        assert _check(sim, account).status == "ok"

    def test_shared_mode_applies_at_once(self, files):
        account = Account(10.0)
        skimmer = ProfitSkimmer(**files, apply_withdrawals_immediately=True)
        sim = SimulatedBankroll()
        sim.initialize(account.balance, sim.fill_seq)
        sim.set_aside(2.0)
        self._declare(files, 1.0)
        skimmer.step(sim, now=ts(27, 10))
        assert sim.reserve == pytest.approx(1.0)
        assert json.loads(open(files["state_path"]).read())["inbox_done"]["w-1.0"] == "withdrawn"


def test_cli_status_and_withdraw(files, capsys):
    account, skimmer, sim = _setup(files)
    _trade(sim, account, 0.50, 1.0)
    skimmer.step(sim, now=ts(27, 0, 5))
    paths = dict(files, status_path=files["state_path"] + ".missing", prog="t")
    assert cli(["withdraw", "0.05"], **paths) == 0
    assert cli(["status"], **paths) == 0
    out = capsys.readouterr().out
    assert "daily-10pct" in out and "reserve: $0.0500" in out and "inbox entries not finished" in out
    assert cli(["check-rules"], **paths) == 0


def test_reconciler_counts_the_reserve():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resolution_alpha"))
    from account_reconciler import AccountReconciler

    rec = AccountReconciler()
    status = {"initialized": True, "updated_ts": 100.0, "sim_cash": 10.0, "reserve": 0.0, "allocation_epoch": "e"}
    assert rec.check({"ra": status}, 10.0, 100.0).status == "rebaselined"
    skimmed = dict(status, sim_cash=9.0, reserve=1.0)
    assert rec.check({"ra": skimmed}, 10.0, 100.0).status == "ok"
