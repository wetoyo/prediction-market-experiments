"""Builds a Deribit BTC options implied-vol surface and interpolates it to
an arbitrary (strike, time-to-expiry) point -- Kalshi's BTC markets settle
at strikes/times that essentially never line up with Deribit's actual listed
strikes and expiries, so this is the piece that bridges the two.

Two interpolation steps, both standard practice for building a vol surface
from a discrete option chain:
  1. Within a single Deribit expiry, interpolate implied vol across strikes
     linearly in log-moneyness ln(K/F) (flat-extrapolated past the smile's
     own strike range).
  2. Across the two Deribit expiries bracketing the target time, interpolate
     *total variance* (sigma^2 * T) linearly in T, then convert back to a
     vol -- the standard "flat forward variance" term-structure interpolation
     (linearly interpolating sigma itself would understate variance at the
     target date whenever the term structure isn't flat).

Only the OTM side of each strike is used (calls for K >= forward, puts for
K < forward) since that's consistently the more liquidly quoted side on
Deribit's book.
"""

import bisect
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from derbit.fetch_historical import get_book_summary_by_currency, get_index_price, get_instruments  # noqa: E402

from black_scholes import prob_forward_above_strike, prob_forward_below_strike  # noqa: E402

SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass
class ExpirySmile:
    expiration_timestamp_ms: int
    years_to_expiry: float
    forward: float
    strikes: list[float]  # sorted ascending
    log_moneyness: list[float]  # ln(K/F), parallel to strikes
    ivs: list[float]  # decimal (0.55, not 55), parallel to strikes


@dataclass
class Surface:
    spot: float
    fetched_at: float
    expiries: list[ExpirySmile]  # sorted by years_to_expiry ascending


@dataclass
class ProbabilityEstimate:
    prob_yes: float
    forward_used: float
    sigma_used: float
    years_to_expiry: float
    extrapolated: bool  # True if target T fell outside the surface's covered range


def build_surface(currency: str = "BTC") -> Surface:
    """Fetches Deribit's current option chain and organizes it into a
    per-expiry vol smile. One REST round-trip pair, safe to reuse across many
    probability lookups (e.g. once per strategy scan across all open Kalshi
    markets for this currency).
    """
    now_ms = time.time() * 1000.0
    instruments = {
        inst["instrument_name"]: inst
        for inst in get_instruments(currency, "option", expired=False)
    }
    book_summary = get_book_summary_by_currency(currency, "option")

    by_expiry: dict[int, dict] = {}
    for entry in book_summary:
        name = entry.get("instrument_name")
        inst = instruments.get(name)
        if inst is None:
            continue
        iv_pct = entry.get("mark_iv")
        forward = entry.get("underlying_price")
        strike = inst.get("strike")
        if not iv_pct or not forward or not strike:
            continue  # no live quote to build a smile point from

        expiry_ms = inst["expiration_timestamp"]
        is_otm = (inst["option_type"] == "call") == (strike >= forward)
        if not is_otm:
            continue

        bucket = by_expiry.setdefault(expiry_ms, {"forwards": [], "points": []})
        bucket["forwards"].append(forward)
        bucket["points"].append((strike, iv_pct / 100.0))

    expiries: list[ExpirySmile] = []
    for expiry_ms, bucket in by_expiry.items():
        years_to_expiry = max((expiry_ms - now_ms) / 1000.0, 0.0) / SECONDS_PER_YEAR
        if years_to_expiry <= 0 or len(bucket["points"]) < 2:
            continue  # expired, or too few quotes to interpolate a smile from
        forward = sum(bucket["forwards"]) / len(bucket["forwards"])
        points = sorted(bucket["points"])
        strikes = [p[0] for p in points]
        ivs = [p[1] for p in points]
        log_moneyness = [math.log(k / forward) for k in strikes]
        expiries.append(ExpirySmile(
            expiration_timestamp_ms=expiry_ms,
            years_to_expiry=years_to_expiry,
            forward=forward,
            strikes=strikes,
            log_moneyness=log_moneyness,
            ivs=ivs,
        ))
    expiries.sort(key=lambda e: e.years_to_expiry)

    spot = get_index_price(f"{currency.lower()}_usd").get("index_price", 0.0)
    return Surface(spot=spot, fetched_at=time.time(), expiries=expiries)


def _interp_smile_iv(smile: ExpirySmile, strike: float) -> float:
    """Linear interpolation in log-moneyness, flat-extrapolated past the
    smile's own strike range (there's no information past the last quoted
    strike, so holding the edge IV flat is the least-assumption choice).
    """
    x = math.log(strike / smile.forward)
    xs = smile.log_moneyness
    if x <= xs[0]:
        return smile.ivs[0]
    if x >= xs[-1]:
        return smile.ivs[-1]
    i = bisect.bisect_right(xs, x) - 1
    x0, x1 = xs[i], xs[i + 1]
    y0, y1 = smile.ivs[i], smile.ivs[i + 1]
    weight = (x - x0) / (x1 - x0)
    return y0 + weight * (y1 - y0)


def _iv_and_forward_at(surface: Surface, strike: float, years_to_expiry: float) -> tuple[float, float, bool]:
    """Returns (sigma, forward, extrapolated) for an arbitrary target expiry,
    via flat-forward total-variance interpolation across the two Deribit
    expiries bracketing `years_to_expiry` (or flat extrapolation from the
    nearest single expiry if the target falls outside the surface's range).
    """
    expiries = surface.expiries
    if not expiries:
        raise ValueError("empty Deribit options surface -- no live quotes to build a smile from")

    if years_to_expiry <= expiries[0].years_to_expiry:
        e = expiries[0]
        return _interp_smile_iv(e, strike), e.forward, years_to_expiry < e.years_to_expiry
    if years_to_expiry >= expiries[-1].years_to_expiry:
        e = expiries[-1]
        return _interp_smile_iv(e, strike), e.forward, years_to_expiry > e.years_to_expiry

    ts = [e.years_to_expiry for e in expiries]
    i = bisect.bisect_right(ts, years_to_expiry) - 1
    lo, hi = expiries[i], expiries[i + 1]

    iv_lo = _interp_smile_iv(lo, strike)
    iv_hi = _interp_smile_iv(hi, strike)
    total_var_lo = iv_lo * iv_lo * lo.years_to_expiry
    total_var_hi = iv_hi * iv_hi * hi.years_to_expiry
    weight = (years_to_expiry - lo.years_to_expiry) / (hi.years_to_expiry - lo.years_to_expiry)
    total_var = total_var_lo + weight * (total_var_hi - total_var_lo)
    sigma = math.sqrt(max(total_var, 0.0) / years_to_expiry)

    forward = lo.forward + weight * (hi.forward - lo.forward)
    return sigma, forward, False


def estimate_probability(
    surface: Surface, *, direction: str, strike: float, seconds_to_expiry: float
) -> ProbabilityEstimate:
    """`direction` is "above" (YES if settlement >= strike) or "below" (YES
    if settlement <= strike), matching Kalshi's strike_type convention.
    """
    years_to_expiry = max(seconds_to_expiry, 0.0) / SECONDS_PER_YEAR
    sigma, forward, extrapolated = _iv_and_forward_at(surface, strike, years_to_expiry)

    if direction == "above":
        prob_yes = prob_forward_above_strike(forward, strike, sigma, years_to_expiry)
    elif direction == "below":
        prob_yes = prob_forward_below_strike(forward, strike, sigma, years_to_expiry)
    else:
        raise ValueError(f"unknown direction {direction!r}, expected 'above' or 'below'")

    return ProbabilityEstimate(
        prob_yes=min(max(prob_yes, 0.0), 1.0),
        forward_used=forward,
        sigma_used=sigma,
        years_to_expiry=years_to_expiry,
        extrapolated=extrapolated,
    )


if __name__ == "__main__":
    surface = build_surface("BTC")
    print(f"spot={surface.spot} expiries_loaded={len(surface.expiries)}")
    for e in surface.expiries[:5]:
        print(
            f"  T={e.years_to_expiry * 365:.1f}d forward={e.forward:.0f} "
            f"strikes={len(e.strikes)} atm_iv~{_interp_smile_iv(e, e.forward):.3f}"
        )

    if surface.expiries:
        strike = round(surface.spot / 1000.0) * 1000.0
        est = estimate_probability(surface, direction="above", strike=strike, seconds_to_expiry=3600)
        print(f"\nP(BTC > {strike:.0f} in 1h) = {est.prob_yes:.4f} (sigma={est.sigma_used:.3f}, F={est.forward_used:.0f})")
