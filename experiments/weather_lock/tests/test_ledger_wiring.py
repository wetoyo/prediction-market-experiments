"""Offline: the IOC buy path through a TaggedLedger over the shared fake
Kalshi account (../../shared/tests/fake_kalshi.py)."""

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parents[2] / "shared" / "tests"))
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

import pytest

fake_kalshi = pytest.importorskip("fake_kalshi")
from order_manager import OrderManager, OrderRefused  # noqa: E402
from tagged_ledger import TaggedLedger  # noqa: E402


def _manager(kalshi, **kw):
    tmp = Path(tempfile.mkdtemp())
    m = OrderManager.__new__(OrderManager)
    m.dry_run = False
    m._client = kalshi
    m.ledger = TaggedLedger(kalshi, order_tag="wl", status_path=str(tmp / "s.json"),
                            divergence_path=str(tmp / "d.jsonl"), **kw)
    m.ledger.sync()
    return m


def test_no_buy_books_into_ledger():
    kalshi = fake_kalshi.FakeKalshi(100.0)
    m = _manager(kalshi)
    filled = m.buy("KXHIGHNY-26NOV08-T61", "no", 5, 0.96)
    (order,) = kalshi.orders.values()
    assert order["outcome_side"] == "no" and order["client_order_id"].startswith("wl-n-")
    assert filled == 5 and m.ledger.sim.positions["KXHIGHNY-26NOV08-T61"].no == 5


def test_partial_ioc_leaves_nothing_resting():
    kalshi = fake_kalshi.FakeKalshi(100.0)
    m = _manager(kalshi)
    kalshi.next_fill = 2
    assert m.buy("KXHIGHNY-26NOV08-T61", "no", 5, 0.96) == 2
    (order,) = kalshi.orders.values()
    assert order["status"] == "canceled"
    m.ledger.sync()
    assert not m.ledger.sim.holds
    assert abs(m.ledger.sim.available_cash() - kalshi.available()) < 1e-9


def test_refused_over_allocation():
    kalshi = fake_kalshi.FakeKalshi(100.0)
    m = _manager(kalshi, size_from_sim=True, allocation_dollars=2.0)
    with pytest.raises(OrderRefused):
        m.buy("KXHIGHNY-26NOV08-T61", "no", 5, 0.96)
