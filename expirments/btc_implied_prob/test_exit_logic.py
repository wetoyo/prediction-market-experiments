"""Offline, no-network verification of _check_exit_conditions' trigger logic
(dry-run path only -- see test_positions_tracking.py for the live-only
resting-order maintenance path, which needs a different fake manager).

Constructs synthetic positions/markets/probability estimates entirely in
memory -- no Deribit or Kalshi calls -- and asserts each of the exit paths
(take-profit, trailing-stop, no-trigger, "no"-side pricing, bad-quote skip)
behave the way _check_exit_conditions' docstring claims. This can't validate
the live IV surface or real fills -- only that the exit *decision logic*
itself does what it says given a quote/probability snapshot. Run directly:

    python test_exit_logic.py
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
config.ENABLE_TRAILING_EXIT = True  # force on regardless of env -- this test targets that code path
config.ENABLE_TRAILING_STOP = True  # ditto -- default is False as of 2026-08-15, this file exercises both paths

import deribit_iv
import strategy
from kalshi_btc_markets import ActiveMarket


class FakeManager:
    """dry_run=True -- exercises _check_exit_conditions' simulated tick-driven
    take-profit path, not the live resting-order path (see
    test_positions_tracking.py for that). Records buy_favored_side calls
    instead of touching OrderManager/Kalshi.
    """

    def __init__(self):
        self.dry_run = True
        self.calls = []

    def buy_favored_side(self, ticker, side, contracts, limit_price):
        self.calls.append({"ticker": ticker, "side": side, "contracts": contracts, "limit_price": limit_price})
        return {"dry_run": True}


def _market(ticker, yes_bid, yes_ask, seconds_left=600.0):
    now = datetime.now(timezone.utc)
    return ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction="above", strike=100000.0,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=seconds_left),
        settlement_average_seconds=60.0, yes_bid=yes_bid, yes_ask=yes_ask,
    )


def _stub_probability(prob_yes):
    """Patches deribit_iv.estimate_probability to return a fixed prob_yes,
    independent of surface/strike/time -- isolates _check_exit_conditions'
    own branching logic from the Black-76 math (tested separately, and not
    what this file is checking).
    """
    def fake(surface, *, direction, strike, seconds_to_expiry):
        return deribit_iv.ProbabilityEstimate(
            prob_yes=prob_yes, forward_used=strike, sigma_used=0.5,
            years_to_expiry=seconds_to_expiry / deribit_iv.SECONDS_PER_YEAR, extrapolated=False,
        )
    deribit_iv.estimate_probability = fake  # shared module object -- strategy.deribit_iv sees this too


def _position(market, side="yes", contracts=5.0, entry_price=0.90, entry_edge=0.05, peak_price=None):
    return {
        "market": market, "side": side, "contracts": contracts,
        "entry_edge": entry_edge, "entry_price": entry_price,
        "peak_price": peak_price if peak_price is not None else entry_price,
        "tp_order_price": None,  # unused on the dry-run path, present for schema consistency
    }


def run(name, open_positions, markets_by_ticker, prob_yes, expect_exit):
    _stub_probability(prob_yes)
    fake_manager = FakeManager()
    surface = object()  # never touched -- estimate_probability is stubbed above
    ticker = next(iter(open_positions))

    strategy._check_exit_conditions(open_positions, markets_by_ticker, surface, fake_manager)

    exited = ticker not in open_positions
    assert exited == expect_exit, f"{name}: expected exit={expect_exit}, got exit={exited}"
    if expect_exit:
        assert len(fake_manager.calls) == 1, f"{name}: expected exactly one exit order, got {fake_manager.calls}"
    else:
        assert len(fake_manager.calls) == 0, f"{name}: expected no exit order, got {fake_manager.calls}"
    print(f"PASS  {name}")


# 1. Take-profit: held "yes" -- current_price must read off yes_bid (what it could be sold for
# right now), not yes_ask. yes_bid=0.96 -> edge = 0.975-0.96 = 0.015 <= EXIT_EDGE_THRESHOLD (0.02).
m1 = _market("T1", yes_bid=0.96, yes_ask=0.97)
run(
    "take_profit_triggers",
    {"T1": _position(m1, side="yes", entry_price=0.90, peak_price=0.90)},
    {"T1": m1},
    prob_yes=0.975,
    expect_exit=True,
)

# 1b. Regression pin for the 2026-08-15 pricing fix: same quotes as #1 but with the ORIGINAL
# prob_yes (0.985) that used to false-trigger take-profit under the old (wrong-side-of-spread)
# formula (old current_price = yes_ask = 0.97 -> edge 0.015). Correct current_price = yes_bid =
# 0.96 -> edge = 0.985-0.96 = 0.025, above threshold -- must NOT exit.
run(
    "does_not_falsely_trigger_on_ask_side_pricing_bug",
    {"T1b": _position(m1, side="yes", entry_price=0.90, peak_price=0.90)},
    {"T1b": m1},
    prob_yes=0.985,
    expect_exit=False,
)

# 2. Trailing-stop: held "yes", peak climbed to 0.95, now dropped to 0.87 (yes_bid, drop=0.08 >=
# 0.05); edge still wide open.
m2 = _market("T2", yes_bid=0.87, yes_ask=0.88)
run(
    "trailing_stop_triggers",
    {"T2": _position(m2, side="yes", entry_price=0.80, peak_price=0.95)},
    {"T2": m2},
    prob_yes=0.99,  # current_price=0.87, edge=0.12 (way above threshold) -- only trailing stop should fire
    expect_exit=True,
)

# 2b. Same setup as #2, but with ENABLE_TRAILING_STOP off (the 2026-08-15 default) -- must NOT
# exit even though the drop condition is met, since take-profit's edge is also nowhere close.
config.ENABLE_TRAILING_STOP = False
run(
    "trailing_stop_does_not_fire_when_toggle_is_off",
    {"T2b": _position(m2, side="yes", entry_price=0.80, peak_price=0.95)},
    {"T2b": m2},
    prob_yes=0.99,
    expect_exit=False,
)
config.ENABLE_TRAILING_STOP = True  # restore for the remaining scenarios below

# 3. Holds: edge still wide, price hasn't dropped from peak.
m3 = _market("T3", yes_bid=0.84, yes_ask=0.85)
run(
    "holds_when_neither_condition_met",
    {"T3": _position(m3, side="yes", entry_price=0.85, peak_price=0.85)},
    {"T3": m3},
    prob_yes=0.95,  # current_price=0.84, edge=0.11 (> 0.02), trailing_drop=0.01 (< 0.05)
    expect_exit=False,
)

# 4. Bad quote skip: yes_bid is 0.0 (missing/default) -- must skip, not misread current_price as 1.0.
m4 = _market("T4", yes_bid=0.0, yes_ask=0.90)
run(
    "skips_on_missing_yes_bid",
    {"T4": _position(m4, side="no", entry_price=0.15, peak_price=0.15)},
    {"T4": m4},
    prob_yes=0.10,
    expect_exit=False,
)

# 5. "no"-side pricing: current_price for a held "no" position must come from (1 - yes_ask) (what
# it could be sold for), not (1 - yes_bid).
m5 = _market("T5", yes_bid=0.05, yes_ask=0.08)
run(
    "no_side_prices_off_yes_ask",
    {"T5": _position(m5, side="no", entry_price=0.20, peak_price=0.20)},
    {"T5": m5},
    prob_yes=0.94,  # model_prob_held (no)=1-0.94=0.06; current_price (no)=1-0.08=0.92 -> edge=-0.86 <= 0.02
    expect_exit=True,
)

print("\nall scenarios passed")
