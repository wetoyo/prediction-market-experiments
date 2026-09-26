"""Offline, no-network test of the per-runner ledger wiring (2026-09-26):
order_manager.OrderManager with a ../shared/tagged_ledger.py ledger over a
fake Kalshi account (../shared/tests/fake_kalshi.py). Checks a leg's resting
remainder is booked when it fills later, a basket that doesn't fit in the
ledger's cash is skipped whole (not bought in part), and live sizing uses
the real/ledger bankroll instead of DRY_RUN_SIMULATED_BALANCE_DOLLARS. Run
directly:

    python test_ledger_wiring.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "tests"))

import strategy
from fake_kalshi import FakeKalshi
from order_manager import OrderManager
from selection import BasketLeg, BasketPlan
from tagged_ledger import TaggedLedger

_tmp = Path(tempfile.mkdtemp())


def _manager(kalshi, name, **ledger_kw):
    manager = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    manager.dry_run = False
    manager._client = kalshi
    manager.ledger = TaggedLedger(
        kalshi, order_tag="gfa", status_path=str(_tmp / f"{name}.json"),
        divergence_path=str(_tmp / f"{name}.jsonl"), **ledger_kw,
    )
    manager.ledger.sync()
    return manager


class _Event:
    event_ticker = "KXPGATOUR-26OCT04"
    series_ticker = "KXPGATOUR"
    close_time = datetime.now(timezone.utc) + timedelta(days=3)


def _plan(*legs):
    plan = BasketPlan(method="devig_edge", field_size=40, overround=1.2, devig_method="proportional")
    for ticker, price, contracts in legs:
        plan.legs.append(BasketLeg(ticker=ticker, name=ticker, buy_price=price, fair_prob=price + 0.05,
                                   contracts=contracts, edge_per_contract=0.04, cost=price * contracts + 0.01))
    plan.total_cost = sum(leg.cost for leg in plan.legs)
    return plan


# 1. A leg's GTC remainder rests on a thin book and fills later: booked, hold released.
kalshi = FakeKalshi(100.0)
manager = _manager(kalshi, "g1")
kalshi.next_fill = 2
manager.buy_favored_side("L1", "yes", 10, 0.05)
(order_id,) = manager.ledger.tracked
assert abs(manager.ledger.sim.holds[order_id] - 8 * 0.05) < 1e-9
kalshi.fill_resting(order_id, 8)
manager.ledger.sync()
assert manager.ledger.sim.positions["L1"].yes == 10.0 and not manager.ledger.sim.holds
assert abs(manager.ledger.sim.available_cash() - kalshi.available()) < 1e-9
print("PASS  resting_leg_fill_booked")

# 2. A basket past the ledger's available cash is skipped whole.
kalshi = FakeKalshi(100.0)
manager = _manager(kalshi, "g2", size_from_sim=True, allocation_dollars=3.0)
event = _Event()
open_positions = {}
plan = _plan(("L1", 0.10, 10), ("L2", 0.20, 10))  # ~$3.02
strategy.execute([event], {event.event_ticker: plan}, open_positions, manager)
assert not kalshi.orders and not open_positions, "no leg of an unaffordable basket may be bought"
plan = _plan(("L1", 0.10, 10), ("L2", 0.10, 10))  # ~$2.02
strategy.execute([event], {event.event_ticker: plan}, open_positions, manager)
assert set(open_positions) == {"L1", "L2"} and len(kalshi.orders) == 2
print("PASS  unaffordable_basket_skipped_whole")

# 3. Live sizing reads the ledger's bankroll, not DRY_RUN_SIMULATED_BALANCE_DOLLARS.
assert abs(strategy._live_bankroll(manager) - manager.ledger.sim.available_cash()) < 1e-9
print("PASS  live_bankroll_is_the_ledger")

print("\nall scenarios passed")
