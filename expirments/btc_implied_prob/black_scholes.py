"""Black-76 digital ("cash-or-nothing") probability model.

Deribit BTC options are quoted against a per-expiry forward (each book
summary entry's `underlying_price`, the synthetic future implied by that
expiry's basis), not spot, and `mark_iv` is already an annualized Black-76
implied vol on that forward. Under the T-forward risk-neutral measure the
forward is driftless, so the probability that it finishes above a strike K
at expiry is exactly the textbook N(d2) term from Black-76 -- no separate
risk-free rate or discounting needed, since the forward already embeds
whatever basis/funding the market is pricing.
"""

import math


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_forward_above_strike(forward: float, strike: float, sigma: float, years_to_expiry: float) -> float:
    """P(F_T > K) under the T-forward measure.

    `sigma` is annualized vol as a decimal (0.55, not 55). Degenerates to a
    step function when there's no time or vol left for randomness -- the
    forward has either already cleared the strike or hasn't.
    """
    if years_to_expiry <= 0 or sigma <= 0:
        return 1.0 if forward > strike else 0.0
    variance = sigma * sigma * years_to_expiry
    d2 = (math.log(forward / strike) - 0.5 * variance) / math.sqrt(variance)
    return _normal_cdf(d2)


def prob_forward_below_strike(forward: float, strike: float, sigma: float, years_to_expiry: float) -> float:
    return 1.0 - prob_forward_above_strike(forward, strike, sigma, years_to_expiry)
