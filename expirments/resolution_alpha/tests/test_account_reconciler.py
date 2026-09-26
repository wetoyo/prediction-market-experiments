"""Phase 3: several runners on one account -- order tagging, shared-account
mode (OrderManager), ledger resume, and account_reconciler's summed check.
The fake account uses the same payload shapes as test_sim_bankroll.py.
"""

import json
import time

import pytest

import config
from account_reconciler import AccountReconciler, attribute, overlapping_tickers
from order_manager import OrderManager
from sim_bankroll import SimulatedBankroll

T1 = "KXBTC15M-26SEP260100-00"
T2 = "KXETH15M-26SEP260100-00"


class _Account:
    """One Kalshi account shared by several runners: real cash semantics,
    unique order ids, exact costs on GET /portfolio/orders/{id}."""

    def __init__(self, balance):
        self.balance = balance
        self.orders = {}
        self.settlements = []
        self.positions = []
        self.placed = []

    def get_balance(self):
        return {"balance_dollars": f"{self.balance:.4f}"}

    def get_positions(self):
        return {"market_positions": self.positions}

    def place_order(self, *, ticker, side, count, price, client_order_id=None, **kw):
        # 2 NO @ 0.72 (api ask @ 0.28), same numbers as test_sim_bankroll's
        order_id = f"o{len(self.orders) + 1}"
        self.balance -= 1.44 + 0.0283
        self.orders[order_id] = {
            "order_id": order_id, "ticker": ticker, "client_order_id": client_order_id, "fill_count_fp": "2.00",
            "taker_fill_cost_dollars": "1.440000", "maker_fill_cost_dollars": "0.000000",
            "taker_fees_dollars": "0.028300", "maker_fees_dollars": "0.000000",
        }
        self.placed.append(client_order_id)
        return {"order_id": order_id, "fill_count": "2.00", "average_fill_price": "0.2800",
                "average_fee_paid": "0.0142", "remaining_count": "0.00"}

    def settle(self, ticker, result, credit):
        self.settlements.append({"ticker": ticker, "market_result": result, "revenue": int(credit * 100)})
        self.balance += credit

    def _request(self, method, path, params=None):
        if path.startswith("/portfolio/orders/"):
            return {"order": self.orders[path.rsplit("/", 1)[1]]}
        if path == "/portfolio/orders":  # min_ts ignored; callers dedupe by order id
            return {"orders": list(self.orders.values()), "cursor": ""}
        if path == "/portfolio/settlements":
            return {"settlements": self.settlements, "cursor": ""}
        raise AssertionError(path)


def _runner(account, tag, status_path, monkeypatch, allocation_dollars=None, shared=True):
    om = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    om.dry_run = False
    om._client = account
    om._sim = SimulatedBankroll(allocation_dollars=allocation_dollars)
    om._size_from_sim = True
    om._shared_account = shared
    om.order_tag = tag
    om._status_path = str(status_path)
    return om


def _poll(om, monkeypatch):
    # every runner has its own LOG_DIR in production; point config at this one's
    monkeypatch.setattr(config, "SIM_BANKROLL_STATUS_PATH", om._status_path)
    om.get_balance_dollars()
    om.sync_sim_bankroll()


def _status(om):
    return json.loads(open(om._status_path).read())


@pytest.fixture(autouse=True)
def _divergence_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SIM_BANKROLL_DIVERGENCE_LOG_PATH", str(tmp_path / "div.jsonl"))


class TestOrderTagging:
    def test_client_order_id_carries_the_runner_tag(self, tmp_path, monkeypatch):
        account = _Account(10.0)
        om = _runner(account, "ra", tmp_path / "a.json", monkeypatch)
        om.buy_favored_side(T1, "no", 2, 0.72)
        om.buy_favored_side(T1, "no", 2, 0.72)
        om.buy_favored_side(T1, "yes", 2, 0.72)
        first, second, third = account.placed
        assert first.startswith("ra-n-") and second.startswith("ra-n-") and third.startswith("ra-y-")
        assert first != second and len(first) <= 36  # no longer than the bare uuid4 it replaced

    def test_tag_with_a_dash_is_rejected(self):
        with pytest.raises(ValueError):
            OrderManager(dry_run=True, order_tag="a-b")

    def test_shared_account_needs_a_dollar_allocation(self):
        with pytest.raises(ValueError):
            OrderManager(dry_run=True, shared_account=True, allocation_dollars=0.0)


class TestSharedAccountMode:
    def test_other_runners_moves_are_not_a_divergence(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        account.balance -= 5.0  # another runner's fill
        for _ in range(3):
            _poll(a, monkeypatch)
        assert a._sim.divergence_count == 0 and a._sim.cash == pytest.approx(8.0)
        assert _status(a)["last_check"]["status"] == "shared"

    def test_fresh_ledger_does_not_adopt_account_positions(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        account.positions = [{"ticker": T1, "position_fp": "-2.00"}]  # someone else's
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        assert a._sim.positions == {}

    def test_restart_resumes_cash_and_positions_from_its_own_status(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        a.buy_favored_side(T1, "no", 2, 0.72)
        _poll(a, monkeypatch)
        before = _status(a)
        account.settle(T1, "no", 2.0)  # settles while the runner is down

        restarted = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(restarted, monkeypatch)  # resumes
        _poll(restarted, monkeypatch)  # picks up the missed settlement
        assert restarted._sim.resumed_from == before["instance_id"]
        assert restarted._sim.allocation_epoch == before["allocation_epoch"]
        assert restarted._sim.cash == pytest.approx(8.0 - 1.4683 + 2.0)
        assert restarted._sim.positions == {}

    def test_fill_lost_in_a_crash_is_recovered_on_resume(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        a.buy_favored_side(T1, "no", 2, 0.72)  # booked in memory, status not rewritten: then it dies
        assert _status(a)["sim_cash"] == pytest.approx(8.0)

        restarted = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(restarted, monkeypatch)
        assert restarted._sim.recovered_orders == 1
        assert restarted._sim.cash == pytest.approx(8.0 - 1.4683)
        assert restarted._sim.positions[T1].no == 2.0
        account.settle(T1, "no", 2.0)
        _poll(restarted, monkeypatch)
        assert restarted._sim.cash == pytest.approx(8.0 - 1.4683 + 2.0)

    def test_fills_the_status_already_has_are_not_booked_twice(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        a.buy_favored_side(T1, "no", 2, 0.72)
        _poll(a, monkeypatch)  # written out, exact cost applied
        restarted = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(restarted, monkeypatch)
        assert restarted._sim.recovered_orders == 0
        assert restarted._sim.cash == pytest.approx(8.0 - 1.4683)

    def test_no_sizing_until_the_ledger_has_resumed(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        monkeypatch.setattr(config, "SIM_BANKROLL_STATUS_PATH", a._status_path)
        assert a.get_balance_dollars() == 0.0
        a.sync_sim_bankroll()
        assert a.get_balance_dollars() == pytest.approx(8.0)

    def test_lost_state_with_recent_fills_refuses_to_trade(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        a.buy_favored_side(T1, "no", 2, 0.72)
        _poll(a, monkeypatch)
        (tmp_path / "a.json").unlink()  # state lost

        restarted = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        for _ in range(2):
            _poll(restarted, monkeypatch)
        assert not restarted._sim.initialized and restarted.get_balance_dollars() == 0.0

        monkeypatch.setattr(config, "SIM_BANKROLL_ALLOW_FRESH_ALLOCATION", True)
        _poll(restarted, monkeypatch)
        assert restarted._sim.initialized and restarted.get_balance_dollars() == pytest.approx(8.0)

    def test_a_brand_new_runner_starts_fresh(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        other = _runner(account, "b", tmp_path / "b.json", monkeypatch, allocation_dollars=3.0)
        _poll(other, monkeypatch)
        other.buy_favored_side(T2, "no", 2, 0.72)  # another tag's history doesn't count
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        assert a._sim.initialized and a._sim.cash == pytest.approx(8.0)

    def test_two_runners_on_one_status_file_dont_clobber(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "x.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        b = _runner(account, "b", tmp_path / "x.json", monkeypatch, allocation_dollars=3.0)
        _poll(b, monkeypatch)
        _poll(b, monkeypatch)
        assert _status(a)["order_tag"] == "a"

    def test_status_from_another_tag_is_not_resumed(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "x.json", monkeypatch, allocation_dollars=8.0)
        _poll(a, monkeypatch)
        b = _runner(account, "b", tmp_path / "x.json", monkeypatch, allocation_dollars=3.0)
        _poll(b, monkeypatch)
        assert b._sim.resumed_from is None and b._sim.cash == pytest.approx(3.0)

    def test_single_runner_mode_is_unchanged(self, tmp_path, monkeypatch):
        account = _Account(10.0)
        a = _runner(account, "ra", tmp_path / "a.json", monkeypatch, shared=False)
        account.positions = [{"ticker": T1, "position_fp": "-2.00"}]
        _poll(a, monkeypatch)
        assert a._sim.positions[T1].no == 2.0  # still adopts
        account.balance += 5.0
        _poll(a, monkeypatch)
        _poll(a, monkeypatch)
        assert a._sim.divergence_count == 1  # still self-checks


class TestAccountReconcilerWithRealLedgers:
    def test_two_runners_trading_and_settling_stay_ok(self, tmp_path, monkeypatch):
        account = _Account(20.0)  # $9 of it unallocated reserve
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        b = _runner(account, "b", tmp_path / "b.json", monkeypatch, allocation_dollars=3.0)
        rec = AccountReconciler()

        def tick():
            _poll(a, monkeypatch)
            _poll(b, monkeypatch)
            return rec.check({"a": _status(a), "b": _status(b)}, account.balance, time.time())

        assert tick().status == "rebaselined"  # first view: sets the baseline
        assert tick().status == "ok"
        a.buy_favored_side(T1, "no", 2, 0.72)
        b.buy_favored_side(T2, "no", 2, 0.72)
        tick()  # exact costs
        assert tick().status == "ok"
        account.settle(T1, "no", 2.0)
        account.settle(T2, "yes", 0.0)
        tick()
        assert tick().status == "ok"
        assert rec.divergence_count == 0
        assert a._sim.cash == pytest.approx(8.0 - 1.4683 + 2.0)
        assert b._sim.cash == pytest.approx(3.0 - 1.4683)

    def test_a_missed_fill_is_confirmed_and_attributed(self, tmp_path, monkeypatch):
        account = _Account(20.0)
        a = _runner(account, "a", tmp_path / "a.json", monkeypatch, allocation_dollars=8.0)
        b = _runner(account, "b", tmp_path / "b.json", monkeypatch, allocation_dollars=3.0)
        rec = AccountReconciler()

        def tick():
            _poll(a, monkeypatch)
            _poll(b, monkeypatch)
            return rec.check({"a": _status(a), "b": _status(b)}, account.balance, time.time())

        tick()
        tick()
        b._sim.record_fill = lambda *args: 0.0  # b's ledger drops its own fill
        b.buy_favored_side(T2, "no", 2, 0.72)
        assert tick().status == "suspect"
        result = tick()
        assert result.status == "diverged" and result.gap == pytest.approx(1.4683)
        statuses = {"a": _status(a), "b": _status(b)}
        blame = attribute(statuses, list(account.orders.values()), [])
        assert [o["order_id"] for o in blame["missed_by_ledger"]["b"]] == ["o1"]
        assert "a" not in blame["missed_by_ledger"] and blame["untagged_filled_orders"] == []
        assert tick().status == "ok"  # re-baselined


def _st(cash, epoch="e1", fill_seq=0, updated=None, pending=0, positions=None, tag="a"):
    return {"initialized": True, "sim_cash": cash, "allocation_epoch": epoch, "fill_seq": fill_seq,
            "updated_ts": time.time() if updated is None else updated, "pending_exact_orders": pending,
            "open_positions": positions or {}, "order_tag": tag}


class TestAccountReconcilerCheck:
    def test_one_check_transient_is_not_counted(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 10.0, now)
        assert rec.check({"a": _st(10.0)}, 12.0, now).status == "suspect"  # settlement not yet in the ledger
        assert rec.check({"a": _st(12.0)}, 12.0, now).status == "ok"
        assert rec.divergence_count == 0

    def test_a_new_fill_between_checks_resets_the_suspect(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 10.0, now)
        rec.check({"a": _st(10.0, fill_seq=1)}, 9.0, now)
        assert rec.check({"a": _st(10.0, fill_seq=2)}, 9.0, now).status == "suspect"

    def test_a_changing_gap_is_not_confirmed(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 10.0, now)
        rec.check({"a": _st(10.0)}, 9.0, now)
        assert rec.check({"a": _st(10.0)}, 8.0, now).status == "suspect"

    def test_fresh_allocation_rebaselines_but_resume_does_not(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 20.0, now)
        assert rec.check({"a": _st(4.0, epoch="e2")}, 20.0, now).status == "rebaselined"
        assert rec.check({"a": _st(4.0, epoch="e2")}, 20.0, now).status == "ok"

    def test_fresh_allocation_is_called_out(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 20.0, now)
        result = rec.check({"a": _st(4.0, epoch="e2")}, 20.0, now)
        assert "a FRESHLY ALLOCATED (epoch e1 -> e2)" in result.reason

    def test_a_new_ledger_rebaselines(self):
        rec = AccountReconciler()
        now = time.time()
        rec.check({"a": _st(10.0)}, 20.0, now)
        assert rec.check({"a": _st(10.0), "b": _st(5.0, tag="b")}, 20.0, now).status == "rebaselined"

    @pytest.mark.parametrize("status, reason", [
        (None, "no status file"),
        ({"initialized": False}, "not initialized"),
        (_st(10.0, updated=0.0), "old"),
        (_st(10.0, pending=1), "exact fill cost"),
    ])
    def test_inconclusive(self, status, reason):
        result = AccountReconciler().check({"a": status}, 10.0, time.time())
        assert result.status == "inconclusive" and reason in result.reason

    def test_overlapping_tickers_are_flagged(self):
        statuses = {"a": _st(1.0, positions={T1: {}}), "b": _st(1.0, positions={T1: {}, T2: {}})}
        assert overlapping_tickers(statuses) == {T1: ["a", "b"]}


class TestAttribute:
    def test_untagged_fill_and_settlement_holders(self):
        statuses = {"a": _st(1.0, positions={T1: {}})}
        statuses["a"]["recent_order_ids"] = {"o1": 0.0}
        orders = [
            {"order_id": "o1", "client_order_id": "a-x", "fill_count_fp": "2.00", "taker_fill_cost_dollars": "1.0"},
            {"order_id": "o2", "client_order_id": "3f2c-uuid", "fill_count_fp": "1.00",
             "taker_fill_cost_dollars": "0.9"},
            {"order_id": "o3", "client_order_id": "a-y", "fill_count_fp": "0.00"},  # zero fill: ignored
        ]
        blame = attribute(statuses, orders, [{"ticker": T1, "market_result": "no"}])
        assert blame["missed_by_ledger"] == {}
        assert [o["order_id"] for o in blame["untagged_filled_orders"]] == ["o2"]
        assert blame["settlements_in_window"] == [{"ticker": T1, "market_result": "no", "held_by": ["a"]}]
