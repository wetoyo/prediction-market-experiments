"""ws_feed.KalshiWebsocketFeed orderbook_delta seq-gap detection.

Kalshi tags every message on the orderbook_delta subscription with a `seq`
that increments by exactly 1. A skip means one or more delta messages were
lost -- the book is then silently wrong until a resync. These pin the
gap-detection + resync-request logic (recovery itself is in the async
_subscription_sync_loop and is not exercised here).
"""

from ws_feed import KalshiWebsocketFeed


def _delta(seq, ticker="KXBTC15M-TEST", side="yes"):
    return {
        "type": "orderbook_delta",
        "seq": seq,
        "msg": {"market_ticker": ticker, "side": side, "price_dollars": "0.50", "delta_fp": "3"},
    }


def _snapshot(seq, ticker="KXBTC15M-TEST"):
    return {
        "type": "orderbook_snapshot",
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }


def test_first_message_baselines_without_resync():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(42))
    assert f._last_orderbook_seq == 42
    assert f._resync_requested is False


def test_in_order_stream_never_requests_resync():
    f = KalshiWebsocketFeed()
    for s in (5, 6, 7, 8, 9):
        f._handle_message(_delta(s))
    assert f._last_orderbook_seq == 9
    assert f._resync_requested is False


def test_gap_requests_resync_and_advances_seq():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(5))
    f._handle_message(_delta(8))  # 6 and 7 lost
    assert f._resync_requested is True
    assert f._last_orderbook_seq == 8  # not stuck at 5 -- next msg must not re-trigger


def test_snapshots_count_toward_the_same_seq_stream():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(5))
    f._handle_message(_snapshot(7))  # add_markets snapshot arriving with a gap
    assert f._resync_requested is True
    assert f._last_orderbook_seq == 7


def test_duplicate_or_reordered_seq_does_not_resync_or_rewind():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(5))
    f._handle_message(_delta(4))
    f._handle_message(_delta(5))
    assert f._resync_requested is False
    assert f._last_orderbook_seq == 5


def test_new_sid_rebaselines_seq():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(100))
    f._handle_message({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}})
    assert f._last_orderbook_seq is None
    f._handle_message(_delta(3))  # fresh sid starts low -- must not look like a gap
    assert f._resync_requested is False
    assert f._last_orderbook_seq == 3


def test_missing_seq_field_is_ignored():
    f = KalshiWebsocketFeed()
    f._handle_message(_delta(5))
    msg = _delta(6)
    del msg["seq"]
    f._handle_message(msg)
    assert f._resync_requested is False
    assert f._last_orderbook_seq == 5
