"""Unit tests for order_manager -- the favored-side ("yes"/"no") -> Kalshi API
("bid"/"ask", YES-dollar price) translation and the dry-run guard. No network,
no credentials: the live end-to-end path is exercised separately by
live_buy_smoke.py.
"""

import pytest

from order_manager import OrderManager, _to_api_order


class TestToApiOrder:
    def test_yes_passes_price_through_as_bid(self):
        assert _to_api_order("yes", 0.42) == ("bid", 0.42)

    def test_no_becomes_ask_at_one_minus_price(self):
        # Buying NO at 0.01 == selling YES at 0.99.
        assert _to_api_order("no", 0.01) == ("ask", 0.99)

    def test_deci_cent_prices_survive(self):
        # The crypto interval series migrated to `tapered_deci_cent` (0.001
        # tick in [0, 0.10] and [0.90, 1.00]). `limit_price` is a real book
        # level, already tick-aligned; rounding must NOT snap it to the cent.
        assert _to_api_order("yes", 0.973) == ("bid", 0.973)
        assert _to_api_order("no", 0.973) == ("ask", 0.027)  # 1 - 0.973, still deci-cent aligned
        assert _to_api_order("yes", 0.996) == ("bid", 0.996)  # was round()->1.0000 -> invalid_price
        assert _to_api_order("no", 0.996) == ("ask", 0.004)

    def test_float_noise_is_still_cleaned(self):
        # 4-decimal rounding is kept purely as a float-noise net.
        side, price = _to_api_order("no", 0.973)
        assert price == round(1.0 - 0.973, 4)

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError):
            _to_api_order("buy", 0.5)

    def test_price_outside_open_interval_raises(self):
        with pytest.raises(ValueError):
            _to_api_order("yes", 1.0)   # not a tradeable level
        with pytest.raises(ValueError):
            _to_api_order("no", 1.0)    # -> ask 0.0
        with pytest.raises(ValueError):
            _to_api_order("yes", 0.0)


class TestBuyFavoredSideDryRun:
    def test_dry_run_does_not_build_a_client(self):
        om = OrderManager(dry_run=True)
        assert om._client is None

    def test_dry_run_returns_synthetic_response_without_placing(self):
        om = OrderManager(dry_run=True)
        resp = om.buy_favored_side("KXBTCD-26AUG2817-T79999.99", "yes", 1, 0.01)
        assert resp["dry_run"] is True
        assert resp["ticker"] == "KXBTCD-26AUG2817-T79999.99"
        assert resp["side"] == "yes"
        assert resp["count"] == "1.00"
        assert resp["price"] == "0.0100"

    def test_dry_run_no_side_prices_in_yes_terms(self):
        om = OrderManager(dry_run=True)
        resp = om.buy_favored_side("KXBTCD-26AUG2817-T79999.99", "no", 3, 0.01)
        assert resp["side"] == "no"
        assert resp["count"] == "3.00"
        assert resp["price"] == "0.9900"  # sell YES @ 0.99 == buy NO @ 0.01

    def test_dry_run_accepts_shard_and_tif_kwargs(self):
        om = OrderManager(dry_run=True)
        # Must not raise -- these are threaded through to place_order live.
        resp = om.buy_favored_side(
            "KXBTC15M-26AUG281700-00", "yes", 5, 0.93,
            exchange_index=2, time_in_force="immediate_or_cancel", reduce_only=True,
        )
        assert resp["price"] == "0.9300"


class TestPlaceOrderWiring:
    """buy_favored_side -> KalshiTradingClient.place_order argument wiring,
    with a fake client (no network).
    """

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def place_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"order_id": "x", "fill_count": kwargs["count"], "remaining_count": "0.00"}

    def _om(self):
        om = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
        om.dry_run = False
        om._client = self._FakeClient()
        return om

    def test_defaults_to_ioc_and_forwards_shard(self):
        om = self._om()
        om.buy_favored_side("KXBTC15M-26AUG281700-00", "yes", 5, 0.93, exchange_index=2)
        (call,) = om._client.calls
        assert call["side"] == "bid"
        assert call["price"] == "0.9300"
        assert call["count"] == "5.00"
        assert call["time_in_force"] == "immediate_or_cancel"
        assert call["exchange_index"] == 2

    def test_no_side_translation_and_reduce_only(self):
        om = self._om()
        om.buy_favored_side("KXBTC15M-26AUG281700-00", "no", 2, 0.962, reduce_only=True)
        (call,) = om._client.calls
        assert call["side"] == "ask"
        assert call["price"] == "0.0380"  # 1 - 0.962
        assert call["reduce_only"] is True
