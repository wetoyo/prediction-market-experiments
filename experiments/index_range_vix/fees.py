"""Kalshi taker fee, copied from ../resolution_alpha/fees.py (formula and
rounding validated against live fills there). The index series report
`fee_type: "quadratic"`, `fee_multiplier: 1` (checked on /series/KXINX,
KXINXU, KXNASDAQ100, KXNASDAQ100U on 2026-10-24), i.e. the standard
0.07 * n * p * (1 - p).

Kalshi's published schedule rounds the per-order fee up to the cent;
resolution_alpha's live fills showed rounding to $0.0001. `estimate_fee_dollars`
keeps the validated $0.0001 rounding; `fee_per_contract_cent_rounded` is the
conservative per-order cent rounding the backtest uses (at a given order size).
"""

import math

DEFAULT_FEE_RATE = 0.07


def estimate_fee_dollars(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    # round() first: 0.07 * 0.25 is 0.017500000000000002 in floats, which a bare
    # ceil would bump to 0.0176 (resolution_alpha's copy has this off-by-a-hair).
    return math.ceil(round(raw * 10000, 6)) / 10000.0


def fee_per_contract_cent_rounded(price: float, contracts_per_order: int = 10,
                                  fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """ceil(0.07 * n * p * (1-p) * 100) / 100 for an n-contract order, per contract."""
    n = max(1, int(contracts_per_order))
    return math.ceil(round(fee_rate * n * price * (1.0 - price) * 100, 9)) / 100.0 / n
