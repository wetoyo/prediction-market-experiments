"""sim_bankroll's margin bucket (perps collateral, 2026-10-21): transfers out
of and back into trading cash, the balance check while one is in flight,
marks booking hedge P&L, and restart. No network."""

from sim_bankroll import SimulatedBankroll


def _sim(cash=100.0):
    sim = SimulatedBankroll(allocation_dollars=cash)
    sim.initialize(real_balance=cash, fill_seq_at_balance=0)
    return sim


class TestMargin:
    def test_transfer_out_moves_cash_to_margin_and_is_capped(self):
        sim = _sim(10.0)
        assert sim.move_to_margin("t1", 4.0) == 4.0
        assert sim.available_cash() == 6.0 and sim.margin == 4.0
        assert sim.move_to_margin("t2", 50.0) == 6.0  # never more than the available cash
        assert sim.available_cash() == 0.0

    def test_check_reports_in_flight_transfer_then_ok_once_debited(self):
        sim = _sim(100.0)
        sim.move_to_margin("t1", 30.0)
        result = sim.check(real_balance=100.0, fill_seq_at_balance=sim.fill_seq)  # not debited yet
        assert result.status == "transfer"
        result = sim.check(real_balance=100.0, fill_seq_at_balance=sim.fill_seq)
        assert result.status == "transfer" and sim.divergence_count == 0
        assert sim.check(real_balance=70.0, fill_seq_at_balance=sim.fill_seq).status == "ok"

    def test_transfer_back_and_return_flow(self):
        sim = _sim(100.0)
        sim.move_to_margin("t1", 30.0)
        sim.transfer_landed("t1")
        sim.move_from_margin("t2", 30.0)
        assert sim.check(real_balance=70.0, fill_seq_at_balance=sim.fill_seq).status == "transfer"
        assert sim.check(real_balance=100.0, fill_seq_at_balance=sim.fill_seq).status == "ok"
        assert sim.available_cash() == 100.0 and sim.margin == 0.0

    def test_marks_book_hedge_pnl_and_skip_while_in_flight(self):
        sim = _sim(100.0)
        assert sim.mark_margin(5.0) == 0.0  # first mark adopts what's already there
        assert sim.margin == 5.0 and sim.trading_pnl == 0.0
        sim.move_to_margin("t1", 20.0)
        assert sim.mark_margin(5.0) is None  # equity read may predate the credit
        sim.transfer_landed("t1")
        assert abs(sim.mark_margin(23.5) - (-1.5)) < 1e-9  # hedge lost $1.50
        assert abs(sim.trading_pnl - (-1.5)) < 1e-9 and sim.margin == 23.5
        # The hedge's money stays out of the check: real balance only moved by the transfer.
        assert sim.check(real_balance=80.0, fill_seq_at_balance=sim.fill_seq).status == "ok"

    def test_resume_carries_margin_and_in_flight(self):
        sim = _sim(100.0)
        sim.mark_margin(0.0)
        sim.move_to_margin("t1", 25.0)
        resumed = SimulatedBankroll(allocation_dollars=100.0)
        resumed.resume(sim.snapshot(), real_balance=75.0, fill_seq_at_balance=0)
        assert resumed.margin == 25.0 and resumed.margin_marked
        assert list(resumed.transfers_in_flight) == ["t1"]
        assert resumed.available_cash() == 75.0
