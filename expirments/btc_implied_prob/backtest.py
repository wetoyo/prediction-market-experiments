"""Historical calibration backtest for btc_implied_prob.

Deribit's public REST API does not expose historical implied-vol surfaces
or option order books -- every function in derbit/fetch_historical.py
(get_instruments, get_order_book, get_book_summary_by_currency, ...) is a
*current-state* snapshot, and this project hasn't been continuously
archiving them long enough to have built a local history either
(Data/derbit.db holds one test row). The one genuinely historical vol series
Deribit's public API exposes is get_historical_volatility -- BTC's own
*realized*-volatility index, hourly buckets, ~16 days back at time of
writing -- which is NOT the live strategy's input (a live per-expiry
*implied*-vol smile read off the current option chain). Realized and
implied vol are correlated but implied usually carries a variance risk
premium above realized, so this backtest tests a related but distinct
model: the identical Black-76 N(d2) formula from black_scholes.py (the same
function strategy.py calls), fed a realized-vol reading instead of a live
options-market-implied one, with the forward approximated by spot (no
historical futures/basis curve is available either -- reasonable for BTC's
usually-small carry, not exact).

A pass here means "the probability math and time-decay handling calibrate
against history," not "the live IV-surface edge is real" -- that second
claim can only be validated by letting strategy.py run and log its own
predictions going forward (see live/logs/).

Spot price is Coinbase's free historical 1-minute candles -- same source
and caveats (1-minute granularity, 300-candle/request paging) as
../resolution_alpha/backtest.py, which this mirrors structurally: same
settled-market discovery shape, same calibration-table + would-have-traded
report.

Usage:
    python backtest.py [--hours 24] [--decision-seconds 90,60,30] [--min-prob 0.97]
"""

import argparse
import bisect
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from derbit.fetch_historical import get_historical_volatility  # noqa: E402

from black_scholes import prob_forward_above_strike, prob_forward_below_strike  # noqa: E402
from config import INTERVAL_FREQUENCIES  # noqa: E402
from kalshi_btc_markets import _extract_strike, _fetch_markets_with_retry, _parse_time  # noqa: E402
from kalshi_gateway import fetch_series  # noqa: E402

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLE_GRANULARITY_SECONDS = 60
MAX_CANDLES_PER_REQUEST = 300  # Coinbase's per-request cap at 60s granularity = 5h
SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass
class SettledMarket:
    ticker: str
    direction: str
    strike: float
    close_time: datetime
    result: str  # "yes" or "no"


def find_settled_btc_markets(lookback_hours: float) -> list[SettledMarket]:
    """Mirrors kalshi_btc_markets.find_active_btc_markets's series filter and
    strike parsing exactly, but against status="settled" markets.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    settled: list[SettledMarket] = []

    series_list = fetch_series(category="Crypto")
    qualifying = [
        s for s in series_list
        if s.get("frequency") in INTERVAL_FREQUENCIES and "BTC" in [t.upper() for t in (s.get("tags") or [])]
    ]
    print(f"scanning {len(qualifying)} qualifying BTC interval series for settled markets...")

    for series in qualifying:
        markets = _fetch_markets_with_retry(series["ticker"], status="settled")
        if markets is None:
            continue
        time.sleep(0.5)  # be polite to the shared Kalshi rate limit budget

        kept = 0
        for market in markets:
            result = market.get("result")
            if result not in ("yes", "no"):
                continue
            parsed = _extract_strike(market)
            if parsed is None:
                continue
            direction, strike = parsed
            try:
                close_time = _parse_time(market["close_time"])
            except (KeyError, ValueError):
                continue
            if close_time < cutoff:
                continue
            settled.append(SettledMarket(
                ticker=market["ticker"], direction=direction, strike=strike,
                close_time=close_time, result=result,
            ))
            kept += 1
        if kept:
            print(f"  {series['ticker']}: {kept} settled markets in lookback window")

    return settled


def fetch_candles(start: datetime, end: datetime) -> list[tuple[float, float]]:
    """Returns [(epoch_seconds, close_price), ...] ascending by time, at
    1-minute granularity, paging Coinbase's public historical-candles
    endpoint backwards in <=300-candle (5h) chunks.
    """
    candles: list[tuple[float, float]] = []
    page_end = end
    while page_end > start:
        page_start = max(start, page_end - timedelta(seconds=MAX_CANDLES_PER_REQUEST * CANDLE_GRANULARITY_SECONDS))
        try:
            response = requests.get(
                COINBASE_CANDLES_URL,
                params={
                    "granularity": CANDLE_GRANULARITY_SECONDS,
                    "start": page_start.isoformat(),
                    "end": page_end.isoformat(),
                },
                headers={"User-Agent": "btc_implied_prob-backtest"},
                timeout=20,
            )
            response.raise_for_status()
            for row in response.json():
                ts, _low, _high, _open, close, _volume = row
                # Label by bucket end (ts + granularity), not bucket start --
                # the close price isn't knowable until the bucket ends, so a
                # later "candles with timestamp <= decision_epoch" filter
                # can't leak up to 60s of future price into a decision point.
                candles.append((float(ts) + CANDLE_GRANULARITY_SECONDS, float(close)))
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"    candle fetch failed [{page_start}..{page_end}]: {exc}")
        page_end = page_start
        time.sleep(0.1)  # be polite to Coinbase's public endpoint

    candles.sort(key=lambda c: c[0])
    return candles


def _value_at_or_before(series: list[tuple[float, float]], epoch_seconds: float) -> float | None:
    """Last (timestamp, value) in an ascending-by-timestamp series with
    timestamp <= epoch_seconds, or None if the series doesn't reach back
    that far.
    """
    timestamps = [t for t, _ in series]
    i = bisect.bisect_right(timestamps, epoch_seconds) - 1
    return series[i][1] if i >= 0 else None


@dataclass
class Trial:
    market: SettledMarket
    decision_seconds: float
    favored_side: str
    favored_probability: float
    correct: bool  # favored_side matched the actual result


def evaluate_market(
    market: SettledMarket,
    candles: list[tuple[float, float]],
    hv_series: list[tuple[float, float]],
    decision_seconds: float,
) -> Trial | None:
    decision_epoch = market.close_time.timestamp() - decision_seconds

    spot = _value_at_or_before(candles, decision_epoch)
    sigma_pct = _value_at_or_before(hv_series, decision_epoch)
    if spot is None or sigma_pct is None or sigma_pct <= 0:
        return None
    sigma = sigma_pct / 100.0

    years_to_expiry = max(decision_seconds, 0.0) / SECONDS_PER_YEAR
    if market.direction == "above":
        prob_yes = prob_forward_above_strike(spot, market.strike, sigma, years_to_expiry)
    else:
        prob_yes = prob_forward_below_strike(spot, market.strike, sigma, years_to_expiry)

    favored_side = "yes" if prob_yes >= 0.5 else "no"
    favored_probability = prob_yes if favored_side == "yes" else 1.0 - prob_yes

    return Trial(
        market=market,
        decision_seconds=decision_seconds,
        favored_side=favored_side,
        favored_probability=favored_probability,
        correct=(favored_side == market.result),
    )


PROB_BUCKETS = [(0.50, 0.90), (0.90, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 0.995), (0.995, 1.0)]


def print_calibration_table(trials: list[Trial]) -> None:
    print(f"{'model prob bucket':<20}{'n':>6}{'actual win rate':>18}{'avg model prob':>16}")
    for lo, hi in PROB_BUCKETS:
        bucket = [t for t in trials if lo <= t.favored_probability < hi]
        if not bucket:
            continue
        win_rate = sum(t.correct for t in bucket) / len(bucket)
        avg_model_prob = sum(t.favored_probability for t in bucket) / len(bucket)
        print(f"[{lo:.3f},{hi:.3f})".ljust(20) + f"{len(bucket):>6}{win_rate:>17.1%}{avg_model_prob:>16.4f}")


def run(hours: float, decision_seconds_list: list[float], min_prob: float) -> None:
    settled = find_settled_btc_markets(hours)
    print(f"\n{len(settled)} total settled BTC interval markets in the last {hours}h\n")
    if not settled:
        print("nothing to backtest -- widen --hours")
        return

    start = min(m.close_time for m in settled) - timedelta(minutes=2)
    end = max(m.close_time for m in settled) + timedelta(minutes=1)
    print(f"fetching BTC-USD candles: {start} .. {end}")
    candles = fetch_candles(start, end)

    print("fetching Deribit BTC historical (realized) volatility...")
    hv_raw = get_historical_volatility("BTC")
    hv_series = sorted((ts_ms / 1000.0, value) for ts_ms, value in hv_raw)
    if hv_series:
        print(f"  {len(hv_series)} points, {datetime.fromtimestamp(hv_series[0][0], tz=timezone.utc)} "
              f".. {datetime.fromtimestamp(hv_series[-1][0], tz=timezone.utc)}")

    for decision_seconds in decision_seconds_list:
        trials = [
            t for market in settled
            if (t := evaluate_market(market, candles, hv_series, decision_seconds)) is not None
        ]

        print(f"\n=== decision point: T-{decision_seconds:.0f}s before close ({len(trials)} evaluable markets) ===")
        print_calibration_table(trials)

        would_trade = [t for t in trials if t.favored_probability >= min_prob]
        if would_trade:
            win_rate = sum(t.correct for t in would_trade) / len(would_trade)
            avg_model_prob = sum(t.favored_probability for t in would_trade) / len(would_trade)
            print(
                f"would-have-traded (model prob >= {min_prob}): {len(would_trade)} signals, "
                f"actual win rate {win_rate:.1%}, avg model prob {avg_model_prob:.4f} "
                f"(gap: {avg_model_prob - win_rate:+.4f})"
            )
        else:
            print(f"would-have-traded (model prob >= {min_prob}): 0 signals")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24.0, help="lookback window of settled markets to test")
    parser.add_argument(
        "--decision-seconds", type=str, default="90,60,30",
        help="comma-separated list of seconds-before-close decision points to evaluate",
    )
    parser.add_argument("--min-prob", type=float, default=0.97, help="favored-probability threshold to call a 'signal'")
    args = parser.parse_args()

    decision_points = [float(x) for x in args.decision_seconds.split(",")]
    run(args.hours, decision_points, args.min_prob)
