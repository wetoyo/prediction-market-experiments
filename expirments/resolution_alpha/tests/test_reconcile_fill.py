"""Regression tests for runner._reconcile_fill -- turning a place_order
response into (filled_contracts, favored_side_avg_price, fee_dollars).

The bug this pins (live 2026-08-28): Kalshi's `average_fill_price` is always
YES-denominated, so a NO fill came back as ~0.03 and was booked as the cost
per contract instead of ~0.97. `cost = avg_price * filled_size` then barely
moved the bankroll, and MAX_SIZE_MODE fired another full-size order next tick.
"""

from types import SimpleNamespace

from runner import _reconcile_fill


def _sim(avg_price, filled_size):
    """Stand-in for orderbook.FillEstimate (favored-side-denominated)."""
    return SimpleNamespace(avg_price=avg_price, filled_size=filled_size)


def test_dry_run_returns_simulated():
    filled, price, fee = _reconcile_fill({}, _sim(0.9500, 50.0), "no", dry_run=True)
    assert (filled, price) == (50.0, 0.9500)
    assert fee >= 0.0


def test_live_yes_fill_price_passes_through():
    resp = {"fill_count": "1.00", "average_fill_price": "0.0100", "average_fee_paid": "0.0007"}
    filled, price, fee = _reconcile_fill(resp, _sim(0.0100, 1.0), "yes", dry_run=False)
    assert filled == 1.0
    assert price == 0.0100
    assert round(fee, 4) == 0.0007


def test_live_no_fill_price_converted_to_favored_side():
    # The incident: NO buy, response avg_fill_price is the ~0.027 YES price.
    resp = {"fill_count": "133.59", "average_fill_price": "0.0270", "average_fee_paid": "0.0019"}
    filled, price, fee = _reconcile_fill(resp, _sim(0.9730, 202.0), "no", dry_run=False)
    assert filled == 133.59
    assert abs(price - 0.9730) < 1e-9        # 1 - 0.0270, NOT 0.0270
    assert 0.24 < fee < 0.26                 # ~0.0019 * 133.59, not * nothing


def test_zero_fill():
    resp = {"fill_count": "0.00", "average_fill_price": "0.0000"}
    assert _reconcile_fill(resp, _sim(0.97, 200.0), "no", dry_run=False) == (0.0, 0.0, 0.0)


def test_implausible_price_falls_back_to_simulated():
    # avg_fill_price nowhere near the simulated favored-side price (e.g. an API
    # shape change, or a denomination mistake) -> trust the simulation, warn.
    resp = {"fill_count": "100.00", "average_fill_price": "0.9730"}  # -> favored 0.027, sim says 0.973
    filled, price, fee = _reconcile_fill(resp, _sim(0.9730, 100.0), "no", dry_run=False)
    assert filled == 100.0
    assert price == 0.9730                   # simulated, not the 0.027 conversion


def test_missing_price_falls_back_to_simulated():
    resp = {"fill_count": "10.00"}
    filled, price, _ = _reconcile_fill(resp, _sim(0.955, 10.0), "yes", dry_run=False)
    assert (filled, price) == (10.0, 0.955)


def test_absurd_fee_field_is_rejected_for_the_estimate():
    # average_fee_paid is per-contract; a value that would make total fee
    # exceed the notional is not trusted.
    resp = {"fill_count": "10.00", "average_fill_price": "0.0300", "average_fee_paid": "50"}
    _, price, fee = _reconcile_fill(resp, _sim(0.97, 10.0), "no", dry_run=False)
    assert price == 0.97
    assert 0.0 <= fee <= 10.0               # estimate_fee_dollars, not 50 * 10
