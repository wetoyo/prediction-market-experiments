"""Offline, no-network regression test for the 2026-08-15 incident: with
open_positions tracked only when config.ENABLE_TRAILING_EXIT was on,
_execute had no record of tickers it already held and re-bought the same
ones every tick. Pins the fix (unconditional dedup) plus positions_store's
save/load round-trip and Kalshi reconciliation. Run directly:

    python test_positions_tracking.py
"""

import json
from datetime import datetime, timedelta, timezone

import config
config.ENABLE_TRAILING_EXIT = False  # the exact condition that let the bug through -- toggle OFF

import positions_store
import strategy
from kalshi_btc_markets import ActiveMarket


class FakeManager:
    def __init__(self, dry_run=True, balance=1000.0, positions=None):
        self.dry_run = dry_run
        self._balance = balance
        self._positions = positions or {"market_positions": [], "event_positions": []}
        self.buy_calls = []

    def get_balance_dollars(self):
        return self._balance

    def get_positions(self):
        return self._positions

    def buy_favored_side(self, ticker, side, contracts, limit_price):
        self.buy_calls.append({"ticker": ticker, "side": side, "contracts": contracts, "limit_price": limit_price})
        return {"dry_run": True}


def _market(ticker="T1", strike=100000.0, seconds_left=3600.0):
    now = datetime.now(timezone.utc)
    return ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction="above", strike=strike,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=seconds_left),
        settlement_average_seconds=60.0, yes_bid=0.85, yes_ask=0.87,
    )


def _signal(market, edge_after_fee=0.05, trade_price=0.87, favored_probability=0.95):
    return strategy.TradeSignal(
        market=market, seconds_to_close=3600.0, model_prob_yes=favored_probability, yes_mid=0.86,
        favored_side="yes", favored_probability=favored_probability, trade_price=trade_price,
        edge_after_fee=edge_after_fee, extrapolated=False,
    )


# 1. Core bug fix: a second _execute call on the same still-open ticker must not re-buy.
market = _market("T1")
signal = _signal(market)
manager = FakeManager(dry_run=True)
open_positions = {}

strategy._execute([signal], open_positions, manager)
assert len(manager.buy_calls) == 1, f"expected 1 buy on first pass, got {len(manager.buy_calls)}"
assert "T1" in open_positions, "position should be registered after the first buy"

strategy._execute([signal], open_positions, manager)
assert len(manager.buy_calls) == 1, (
    f"BUG REGRESSION: _execute re-bought an already-open ticker -- expected still 1 buy call, "
    f"got {len(manager.buy_calls)}"
)
print("PASS  dedup_prevents_reentry_on_open_ticker")

# 2. Tracking must happen even with ENABLE_TRAILING_EXIT off (the exact condition that hid the bug).
assert config.ENABLE_TRAILING_EXIT is False
assert open_positions["T1"]["side"] == "yes" and open_positions["T1"]["contracts"] > 0
print("PASS  position_tracked_even_with_exit_toggle_off")

# 3. positions_store save/load round-trip.
tmp_path = "test_positions_state.tmp.json"
positions_store.save(tmp_path, open_positions)
reloaded = positions_store.load(tmp_path)
assert set(reloaded.keys()) == {"T1"}
r = reloaded["T1"]
o = open_positions["T1"]
assert r["side"] == o["side"] and r["contracts"] == o["contracts"] and r["entry_price"] == o["entry_price"]
assert r["market"].ticker == o["market"].ticker and r["market"].strike == o["market"].strike
assert r["market"].close_time == o["market"].close_time
print("PASS  save_load_round_trip_preserves_position")
import os
os.remove(tmp_path)

# 4. Reconciliation picks up a real Kalshi position this process has no local record of.
kalshi_positions = {
    "market_positions": [
        {"ticker": "T2", "position_fp": "-50.00", "total_traded_dollars": "3.00"},  # NO, untracked -- should reconcile
        {"ticker": "T1", "position_fp": "5.00", "total_traded_dollars": "4.35"},  # already tracked -- should be skipped
        {"ticker": "T3", "position_fp": "0.00", "total_traded_dollars": "0.00"},  # flat -- should be skipped
    ],
    "event_positions": [],
}
manager2 = FakeManager(dry_run=False, positions=kalshi_positions)
open_positions2 = {"T1": dict(open_positions["T1"])}  # already tracked, should NOT be overwritten
market_t2 = _market("T2", strike=105000.0)
markets_by_ticker = {"T1": market, "T2": market_t2}  # note: no "T3" -- simulates a market outside this tick's scan

original_t1 = dict(open_positions2["T1"])
positions_store.reconcile_with_kalshi(open_positions2, manager2, markets_by_ticker)

assert open_positions2["T1"] == original_t1, "reconciliation must not overwrite an already-tracked position"
assert "T2" in open_positions2, "reconciliation should have added the untracked real position"
t2 = open_positions2["T2"]
assert t2["side"] == "no" and t2["contracts"] == 50.0
assert abs(t2["entry_price"] - (3.00 / 50.0)) < 1e-9
assert t2["entry_edge"] is None and t2["reconciled"] is True
assert "T3" not in open_positions2, "flat (position_fp=0) Kalshi entries should not be reconciled"
print("PASS  reconcile_adds_untracked_position_and_skips_known_and_flat")

# 5. Reconciliation is a no-op in dry-run (nothing real to reconcile against).
manager_dry = FakeManager(dry_run=True, positions=kalshi_positions)
open_positions3 = {}
positions_store.reconcile_with_kalshi(open_positions3, manager_dry, markets_by_ticker)
assert open_positions3 == {}, "reconciliation must no-op in dry-run"
print("PASS  reconcile_is_noop_in_dry_run")

print("\nall scenarios passed")
