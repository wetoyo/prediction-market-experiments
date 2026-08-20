"""Offline, no-network verification of the live-only resting take-profit
order path (strategy.py's _place_resting_exit_order / _cancel_resting_orders
/ _maintain_take_profit_order, and _check_exit_conditions' manager.dry_run
branch that delegates to them). Added 2026-08-15 alongside the feature:
replaces up-to-a-full-tick polling latency on take-profit with an order
resting directly on Kalshi's book, repriced as the model target drifts.
Run directly:

    python test_resting_exit_orders.py
"""

from datetime import datetime, timedelta, timezone

import config
config.ENABLE_TRAILING_EXIT = True
config.ENABLE_TRAILING_STOP = True  # default is False as of 2026-08-15 -- scenario 6b below needs it on

import deribit_iv
import strategy
from kalshi_btc_markets import ActiveMarket


class FakeLiveManager:
    """dry_run=False -- exercises the live resting-order path. Simulates just
    enough of a real order book to test against: buy_favored_side always
    rests (never auto-fills; tests simulate a fill by clearing _resting[ticker]
    directly, standing in for Kalshi's matching engine), get_resting_orders
    reads that state back, cancel_order removes by order_id. Not a mock of
    Kalshi's actual matching semantics -- just enough surface for
    strategy.py's maintenance logic to operate against.
    """

    def __init__(self):
        self.dry_run = False
        self._resting: dict[str, list[dict]] = {}
        self._next_id = 1
        self.buy_calls = []
        self.cancel_calls = []

    def get_resting_orders(self, ticker):
        return list(self._resting.get(ticker, []))

    def cancel_order(self, order_id):
        for ticker, orders in self._resting.items():
            self._resting[ticker] = [o for o in orders if o["order_id"] != order_id]
        self.cancel_calls.append(order_id)
        return {"order_id": order_id, "reduced_by": "1.00"}

    def buy_favored_side(self, ticker, side, contracts, limit_price):
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        self._resting.setdefault(ticker, []).append(
            {"order_id": order_id, "ticker": ticker, "side": side, "yes_price_dollars": f"{limit_price:.4f}"}
        )
        self.buy_calls.append({"ticker": ticker, "side": side, "contracts": contracts, "limit_price": limit_price})
        return {"order_id": order_id, "fill_count": "0.00", "remaining_count": f"{contracts:.2f}"}


def _market(ticker, yes_bid, yes_ask, seconds_left=600.0):
    now = datetime.now(timezone.utc)
    return ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction="above", strike=100000.0,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=seconds_left),
        settlement_average_seconds=60.0, yes_bid=yes_bid, yes_ask=yes_ask,
    )


def _position(side="yes", contracts=5.0, entry_price=0.90, tp_order_price=None):
    return {
        "market": None, "side": side, "contracts": contracts,
        "entry_edge": 0.05, "entry_price": entry_price, "peak_price": entry_price,
        "reconciled": False, "tp_order_price": tp_order_price,
    }


# 1. Nothing resting yet, valid target -> places one, records the target on the position.
manager = FakeLiveManager()
open_positions = {"T1": _position(side="yes")}
strategy._maintain_take_profit_order("T1", open_positions, model_prob_held=0.95, manager=manager)
assert len(manager.buy_calls) == 1, manager.buy_calls
assert manager.buy_calls[0]["side"] == "no"  # exit side for a held "yes"
expected_target = round(0.95 - config.EXIT_EDGE_THRESHOLD, 2)
assert open_positions["T1"]["tp_order_price"] == expected_target, open_positions["T1"]
assert abs(manager.buy_calls[0]["limit_price"] - round(1.0 - expected_target, 2)) < 1e-9
print("PASS  places_resting_order_when_none_exists")

# 2. Already resting at the current target -> leaves it alone (no new buy/cancel calls).
manager2 = FakeLiveManager()
open_positions2 = {"T2": _position(side="yes", tp_order_price=round(0.95 - config.EXIT_EDGE_THRESHOLD, 2))}
manager2._resting["T2"] = [{"order_id": "existing-1", "ticker": "T2", "side": "no", "yes_price_dollars": "0.9300"}]
strategy._maintain_take_profit_order("T2", open_positions2, model_prob_held=0.95, manager=manager2)
assert manager2.buy_calls == [] and manager2.cancel_calls == [], (manager2.buy_calls, manager2.cancel_calls)
print("PASS  leaves_correctly_priced_resting_order_alone")

# 3. Resting but at a stale target (model drifted) -> cancels the old one, places a new one.
manager3 = FakeLiveManager()
stale_target = round(0.90 - config.EXIT_EDGE_THRESHOLD, 2)
open_positions3 = {"T3": _position(side="yes", tp_order_price=stale_target)}
manager3._resting["T3"] = [{"order_id": "existing-2", "ticker": "T3", "side": "no", "yes_price_dollars": "0.9000"}]
strategy._maintain_take_profit_order("T3", open_positions3, model_prob_held=0.97, manager=manager3)  # model moved
assert manager3.cancel_calls == ["existing-2"], manager3.cancel_calls
assert len(manager3.buy_calls) == 1, manager3.buy_calls
new_target = round(0.97 - config.EXIT_EDGE_THRESHOLD, 2)
assert open_positions3["T3"]["tp_order_price"] == new_target
print("PASS  reprices_when_target_has_drifted")

# 4. Was resting, now gone (and we didn't just cancel it ourselves) -> treat as filled, close position.
manager4 = FakeLiveManager()
open_positions4 = {"T4": _position(side="yes", tp_order_price=0.93)}
# manager4._resting["T4"] deliberately left empty -- simulates Kalshi having filled it
strategy._maintain_take_profit_order("T4", open_positions4, model_prob_held=0.95, manager=manager4)
assert "T4" not in open_positions4, "position should have been closed on detected fill"
assert manager4.buy_calls == [], "must not place a fresh order for an already-filled position"
print("PASS  detects_fill_and_closes_position")

# 5. Degenerate target (edge threshold exceeds model prob) and nothing resting yet -> no-op, no crash.
manager5 = FakeLiveManager()
open_positions5 = {"T5": _position(side="yes", tp_order_price=None)}
strategy._maintain_take_profit_order("T5", open_positions5, model_prob_held=0.01, manager=manager5)
assert manager5.buy_calls == [] and "T5" in open_positions5
assert open_positions5["T5"]["tp_order_price"] is None
print("PASS  degenerate_target_is_a_noop")

# 6. End-to-end via _check_exit_conditions in live mode: take-profit does NOT fire a marketable
# order even when current_edge crosses the threshold (that's the resting order's job now) --
# instead a resting order gets placed/maintained. Trailing-stop still fires a marketable exit,
# and cancels any resting take-profit order first.
def _stub_probability(prob_yes):
    def fake(surface, *, direction, strike, seconds_to_expiry):
        return deribit_iv.ProbabilityEstimate(
            prob_yes=prob_yes, forward_used=strike, sigma_used=0.5,
            years_to_expiry=seconds_to_expiry / deribit_iv.SECONDS_PER_YEAR, extrapolated=False,
        )
    deribit_iv.estimate_probability = fake


manager6 = FakeLiveManager()
m6 = _market("T6", yes_bid=0.96, yes_ask=0.97)  # current_price (yes) = yes_bid = 0.96 -- edge would clear take-profit
position6 = _position(side="yes", entry_price=0.80)
position6["market"] = m6
position6["peak_price"] = 0.80
open_positions6 = {"T6": position6}
_stub_probability(0.975)  # edge = 0.975-0.96 = 0.015 <= EXIT_EDGE_THRESHOLD -- would take-profit on the dry-run path

strategy._check_exit_conditions(open_positions6, {"T6": m6}, object(), manager6)

assert "T6" in open_positions6, (
    "take-profit crossing must not immediately close the position live -- placing/maintaining a "
    "resting order (which does NOT immediately delete the position) is the resting order's job now, "
    "not an immediate marketable exit the way the old one-shot code path worked"
)
assert len(manager6.buy_calls) == 1, f"should have placed exactly one resting take-profit order, got {manager6.buy_calls}"
assert open_positions6["T6"]["tp_order_price"] is not None, "resting order's target should now be tracked"
print("PASS  check_exit_conditions_delegates_take_profit_to_resting_order_when_live")

# 6b. Same setup, but trailing-stop crosses instead -- must fire a marketable exit and cancel
# whatever resting take-profit order was already sitting there.
manager6b = FakeLiveManager()
m6b = _market("T6b", yes_bid=0.80, yes_ask=0.81)
position6b = _position(side="yes", entry_price=0.80)
position6b["market"] = m6b
position6b["peak_price"] = 0.90  # dropped from 0.90 to 0.80 -- 0.10 >= TRAILING_STOP_DROP (0.05)
position6b["tp_order_price"] = 0.93
open_positions6b = {"T6b": position6b}
manager6b._resting["T6b"] = [{"order_id": "resting-tp", "ticker": "T6b", "side": "no", "yes_price_dollars": "0.9300"}]
_stub_probability(0.99)  # edge stays wide open -- only trailing stop should fire

strategy._check_exit_conditions(open_positions6b, {"T6b": m6b}, object(), manager6b)

assert "T6b" not in open_positions6b, "trailing stop should have closed the position"
assert "resting-tp" in manager6b.cancel_calls, "should have canceled the resting take-profit order before the marketable exit"
print("PASS  check_exit_conditions_cancels_resting_order_before_trailing_stop_exit")

# 6c. Same drop as #6b, but ENABLE_TRAILING_STOP off (the 2026-08-15 default) -- must NOT fire a
# marketable exit or touch the resting take-profit order at all, live path included.
config.ENABLE_TRAILING_STOP = False
manager6c = FakeLiveManager()
m6c = _market("T6c", yes_bid=0.80, yes_ask=0.81)
position6c = _position(side="yes", entry_price=0.80)
position6c["market"] = m6c
position6c["peak_price"] = 0.90
# Matches what _maintain_take_profit_order will compute this tick (round(0.99-0.02,2)) so the
# take-profit leg sees no reprice needed -- isolates this scenario to just the trailing-stop
# toggle, rather than also exercising (harmless, but unrelated) take-profit repricing.
position6c["tp_order_price"] = 0.97
open_positions6c = {"T6c": position6c}
manager6c._resting["T6c"] = [{"order_id": "resting-tp-2", "ticker": "T6c", "side": "no", "yes_price_dollars": "0.0300"}]
_stub_probability(0.99)

strategy._check_exit_conditions(open_positions6c, {"T6c": m6c}, object(), manager6c)

assert "T6c" in open_positions6c, "position should still be open -- trailing-stop toggle is off"
assert manager6c.buy_calls == [], f"should not have placed/repriced any order, got {manager6c.buy_calls}"
assert manager6c.cancel_calls == [], f"should not have canceled the resting take-profit order, got {manager6c.cancel_calls}"
config.ENABLE_TRAILING_STOP = True  # restore
print("PASS  trailing_stop_does_not_fire_live_when_toggle_is_off")

print("\nall scenarios passed")
