"""Offline tests for the pricing model and the backtest's fill/stat rules."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest  # noqa: E402
import data  # noqa: E402
import fees  # noqa: E402
import model  # noqa: E402

H = 3600.0


def _ladder():
    return [
        {"strike_type": "less", "floor_strike": None, "cap_strike": 60},
        {"strike_type": "between", "floor_strike": 60, "cap_strike": 61},
        {"strike_type": "between", "floor_strike": 62, "cap_strike": 63},
        {"strike_type": "between", "floor_strike": 64, "cap_strike": 65},
        {"strike_type": "between", "floor_strike": 66, "cap_strike": 67},
        {"strike_type": "greater", "floor_strike": 67, "cap_strike": None},
    ]


def test_bucket_bounds_cover_integers_once():
    ladder = _ladder()
    for temp in range(40, 90):
        hits = [m for m in ladder if model.bucket_contains(m, temp)]
        assert len(hits) == 1, temp


def test_bucket_probs_sum_to_one():
    em = model.ErrorModel()
    for mu in (55.0, 63.2, 70.0):
        s = sum(model.bucket_prob(em, m, mu, 2.5) for m in _ladder())
        assert abs(s - 1.0) < 1e-9
    em_t = model.ErrorModel(dist="t")
    assert abs(sum(model.bucket_prob(em_t, m, 63.0, 2.0) for m in _ladder()) - 1.0) < 1e-9


def test_mos_index_respects_publication_delay():
    rt0 = 1_000_000 * H
    fc = {rt0: {"2026-07-01": (80.0, 2.0)}, rt0 + 6 * H: {"2026-07-01": (84.0, 2.0)}}
    idx = model.MosIndex(fc, delay_hours=4.0)
    # 5h after the later run's nominal time minus 1h: only the first run is out
    assert idx.latest(rt0 + 9 * H, "2026-07-01")[0] == 80.0
    assert idx.latest(rt0 + 10 * H, "2026-07-01")[0] == 84.0
    assert idx.latest(rt0 + 3 * H, "2026-07-01") is None  # nothing published yet
    assert idx.latest(rt0 + 10 * H, "2026-07-02") is None


def test_mos_max_rows_map_to_local_day():
    rows = [
        {"runtime": "2026-07-01 07:00:00", "ftime": "2026-07-02 00:00:00", "n_x": 88.0, "sd": 2.0},
        {"runtime": "2026-07-01 07:00:00", "ftime": "2026-07-02 12:00:00", "n_x": 70.0},  # overnight min
    ]
    fc = data.mos_max_forecasts(rows)
    (only,) = fc.values()
    assert only == {"2026-07-01": (88.0, 2.0)}


def test_error_model_learns_bias_and_spread():
    rows = []
    for i in range(400):
        day = f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        rows.append({"city": "X", "label": "L", "day": day, "f": {"NBS": 70.0}, "obs": 72.0 + (1 if i % 2 else -1) * 2})
    em = model.ErrorModel().fit(rows)
    mu, sigma = em.predict("X", "L", "2025-06-15", {"NBS": 70.0})
    assert abs(mu - 72.0) < 1e-6
    assert abs(sigma - 2.0) < 0.05
    assert em.predict("Y", "L", "2025-06-15", {"NBS": 70.0}) is None


def test_fee_rounding():
    assert fees.estimate_fee_dollars_cent(0.50, 1) == 0.02   # 1.75c -> 2c
    assert fees.estimate_fee_dollars_cent(0.05, 1) == 0.01   # 0.33c -> 1c
    assert fees.estimate_fee_dollars(0.50, 1) == 0.0175


def test_quote_at_uses_last_closed_candle_only():
    candles = [[10 * H, 0.30, 0.32, 5.0], [11 * H, 0.40, 0.42, 1.0], [12 * H, 0.0, 1.0, 0.0]]
    assert backtest.quote_at(candles, 11 * H + 1) == (0.40, 0.42)
    assert backtest.quote_at(candles, 12 * H) == (None, None)  # empty book
    assert backtest.quote_at(candles, 9 * H) is None
    assert backtest.quote_at(candles, 15 * H) is None  # stale


def test_select_trades_edges_and_slippage():
    cands = [
        {"label": "d0_08h", "p": 0.60, "bid": 0.40, "ask": 0.45, "won": True, "day": "d", "event": "e", "city": "c"},
        {"label": "d0_08h", "p": 0.10, "bid": 0.30, "ask": 0.33, "won": False, "day": "d", "event": "e", "city": "c"},
        {"label": "d0_08h", "p": 0.50, "bid": 0.48, "ask": 0.50, "won": False, "day": "d", "event": "e", "city": "c"},
    ]
    tr = backtest.select_trades(cands, threshold=0.05, min_price=0.02, sides="both", labels="both")
    assert [t["side"] for t in tr] == ["yes", "no"]
    assert math.isclose(tr[0]["cost"], 0.45 + 0.02)
    assert math.isclose(tr[0]["pnl"], 1 - 0.47)
    assert math.isclose(tr[1]["price"], 0.70)
    tr_s = backtest.select_trades(cands, threshold=0.05, min_price=0.02, sides="both", labels="both", slip=0.01)
    assert len(tr_s) == 2 and math.isclose(tr_s[0]["price"], 0.46)
    s = backtest.summarize(tr)
    assert s["trades"] == 2 and s["pnl"] > 0


def test_bootstrap_and_drop_best_days():
    trades = [{"day": f"d{i}", "event": f"e{i}", "pnl": 0.1, "cost": 0.5} for i in range(20)]
    lo, hi = backtest.bootstrap_roi(trades, reps=200)
    assert math.isclose(lo, 0.2) and math.isclose(hi, 0.2)
    assert len(backtest.drop_best_days(trades, 5)) == 15
