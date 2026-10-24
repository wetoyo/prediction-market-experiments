"""Tests for the 2026-10-24 latency work: the ws feed's update signal that
event-driven evaluation waits on, the per-underlying spot/vol cache, the
off-event-loop sample writer, and the zero-fill retry cooldown that keeps
faster evaluation from re-firing IOC orders at every delta.
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config
import runner
import sampling
from discovery import ActiveMarket
from ws_feed import KalshiWebsocketFeed


# -- ws_feed.wait_for_update -------------------------------------------------

def _delta(feed, price=0.05):
    feed._apply_orderbook_delta({"market_ticker": "T", "side": "no", "price_dollars": str(price), "delta_fp": "1"})


def test_wait_for_update_times_out_without_an_update():
    async def go():
        return await KalshiWebsocketFeed().wait_for_update(0.01)
    assert asyncio.run(go()) is False


def test_wait_for_update_returns_at_once_for_an_update_applied_earlier_and_consumes_it():
    async def go():
        feed = KalshiWebsocketFeed()
        _delta(feed)
        first = await feed.wait_for_update(5.0)
        second = await feed.wait_for_update(0.01)
        return first, second
    assert asyncio.run(go()) == (True, False)


def test_wait_for_update_wakes_on_an_index_tick_while_waiting():
    async def go():
        feed = KalshiWebsocketFeed()

        async def tick():
            await asyncio.sleep(0.01)
            feed._apply_cfbenchmarks_value(
                {"index_id": "BRTI", "data": '{"value": 100000.0, "time": %d}' % int(time.time() * 1000)}
            )
        started = time.monotonic()
        asyncio.get_running_loop().create_task(tick())
        woke = await feed.wait_for_update(5.0)
        return woke, time.monotonic() - started
    woke, waited = asyncio.run(go())
    assert woke and waited < 1.0


# -- runner._spot_and_vol cache ------------------------------------------------

class _CountingWsFeed:
    def __init__(self, history):
        self.history = history
        self.history_calls = 0

    def connected(self):
        return True

    def index_id_for(self, underlying):
        return "BRTI" if underlying == "BTC" else None

    def latest_index_value(self, underlying):
        return self.history[-1] if self.history else None

    def index_history(self, underlying):
        self.history_calls += 1
        return list(self.history)


def _market(ticker="KXBTC15M-X", seconds_to_close=30.0, strike=90_000.0, interval_minutes=15.0):
    close_time = datetime.now(timezone.utc) + timedelta(seconds=seconds_to_close)
    return ActiveMarket(
        ticker=ticker, event_ticker="EVT", series_ticker="KXBTC15M", underlying="BTC", direction="above",
        strike=strike, open_time=close_time - timedelta(minutes=15), close_time=close_time,
        interval_minutes=interval_minutes,
    )


def _history(n=60, end=None):
    end = time.time() if end is None else end
    return [(end - (n - 1 - i), 100_000.0 + (7.0 if i % 2 else -5.0) * (i % 5)) for i in range(n)]


def test_spot_and_vol_computed_once_per_new_sample_across_markets():
    runner._spot_vol_cache.clear()
    ws = _CountingWsFeed(_history())
    a = runner._spot_and_vol(_market("A"), spot_feed=None, ws_feed=ws)
    b = runner._spot_and_vol(_market("B"), spot_feed=None, ws_feed=ws)
    assert a is b and ws.history_calls == 1
    assert a[3] is not None and a[1] == ws.history[-1][1]

    ws.history.append((ws.history[-1][0] + 1.0, 100_020.0))
    c = runner._spot_and_vol(_market("A"), spot_feed=None, ws_feed=ws)
    assert ws.history_calls == 2 and c[1] == 100_020.0 and len(c[2]) == len(ws.history)


# -- sampling.SampleWriter -----------------------------------------------------

def _sample(writer, ticker="T"):
    return writer.record_sample(
        ticker=ticker, series_ticker="S", category="crypto", underlying="BTC", direction="above",
        strike=1.0, close_time=datetime.now(timezone.utc), seconds_to_expiry=10.0, spot=2.0,
        favored_side="yes", model_prob=0.99, market_price=0.95, z=3.0,
    )


def test_sample_writer_persists_rows_and_outcomes_off_thread(tmp_path):
    db = str(tmp_path / "samples.db")
    sampling.init_db(db)
    sampling.record_sample(  # an existing row: new ids must continue after it
        db, ticker="OLD", series_ticker="S", category="crypto", underlying="BTC", direction="above",
        strike=1.0, close_time=datetime.now(timezone.utc), seconds_to_expiry=10.0, spot=2.0,
        favored_side="yes", model_prob=0.99, market_price=None, z=None,
    )
    writer = sampling.SampleWriter(db, flush_interval=0.01)
    first, second = _sample(writer, "A"), _sample(writer, "B")
    writer.record_trade_outcome(sample_id=first, traded=True, skip_reason=None)
    writer.record_trade_outcome(sample_id=second, traded=False, skip_reason="low_edge,dynamic_window")
    writer.close()

    rows = sqlite3.connect(db).execute("SELECT id, ticker, traded, skip_reason FROM samples ORDER BY id").fetchall()
    assert rows == [(1, "OLD", 0, None), (2, "A", 1, None), (3, "B", 0, "low_edge,dynamic_window")]


# -- zero-fill retry cooldown (evaluate_and_maybe_trade end to end) ------------

class _BookWsFeed(_CountingWsFeed):
    def get_orderbook_fp(self, ticker):
        # NO bids at 0.07 -> YES asks at 0.93, deep enough for any size here.
        return {"yes_dollars": [], "no_dollars": [["0.0700", "500"]]}

    def get_orderbook_age(self, ticker):
        return 0.0


class _ZeroFillOrderManager:
    dry_run = False

    def __init__(self):
        self.orders = 0

    def buy_favored_side(self, *args, **kwargs):
        self.orders += 1
        return {"fill_count": 0}


def test_zero_fill_waits_out_the_retry_cooldown(monkeypatch):
    runner._spot_vol_cache.clear()
    monkeypatch.setattr(config, "ZERO_FILL_RETRY_SECONDS", 0.2)
    ws = _BookWsFeed(_history())
    om = _ZeroFillOrderManager()
    market = _market(seconds_to_close=40.0)
    cycle_state = {"bankroll_dollars": 1000.0}
    stats = runner._new_stats()

    def evaluate():
        asyncio.run(runner.evaluate_and_maybe_trade(market, None, ws, om, cycle_state, stats, {}))

    evaluate()
    assert om.orders == 1, stats  # cleared every gate and fired; Kalshi filled nothing
    evaluate()
    assert om.orders == 1 and stats["skip_zero_fill_cooldown"] == 1
    time.sleep(0.25)
    evaluate()
    assert om.orders == 2


# -- runner._wait_for_next_pass ------------------------------------------------

def _timed_wait(monkeypatch, event_driven, update_after=None):
    monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(config, "EVENT_MIN_INTERVAL_SECONDS", 0.05)

    async def go():
        feed = KalshiWebsocketFeed()
        if update_after is not None:
            asyncio.get_running_loop().call_later(update_after, _delta, feed)
        started = time.time()
        await runner._wait_for_next_pass(feed, event_driven, started)
        return time.time() - started
    return asyncio.run(go())


def test_wait_for_next_pass_wakes_early_on_an_update_when_event_driven(monkeypatch):
    assert _timed_wait(monkeypatch, True, update_after=0.1) < 0.3


def test_wait_for_next_pass_respects_the_min_interval(monkeypatch):
    assert _timed_wait(monkeypatch, True, update_after=0.0) >= 0.05


def test_wait_for_next_pass_falls_back_to_the_poll_interval(monkeypatch):
    assert _timed_wait(monkeypatch, True) >= 0.45  # event-driven, but the feed stays quiet
    assert _timed_wait(monkeypatch, False, update_after=0.05) >= 0.45  # updates ignored when not event-driven
