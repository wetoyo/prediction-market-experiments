"""Sizing tests for runner._size_for_max_available (MAX_SIZE_MODE) and
_size_for_edge (Kelly/edge mode).

Pins:
- the fee reserve -- Kalshi checks `balance >= contract_cost + fee` at order
  time and 400s the whole order if short (live 2026-08-28: 221 YES @ 0.902 =
  $199.34 + ~$1.37 fee vs $200.17 balance) -- for BOTH sizers.
- _size_for_edge's separate `kelly_probability` input (config.KELLY_USE_MARKET_PROB).
"""

from fees import estimate_fee_dollars
from runner import _size_for_edge, _size_for_max_available


def _ob_yes_ask(ask_price: float, depth: float) -> dict:
    """orderbook_fp whose YES ask is `ask_price` with `depth` size. walk_book
    buys YES by crossing NO bids, and a NO bid at p is a YES ask at 1 - p.
    """
    return {"yes_dollars": [], "no_dollars": [[f"{1.0 - ask_price:.4f}", f"{depth:.2f}"]]}


def _all_in(price, size):
    return size * price + estimate_fee_dollars(price, size)


def test_reserves_for_fee_at_high_price():
    bankroll = 200.17
    price = 0.902
    size = _size_for_max_available(_ob_yes_ask(price, 100_000), "yes", bankroll, float("inf"))

    assert size < int(bankroll // price)          # 221 -- the old, fee-blind behaviour
    assert _all_in(price, size) <= bankroll       # the constraint Kalshi enforces
    assert _all_in(price, size + max(1, int(size * 0.01))) > bankroll  # ~maximal


def test_reserves_for_fee_at_low_price():
    bankroll = 100.0
    price = 0.02
    size = _size_for_max_available(_ob_yes_ask(price, 1_000_000), "yes", bankroll, float("inf"))
    assert _all_in(price, size) <= bankroll
    assert _all_in(price, size + max(1, int(size * 0.01))) > bankroll  # ~maximal


def test_still_book_depth_bound_when_depth_below_cash():
    # 50 contracts of depth, cash could afford thousands -> take the 50.
    size = _size_for_max_available(_ob_yes_ask(0.95, 50), "yes", 10_000.0, float("inf"))
    assert size == 50


def test_zero_when_no_book():
    assert _size_for_max_available({"yes_dollars": [], "no_dollars": []}, "yes", 100.0, float("inf")) == 0.0


# --- _size_for_edge -------------------------------------------------------

_DEEP = 1_000_000  # book depth that never binds


def test_size_for_edge_kelly_probability_controls_the_cap():
    ob = _ob_yes_ask(0.90, _DEEP)
    # edge_probability clears MIN_EDGE_DOLLARS in both; only kelly_probability differs.
    small = _size_for_edge(ob, "yes", 0.98, 0.905, 1_000.0, 0.5, _DEEP)  # market-prob-ish: q barely > p
    big = _size_for_edge(ob, "yes", 0.98, 0.98, 1_000.0, 0.5, _DEEP)     # model-prob: q well above p
    assert 0 < small < big
    assert small < 60          # ~28 from Kelly at q=0.905
    assert big > 300           # ~444 from Kelly at q=0.98


def test_size_for_edge_reserves_for_fee_and_cash():
    # q=1, full Kelly, high price -> Kelly alone wants ~bankroll/price
    # contracts; the fee reserve must pull the result under the balance.
    price, bankroll = 0.97, 100.0
    size = _size_for_edge(_ob_yes_ask(price, _DEEP), "yes", 1.0, 1.0, bankroll, 1.0, _DEEP)
    assert size > 0
    assert size * price + estimate_fee_dollars(price, size) <= bankroll
    assert size < int(bankroll // price)  # strictly under the fee-blind cash max


def test_size_for_edge_gate_uses_edge_probability_not_kelly():
    # edge at 0.90 is only 0.01 < MIN_EDGE_DOLLARS (0.02) -> nothing, however
    # confident kelly_probability is.
    assert _size_for_edge(_ob_yes_ask(0.90, _DEEP), "yes", 0.91, 0.999, 1_000.0, 0.5, _DEEP) == 0.0


def test_size_for_edge_book_depth_bound():
    size = _size_for_edge(_ob_yes_ask(0.90, 40), "yes", 0.98, 0.98, 1_000.0, 0.5, _DEEP)
    assert size == 40
