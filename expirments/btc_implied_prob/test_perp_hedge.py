"""Offline, no-network test of the perp delta hedge (hedger.py, added
2026-10-21 in place of the take-profit/trailing-stop exits): delta sign and
size against a finite difference of the model probability, rebalance band,
cap, unwinding near close and after settlement, and the live order shape.
Run directly:

    python test_perp_hedge.py
"""

import math
from datetime import datetime, timedelta, timezone

import config
import deribit_iv
import hedger
import strategy
from kalshi_btc_markets import ActiveMarket
from order_manager import OrderManager

PERP = {"ticker": "KXBTCPERP", "contract_size": "0.000100", "underlying_multiplier": "1.000000",
        "tick_size": "0.0001", "bid": "8.4598", "ask": "8.4602"}
hedger.fetch_margin_market = lambda ticker: dict(PERP)

FORWARD = 84600.0


def _surface(sigma=0.5):
    def smile(days):
        strikes = [70000.0, 84600.0, 100000.0]
        return deribit_iv.ExpirySmile(
            expiration_timestamp_ms=0, years_to_expiry=days / 365.0, forward=FORWARD, strikes=strikes,
            log_moneyness=[math.log(k / FORWARD) for k in strikes], ivs=[sigma] * 3,
        )
    return deribit_iv.Surface(spot=FORWARD, fetched_at=0.0, expiries=[smile(0.01), smile(7.0)])


def _position(ticker, side="yes", direction="above", strike=85000.0, seconds_left=3600.0, contracts=10.0):
    now = datetime.now(timezone.utc)
    market = ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction=direction, strike=strike,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=seconds_left),
        settlement_average_seconds=60.0, yes_bid=0.0, yes_ask=0.0,
    )
    return {"market": market, "side": side, "contracts": contracts, "entry_edge": 0.05,
            "entry_price": 0.5, "reconciled": False}


def _dry_manager():
    return OrderManager(dry_run=True)


surface = _surface()
now = datetime.now(timezone.utc)

# 1. Delta matches a finite difference of the model probability, and signs are right.
def prob(forward):
    s = _surface()
    for e in s.expiries:
        e.forward = forward
    return deribit_iv.estimate_probability(s, direction="above", strike=85000.0, seconds_to_expiry=3600.0).prob_yes

numeric = (prob(FORWARD + 1.0) - prob(FORWARD - 1.0)) / 2.0
analytic = deribit_iv.estimate_delta_yes(surface, direction="above", strike=85000.0, seconds_to_expiry=3600.0)
assert abs(analytic - numeric) / numeric < 1e-3, (analytic, numeric)
assert deribit_iv.estimate_delta_yes(surface, direction="below", strike=85000.0, seconds_to_expiry=3600.0) == -analytic
d_yes = hedger.portfolio_delta_btc({"A": _position("A", "yes")}, surface, now)
d_no = hedger.portfolio_delta_btc({"A": _position("A", "no")}, surface, now)
assert d_yes > 0 and abs(d_yes + d_no) < 1e-15
print("PASS  delta_matches_finite_difference_and_signs")

# 2. YES-above is long BTC, so the hedge shorts the perp by delta / contract size.
manager = _dry_manager()
h = hedger.PerpHedger(manager)
positions = {"A": _position("A", "yes")}
h.rebalance(positions, surface)
expected = round(-d_yes / 0.0001)
assert expected < 0 and manager.get_perp_position("KXBTCPERP") == expected, (expected, manager._sim_perp_positions)
print(f"PASS  yes_above_hedged_short ({expected} contracts)")

# 3. A move smaller than the rebalance band doesn't trade; a flat target always does.
held = manager.get_perp_position("KXBTCPERP")
positions["A"]["contracts"] = 10.0 * (1 + 0.5 / abs(expected))  # target moves by < 1 contract
h.rebalance(positions, surface)
assert manager.get_perp_position("KXBTCPERP") == held
positions["A"] = _position("A", "yes", seconds_left=config.HEDGE_STOP_SECONDS_BEFORE_CLOSE - 1)
h.rebalance(positions, surface)
assert manager.get_perp_position("KXBTCPERP") == 0, "position inside the stop window should unwind the hedge"
print("PASS  band_skips_small_moves_and_stop_window_unwinds")

# 4. Settled positions are pruned and the leftover hedge goes flat.
manager = _dry_manager()
h = hedger.PerpHedger(manager)
positions = {"A": _position("A", "no")}
h.rebalance(positions, surface)
assert manager.get_perp_position("KXBTCPERP") > 0
positions["A"]["market"].close_time = now - timedelta(seconds=1)
strategy._prune_settled(positions)
assert positions == {}
h.rebalance(positions, surface)
assert manager.get_perp_position("KXBTCPERP") == 0
print("PASS  settled_positions_pruned_and_hedge_flattened")

# 5. The cap bounds the hedge however large the delta.
manager = _dry_manager()
hedger.PerpHedger(manager).rebalance({"A": _position("A", "yes", strike=84600.0, contracts=1000.0)}, surface)
assert manager.get_perp_position("KXBTCPERP") == -int(config.HEDGE_MAX_CONTRACTS)
print("PASS  hedge_capped")


# 6. Live: reads the margin position, sends an IOC through the book on the right side.
class FakeClient:
    def __init__(self, held):
        self.held = held
        self.orders = []

    def get_margin_positions(self, ticker=None):
        return {"positions": [{"market_ticker": "KXBTCPERP", "position": f"{self.held:.2f}"},
                              {"market_ticker": "KXETHPERP", "position": "5.00"}]}

    def place_margin_order(self, **kw):
        self.orders.append(kw)
        return {"order_id": "x", "fill_count": kw["count"], "remaining_count": "0.00"}


live = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
live.dry_run = False
live._client = FakeClient(held=-3.0)
hedger.PerpHedger(live).rebalance({}, surface)  # nothing open: buy back the 3 short
order = live._client.orders[0]
assert order["side"] == "bid" and order["count"] == "3.00", order
assert order["price"] == f"{math.ceil(8.4602 * (1 + config.HEDGE_MAX_SLIPPAGE) / 0.0001) * 0.0001:.4f}", order
live._client = FakeClient(held=0.0)
hedger.PerpHedger(live).rebalance({"A": _position("A", "yes")}, surface)
order = live._client.orders[0]
assert order["side"] == "ask" and float(order["count"]) == -expected and float(order["price"]) < 8.4598, order
print("PASS  live_orders_ioc_through_the_book")

print("\nall scenarios passed")
