"""sim_bankroll.SimulatedBankroll (pure ledger) and its OrderManager wiring,
against a fake exchange whose payload shapes are copied from real Kalshi
responses (2026-09-22: POST order response, GET /portfolio/orders/{id},
GET /portfolio/settlements, GET /portfolio/balance).
"""

import json

import pytest

import config
from order_manager import OrderManager
from sim_bankroll import SimulatedBankroll

TICKER = "KXBTC15M-26SEP222045-45"


def _post_response(order_id="o1", fill_count="2.00", yes_price="0.2800", fee_per="0.0142"):
    # POST /portfolio/events/orders: 4-decimal per-contract averages only
    return {"order_id": order_id, "fill_count": fill_count, "average_fill_price": yes_price,
            "average_fee_paid": fee_per, "remaining_count": "0.00"}


def _get_order(fill_cost="1.440000", fee="0.028300", count="2.00"):
    return {"taker_fill_cost_dollars": fill_cost, "maker_fill_cost_dollars": "0.000000",
            "taker_fees_dollars": fee, "maker_fees_dollars": "0.000000", "fill_count_fp": count}


def _settlement(ticker=TICKER, result="no"):
    return {"ticker": ticker, "market_result": result, "revenue": 200, "value": 0 if result == "no" else 100}


def _ready(balance=10.0, **kw):
    sim = SimulatedBankroll(**kw)
    assert sim.initialize(balance, sim.fill_seq) is not None
    return sim


class TestLedger:
    def test_fraction_allocation(self):
        sim = _ready(100.0, allocation_fraction=0.25)
        assert sim.cash == pytest.approx(25.0)
        assert sim.offset == pytest.approx(-75.0)

    def test_dollar_allocation_overrides_fraction(self):
        sim = _ready(100.0, allocation_dollars=40.0, allocation_fraction=0.25)
        assert sim.cash == pytest.approx(40.0)

    def test_no_fill_converts_yes_price_and_charges_fee(self):
        sim = _ready(10.0)
        assert sim.record_fill(TICKER, "no", _post_response()) == 2.0
        assert sim.cash == pytest.approx(10.0 - (0.72 * 2 + 0.0142 * 2))
        assert sim.positions[TICKER].no == 2.0

    def test_zero_fill_books_nothing(self):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "yes", _post_response(fill_count="0.00"))
        assert sim.cash == 10.0 and not sim.positions and sim.fill_seq == 0

    def test_exact_cost_replaces_the_approximation(self):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response())
        sim.apply_exact_cost("o1", _get_order())
        assert sim.cash == pytest.approx(10.0 - 1.44 - 0.0283)
        assert not sim.pending_exact

    def test_unusable_price_still_books_contracts_then_exact_fixes_cost(self):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response(yes_price=None, fee_per="0"))
        assert sim.positions[TICKER].no == 2.0
        sim.apply_exact_cost("o1", _get_order())
        assert sim.cash == pytest.approx(10.0 - 1.4683)

    @pytest.mark.parametrize("result,expected_payout", [("no", 2.0), ("yes", 0.0)])
    def test_settlement_pays_own_winning_contracts(self, result, expected_payout):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response())
        sim.apply_exact_cost("o1", _get_order())
        assert sim.apply_settlement(_settlement(result=result)) == expected_payout
        assert TICKER not in sim.positions

    def test_settlement_for_unheld_ticker_is_ignored(self):
        sim = _ready(10.0)
        assert sim.apply_settlement(_settlement()) is None
        assert sim.cash == 10.0

    def test_opposite_side_fill_redeems_matched_pairs(self):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response(order_id="a", fill_count="2.00", yes_price="0.0800", fee_per="0"))
        sim.record_fill(TICKER, "yes", _post_response(order_id="b", fill_count="1.00", yes_price="0.3000", fee_per="0"))
        # -2*0.92 - 1*0.30 + 1 pair * $1
        assert sim.cash == pytest.approx(10.0 - 1.84 - 0.30 + 1.0)
        assert sim.positions[TICKER].no == 1.0 and sim.positions[TICKER].yes == 0.0

    def test_adopted_positions_settle_without_divergence(self):
        sim = SimulatedBankroll()
        sim.initialize(10.0, 0, {TICKER: ("no", 2.0)})
        sim.apply_settlement(_settlement())
        assert sim.check(12.0, sim.fill_seq).status == "ok"


class TestCheck:
    def test_in_lockstep_is_ok(self):
        sim = _ready(10.0, allocation_fraction=0.5)
        sim.record_fill(TICKER, "no", _post_response())
        sim.apply_exact_cost("o1", _get_order())
        seq = sim.fill_seq
        assert sim.check(10.0 - 1.4683, seq).status == "ok"
        sim.apply_settlement(_settlement())
        assert sim.check(10.0 - 1.4683 + 2.0, seq).status == "ok"
        assert sim.divergence_count == 0

    def test_one_poll_transient_is_not_counted(self):
        sim = _ready(10.0)
        assert sim.check(12.0, 0).status == "suspect"  # settlement credit seen before its record
        assert sim.check(10.0, 0).status == "ok"
        assert sim.divergence_count == 0

    def test_confirmed_divergence_is_counted_and_resynced(self):
        sim = _ready(10.0, allocation_fraction=0.5)  # cash 5, offset -5
        assert sim.check(9.0, 0).status == "suspect"
        result = sim.check(9.0, 0)
        assert result.status == "diverged" and result.gap == pytest.approx(1.0)
        assert sim.divergence_count == 1
        assert sim.cash == pytest.approx(4.0)  # resynced: real 9 + offset -5
        assert sim.check(9.0, 0).status == "ok"
        # a second, separate divergence counts again
        sim.check(8.5, 0)
        sim.check(8.5, 0)
        assert sim.divergence_count == 2

    def test_fill_after_balance_snapshot_is_inconclusive(self):
        sim = _ready(10.0)
        seq = sim.fill_seq
        sim.record_fill(TICKER, "no", _post_response())
        sim.apply_exact_cost("o1", _get_order())
        assert sim.check(10.0, seq).status == "inconclusive"
        assert sim.divergence_count == 0

    def test_pending_exact_cost_is_inconclusive(self):
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response())
        assert sim.check(10.0 - 1.4683, sim.fill_seq).status == "inconclusive"

    def test_initialize_refuses_a_balance_that_predates_a_fill(self):
        sim = SimulatedBankroll()
        seq = sim.fill_seq
        sim.record_fill(TICKER, "no", _post_response())  # counted, not booked
        assert sim.initialize(10.0, seq) is None and not sim.initialized
        assert sim.initialize(8.5317, sim.fill_seq) == pytest.approx(8.5317)


class TestResume:
    def test_snapshot_round_trips_through_resume(self):
        sim = _ready(10.0, allocation_dollars=6.0)
        sim.record_fill(TICKER, "no", _post_response())  # leaves an exact cost pending
        state = json.loads(json.dumps(sim.snapshot()))
        resumed = SimulatedBankroll(allocation_dollars=6.0)
        assert resumed.resume(state, 8.53, resumed.fill_seq) == pytest.approx(sim.cash)
        assert resumed.allocation_epoch == sim.allocation_epoch and resumed.resumed_from == sim.instance_id
        assert resumed.positions[TICKER].no == 2.0
        assert resumed.positions[TICKER].opened_ts == sim.positions[TICKER].opened_ts
        resumed.apply_exact_cost("o1", _get_order())
        sim.apply_exact_cost("o1", _get_order())
        assert resumed.cash == pytest.approx(sim.cash)

    def test_old_order_ids_are_pruned(self, monkeypatch):
        import sim_bankroll
        sim = _ready(10.0)
        sim.record_fill(TICKER, "no", _post_response(order_id="old"))
        sim.recent_order_ids["old"] -= sim_bankroll.RECENT_ORDER_ID_TTL_SECONDS + 1
        sim.record_fill(TICKER, "no", _post_response(order_id="new"))
        assert set(sim.recent_order_ids) == {"new"}


class TestSizingCash:
    def test_before_init_is_the_coming_allocation(self):
        sim = SimulatedBankroll(allocation_fraction=0.25)
        assert sim.sizing_cash(100.0) == pytest.approx(25.0)
        assert SimulatedBankroll(allocation_dollars=40.0).sizing_cash(100.0) == pytest.approx(40.0)

    def test_is_the_ledger_cash(self):
        sim = _ready(100.0, allocation_fraction=0.25)
        sim.record_fill(TICKER, "no", _post_response())
        assert sim.sizing_cash(100.0) == pytest.approx(sim.cash)

    def test_never_more_than_the_account_holds(self):
        assert SimulatedBankroll(allocation_dollars=40.0).sizing_cash(30.0) == pytest.approx(30.0)
        sim = _ready(10.0)
        sim.cash = 12.0  # e.g. a fill the ledger missed
        assert sim.sizing_cash(10.0) == pytest.approx(10.0)

    def test_never_negative(self):
        sim = _ready(10.0)
        sim.cash = -0.5
        assert sim.sizing_cash(10.0) == 0.0


class _FakeExchange:
    """Kalshi account with real cash semantics: fills debit exact cost + fee,
    settlements credit $1 per winning contract."""

    def __init__(self, balance):
        self.balance = balance
        self.orders = {}
        self.settlements = []
        self.positions = []
        self.requests = []

    def get_balance(self):
        return {"balance_dollars": f"{self.balance:.4f}"}

    def get_positions(self):
        return {"market_positions": self.positions}

    def place_order(self, **kw):
        self.balance -= 1.44 + 0.0283
        self.orders["o1"] = _get_order()
        return _post_response()

    def settle(self, settlement, credit):
        self.settlements.append(settlement)
        self.balance += credit

    def _request(self, method, path, params=None):
        self.requests.append(path)
        if path.startswith("/portfolio/orders/"):
            return {"order": self.orders[path.rsplit("/", 1)[1]]}
        if path == "/portfolio/settlements":
            return {"settlements": self.settlements, "cursor": ""}
        raise AssertionError(path)


@pytest.fixture
def om(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SIM_BANKROLL_STATUS_PATH", str(tmp_path / "sim.json"))
    monkeypatch.setattr(config, "SIM_BANKROLL_DIVERGENCE_LOG_PATH", str(tmp_path / "div.jsonl"))
    om = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    om.dry_run = False
    om._client = _FakeExchange(10.0)
    om._sim = SimulatedBankroll()
    return om


def _poll(om):
    om.get_balance_dollars()
    om.sync_sim_bankroll()


class TestOrderManagerWiring:
    def test_trade_then_settlement_never_diverges(self, om, tmp_path):
        _poll(om)  # initializes
        assert om._sim.initialized
        om.buy_favored_side(TICKER, "no", 2, 0.72)
        _poll(om)
        om._client.settle(_settlement(), 2.0)
        _poll(om)
        _poll(om)
        assert om._sim.divergence_count == 0
        assert om._sim.cash == pytest.approx(om._client.balance)
        status = json.loads((tmp_path / "sim.json").read_text())
        assert status["divergence_count"] == 0 and status["last_check"]["status"] == "ok"
        assert not (tmp_path / "div.jsonl").exists()

    def test_get_balance_return_value_is_unchanged(self, om):
        _poll(om)
        om._sim.cash = 1.0  # a wrong ledger must not leak into sizing
        assert om.get_balance_dollars() == pytest.approx(10.0)

    def test_untracked_balance_move_is_counted_logged_and_resynced(self, om, tmp_path):
        _poll(om)
        om._client.balance += 5.0  # e.g. a manual deposit this runner didn't make
        _poll(om)
        _poll(om)
        assert om._sim.divergence_count == 1
        assert om._sim.cash == pytest.approx(15.0)
        (event,) = [json.loads(line) for line in (tmp_path / "div.jsonl").read_text().splitlines()]
        assert event["gap"] == pytest.approx(-5.0) and event["divergence_count_this_run"] == 1
        _poll(om)
        assert om._sim.divergence_count == 1

    def test_ledger_failure_never_reaches_the_order_path(self, om):
        _poll(om)
        om._sim.record_fill = lambda *a: 1 / 0
        response = om.buy_favored_side(TICKER, "no", 2, 0.72)
        assert response["fill_count"] == "2.00"

    def test_sync_failure_is_swallowed(self, om):
        _poll(om)
        om.buy_favored_side(TICKER, "no", 2, 0.72)
        om._client._request = lambda *a, **k: 1 / 0
        _poll(om)  # must not raise

    def test_no_settlement_calls_without_open_positions(self, om):
        _poll(om)
        _poll(om)
        assert om._client.requests == []

    def test_restart_adopts_open_positions(self, om):
        om._client.positions = [{"ticker": TICKER, "position_fp": "-2.00"}]
        _poll(om)
        om._client.settle(_settlement(), 2.0)
        _poll(om)
        _poll(om)
        assert om._sim.divergence_count == 0


class TestSizeFromSim:
    @pytest.fixture
    def sizing_om(self, om):
        om._size_from_sim = True
        om._sim = SimulatedBankroll(allocation_fraction=0.25)
        return om

    def test_sizes_off_the_allocation_from_the_first_poll(self, sizing_om):
        assert sizing_om.get_balance_dollars() == pytest.approx(2.5)  # before the first sync initializes

    def test_sizes_off_the_ledger_through_a_trade_and_settlement(self, sizing_om):
        _poll(sizing_om)
        sizing_om.buy_favored_side(TICKER, "no", 2, 0.72)
        assert sizing_om.get_balance_dollars() == pytest.approx(2.5 - 1.4683, abs=1e-3)  # provisional 4dp cost
        sizing_om.sync_sim_bankroll()
        assert sizing_om.get_balance_dollars() == pytest.approx(2.5 - 1.4683)  # exact cost
        sizing_om.sync_sim_bankroll()
        sizing_om._client.settle(_settlement(), 2.0)
        _poll(sizing_om)
        _poll(sizing_om)
        assert sizing_om.get_balance_dollars() == pytest.approx(2.5 - 1.4683 + 2.0)
        assert sizing_om._sim.divergence_count == 0

    def test_ledger_bug_is_capped_by_the_real_balance(self, sizing_om):
        _poll(sizing_om)
        sizing_om._sim.cash = 50.0
        assert sizing_om.get_balance_dollars() == pytest.approx(10.0)

    def test_off_by_default(self, om):
        _poll(om)
        assert om.get_balance_dollars() == pytest.approx(10.0)
