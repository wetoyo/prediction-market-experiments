"""Offline tests for the longshot_fade backtest mechanics (no network)."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest as bt  # noqa: E402
import data  # noqa: E402
import fees  # noqa: E402

H = 3600
CLOSE = 1_000_000 * H


def market(ticker="S-E-A", event="S-E", result="no", open_h=48, volume=5000):
    return {"ticker": ticker, "event": event, "series": "S", "open_ts": CLOSE - open_h * H, "close_ts": CLOSE,
            "settled_ts": CLOSE + 600, "result": result, "volume": volume}


SERIES = {"S": {"category": "Politics", "fee_type": "quadratic", "fee_multiplier": 1, "frequency": "custom"}}


def test_vectorised_fee_matches_fees_py():
    for p in np.linspace(0.01, 0.99, 99):
        for rate in (0.07, 0.035, 0.0):
            v = bt.fee_per_contract(np.array([p]), np.array([rate]))[0]
            assert v == pytest.approx(fees.estimate_fee_dollars(p, bt.FEE_LOT, rate) / bt.FEE_LOT, abs=1e-12)
            c = bt.fee_per_contract(np.array([p]), np.array([rate]), cent_1lot=True)[0]
            assert c == pytest.approx(fees.estimate_fee_dollars_cent_rounded(p, 1, rate), abs=1e-12)


def test_fee_known_fills():
    # the three real fills documented in resolution_alpha/fees.py
    assert fees.estimate_fee_dollars(0.01, 1) == pytest.approx(0.0007)
    assert fees.estimate_fee_dollars(0.925, 2) == pytest.approx(0.0098)
    assert fees.estimate_fee_dollars(0.09, 2) == pytest.approx(0.0115)
    assert fees.estimate_fee_dollars_cent_rounded(0.90, 1) == pytest.approx(0.01)
    assert fees.fee_rate_for_series("quadratic", 0.5) == pytest.approx(0.035)


def test_quote_uses_only_candles_at_or_before_entry():
    candles = [[CLOSE - 30 * H, 0.05, 0.07, 10], [CLOSE - 2 * H, 0.04, 0.06, 5], [CLOSE - H, 0.90, 0.95, 99]]
    q = bt.quote_at(candles, CLOSE - H - 1)  # the 0.90 candle ends 1s after entry
    assert q[1:3] == (0.04, 0.06)
    q = bt.quote_at(candles, CLOSE - H)      # ends exactly at entry: usable
    assert q[1:3] == (0.90, 0.95)
    assert bt.quote_at(candles, CLOSE - 31 * H) is None


def test_build_rows_skips_one_sided_and_unopened():
    candles = {
        "A": [[CLOSE - 25 * H, 0.05, 0.07, 1], [CLOSE - 7 * H, 0.0, 0.04, 1], [CLOSE - 2 * H, 0.03, 0.05, 1]],
        "B": [[CLOSE - 2 * H, 0.03, 0.05, 1]],
    }
    ms = [market("A", result="no"), market("B", result="yes", open_h=3)]
    rows = bt.build_rows(ms, candles, SERIES)
    got = {(r["ticker"], r["h"]) for r in rows}
    # A@6h sees the bid-0 candle -> skipped; B opened 3h before close -> only 1h
    assert got == {("A", 24), ("A", 1), ("B", 1)}
    a1 = next(r for r in rows if r["ticker"] == "A" and r["h"] == 1)
    assert a1["mid"] == pytest.approx(0.04) and not a1["yes"]


def _table():
    rows = [
        dict(ticker="A", event="E1", series="S", category="Politics", mech=False, settled_ts=1, h=1, bid=0.04,
             ask=0.06, mid=0.05, spread=0.02, stale_h=0, yes=False, vol24=500, volume=1000, fee_rate=0.07),
        dict(ticker="B", event="E1", series="S", category="Politics", mech=False, settled_ts=2, h=1, bid=0.92,
             ask=0.94, mid=0.93, spread=0.02, stale_h=0, yes=True, vol24=500, volume=1000, fee_rate=0.07),
        dict(ticker="C", event="E2", series="T", category="Sports", mech=False, settled_ts=3, h=1, bid=0.08,
             ask=0.10, mid=0.09, spread=0.02, stale_h=0, yes=True, vol24=50, volume=1000, fee_rate=0.07),
    ]
    return bt.Table(rows)


def test_trade_prices_and_pnl():
    tb = _table()
    idx, price, cost, pnl, buy_yes = bt.trades(tb, bt.band_mask(tb, "no", 0.01, 0.10), "no")
    assert list(tb.ticker[idx]) == ["A", "C"]
    assert price == pytest.approx([0.96, 0.92])          # NO ask = 1 - yes_bid
    fee_a = fees.estimate_fee_dollars(0.96, bt.FEE_LOT) / bt.FEE_LOT
    assert pnl[0] == pytest.approx(1 - 0.96 - fee_a)      # A resolved NO: win
    assert pnl[1] == pytest.approx(-cost[1])              # C resolved YES: lose
    idx, price, _, pnl, buy_yes = bt.trades(tb, bt.band_mask(tb, "yes", 0.01, 0.10), "yes")
    assert list(tb.ticker[idx]) == ["B"] and price[0] == pytest.approx(0.94) and buy_yes.all()
    _, price1, _, _, _ = bt.trades(tb, bt.band_mask(tb, "no", 0.01, 0.10), "no", slip=0.01)
    assert price1 == pytest.approx([0.97, 0.93])


def test_any_side_buys_the_favourite():
    tb = _table()
    idx, price, _, _, buy_yes = bt.trades(tb, bt.band_mask(tb, "any", 0.01, 0.10), "any")
    assert dict(zip(tb.ticker[idx], buy_yes)) == {"A": False, "B": True, "C": False}


def test_config_filters():
    tb = _table()
    period = np.ones(tb.n, bool)
    cfg = bt.Config("no", 0.01, 0.10, 1, "all", 100, 0.10)
    assert list(tb.ticker[bt.config_mask(tb, cfg, period)]) == ["A"]   # C fails vol24
    cfg = bt.Config("no", 0.01, 0.10, 1, "Sports", 0, 0.10)
    assert list(tb.ticker[bt.config_mask(tb, cfg, period)]) == ["C"]
    cfg = bt.Config("no", 0.01, 0.10, 6, "all", 0, 0.10)
    assert not bt.config_mask(tb, cfg, period).any()


def test_bootstrap_resamples_events_not_markets():
    # one event with 50 identical winning markets, 50 events with one losing market each:
    # an event bootstrap must give a much wider CI than a market bootstrap would.
    ev = np.array([0] * 50 + list(range(1, 51)))
    cost = np.ones(100)
    pnl = np.array([0.1] * 50 + [-0.1] * 50)
    lo, hi = bt.boot_ci(ev, cost, pnl, n=4000)
    assert lo < -0.05 and hi > 0.05
    r, se = bt.cluster_stats(ev, cost, pnl)
    assert r == pytest.approx(0.0)
    assert se > 0.05


def test_parse_candles_live_and_historical_shapes():
    live = [{"end_period_ts": 10, "volume_fp": "3.00", "yes_bid": {"close_dollars": "0.0400"},
             "yes_ask": {"close_dollars": "0.0600"}}]
    hist = [{"end_period_ts": 10, "volume": "3.00", "yes_bid": {"close": "0.0400"}, "yes_ask": {"close": "0.0600"}}]
    assert data.parse_candles(live) == data.parse_candles(hist) == [[10, 0.04, 0.06, 3.0]]
    assert data.parse_candles([{"end_period_ts": 1, "yes_bid": {"close": None}, "yes_ask": {}}]) == [[1, None, None, 0.0]]


def test_slim_drops_scalar_and_voided():
    base = {"ticker": "S-E-A", "event_ticker": "S-E", "close_time": "2026-01-01T00:00:00Z",
            "open_time": "2025-12-30T00:00:00Z", "volume_fp": "10"}
    assert data.slim(dict(base, result="")) is None
    assert data.slim(dict(base, result="scalar")) is None
    s = data.slim(dict(base, result="yes"))
    assert s["series"] == "S" and s["result"] == "yes" and s["volume"] == 10
