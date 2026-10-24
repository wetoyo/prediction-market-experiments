"""Lognormal ("Black-76") digital probabilities for index range and
above/below markets. Same model as ../../btc_implied_prob/black_scholes.py,
plus the bucket (range) case.

Over a few intraday hours the forward is the spot to well under a basis
point (rates x hours), so spot is used directly as the forward. `sigma` is
annualized; `years` is in *trading* years (see model.py for the clock).
"""

import math


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_above(forward: float, strike: float, sigma: float, years: float) -> float:
    """P(F_T > K) under the T-forward measure: N(d2)."""
    if years <= 0 or sigma <= 0:
        return 1.0 if forward > strike else 0.0
    sd = sigma * math.sqrt(years)
    d2 = (math.log(forward / strike) - 0.5 * sd * sd) / sd
    return _normal_cdf(d2)


def prob_range(forward: float, lo: float | None, hi: float | None, sigma: float, years: float) -> float:
    """P(lo <= F_T <= hi); None means unbounded on that side."""
    p_lo = 1.0 if lo is None else prob_above(forward, lo, sigma, years)
    p_hi = 0.0 if hi is None else prob_above(forward, hi, sigma, years)
    return min(1.0, max(0.0, p_lo - p_hi))
