"""Offline, no-network test of the per-runner ledger wiring (2026-09-26):
order_manager.OrderManager with a ../shared/tagged_ledger.py ledger over a
fake Kalshi account (../shared/tests/fake_kalshi.py). Checks orders are
tagged and booked, a resting take-profit's later fill lands in the ledger,
SIZE_FROM_SIM_BANKROLL sizes off and caps at the ledger, a refused entry
isn't registered as a position, and shared-account mode only sees this
runner's own resting orders and positions. Run directly:

    python test_ledger_wiring.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "tests"))

import config
import strategy
from fake_kalshi import FakeKalshi
from kalshi_btc_markets import ActiveMarket
from order_manager import OrderManager, OrderRefused
from tagged_ledger import TaggedLedger, split_client_order_id

config.ENABLE_TRAILING_EXIT = False
_tmp = Path(tempfile.mkdtemp())


def _manager(kalshi, name, **ledger_kw):
    manager = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    manager.dry_run = False
    manager._client = kalshi
    manager.ledger = TaggedLedger(
        kalshi, order_tag="bip", status_path=str(_tmp / f"{name}.json"),
        divergence_path=str(_tmp / f"{name}.jsonl"), **ledger_kw,
    )
    manager.ledger.sync()
    return manager


def _market(ticker, yes_bid=0.85, yes_ask=0.87):
    now = datetime.now(timezone.utc)
    return ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction="above", strike=100000.0,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=3600),
        settlement_average_seconds=60.0, yes_bid=yes_bid, yes_ask=yes_ask,
    )


def _signal(market, trade_price=0.87, favored_probability=0.95):
    return strategy.TradeSignal(
        market=market, seconds_to_close=3600.0, model_prob_yes=favored_probability, yes_mid=0.86,
        favored_side="yes", favored_probability=favored_probability, trade_price=trade_price,
        edge_after_fee=0.05, extrapolated=False,
    )


# 1. Orders are tagged, and a resting take-profit's later fill is booked.
kalshi = FakeKalshi(100.0)
manager = _manager(kalshi, "s1")
manager.buy_favored_side("T1", "yes", 3, 0.60)
order = next(iter(kalshi.orders.values()))
assert split_client_order_id(order["client_order_id"]) == ("bip", "yes"), order["client_order_id"]
kalshi.next_fill = 0
manager.buy_favored_side("T1", "no", 3, 0.25)  # resting take-profit: sells the held YES at 0.75
tp = [o for o in kalshi.orders.values() if o["status"] == "resting"][0]["order_id"]
kalshi.fill_resting(tp, 3)
manager.ledger.sync()
assert not manager.ledger.sim.positions, manager.ledger.sim.positions
assert abs(manager.ledger.sim.available_cash() - kalshi.available()) < 1e-9
assert manager.ledger.sim.divergence_count == 0
print("PASS  orders_tagged_and_resting_fill_booked")

# 2. SIZE_FROM_SIM_BANKROLL: the balance is the ledger's, and an order past it is refused.
kalshi = FakeKalshi(100.0)
manager = _manager(kalshi, "s2", size_from_sim=True, allocation_dollars=5.0)
assert abs(manager.get_balance_dollars() - 5.0) < 1e-9
try:
    manager.buy_favored_side("T2", "yes", 10, 0.87)
    raise AssertionError("an order past the ledger's cash should be refused")
except OrderRefused:
    pass
assert not kalshi.orders, "a refused order must not reach the exchange"
print("PASS  sizes_off_and_caps_at_the_ledger")

# 3. _execute skips a refused entry instead of registering a position it doesn't hold.
open_positions = {}
config_max = config.MAX_CONTRACTS_PER_TRADE
config.MAX_CONTRACTS_PER_TRADE = 1000.0
manager.get_balance_dollars = lambda: 1000.0  # a sizing bug: Kelly asks for far more than the ledger has
strategy._execute([_signal(_market("T3"))], open_positions, manager)
config.MAX_CONTRACTS_PER_TRADE = config_max
assert open_positions == {}, open_positions
print("PASS  refused_entry_not_registered")

# 4. Shared-account mode: only this runner's resting orders and positions.
kalshi = FakeKalshi(100.0)
kalshi.next_fill = 0
kalshi.place_order("T4", "bid", "2.00", "0.5000", client_order_id="ra-y-" + "0" * 28)  # another runner's
manager = _manager(kalshi, "s4", size_from_sim=True, shared_account=True, allocation_dollars=20.0)
kalshi.next_fill = 1
manager.buy_favored_side("T4", "yes", 2, 0.40)
resting = manager.get_resting_orders("T4")
assert [split_client_order_id(o["client_order_id"])[0] for o in resting] == ["bip"], resting
positions = manager.get_positions()["market_positions"]
assert positions == [{"ticker": "T4", "position_fp": 1.0, "total_traded_dollars": 0.0}], positions
print("PASS  shared_mode_sees_only_own_orders_and_positions")

print("\nall scenarios passed")
