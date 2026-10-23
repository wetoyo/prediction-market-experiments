"""Offline, no-network test of the perp delta hedge (hedger.py, added
2026-10-21 in place of the take-profit/trailing-stop exits): delta sign and
size against a finite difference of the model probability, rebalance band,
cap, unwinding near close and after settlement, the live order shape, the
hedge cost and margin in entry pricing and sizing, and margin funding out of
the runner's ledger. Run directly:

    python test_perp_hedge.py
"""

import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "tests"))

import config
import deribit_iv
import hedger
import strategy
from fake_kalshi import FakeKalshi
from kalshi_btc_markets import ActiveMarket
from order_manager import OrderManager
from tagged_ledger import TaggedLedger

FORWARD = 84600.0
PERP = hedger.PerpQuote(
    ticker="KXBTCPERP", contract_btc=0.0001, bid=8.4598, ask=8.4602, tick=0.0001,
    long_leverage=6.0, short_leverage=6.0, taker_fee_rate=0.001, funding_rate=0.0001,
)


def _surface(sigma=0.5):
    def smile(days):
        strikes = [70000.0, 84600.0, 100000.0]
        return deribit_iv.ExpirySmile(
            expiration_timestamp_ms=0, years_to_expiry=days / 365.0, forward=FORWARD, strikes=strikes,
            log_moneyness=[math.log(k / FORWARD) for k in strikes], ivs=[sigma] * 3,
        )
    return deribit_iv.Surface(spot=FORWARD, fetched_at=0.0, expiries=[smile(0.01), smile(7.0)])


def _market(ticker, direction="above", strike=85000.0, seconds_left=3600.0, yes_bid=0.0, yes_ask=0.0):
    now = datetime.now(timezone.utc)
    return ActiveMarket(
        ticker=ticker, event_ticker="EV", series_ticker="KXBTCD", direction=direction, strike=strike,
        open_time=now - timedelta(minutes=10), close_time=now + timedelta(seconds=seconds_left),
        settlement_average_seconds=60.0, yes_bid=yes_bid, yes_ask=yes_ask,
    )


def _position(ticker, side="yes", direction="above", strike=85000.0, seconds_left=3600.0, contracts=10.0):
    return {"market": _market(ticker, direction, strike, seconds_left), "side": side, "contracts": contracts,
            "entry_edge": 0.05, "entry_price": 0.5, "reconciled": False}


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
manager = OrderManager(dry_run=True)
h = hedger.PerpHedger(manager)
positions = {"A": _position("A", "yes")}
h.rebalance(positions, surface, PERP)
expected = round(-d_yes / 0.0001)
assert expected < 0 and manager.get_perp_position("KXBTCPERP") == expected, (expected, manager._sim_perp_positions)
print(f"PASS  yes_above_hedged_short ({expected} contracts)")

# 3. A move smaller than the rebalance band doesn't trade; the stop window unwinds.
held = manager.get_perp_position("KXBTCPERP")
positions["A"]["contracts"] = 10.0 * (1 + 0.5 / abs(expected))  # target moves by < 1 contract
h.rebalance(positions, surface, PERP)
assert manager.get_perp_position("KXBTCPERP") == held
positions["A"] = _position("A", "yes", seconds_left=config.HEDGE_STOP_SECONDS_BEFORE_CLOSE - 1)
h.rebalance(positions, surface, PERP)
assert manager.get_perp_position("KXBTCPERP") == 0, "position inside the stop window should unwind the hedge"
print("PASS  band_skips_small_moves_and_stop_window_unwinds")

# 4. Settled positions are pruned and the leftover hedge goes flat.
manager = OrderManager(dry_run=True)
h = hedger.PerpHedger(manager)
positions = {"A": _position("A", "no")}
h.rebalance(positions, surface, PERP)
assert manager.get_perp_position("KXBTCPERP") > 0
positions["A"]["market"].close_time = now - timedelta(seconds=1)
strategy._prune_settled(positions)
assert positions == {}
h.rebalance(positions, surface, PERP)
assert manager.get_perp_position("KXBTCPERP") == 0
print("PASS  settled_positions_pruned_and_hedge_flattened")

# 5. The cap bounds the hedge however large the delta.
manager = OrderManager(dry_run=True)
hedger.PerpHedger(manager).rebalance({"A": _position("A", "yes", strike=84600.0, contracts=1000.0)}, surface, PERP)
assert manager.get_perp_position("KXBTCPERP") == -int(config.HEDGE_MAX_CONTRACTS)
print("PASS  hedge_capped")

# 6. Entry pricing: the hedge cost comes off the edge, and sizing leaves room for its margin.
market = _market("E", strike=85000.0, yes_bid=0.30, yes_ask=0.32)
plain = strategy._evaluate(market, surface, now)
hedged = strategy._evaluate(market, surface, now, PERP)
abs_delta = abs(deribit_iv.estimate_delta_yes(surface, direction="above", strike=85000.0,
                                              seconds_to_expiry=plain.seconds_to_close))
notional = abs_delta * PERP.btc_price
hedged_secs = plain.seconds_to_close - config.HEDGE_STOP_SECONDS_BEFORE_CLOSE
want_cost = (notional * (0.001 + (PERP.ask - PERP.bid) / 2 / PERP.mid) * config.HEDGE_TURNOVER
             + notional * 0.0001 * hedged_secs / hedger.FUNDING_PERIOD_SECONDS)
assert abs(hedged.hedge_cost - want_cost) < 1e-12 and hedged.hedge_cost > 0
assert abs(plain.edge_after_fee - hedged.edge_after_fee - want_cost) < 1e-12
assert abs(hedged.hedge_margin - notional / 6.0 * config.HEDGE_MARGIN_TARGET_RATIO) < 1e-12
late = strategy._evaluate(_market("L", seconds_left=100.0, yes_bid=0.30, yes_ask=0.32), surface, now, PERP)
assert late.hedge_cost == 0.0 and late.hedge_margin == 0.0  # inside the stop window: never hedged


class SizingManager:
    dry_run = True

    def __init__(self):
        self.buys = []

    def buy_favored_side(self, ticker, side, contracts, limit_price):
        self.buys.append(contracts)


sig = strategy.TradeSignal(
    market=market, seconds_to_close=3600.0, model_prob_yes=0.95, yes_mid=0.31, favored_side="yes",
    favored_probability=0.95, trade_price=0.32, edge_after_fee=0.5, extrapolated=False,
    hedge_cost=0.01, hedge_margin=0.5,
)
sizing = SizingManager()
saved = (config.DRY_RUN_SIMULATED_BALANCE_DOLLARS, config.KELLY_FRACTION, config.MAX_CONTRACTS_PER_TRADE)
config.DRY_RUN_SIMULATED_BALANCE_DOLLARS, config.KELLY_FRACTION, config.MAX_CONTRACTS_PER_TRADE = 5.0, 1.0, 100.0
second = strategy.TradeSignal(**{**sig.__dict__, "market": _market("E2", yes_bid=0.30, yes_ask=0.32)})
strategy._execute([sig, second], {}, sizing)
config.DRY_RUN_SIMULATED_BALANCE_DOLLARS, config.KELLY_FRACTION, config.MAX_CONTRACTS_PER_TRADE = saved
per_contract = 0.32 + 0.0153 + 0.5  # price + Kalshi fee + hedge margin
assert sizing.buys[0] == math.floor(5.0 / per_contract), sizing.buys
assert sum(sizing.buys) * per_contract <= 5.0 + 1e-9, sizing.buys  # the second entry only gets what's left
print("PASS  hedge_cost_in_edge_and_margin_in_sizing")


# 7. Live: reads the margin position, sends an IOC through the book on the right side.
class FakeClient(FakeKalshi):
    """FakeKalshi (event side, ledger-compatible) plus a perps margin instance."""

    def __init__(self, balance=100.0, held=0.0, equity=0.0):
        super().__init__(balance)
        self.held = held
        self.equity = equity
        self.margin_orders = []
        self.transfers = []
        self.land_transfers = True

    def get_margin_positions(self, ticker=None):
        return {"positions": [{"market_ticker": "KXBTCPERP", "position": f"{self.held:.2f}"},
                              {"market_ticker": "KXETHPERP", "position": "5.00"}]}

    def place_margin_order(self, **kw):
        self.margin_orders.append(kw)
        return {"order_id": "x", "fill_count": kw["count"], "remaining_count": "0.00"}

    def get_margin_balance(self):
        return {"subaccount_balances": [{"subaccount": 0, "account_equity": f"{self.equity:.4f}"}],
                "settled_funds": "0"}

    def transfer_between_instances(self, source, destination, amount_dollars):
        sign = 1 if destination == "margined" else -1
        self.transfers.append(sign * amount_dollars)
        self.cash -= sign * amount_dollars
        if self.land_transfers:
            self.equity += sign * amount_dollars
        return {"transfer_id": f"tr{len(self.transfers)}"}


_tmp = Path(tempfile.mkdtemp())


def _live(client, allocation=None):
    m = OrderManager.__new__(OrderManager)  # skip __init__ (would need creds)
    m.dry_run = False
    m._client = client
    m.ledger = None
    if allocation is not None:
        m.ledger = TaggedLedger(client, order_tag="bip", status_path=str(_tmp / f"s{id(client)}.json"),
                                divergence_path=str(_tmp / f"d{id(client)}.jsonl"),
                                size_from_sim=True, allocation_dollars=allocation)
        m.ledger.sync()
    return m


live = _live(FakeClient(held=-3.0, equity=50.0))
hedger.PerpHedger(live).rebalance({}, surface, PERP)  # nothing open: buy back the 3 short
order = live._client.margin_orders[0]
assert order["side"] == "bid" and order["count"] == "3.00", order
assert order["price"] == f"{math.ceil(8.4602 * (1 + config.HEDGE_MAX_SLIPPAGE) / 0.0001) * 0.0001:.4f}", order
live = _live(FakeClient(equity=50.0))
hedger.PerpHedger(live).rebalance({"A": _position("A", "yes")}, surface, PERP)
order = live._client.margin_orders[0]
assert order["side"] == "ask" and float(order["count"]) == -expected and float(order["price"]) < 8.4598, order
print("PASS  live_orders_ioc_through_the_book")

# 8. Funding: tops up out of the ledger, books it, then sweeps back when flat.
client = FakeClient(balance=100.0, equity=0.0)
live = _live(client, allocation=20.0)
h = hedger.PerpHedger(live)
positions = {"A": _position("A", "yes", contracts=1.0)}
small = round(-hedger.portfolio_delta_btc(positions, surface, now) / 0.0001)
need = abs(small) * PERP.mid / 6.0
assert need * config.HEDGE_MARGIN_TARGET_RATIO < 20.0  # fits the allocation
h.rebalance(positions, surface, PERP)
assert len(client.transfers) == 1 and abs(client.transfers[0] - need * config.HEDGE_MARGIN_TARGET_RATIO) < 1e-3
sim = live.ledger.sim
assert abs(sim.margin - client.transfers[0]) < 1e-9 and abs(sim.available_cash() - (20.0 - client.transfers[0])) < 1e-9
assert float(client.margin_orders[0]["count"]) == -small  # the transfer counts as incoming margin
h.rebalance(positions, surface, PERP)  # transfer shows on the margin side: landed
assert not h._pending and not sim.transfers_in_flight and len(client.transfers) == 1
client.equity -= 0.40  # the hedge lost 40 cents
client.held = small
h.rebalance(positions, surface, PERP)
assert abs(sim.trading_pnl - (-0.40)) < 1e-9, sim.trading_pnl
client.held = 0.0
h.rebalance({}, surface, PERP)  # flat and nothing open: sweep it all back
assert client.transfers[-1] < 0 and abs(client.equity) < 1e-3, (client.transfers, client.equity)
h.rebalance({}, surface, PERP)
assert abs(sim.available_cash() - (20.0 - 0.40)) < 1e-3 and abs(sim.margin) < 1e-3
print("PASS  margin_topped_up_from_ledger_and_swept_back")

# 9. Funding is capped at the ledger's cash, and the hedge clipped to what that margin carries.
client = FakeClient(balance=100.0, equity=0.0)
client.land_transfers = False
live = _live(client, allocation=10.0)
h = hedger.PerpHedger(live)
h.rebalance({"A": _position("A", "yes", contracts=40.0)}, surface, PERP)
assert abs(client.transfers[0] - 10.0) < 1e-9, client.transfers  # all the ledger had
supported = int(10.0 * 6.0 / (PERP.mid * config.HEDGE_MARGIN_TOPUP_RATIO))
assert float(client.margin_orders[0]["count"]) == supported, (client.margin_orders, supported)
h.rebalance({"A": _position("A", "yes", contracts=40.0)}, surface, PERP)
assert len(client.transfers) == 1, "no second transfer while one is in flight"
h._pending["tr1"]["ts"] -= config.HEDGE_TRANSFER_TIMEOUT_SECONDS
h.rebalance({"A": _position("A", "yes", contracts=40.0)}, surface, PERP)
assert not live.ledger.sim.transfers_in_flight, "timed-out transfer should stop being waited on"
print("PASS  funding_capped_at_ledger_and_hedge_clipped")

print("\nall scenarios passed")
