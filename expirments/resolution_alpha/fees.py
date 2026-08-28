"""Kalshi trading fee calculation.

Formula: fee = 0.07 * contracts * price * (1 - price), rounded UP to the
nearest $0.0001 (not the nearest cent -- see below). The KXBTC15M series
reported `fee_type: "quadratic"`, `fee_multiplier: 1` when checked live on
2026-08-04, consistent with this shape.

Rounding confirmed exactly against three real fills on 2026-08-06 (taker_fees_dollars
from live get_orders responses):
  contracts=1 price=0.0100 -> raw=0.000693 -> actual fee $0.0007
  contracts=2 price=0.9250 -> raw=0.009712 -> actual fee $0.0098
  contracts=2 price=0.0900 -> raw=0.011466 -> actual fee $0.0115
All three match ceil(raw * 10000) / 10000 exactly. An earlier version of
this function rounded up to the nearest whole *cent* instead
(ceil(raw*100)/100), which put an effective $0.01 floor under every trade's
estimated fee regardless of the real (often much smaller) amount -- a large
overestimate for exactly the small-contract, extreme-probability trades this
strategy targets, silently failing the edge_threshold check in runner.py for
trades that were actually profitable.
"""

import math

DEFAULT_FEE_RATE = 0.07


def estimate_fee_dollars(price: float, contracts: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    if contracts <= 0:
        return 0.0
    raw = fee_rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 10000) / 10000.0
