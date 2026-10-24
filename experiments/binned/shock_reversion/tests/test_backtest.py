"""Offline checks of the backtest mechanics on synthetic candles."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest as bt  # noqa: E402
from fees import estimate_fee_dollars  # noqa: E402

T0 = 1_760_000_400 - (1_760_000_400 % 3600)
H = 3600


def candles(quotes, vol=10.0):
    """quotes: list of (bid, ask) per hour starting at T0."""
    return [[T0 + i * H, b, a, vol] for i, (b, a) in enumerate(quotes)]


def market(result="no", close_h=200):
    iso = bt.datetime.fromtimestamp(T0 + close_h * H, bt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ticker": "M1", "event_ticker": "E1", "series_ticker": "S", "category": "Politics",
            "result": result, "close_time": iso, "open_time": iso}


def test_fee_matches_kalshi_formula():
    assert bt.fee_per_contract(0.5) == pytest.approx(estimate_fee_dollars(0.5, 10) / 10)
    assert bt.fee_per_contract(0.5) == pytest.approx(0.0175, abs=1e-4)
    assert bt.fee_per_contract(0.5, 0.0) == 0.0


def test_forward_fill_and_empty_sides():
    q = bt.build_quotes([[T0, 0.40, 0.42, 1], [T0 + 3 * H, 0.0, 0.50, 1]])
    assert len(q.bid) == 4
    assert q.bid[1] == q.bid[2] == pytest.approx(0.40)       # no candle -> carried
    assert bt.np.isnan(q.bid[3])                              # bid 0 -> empty side


def test_shock_detected_on_closed_candle_and_entered_next():
    quotes = [(0.40, 0.42)] * 5 + [(0.55, 0.57)] + [(0.52, 0.54)] * 40
    q = bt.build_quotes(candles(quotes))
    ss = bt.find_shocks(market(), q, window=1, thresh=0.10, close_ts=T0 + 200 * H)
    assert len(ss) == 1
    s = ss[0]
    assert s.i_entry == 6 and s.t_entry == T0 + 6 * H          # next candle, not the shock candle
    assert s.move == pytest.approx(0.15)


def test_fade_buys_no_at_one_minus_bid_and_exits_at_one_minus_ask():
    quotes = [(0.40, 0.42)] * 5 + [(0.55, 0.57)] + [(0.52, 0.54)] * 10 + [(0.45, 0.47)] * 30
    m = market()
    q = bt.build_quotes(candles(quotes))
    ss = bt.find_shocks(m, q, 1, 0.10, T0 + 200 * H)
    tr = bt.simulate(m, q, ss, "fade", horizon=6, mult=1.0, real_close=T0 + 200 * H)
    assert len(tr) == 1
    t = tr[0]
    assert t.side == "no"
    assert t.entry == pytest.approx(1 - 0.52)                  # 1 - yes_bid at entry candle
    assert t.exit == pytest.approx(1 - 0.54)                   # 1 - yes_ask six hours later
    assert not t.settled
    exp = (1 - 0.54) - (1 - 0.52) - bt.fee_per_contract(0.48) - bt.fee_per_contract(0.46)
    assert t.pnl() == pytest.approx(exp)
    assert t.pnl(0.01) == pytest.approx(exp - 0.02)            # slippage on both sides


def test_momentum_buys_yes_at_ask_and_settles_if_close_first():
    quotes = [(0.40, 0.42)] * 5 + [(0.55, 0.57)] + [(0.60, 0.62)] * 10
    m = market(result="yes", close_h=12)
    q = bt.build_quotes(candles(quotes))
    ss = bt.find_shocks(m, q, 1, 0.10, T0 + 12 * H)
    tr = bt.simulate(m, q, ss, "momentum", horizon=24, mult=1.0, real_close=T0 + 12 * H)
    t = tr[0]
    assert t.side == "yes" and t.entry == pytest.approx(0.62) and t.settled
    assert t.exit == 1.0 and t.fee_out == 0.0
    assert t.pnl(0.02) == pytest.approx(1.0 - 0.64 - bt.fee_per_contract(0.62))  # no exit slippage at settlement


def test_one_position_per_market():
    quotes = [(0.40, 0.42)] * 3 + [(0.55, 0.57), (0.40, 0.42), (0.55, 0.57), (0.40, 0.42)] + [(0.45, 0.47)] * 30
    m = market()
    q = bt.build_quotes(candles(quotes))
    ss = bt.find_shocks(m, q, 1, 0.10, T0 + 200 * H)
    assert len(ss) >= 3
    tr = bt.simulate(m, q, ss, "fade", horizon=6, mult=1.0, real_close=T0 + 200 * H)
    assert len(tr) == 1


def test_bootstrap_by_event_and_drop_best():
    quotes = [(0.40, 0.42)] * 5 + [(0.55, 0.57)] * 2 + [(0.30, 0.32)] * 20
    m = market()
    q = bt.build_quotes(candles(quotes))
    tr = bt.simulate(m, q, bt.find_shocks(m, q, 1, 0.10, T0 + 200 * H), "fade", 6, 1.0, T0 + 200 * H)
    assert bt.roi(tr) > 0
    assert bt.drop_best_events(tr, 1) == []
