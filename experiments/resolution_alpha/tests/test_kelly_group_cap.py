"""Regression tests for runner._kelly_group_key / _group_kelly_cap_contracts.

The bug this pins (live 2026-09-11): the 15m and 1hr BTC markets both closed
at 8pm (same underlying, same close_time), and each independently sized a
full-Kelly stake off the same bankroll -- the combined position exceeded what
one Kelly calculation for that single correlated bet would have allowed, so
the eventual loss came in bigger than KELLY_FRACTION was meant to cap. The fix
shares one Kelly dollar budget across every ticker that settles at the same
instant, with the longest-interval ("1hr") market evaluated first each tick
so it gets first claim on that shared budget -- see runner.py's
evaluate_and_maybe_trade for the real wiring.
"""

from datetime import datetime, timedelta, timezone

from runner import ActiveMarket, _group_kelly_cap_contracts, _kelly_group_key


def _market(ticker, underlying="BTC", interval_minutes=15.0, close_hour=20, close_minute=0):
    close_time = datetime(2026, 9, 11, close_hour, close_minute, tzinfo=timezone.utc)
    return ActiveMarket(
        ticker=ticker,
        event_ticker="EVT",
        series_ticker="SER",
        underlying=underlying,
        direction="above",
        strike=100_000.0,
        open_time=close_time - timedelta(minutes=15),
        close_time=close_time,
        interval_minutes=interval_minutes,
    )


def test_same_underlying_and_close_time_share_a_group():
    hourly = _market("KXBTCD-26SEP112000-100000", interval_minutes=60.0)
    fifteen_min = _market("KXBTC15M-26SEP112000-100000", interval_minutes=15.0)
    assert _kelly_group_key(hourly) == _kelly_group_key(fifteen_min)


def test_different_close_times_do_not_share_a_group():
    on_the_hour = _market("KXBTC15M-26SEP112000-100000", close_minute=0)
    quarter_past = _market("KXBTC15M-26SEP112015-100000", close_minute=15)
    assert _kelly_group_key(on_the_hour) != _kelly_group_key(quarter_past)


def test_different_underlyings_do_not_share_a_group():
    btc = _market("KXBTCD-26SEP112000-100000", underlying="BTC")
    eth = _market("KXETHD-26SEP112000-3500", underlying="ETH")
    assert _kelly_group_key(btc) != _kelly_group_key(eth)


# --- _group_kelly_cap_contracts --------------------------------------------


def test_no_frozen_cap_yet_falls_back_to_solo_cap():
    # Group untouched this cycle (frozen cap is None, nothing committed) --
    # behaves exactly like the old per-ticker cap: solo_kelly_cap at price.
    cap = _group_kelly_cap_contracts(
        already_filled_this_market=0.0, price=0.80, solo_kelly_cap_contracts=50.0,
        frozen_group_cap_dollars=None, group_committed_dollars=0.0,
    )
    assert cap == 50.0


def test_sibling_fill_consumes_the_shared_budget():
    # 1hr leg already filled and froze a $40 group budget; nothing spent on
    # this (15m) ticker yet. This market's own solo cap would be 60 contracts
    # (would-be full Kelly at its own price/edge) but must be clipped to what
    # the group's frozen $40 budget still has room for at this ticker's price.
    cap = _group_kelly_cap_contracts(
        already_filled_this_market=0.0, price=0.50, solo_kelly_cap_contracts=60.0,
        frozen_group_cap_dollars=40.0, group_committed_dollars=40.0,  # 1hr spent it all
    )
    assert cap == 0.0  # no room left in the shared budget


def test_sibling_partial_spend_leaves_remaining_room():
    cap = _group_kelly_cap_contracts(
        already_filled_this_market=0.0, price=0.50, solo_kelly_cap_contracts=60.0,
        frozen_group_cap_dollars=40.0, group_committed_dollars=25.0,  # $15 left
    )
    assert cap == 30.0  # $15 / 0.50


def test_solo_market_with_no_sibling_is_unaffected():
    # A market with no same-close_time sibling is a "group of one" -- once IT
    # fills and freezes the group cap off its own solo number, its own later
    # tranches see exactly the pre-fix per-ticker behavior.
    cap = _group_kelly_cap_contracts(
        already_filled_this_market=10.0, price=0.80, solo_kelly_cap_contracts=999.0,
        frozen_group_cap_dollars=40.0, group_committed_dollars=8.0,  # 10 contracts @ 0.80
    )
    assert cap == 10.0 + 32.0 / 0.80  # already_filled + remaining room


def test_zero_price_does_not_divide_by_zero():
    cap = _group_kelly_cap_contracts(
        already_filled_this_market=5.0, price=0.0, solo_kelly_cap_contracts=10.0,
        frozen_group_cap_dollars=None, group_committed_dollars=0.0,
    )
    assert cap == 5.0
