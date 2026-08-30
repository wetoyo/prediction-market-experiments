"""Resolution-probability model for a single interval market.

Kalshi settles these markets on a trailing SETTLEMENT_AVERAGE_SECONDS-second
average of CF Benchmarks' Real Time Index ending at close_time, compared
against a strike captured the same way at open_time (confirmed via a live
market's `rules_primary` text on 2026-08-04 -- see README.md and
live/README.md). That means the "decisive moment" isn't an instantaneous last
tick: once inside the last SETTLEMENT_AVERAGE_SECONDS seconds, part of the
settlement average is already locked in and only the remaining seconds are
still stochastic, which shrinks effective variance faster than a naive
"time to close" model would suggest.

Two regimes:
  - far from close (> SETTLEMENT_AVERAGE_SECONDS left): standard
    distance-to-strike z-score, price stdev scaled by sqrt(remaining time).
  - inside the settlement window: blends the realized portion of the
    averaging window (from spot_history) with a shrinking-variance estimate
    for the unrealized remainder. The remainder's contribution uses the
    variance of an arithmetic average of Brownian motion over an interval of
    length tau, sigma^2 * tau / 3, weighted by how much of the 60s window it
    represents -- an approximation, not a rigorously derived Asian-option
    price.

The realized-vol estimate is a simple stdev of log returns. Both this and
the settlement-window blend are flagged in README.md's backtest plan as
things to validate/refine against real resolved-market data before sizing
up -- this module is a reasonable starting point, not a validated model.

Calibration pass (2026-08-29) against live/logs/samples.db (2.36M evaluated
ticks / 249k resolved): the raw model is overconfident on the traded band and
its Gaussian tail is far too thin (realized outcomes flatten to ~1-3% wrong
from z~2.5 out to z~6, where the Gaussian says ~1e-3 -> ~0). Per-sqrt-second
vol does NOT ramp into the close, but conditional vol is fat-tailed (~10% of
positions see 3x+ the trailing estimate after entry). Two blunt corrections
applied below: config.SIGMA_SAFETY_FACTOR widens sigma_used, config.MODEL_PROB_CAP
clamps the reported probability. See those config docstrings for the numbers.
"""

import math
from dataclasses import dataclass

from config import MODEL_PROB_CAP, SETTLEMENT_AVERAGE_SECONDS, SIGMA_SAFETY_FACTOR


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class ProbabilityEstimate:
    prob_yes: float
    favored_side: str  # "yes" or "no"
    favored_probability: float
    settlement_estimate: float
    sigma_used: float
    z: float  # (settlement_estimate - strike) / sigma_used -- positive favors "above"/yes,
    # independent of `direction`. Exposed for exit-monitoring (see runner.py's
    # _z_for_side / _check_exit_conditions) to compare a position's current
    # z-score against its entry z-score in sigma units, not raw probability.


def realized_vol_per_sqrt_second(history: list[tuple[float, float]]) -> float | None:
    """Stdev of log returns normalized by sqrt(elapsed seconds) -- i.e. the
    log-return volatility rate, from a (timestamp, price) history. Returns
    None if there isn't enough history to estimate from, OR if every
    consecutive price in the window is byte-for-byte identical (added
    2026-08-06 after a real live incident: a REST-polled underlying with a
    stale/duplicate quote returned a literal 0.0 here -- not None -- which
    downstream isn't "this asset has zero volatility," it's "we don't have
    a fresh enough sample to estimate volatility from." A literal 0.0 feeds
    into estimate_probability's sigma_used, which floors at 1e-9, turning
    any nonzero distance-to-strike into an absurd z (observed live:
    z=166,565,000,000 -- a since-removed overconfidence gate caught it that
    day and no trade happened, but the right fix is here, at the source).
    Exact equality (not a fuzzy epsilon) is deliberate: repeated identical floats
    only happen from a genuinely stale/duplicate quote, not real price noise,
    so there's no arbitrary threshold to pick.
    """
    if len(history) < 3:
        return None
    normalized_returns = []
    for (t0, p0), (t1, p1) in zip(history, history[1:]):
        dt = t1 - t0
        if dt <= 0 or p0 <= 0 or p1 <= 0:
            continue
        normalized_returns.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(normalized_returns) < 2:
        return None
    mean = sum(normalized_returns) / len(normalized_returns)
    variance = sum((r - mean) ** 2 for r in normalized_returns) / (len(normalized_returns) - 1)
    if variance == 0.0:
        return None
    return math.sqrt(variance)


def estimate_probability(
    *,
    direction: str,
    strike: float,
    spot: float,
    seconds_to_close: float,
    sigma_log_per_sqrt_second: float,
    spot_history: list[tuple[float, float]],
    now_epoch: float,
) -> ProbabilityEstimate:
    """`direction` is "above" (YES if settlement >= strike) or "below" (YES
    if settlement <= strike), matching Kalshi's `strike_type`.
    """
    seconds_to_close = max(seconds_to_close, 0.0)
    sigma_price_rate = spot * sigma_log_per_sqrt_second  # approx $ stdev per sqrt(second)

    if seconds_to_close > SETTLEMENT_AVERAGE_SECONDS:
        settlement_estimate = spot
        sigma_used = sigma_price_rate * math.sqrt(seconds_to_close)
    else:
        window_start_epoch = now_epoch - (SETTLEMENT_AVERAGE_SECONDS - seconds_to_close)
        realized_prices = [price for ts, price in spot_history if ts >= window_start_epoch]
        realized_avg = sum(realized_prices) / len(realized_prices) if realized_prices else spot

        elapsed_fraction = 1.0 - (seconds_to_close / SETTLEMENT_AVERAGE_SECONDS)
        elapsed_fraction = min(max(elapsed_fraction, 0.0), 1.0)
        settlement_estimate = realized_avg * elapsed_fraction + spot * (1.0 - elapsed_fraction)

        remaining_tau = seconds_to_close
        variance_of_remainder_avg = (sigma_price_rate ** 2) * remaining_tau / 3.0
        weight_of_remainder = remaining_tau / SETTLEMENT_AVERAGE_SECONDS
        sigma_used = math.sqrt(variance_of_remainder_avg) * weight_of_remainder

    # SIGMA_SAFETY_FACTOR: the raw sigma_used is calibration-tested (against
    # live/logs/samples.db) to be too small -- the trailing realized-vol input
    # misses ~10% of post-entry vol blow-ups and the Gaussian tail is too thin.
    # Widen before the z-score. See config.SIGMA_SAFETY_FACTOR.
    sigma_used = max(sigma_used * SIGMA_SAFETY_FACTOR, 1e-9)  # also avoids div-by-zero as tau -> 0
    signed_distance = settlement_estimate - strike
    z = signed_distance / sigma_used

    prob_above = _normal_cdf(z)
    prob_yes = prob_above if direction == "above" else (1.0 - prob_above)
    # MODEL_PROB_CAP: the model never actually resolves better than ~99.6%; clamp
    # so it can't report false certainty to downstream sizing/edge. z and
    # sigma_used are returned raw (exit monitoring compares z-drops).
    prob_yes = min(max(prob_yes, 1.0 - MODEL_PROB_CAP), MODEL_PROB_CAP)

    favored_side = "yes" if prob_yes >= 0.5 else "no"
    favored_probability = prob_yes if favored_side == "yes" else 1.0 - prob_yes

    return ProbabilityEstimate(
        prob_yes=prob_yes,
        favored_side=favored_side,
        favored_probability=favored_probability,
        settlement_estimate=settlement_estimate,
        sigma_used=sigma_used,
        z=z,
    )
