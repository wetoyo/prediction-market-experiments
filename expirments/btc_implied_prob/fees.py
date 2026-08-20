"""Kalshi trading fee calculation.

Formula: fee = 0.07 * contracts * price * (1 - price), rounded UP to the
nearest $0.0001. Matches ../resolution_alpha/live/fees.py, which validated
this exact formula and rounding against three real fills on 2026-08-06.
"""

import math

DEFAULT_FEE_RATE = 0.07


def estimate_fee_dollars(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 10000) / 10000.0
