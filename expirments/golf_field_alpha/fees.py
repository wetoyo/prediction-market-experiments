"""Kalshi trading fee calculation.

Formula: fee = 0.07 * contracts * price * (1 - price), rounded UP to the
nearest $0.0001. Kalshi's golf outright-winner series (KXPGATOUR,
KXCHAMPTOUR, KXKFTOUR, KXLPGATOUR, KXDPWORLDTOUR, KXLIVTOUR, KXGOLFTOURN,
...) report `fee_type: "quadratic"`, `fee_multiplier: 1` -- the same shape
as the crypto interval series, so this is a verbatim copy of
../resolution_alpha/live/fees.py (rounding there confirmed exactly against
three real fills on 2026-08-06).

Why fees dominate this strategy: a golf "field" basket buys YES on many
players at once, one contract each, to win exactly $1 back (one winner).
The gross edge per basket is at most a few cents, so a per-leg fee of even
0.1-0.5c across 20-40 legs is the difference between edge and no edge.
Every selection rule in selection.py checks the edge AFTER this fee, per
leg, at the size actually being bought (the quadratic term means the
per-contract fee rises with size).
"""

import math

DEFAULT_FEE_RATE = 0.07


def estimate_fee_dollars(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 10000) / 10000.0


def fee_per_contract(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Total fee for `contracts` at `price`, divided back out to a
    per-contract number -- the quadratic formula isn't linear in size, so
    this is > estimate_fee_dollars(price, 1) once contracts > 1. Used by the
    selection rules, which reason per-leg-per-contract.
    """
    if contracts <= 0:
        return 0.0
    return estimate_fee_dollars(price, contracts, fee_rate) / contracts
