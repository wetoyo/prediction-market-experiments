"""Offline tests: pricing, fees, data parsing and the backtest's no-lookahead
plumbing. No network."""

import math

import black_scholes as bs
import build_dataset as bd
import fees
import kalshi_data as kd
import market_data as md
import backtest as bt


def test_prob_above_symmetry_and_limits():
    assert abs(bs.prob_above(100, 100, 0.2, 1e-9) - 0.5) < 1e-3
    assert bs.prob_above(101, 100, 0.2, 0) == 1.0
    assert bs.prob_above(99, 100, 0.0, 1.0) == 0.0
    p = bs.prob_above(100, 101, 0.2, 1 / 252)
    assert 0.0 < p < 0.5


def test_prob_range_sums_to_one_over_ladder():
    edges = [None] + [7500 + 25 * i for i in range(20)] + [None]
    tot = sum(bs.prob_range(7600, lo, hi, 0.15, 3 / 6.5 / 252) for lo, hi in zip(edges[:-1], edges[1:]))
    assert abs(tot - 1.0) < 1e-9


def test_fees():
    assert fees.estimate_fee_dollars(0.5, 1) == 0.0175
    assert fees.fee_per_contract_cent_rounded(0.5, 1) == 0.02
    assert abs(fees.fee_per_contract_cent_rounded(0.5, 10) - 0.018) < 1e-12
    assert fees.fee_per_contract_cent_rounded(0.01, 1) == 0.01


def test_parse_event_and_strikes():
    d, close = bd.parse_event("KXINX-26OCT23H1600")
    assert d.isoformat() == "2026-10-23" and close.hour == 16
    assert bd.strikes({"strike_type": "between", "floor_strike": 7650, "cap_strike": 7674.9999}) == (7650, 7674.9999)
    assert bd.strikes({"strike_type": "greater", "floor_strike": 8049.9999}) == (8049.9999, None)
    assert bd.strikes({"strike_type": "less", "cap_strike": 7350}) == (None, 7350)


def test_quote_at_never_uses_future_candles():
    rows = [[100, 0.1, 0.2], [160, 0.3, 0.4], [220, 0.5, 0.6]]
    assert bd.quote_at(rows, 99) is None
    assert bd.quote_at(rows, 160) == (0.3, 0.4)
    assert bd.quote_at(rows, 219) == (0.3, 0.4)


def test_parse_candles_live_and_historical():
    live = [{"end_period_ts": 1, "yes_bid": {"close_dollars": "0.22"}, "yes_ask": {"close_dollars": "0.27"}}]
    hist = [{"end_period_ts": 2, "yes_bid": {"close": "0.0100"}, "yes_ask": {"close": "0.0300"}}]
    assert kd.parse_candles(live) == [[1.0, 0.22, 0.27]]
    assert kd.parse_candles(hist) == [[2.0, 0.01, 0.03]]


def test_series_value_at_uses_bar_end():
    s = md.Series([1000.0, 2000.0], [1.0, 2.0])
    assert s.value_at(999) is None
    assert s.value_at(1000) == 1.0
    assert s.value_at(1999) == 1.0


def test_bars_close_stamp_and_cut():
    # 15:30 ET bar (19:30Z) then the 16:00 close stamp (20:00Z) on an EDT day
    t0 = 1792787400  # 2026-10-23 19:30Z
    data = {"timestamp": [t0 - 3600, t0, t0 + 1800], "indicators": {"quote": [{"close": [1.0, 2.0, 3.0]}]}}
    s = md._bars(data, 3600, True)
    assert s.ends == [t0, t0 + 1800]
    assert s.closes == [1.0, 3.0]  # the 15:30 bar and the close stamp share the 16:00 end


def test_signal_side_and_fee_threshold():
    k = {"_all": 1.0}
    row = {"date": "2026-01-02", "series": "KXINX", "index": "SPX", "ts": 0, "close_ts": 3600, "combo": None,
           "lo": 7600, "hi": 7624.9999, "S": 7612, "V": 15.0, "T": 1 / 6.5 / 252, "bid": 0.05, "ask": 0.08,
           "result": 1, "ticker": "X"}
    p = bt.model_prob(row, k)
    assert p > 0.3
    tr = bt.signals([row], k, 0.05, 0.01)
    assert tr and tr[0]["side"] == "yes" and math.isclose(tr[0]["px"], 0.08)
    p_, c_ = bt.pnl(tr)
    assert p_ > 0 and c_ > 0.08
