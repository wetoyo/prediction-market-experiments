import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import backtest as bt  # noqa: E402
import indicators  # noqa: E402
import martingale as mg  # noqa: E402


def test_ladder_win_at_any_step_nets_base_profit():
    b, c, depth = 0.005, 0.0012, 7
    ns = mg.step_notionals(100.0, b, c, depth)
    g = 100.0 * (b - c)
    for k in range(depth + 1):
        losses = sum(n * (b + c) for n in ns[:k])
        assert ns[k] * (b - c) - losses == pytest.approx(g)
        assert mg.next_notional(g, losses, b, c) == pytest.approx(ns[k])


def test_no_cost_ladder_is_plain_doubling_and_fair_at_half():
    ns = mg.step_notionals(1.0, 0.01, 0.0, 7)
    assert ns == [2.0 ** k for k in range(8)]
    # p = 0.5, no costs: the cycle is a fair bet, Kelly exactly 0
    assert mg.kelly_cycle(0.5, 0.01, 0.0, 7) == pytest.approx(0.0, abs=1e-12)
    # survive 7 losses: bust 1/256; 255 straight won cycles ~ 1/e (the 36.8%)
    assert 1 - mg.cycle_win_prob(0.5, 7) == pytest.approx(1 / 256)
    assert mg.cycle_win_prob(0.5, 7) ** 255 == pytest.approx(0.3686, abs=1e-3)


def test_kelly_needs_edge_over_costs():
    b, c = 0.005, 0.0012
    assert mg.kelly_cycle(0.5, b, c, 7) < 0
    assert mg.kelly_single(0.5, b, c) < 0
    be = (b + c) / (2 * b)
    assert mg.kelly_single(be, b, c) == pytest.approx(0.0, abs=1e-12)
    assert mg.kelly_cycle(be + 0.05, b, c, 7) > 0


def test_plan_unfunded_without_edge_and_capped_with_it():
    args = dict(bracket=0.005, cost=0.0012, depth=7, kelly_fraction=0.5, max_cycle_risk=0.25, leverage=6.0)
    assert mg.plan_cycle(1000.0, 0.5, sizing="kelly", **args).n0 == 0
    plan = mg.plan_cycle(1000.0, 0.9, sizing="kelly", **args)
    assert 0 < plan.risk <= 250.0 + 1e-9
    full = mg.plan_cycle(1000.0, 0.5, sizing="full", **args)
    assert full.risk <= 1000.0 + 1e-9  # never more than the bankroll


def test_win_rate_estimator_window_and_prior():
    e = mg.WinRateEstimator(prior=10, window=4)
    assert e.p == 0.5
    for w in (1, 1, 1, 1, 0, 0):
        e.add(bool(w))
    assert e.n == 4 and e.wins == 2
    assert e.p == pytest.approx((2 + 5) / 14)


def test_indicator_directions_follow_trend():
    up = [100 + 0.05 * i * i for i in range(80)]  # accelerating, so the MACD histogram stays > 0
    down = [500 - 0.05 * i * i for i in range(80)]
    assert indicators.directions(up, "macd_rsi")[-1] == 1
    assert indicators.directions(down, "macd_rsi")[-1] == -1
    assert indicators.rsi(up, 14)[-1] == 100.0


def _candle(t, px, lo, hi):
    """Zero spread; opens and closes at px, trades down to lo and up to hi."""
    return [t, px, hi, lo, px, px, hi, lo, px]


def test_simulate_losses_then_win_skims_base_profit():
    # always long, 1% bracket, no costs: lose, lose, then win -> +G skimmed
    p = bt.Params(signal="always_long", bar_minutes=1, bracket=0.01, depth=3, sizing="full", fee_rate=0.0,
                  cost_estimate=0.0, stop_slippage=0.0, bankroll=1000.0, leverage=100.0)
    px = 8.0
    candles = [_candle(60.0, px, px, px)]
    for i, (lo, hi) in enumerate(((7.9, px), (7.9, px), (px, 8.1)), start=2):
        candles.append(_candle(60.0 * i, px, lo, hi))  # stop or target touched; the next bet enters at px
    r = bt.simulate(candles, [], p, trade_from=0, trade_to=candles[-1][0])
    assert r.cycles == 1 and r.cycle_wins == 1 and r.busts == 0
    assert r.steps_hist == {"2": 1}
    assert r.reserve == pytest.approx(1000.0 / 15, rel=0.01)  # G = n0 * b, R = (1+2+4+8) * n0 * b
    assert r.bankroll == pytest.approx(1000.0, abs=1.0)
