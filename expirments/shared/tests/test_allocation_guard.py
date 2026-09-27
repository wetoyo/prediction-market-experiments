"""allocation_guard.AllocationGuard, and the guard wired into TaggedLedger
and resolution_alpha's OrderManager (fresh allocation refused, resume only
warned, the switch into shared mode)."""

import json
import sys
from pathlib import Path

import pytest

import allocation_guard
from allocation_guard import AllocationGuard, list_entries
from fake_kalshi import FakeKalshi
from tagged_ledger import TaggedLedger


def _guard(registry, tag, status_path, allocation=None, fraction=1.0, shared=True, **kw):
    guard = AllocationGuard(tag, str(status_path), allocation, fraction, shared, registry=registry, **kw)
    guard.register()
    return guard


def _write_status(path, sim_cash, reserve=0.0, initialized=True):
    Path(path).write_text(json.dumps({"initialized": initialized, "sim_cash": sim_cash, "reserve": reserve}))


class TestGuard:
    def test_alone_fits(self, tmp_path):
        guard = _guard(tmp_path / "reg", "ra", tmp_path / "ra.json", allocation=10.0)
        assert guard.check(10.0, 10.0)[0]
        assert not guard.check(10.02, 10.0)[0]

    def test_peer_ledger_claim_counts_cash_and_reserve(self, tmp_path):
        reg = tmp_path / "reg"
        _guard(reg, "ra", tmp_path / "ra.json", allocation=6.0)
        _write_status(tmp_path / "ra.json", sim_cash=5.0, reserve=1.0)
        guard = _guard(reg, "bip", tmp_path / "bip.json", allocation=4.0)
        ok, breakdown = guard.check(4.0, 10.0)
        assert ok and "ra $6.00 (ledger)" in breakdown
        assert not guard.check(4.5, 10.0)[0]

    def test_uninitialized_peer_claims_its_configured_allocation(self, tmp_path):
        reg = tmp_path / "reg"
        _guard(reg, "gfa", tmp_path / "missing.json", allocation=7.0)
        guard = _guard(reg, "bip", tmp_path / "bip.json", allocation=4.0)
        ok, breakdown = guard.check(4.0, 10.0)
        assert not ok and "configured allocation" in breakdown

    def test_single_runner_peer_claims_the_whole_balance(self, tmp_path):
        reg = tmp_path / "reg"
        _guard(reg, "ra", tmp_path / "ra.json", shared=False)
        _write_status(tmp_path / "ra.json", sim_cash=12.97)
        guard = _guard(reg, "bip", tmp_path / "bip.json", allocation=1.0)
        assert not guard.check(1.0, 12.97)[0]
        _write_status(tmp_path / "ra.json", sim_cash=12.97, initialized=False)  # before its first sync
        assert not guard.check(1.0, 12.97)[0]

    def test_min_unallocated(self, tmp_path):
        guard = _guard(tmp_path / "reg", "ra", tmp_path / "ra.json", allocation=9.0, min_unallocated_dollars=2.0)
        assert guard.check(8.0, 10.0)[0]
        assert not guard.check(9.0, 10.0)[0]

    def test_own_entry_is_never_a_peer_and_reregistering_a_tag_warns(self, tmp_path):
        reg = tmp_path / "reg"
        _guard(reg, "ra", tmp_path / "ra.json", allocation=10.0)
        again = AllocationGuard("ra", str(tmp_path / "other.json"), 10.0, 1.0, True, registry=reg)
        assert "own tag" in again.register()
        assert again.check(10.0, 10.0)[0]
        assert [e["order_tag"] for e in list_entries(reg)] == ["ra"]

    def test_cli_list_and_remove(self, tmp_path, monkeypatch, capsys):
        reg = tmp_path / "reg"
        monkeypatch.setenv(allocation_guard.REGISTRY_DIR_ENV, str(reg))
        _guard(reg, "ra", tmp_path / "ra.json", shared=False)
        assert allocation_guard.main(["list"]) == 0
        assert "SINGLE-RUNNER" in capsys.readouterr().out
        assert allocation_guard.main(["remove", "ra"]) == 0
        assert list_entries(reg) == []
        assert allocation_guard.main(["remove", "ra"]) == 1


def _ledger(kalshi, tmp_path, tag, guard, **kw):
    return TaggedLedger(kalshi, order_tag=tag, status_path=str(tmp_path / f"{tag}.json"),
                        divergence_path=str(tmp_path / f"{tag}.div.jsonl"), guard=guard, **kw)


class TestTaggedLedgerGuard:
    def test_over_committing_allocation_is_refused_until_it_fits(self, tmp_path):
        reg = tmp_path / "reg"
        kalshi = FakeKalshi(balance=10.0)
        _guard(reg, "ra", tmp_path / "ra.json", allocation=8.0)
        _write_status(tmp_path / "ra.json", sim_cash=8.0)
        guard = _guard(reg, "bip", tmp_path / "bip.json", allocation=5.0)
        ledger = _ledger(kalshi, tmp_path, "bip", guard, allocation_dollars=5.0, size_from_sim=True,
                         shared_account=True)
        ledger.sync()
        assert not ledger.sim.initialized and ledger.trading_blocked()
        assert ledger.sizing_cash(10.0) == 0.0
        assert not ledger.order_allowed("T", "yes", 1, 0.5)[0]
        _write_status(tmp_path / "ra.json", sim_cash=5.0)  # ra lowered
        ledger.sync()
        assert ledger.sim.initialized and not ledger.trading_blocked()
        assert ledger.sizing_cash(10.0) == pytest.approx(5.0)

    def test_sizes_zero_before_the_first_sync(self, tmp_path):
        guard = _guard(tmp_path / "reg", "gfa", tmp_path / "gfa.json", allocation=5.0, shared=False)
        ledger = _ledger(FakeKalshi(balance=10.0), tmp_path, "gfa", guard, allocation_dollars=5.0,
                         size_from_sim=True)
        assert ledger.sizing_cash(10.0) == 0.0
        ledger.sync()
        assert ledger.sizing_cash(10.0) == pytest.approx(5.0)

    def test_switch_into_shared_mode_starts_fresh_at_the_allocation(self, tmp_path):
        reg = tmp_path / "reg"
        kalshi = FakeKalshi(balance=10.0)
        single = _ledger(kalshi, tmp_path, "gfa", None)
        single.sync()
        assert json.loads((tmp_path / "gfa.json").read_text())["sim_cash"] == pytest.approx(10.0)
        guard = _guard(reg, "gfa", tmp_path / "gfa.json", allocation=4.0)
        shared = _ledger(kalshi, tmp_path, "gfa", guard, allocation_dollars=4.0, size_from_sim=True,
                         shared_account=True)
        shared.sync()
        assert shared.sim.initialized and shared.sim.available_cash() == pytest.approx(4.0)


# -- resolution_alpha's OrderManager -------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resolution_alpha"))


class _RAExchange:
    def __init__(self, balance, positions=()):
        self.balance = balance
        self.positions = list(positions)

    def get_balance(self):
        return {"balance_dollars": f"{self.balance:.4f}"}

    def get_positions(self):
        return {"market_positions": self.positions}

    def _request(self, method, path, params=None):
        if path == "/portfolio/orders":
            return {"orders": [], "cursor": ""}
        raise AssertionError(path)


@pytest.fixture
def ra(tmp_path, monkeypatch):
    import config
    from order_manager import OrderManager
    from sim_bankroll import SimulatedBankroll

    monkeypatch.setattr(config, "SIM_BANKROLL_STATUS_PATH", str(tmp_path / "ra.json"))
    monkeypatch.setattr(config, "SIM_BANKROLL_DIVERGENCE_LOG_PATH", str(tmp_path / "ra.div.jsonl"))

    def make(balance=10.0, allocation=None, shared=False, positions=()):
        om = OrderManager.__new__(OrderManager)
        om.dry_run = False
        om._client = _RAExchange(balance, positions)
        om._sim = SimulatedBankroll(allocation_dollars=allocation)
        om._size_from_sim = True
        om._shared_account = shared
        om._guard = _guard(tmp_path / "reg", "ra", tmp_path / "ra.json", allocation=allocation, shared=shared)
        return om
    return make


def _poll(om):
    balance = om.get_balance_dollars()
    om.sync_sim_bankroll()
    return balance


class TestOrderManagerGuard:
    def test_refused_while_a_peer_holds_the_money(self, ra, tmp_path):
        _guard(tmp_path / "reg", "bip", tmp_path / "bip.json", allocation=6.0)
        om = ra()  # single-runner: claims the whole $10
        assert _poll(om) == 0.0
        assert not om._sim.initialized and om._guard_refusal
        assert _poll(om) == 0.0
        (tmp_path / "reg" / "bip.json").unlink()  # bip retired
        assert _poll(om) == 0.0  # this poll's sync initializes
        assert _poll(om) == pytest.approx(10.0)

    def test_alone_trades_from_the_second_poll(self, ra):
        om = ra()
        assert _poll(om) == 0.0
        assert _poll(om) == pytest.approx(10.0)

    def test_switch_into_shared_mode_waits_until_flat(self, ra, tmp_path):
        single = ra()
        _poll(single)
        _poll(single)  # writes a single-runner status file claiming $10
        position = {"ticker": "KXBTCD-X", "position_fp": "2.00"}
        om = ra(allocation=6.0, shared=True, positions=[position])
        assert _poll(om) == 0.0
        assert not om._sim.initialized and "open position" in om._guard_refusal
        om._client.positions = []
        _poll(om)
        assert om._sim.initialized and om._sim.available_cash() == pytest.approx(6.0)
        assert _poll(om) == pytest.approx(6.0)
