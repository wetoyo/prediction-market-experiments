"""sim_bankroll's holds / book_fill and tagged_ledger.TaggedLedger, against
fake_kalshi.FakeKalshi. No network, no credentials."""

import json
import sys
import time
from pathlib import Path

import pytest

from fake_kalshi import FakeKalshi
from sim_bankroll import SimulatedBankroll
from tagged_ledger import TaggedLedger, split_client_order_id

T1 = "KXPGATOUR-26OCT04-SSCH"
T2 = "KXPGATOUR-26OCT04-RMCI"


def _ledger(kalshi, tmp_path, tag="gfa", name="sim.json", **kw):
    return TaggedLedger(
        kalshi, order_tag=tag, status_path=str(tmp_path / name),
        divergence_path=str(tmp_path / (name + ".div.jsonl")), **kw,
    )


def _buy(kalshi, ledger, ticker, side, contracts, limit, fill=None):
    """What an order manager does: tagged POST, then record_order."""
    kalshi.next_fill = fill
    api_side, price = ("bid", limit) if side == "yes" else ("ask", round(1.0 - limit, 4))
    response = kalshi.place_order(ticker=ticker, side=api_side, count=f"{contracts:.2f}", price=f"{price:.4f}",
                                  client_order_id=ledger.new_client_order_id(side))
    ledger.record_order(ticker, side, contracts, limit, response)
    return response["order_id"]


def _status(ledger):
    return json.loads(Path(ledger.status_path).read_text())


class TestSimBankrollHolds:
    def test_holds_come_out_of_available_not_cash(self):
        sim = SimulatedBankroll()
        assert sim.initialize(10.0, 0) == 10.0
        sim.set_hold("o1", 2.5)
        assert sim.available_cash() == pytest.approx(7.5)
        assert sim.cash == pytest.approx(10.0)
        assert sim.sizing_cash(100.0) == pytest.approx(7.5)
        assert sim.check(7.5, sim.fill_seq).status == "ok"
        sim.set_hold("o1", 0)
        assert sim.available_cash() == pytest.approx(10.0)

    def test_initialize_with_holds_allocates_the_available_cash(self):
        sim = SimulatedBankroll(allocation_fraction=0.5)
        assert sim.initialize(20.0, 0, holds={"o1": 4.0}) == pytest.approx(10.0)
        assert sim.cash == pytest.approx(14.0)

    def test_resync_keeps_holds(self):
        sim = SimulatedBankroll()
        sim.initialize(10.0, 0, holds={"o1": 1.0})
        assert sim.check(8.0, sim.fill_seq).status == "suspect"
        assert sim.check(8.0, sim.fill_seq).status == "diverged"
        assert sim.available_cash() == pytest.approx(8.0)
        assert sim.holds == {"o1": 1.0}

    def test_snapshot_resume_round_trips_holds(self):
        sim = SimulatedBankroll()
        sim.initialize(10.0, 0, holds={"o1": 1.5})
        state = sim.snapshot()
        assert state["sim_cash"] == pytest.approx(10.0) and state["ledger_cash"] == pytest.approx(11.5)
        again = SimulatedBankroll()
        assert again.resume(state, 10.0, 0) == pytest.approx(10.0)
        assert again.holds == {"o1": 1.5} and again.offset == pytest.approx(0.0)

    def test_resume_reads_old_status_files_without_ledger_cash(self):
        again = SimulatedBankroll()
        assert again.resume({"sim_cash": 5.0}, 5.0, 0) == pytest.approx(5.0)

    def test_book_fill_is_additive_and_sets_opened_ts(self):
        sim = SimulatedBankroll()
        sim.initialize(10.0, 0)
        sim.book_fill(T1, "yes", 2.0, 0.5, "o1", opened_ts=123.0)
        sim.book_fill(T1, "yes", 1.0, 0.25, "o1")
        sim.book_fill(T1, "yes", 0.0, -0.01, "o1")  # cost correction only
        assert sim.cash == pytest.approx(10.0 - 0.74)
        assert sim.positions[T1].yes == 3.0 and sim.positions[T1].opened_ts == 123.0
        assert sim.fill_seq == 3 and "o1" in sim.recent_order_ids


class TestClientOrderId:
    def test_tagged_ids_round_trip(self, tmp_path):
        ledger = _ledger(FakeKalshi(), tmp_path, tag="bip")
        cid = ledger.new_client_order_id("no")
        assert len(cid) <= 36
        assert split_client_order_id(cid) == ("bip", "no")
        assert split_client_order_id("ra-" + "0" * 32) == ("ra", None)
        assert split_client_order_id("0a1b2c3d-1111-2222-3333-444455556666") == (None, None)

    @pytest.mark.parametrize("tag", ["", "a-b", "toolong"])
    def test_bad_tags_are_rejected(self, tmp_path, tag):
        with pytest.raises(ValueError):
            _ledger(FakeKalshi(), tmp_path, tag=tag)

    def test_shared_mode_needs_a_dollar_allocation(self, tmp_path):
        with pytest.raises(ValueError):
            _ledger(FakeKalshi(), tmp_path, shared_account=True)


class TestSingleRunner:
    def test_gtc_partial_fill_rests_then_fills_later(self, tmp_path):
        kalshi = FakeKalshi(100.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()  # initialize
        assert ledger.sim.available_cash() == pytest.approx(100.0)

        oid = _buy(kalshi, ledger, T1, "yes", 10, 0.20, fill=4)
        assert ledger.sim.positions[T1].yes == 4.0
        assert ledger.sim.holds[oid] == pytest.approx(6 * 0.20)
        ledger.sync()
        assert _status(ledger)["last_check"]["status"] == "ok"
        assert oid in ledger.tracked

        kalshi.fill_resting(oid, 6)  # maker fill, days later
        ledger.sync()
        assert ledger.sim.positions[T1].yes == 10.0
        assert oid not in ledger.tracked and not ledger.sim.holds
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available())
        assert _status(ledger)["last_check"]["status"] == "ok"

    def test_cancel_releases_the_hold(self, tmp_path):
        kalshi = FakeKalshi(50.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        oid = _buy(kalshi, ledger, T1, "yes", 5, 0.30, fill=0)
        assert ledger.sim.available_cash() == pytest.approx(50.0 - 1.5)
        kalshi.cancel(oid)
        ledger.sync()
        assert oid not in ledger.tracked and not ledger.sim.holds and not ledger.sim.positions
        assert ledger.sim.available_cash() == pytest.approx(50.0)
        assert _status(ledger)["last_check"]["status"] == "ok"

    def test_exact_cost_corrects_the_provisional_booking(self, tmp_path):
        kalshi = FakeKalshi(50.0, fee_per_contract=0.01234)  # POST reports 0.0123
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        _buy(kalshi, ledger, T1, "no", 10, 0.85)
        ledger.sync()
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available(), abs=1e-9)

    def test_settlement_pays_out(self, tmp_path):
        kalshi = FakeKalshi(20.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        _buy(kalshi, ledger, T1, "yes", 3, 0.40)
        _buy(kalshi, ledger, T2, "yes", 2, 0.30)
        ledger.sync()
        kalshi.settle(T1, "yes")
        kalshi.settle(T2, "no")
        ledger.sync()
        assert not ledger.sim.positions
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available())
        assert _status(ledger)["last_check"]["status"] == "ok"

    def test_take_profit_on_a_held_position_holds_nothing(self, tmp_path):
        kalshi = FakeKalshi(20.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        _buy(kalshi, ledger, T1, "yes", 3, 0.60)
        tp = _buy(kalshi, ledger, T1, "no", 3, 0.25, fill=0)  # rests: sells the held YES at 0.75
        assert ledger.sim.holds.get(tp, 0.0) == 0.0
        ledger.sync()
        assert _status(ledger)["last_check"]["status"] == "ok"
        kalshi.fill_resting(tp, 3)
        ledger.sync()
        assert not ledger.sim.positions  # the pair redeemed
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available())

    def test_an_unbooked_fill_is_a_confirmed_divergence(self, tmp_path):
        kalshi = FakeKalshi(20.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        kalshi.outside_fill(T1, "yes", 2, 0.5)
        ledger.sync()
        assert _status(ledger)["last_check"]["status"] == "suspect"
        ledger.sync()
        assert ledger.sim.divergence_count == 1
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available())
        assert Path(ledger.divergence_path).read_text().count("\n") == 1

    def test_initialize_adopts_account_positions_and_resting_orders(self, tmp_path):
        kalshi = FakeKalshi(30.0)
        kalshi.next_fill = 1
        resting = kalshi.place_order(T1, "bid", "4.00", "0.2500")["order_id"]  # untagged, pre-ledger
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        assert ledger.sim.positions[T1].yes == 1.0
        assert resting in ledger.tracked
        assert ledger.sim.available_cash() == pytest.approx(kalshi.available())
        kalshi.fill_resting(resting, 3)
        ledger.sync()
        assert ledger.sim.positions[T1].yes == 4.0
        assert _status(ledger)["last_check"]["status"] == "ok"

    def test_status_file_has_what_the_reconciler_reads(self, tmp_path):
        kalshi = FakeKalshi(10.0)
        ledger = _ledger(kalshi, tmp_path)
        ledger.sync()
        _buy(kalshi, ledger, T1, "yes", 2, 0.3, fill=1)
        ledger.sync()
        status = _status(ledger)
        for key in ("initialized", "order_tag", "allocation_epoch", "sim_cash", "fill_seq", "updated_ts",
                    "pending_exact_orders", "open_positions", "recent_order_ids", "tracked_orders", "holds"):
            assert key in status, key
        assert status["pending_exact_orders"] == 0


class TestOrderGuard:
    def test_refuses_orders_past_available_cash(self, tmp_path):
        kalshi = FakeKalshi(10.0)
        ledger = _ledger(kalshi, tmp_path, size_from_sim=True, allocation_dollars=5.0)
        assert ledger.order_allowed(T1, "yes", 1, 0.5)[0] is False  # not initialized yet
        ledger.sync()
        assert ledger.order_allowed(T1, "yes", 10, 4.9)[0] is True
        assert ledger.order_allowed(T1, "yes", 10, 5.1)[0] is False

    def test_closing_a_held_position_is_always_allowed(self, tmp_path):
        kalshi = FakeKalshi(10.0)
        ledger = _ledger(kalshi, tmp_path, size_from_sim=True, allocation_dollars=2.0)
        ledger.sync()
        _buy(kalshi, ledger, T1, "yes", 3, 0.6)  # leaves $0.2 available
        assert ledger.order_allowed(T1, "no", 3, 1.5)[0] is True
        assert ledger.order_allowed(T1, "no", 4, 2.0)[0] is False

    def test_shadow_mode_never_refuses(self, tmp_path):
        ledger = _ledger(FakeKalshi(1.0), tmp_path)
        assert ledger.order_allowed(T1, "yes", 100, 99.0) == (True, "")


class TestSharedAccount:
    def _shared(self, kalshi, tmp_path, **kw):
        return _ledger(kalshi, tmp_path, size_from_sim=True, shared_account=True, allocation_dollars=10.0, **kw)

    def test_fresh_ledger_ignores_other_runners_positions_and_orders(self, tmp_path):
        kalshi = FakeKalshi(50.0)
        kalshi.next_fill = 1
        kalshi.place_order(T1, "bid", "3.00", "0.2000", client_order_id="ra-y-" + "0" * 28)
        ledger = self._shared(kalshi, tmp_path)
        ledger.sync()
        assert ledger.sim.initialized and not ledger.sim.positions and not ledger.tracked
        assert ledger.sim.available_cash() == pytest.approx(10.0)
        assert ledger.own_market_positions() == []

    def test_sizing_is_zero_until_initialized(self, tmp_path):
        ledger = self._shared(FakeKalshi(50.0), tmp_path)
        assert ledger.sizing_cash(50.0) == 0.0

    def test_resume_recovers_an_order_placed_after_the_last_write(self, tmp_path):
        kalshi = FakeKalshi(50.0)
        first = self._shared(kalshi, tmp_path)
        first.sync()
        resting = _buy(kalshi, first, T1, "yes", 5, 0.2, fill=2)
        first.sync()  # status written: knows `resting`
        lost = _buy(kalshi, first, T2, "yes", 4, 0.3)  # booked in memory only, then the process dies
        cash_before_crash = first.sim.available_cash()

        second = self._shared(kalshi, tmp_path)
        second.sync()
        assert second.sim.resumed_from == first.sim.instance_id
        assert second.sim.allocation_epoch == first.sim.allocation_epoch
        assert lost in second.sim.recent_order_ids and T2 in second.sim.positions
        assert resting in second.tracked
        assert second.sim.available_cash() == pytest.approx(cash_before_crash)
        kalshi.fill_resting(resting, 3)
        second.sync()
        assert second.sim.positions[T1].yes == 5.0

    def test_lost_state_fails_closed(self, tmp_path):
        kalshi = FakeKalshi(50.0)
        first = self._shared(kalshi, tmp_path)
        first.sync()
        _buy(kalshi, first, T1, "yes", 2, 0.2)
        Path(first.status_path).unlink()

        second = self._shared(kalshi, tmp_path)
        second.sync()
        assert not second.sim.initialized and second.sizing_cash(50.0) == 0.0

        third = self._shared(kalshi, tmp_path, allow_fresh_allocation=True)
        third.sync()
        assert third.sim.initialized

    def test_a_brand_new_tag_starts_fresh(self, tmp_path):
        kalshi = FakeKalshi(50.0)
        kalshi.place_order(T1, "bid", "2.00", "0.2000", client_order_id="ra-y-" + "0" * 28)
        ledger = self._shared(kalshi, tmp_path)
        ledger.sync()
        assert ledger.sim.initialized

    def test_the_account_reconciler_sums_two_runners_to_the_real_balance(self, tmp_path):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resolution_alpha"))
        from account_reconciler import AccountReconciler

        kalshi = FakeKalshi(100.0)
        a = _ledger(kalshi, tmp_path, tag="bip", name="a.json", size_from_sim=True, shared_account=True,
                    allocation_dollars=20.0)
        b = _ledger(kalshi, tmp_path, tag="gfa", name="b.json", size_from_sim=True, shared_account=True,
                    allocation_dollars=30.0)
        a.sync()
        b.sync()
        reconciler = AccountReconciler()

        def check():
            a.sync()
            b.sync()
            statuses = {"bip": _status(a), "gfa": _status(b)}
            return reconciler.check(statuses, kalshi.available(), time.time()).status

        assert check() == "rebaselined"
        oid = _buy(kalshi, a, "KXBTCD-X", "yes", 5, 0.9, fill=2)
        _buy(kalshi, b, T1, "yes", 10, 0.1, fill=0)
        assert check() == "ok"
        kalshi.fill_resting(oid, 3)
        kalshi.settle(T1, "no")
        assert check() == "ok"
        kalshi.settle("KXBTCD-X", "yes")
        assert check() == "ok"
        kalshi.outside_fill(T2, "yes", 1, 0.5)
        assert check() == "suspect"
        assert check() == "diverged"
