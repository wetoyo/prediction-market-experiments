"""Which side of a temperature market the observations have already decided,
and what to pay for it.

For a high market the observations give H: the final high is >= H. For a low
market they give L: the final low is <= L. LOCK_MARGIN_F is taken off before
anything counts as locked (H - margin, L + margin).

High, final value >= H:
- greater f  (YES if > f):        YES locked once H >= f + 1
- less c     (YES if < c):        NO locked once H >= c
- between f..c:                   NO locked once H > c

Low, final value <= L:
- greater f  (YES if > f):        NO locked once L <= f
- less c     (YES if < c):        YES locked once L < c
- between f..c:                   NO locked once L < f

Nothing here is ever locked the other way: a high can still rise and a low
can still fall until the day ends, and hourly reports can miss the true
peak, so a completed day still isn't treated as a ceiling on its high.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import fees
from discovery import TempMarket
from nws_obs import DayBounds


def locked_side(market: TempMarket, bounds: DayBounds, margin_f: float) -> str | None:
    """"yes", "no" or None (not decided yet)."""
    f, c = market.floor, market.cap
    if market.kind == "high":
        if bounds.high_at_least is None:
            return None
        h = math.floor(bounds.high_at_least - margin_f)
        if market.strike_type == "greater" and f is not None and h >= f + 1:
            return "yes"
        if market.strike_type == "less" and c is not None and h >= c:
            return "no"
        if market.strike_type == "between" and c is not None and h > c:
            return "no"
        return None
    if market.kind == "low":
        if bounds.low_at_most is None:
            return None
        lo = math.ceil(bounds.low_at_most + margin_f)
        if market.strike_type == "greater" and f is not None and lo <= f:
            return "no"
        if market.strike_type == "less" and c is not None and lo < c:
            return "yes"
        if market.strike_type == "between" and f is not None and lo < f:
            return "no"
    return None


@dataclass
class Entry:
    side: str
    limit_price: float     # worst price paid, tick-aligned, in `side`'s own terms
    contracts: float
    best_ask: float
    edge_at_limit: float   # 1 - limit - fee per contract
    worst_cost: float      # contracts * limit + fee


def _edge(price: float, contracts: float) -> float:
    return 1.0 - price - fees.fee_per_contract(price, contracts)


def plan_entry(market: TempMarket, side: str, min_edge: float, max_contracts: float,
               max_cost: float) -> tuple[Entry | None, str]:
    """The IOC buy of a locked side: the highest tick price that still clears
    `min_edge` after fees, and as many contracts as the caps allow. Kalshi
    fills from the best ask up to that limit, so the order sweeps every level
    worth having without walking the book here. Returns (None, reason) when
    there's nothing to buy."""
    ask, size = market.ask(side)
    if ask is None or not 0.0 < ask < 1.0:
        return None, "no ask"
    if max_contracts < 1 or max_cost <= 0:
        return None, "caps exhausted"
    tick = market.tick if market.tick > 0 else 0.01
    n = math.floor(min(max_contracts, max_cost / ask))
    while n >= 1:
        if _edge(ask, n) >= min_edge:
            break
        n -= 1
    if n < 1:
        return None, f"ask {ask:.2f} leaves under {min_edge:.2f} edge after fees"
    # Raise the limit tick by tick while the edge and the cost cap still hold.
    limit = ask
    while True:
        nxt = round(limit + tick, 4)
        if nxt >= 1.0 or _edge(nxt, n) < min_edge or n * nxt + fees.estimate_fee_dollars(nxt, n) > max_cost:
            break
        limit = nxt
    worst = n * limit + fees.estimate_fee_dollars(limit, n)
    return Entry(side, round(limit, 4), float(n), ask, _edge(limit, n), worst), ""
