"""Order-book fill simulation for Kalshi's price-level orderbook.

Kalshi's `/markets/{ticker}/orderbook` response (fetch_orderbook in the
Kalshi client, via kalshi_gateway) returns `yes_dollars`/`no_dollars`:
resting bid levels for each side as [price_string, quantity_string] pairs.
There is no separate displayed ask book -- buying YES means crossing
against resting NO bids (a NO bid at price p is equivalent to a YES ask at
1-p), and vice versa. Confirmed against a live orderbook snapshot on
2026-08-04 (KXBTC15M market, levels sorted ascending by price).

This is the liquidity-aware fill model called out as critical in
README.md: it walks displayed depth for a target size rather than
assuming a fill at best price, so thin end-of-window books show up as
partial (or zero) fills instead of phantom liquidity.
"""

from dataclasses import dataclass, field


@dataclass
class FillEstimate:
    side: str
    requested_size: float
    filled_size: float
    avg_price: float | None  # None if nothing could be filled
    levels_used: list[tuple[float, float]] = field(default_factory=list)  # (price, size) consumed


def _opposite_side_bids(orderbook_fp: dict, side: str) -> list[tuple[float, float]]:
    key = "no_dollars" if side == "yes" else "yes_dollars"
    raw_levels = orderbook_fp.get(key) or []
    levels = [(float(price), float(size)) for price, size in raw_levels]
    return sorted(levels, key=lambda level: level[0], reverse=True)  # highest bid (best) first


def walk_book(orderbook_fp: dict, side: str, target_size: float) -> FillEstimate:
    """Simulates buying `target_size` contracts of `side` ("yes" or "no") by
    walking the opposite side's resting bids from best to worst price. Does
    not assume any fill beyond displayed depth.
    """
    remaining = target_size
    total_cost = 0.0
    filled = 0.0
    levels_used: list[tuple[float, float]] = []

    for bid_price, bid_size in _opposite_side_bids(orderbook_fp, side):
        if remaining <= 0:
            break
        if bid_size <= 0:
            continue
        ask_price = round(1.0 - bid_price, 4)
        take = min(remaining, bid_size)
        total_cost += take * ask_price
        filled += take
        remaining -= take
        levels_used.append((ask_price, take))

    avg_price = (total_cost / filled) if filled > 0 else None
    return FillEstimate(
        side=side,
        requested_size=target_size,
        filled_size=filled,
        avg_price=avg_price,
        levels_used=levels_used,
    )
