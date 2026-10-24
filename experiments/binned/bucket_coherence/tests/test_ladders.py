import math

import numpy as np
import pytest

import ladders
import screen
import snapshot
from fees import estimate_fee_dollars, fee_per_contract_raw


def test_fee_matches_confirmed_fills():
    assert estimate_fee_dollars(0.01, 1) == pytest.approx(0.0007)
    assert estimate_fee_dollars(0.925, 2) == pytest.approx(0.0098)
    assert fee_per_contract_raw(0.5) == pytest.approx(0.0175)


def test_yes_set_edge_sign_and_fee():
    # 3 buckets at 0.30: cost 0.90 + 3 * 0.07*0.3*0.7 = 0.9441
    assert ladders.yes_set_edge([0.3, 0.3, 0.3]) == pytest.approx(1 - 0.9441)
    assert ladders.yes_set_edge([0.5, 0.5]) < 0  # fee turns a $1 sum negative
    assert ladders.yes_set_edge([0.2, 1.0]) == -math.inf  # unbuyable leg


def test_no_set_edge():
    # yes bids 0.4 x3: NO asks 0.6 each, cost 1.8 + 3*0.0168, pays 2
    assert ladders.no_set_edge([0.4, 0.4, 0.4]) == pytest.approx(2 - 1.8 - 3 * 0.07 * 0.24)
    assert ladders.no_set_edge([0.0, 0.6]) == -math.inf


def test_exhaustiveness():
    ms = [{"result": "yes", "open_ts": 0}, {"result": "no", "open_ts": 0}]
    assert ladders.exhaustiveness(ms)["clean"]
    bad = [{"result": "no", "open_ts": 0}, {"result": "no", "open_ts": 1000}]
    e = ladders.exhaustiveness(bad)
    assert not e["clean"] and e["n_yes"] == 0 and e["late_listed"] == 1
    void = [{"result": "yes", "open_ts": 0}, {"result": "void", "open_ts": 0}]
    assert ladders.exhaustiveness(void)["other_results"] == ["void"]
    # a tie settled 50/50 is not one-YES, but a full set is still paid $1
    tie = [{"result": "scalar", "open_ts": 0, "settle": 0.5}, {"result": "scalar", "open_ts": 0, "settle": 0.5}]
    e = ladders.exhaustiveness(tie)
    assert not e["clean"] and e["payout_ok"]
    short = [{"result": "no", "open_ts": 0, "settle": 0.0}, {"result": "no", "open_ts": 0, "settle": 0.0}]
    assert not ladders.exhaustiveness(short)["payout_ok"]


def test_asks_from_book():
    assert ladders.asks_from_book([(0.6, 5), (0.7, 3)]) == [(0.3, 3), (0.4, 5)]


def test_full_set_depth_walks_levels_and_stops_at_thinnest():
    legs = [[(0.3, 10), (0.32, 5)], [(0.3, 4), (0.31, 100)], [(0.3, 50)]]
    r = ladders.full_set_depth(legs, 1.0)
    assert r["top_size"] == 4
    assert r["sets"] == 15  # leg 1 runs out
    assert r["limits"] == [0.32, 0.31, 0.3]
    cost = (10 * 0.3 + 5 * 0.32) + (4 * 0.3 + 11 * 0.31) + 15 * 0.3
    fees = sum(q * fee_per_contract_raw(p) for q, p in
               [(10, 0.3), (5, 0.32), (4, 0.3), (11, 0.31), (15, 0.3)])
    assert r["profit"] == pytest.approx(15 - cost - fees)


def test_full_set_depth_stops_when_marginal_set_loses():
    legs = [[(0.45, 10), (0.6, 10)], [(0.45, 10), (0.6, 10)]]
    r = ladders.full_set_depth(legs, 1.0)
    assert r["sets"] == 10 and r["profit"] > 0


def test_full_set_depth_no_arb():
    r = ladders.full_set_depth([[(0.5, 10)], [(0.5, 10)]], 1.0)
    assert r["sets"] == 0 and r["top_edge"] < 0


def test_fractional_sizes_trade_whole_sets():
    r = ladders.full_set_depth([[(0.3, 2.5)], [(0.3, 9)], [(0.3, 9)]], 1.0)
    assert r["sets"] == 2
    assert r["profit"] == pytest.approx(2 * ladders.yes_set_edge([0.3] * 3))


def test_runs():
    assert ladders.runs([0, 1, 1, 1, 0, 1]) == [(1, 3), (5, 1)]
    assert ladders.runs([]) == []


def test_grid_forward_fill_and_vector_edges_match_scalar():
    g0 = 6000
    candles = {"A": [[6000.0, 0.30, 0.31], [6120.0, 0.28, 0.29]],
               "B": [[5940.0, 0.30, 0.31]],
               "C": [[6060.0, 0.30, 0.31]]}
    grid, tick, bids, asks, changes = screen.grid_quotes(candles, {}, g0, g0 + 180)
    assert np.isnan(asks[2, 0]) and asks[2, 1] == 0.31  # C quoted from minute 1
    assert asks[0, 3] == 0.29  # A forward-filled after its update
    assert list(changes) == [0, 0, 1, 0]
    ye, ne = screen.edges_from_grid(bids, asks)
    assert np.isnan(ye[0])
    assert ye[1] == pytest.approx(ladders.yes_set_edge([0.31, 0.31, 0.31]))
    assert ye[3] == pytest.approx(ladders.yes_set_edge([0.29, 0.31, 0.31]))
    assert ne[3] == pytest.approx(ladders.no_set_edge([0.28, 0.30, 0.30]))


def test_seed_fills_window_start():
    grid, tick, bids, asks, _ = screen.grid_quotes({"A": [], "B": [[6060.0, 0.5, 0.6]]}, {"A": [0.2, 0.25]},
                                                  6000, 6060)
    assert asks[0, 0] == 0.25 and np.isnan(asks[1, 0])


def test_summarize_runs_marks_static():
    grid = np.arange(0, 300, 60.0)
    edge = np.array([-0.1, 0.02, 0.03, 0.01, np.nan])
    changes = np.array([0, 0, 0, 1, 0])
    out = screen.summarize_runs(grid, edge, changes)
    assert out == [[60.0, 3, pytest.approx(0.02), 0.03, 1]]


def test_parse_missing_quotes():
    rows = screen._parse([{"end_period_ts": 60, "yes_bid": {"close_dollars": None}, "yes_ask": {}},
                          {"end_period_ts": 120, "yes_bid": {"close": "0.4"}, "yes_ask": {"close": "0.45"}}])
    assert rows == [[60.0, 0.0, 1.0], [120.0, 0.4, 0.45]]


def test_ticker_date():
    assert screen.ticker_date("KXBTC-26OCT2414").day == 24
    assert screen.ticker_date("KXMLBGAME-26SEP152010KCHOU").month == 9
    assert screen.ticker_date("NOPE") is None


def test_snapshot_evaluate_finds_book_arb():
    # 3 buckets; YES asks come from NO bids: NO bid 0.70 -> YES ask 0.30
    books = {t: {"yes": [(0.25, 50.0)], "no": [(0.70, 8.0), (0.60, 100.0)]} for t in "ABC"}
    r = snapshot.evaluate(books, list("ABC"))
    assert r["yes_top_edge"] == pytest.approx(ladders.yes_set_edge([0.3] * 3))
    assert r["yes_top_size"] == 8 and r["yes_sets"] == 8
    assert r["no_sets"] == 0  # yes bids sum 0.75 < 1
    assert snapshot.evaluate(books, list("ABD"))["error"] == "missing books"
