"""Kalshi trading fee calculation.

Formula: fee = 0.07 * contracts * price * (1 - price). Kalshi's daily
high-temperature series (KXHIGHNY, KXHIGHCHI, KXHIGHT*, ...) report
`fee_type: "quadratic"`, `fee_multiplier: 1` (checked 2026-10-24), the same
shape as the crypto and golf series, so this is a copy of
../golf_field_alpha/fees.py (itself a copy of ../resolution_alpha/live/fees.py).

Rounding: real fills round UP to the nearest $0.0001 (confirmed against
three fills in resolution_alpha). `estimate_fee_dollars_cent` rounds up to
the whole cent instead -- stricter, at most 1c per order more -- and is what
the backtest charges by default, so the backtest never flatters the edge.
"""

import math

DEFAULT_FEE_RATE = 0.07


def estimate_fee_dollars(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    # round() first: 0.07*0.5*0.5*1e4 is 175.00000000000003 in floats, which
    # the verbatim copy ceil()s to 176 (a $0.0001 overcharge).
    return math.ceil(round(raw * 10000, 9)) / 10000.0


def fee_per_contract(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Total fee for `contracts` at `price`, divided back out to a
    per-contract number -- the quadratic formula isn't linear in size, so
    this is > estimate_fee_dollars(price, 1) once contracts > 1. Used by the
    selection rules, which reason per-leg-per-contract.
    """
    if contracts <= 0:
        return 0.0
    return estimate_fee_dollars(price, contracts, fee_rate) / contracts


def estimate_fee_dollars_cent(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """ceil(0.07 * n * p * (1-p) * 100) / 100: the conservative cent rounding."""
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100.0
