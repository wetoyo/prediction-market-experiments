"""Historical calibration backtest for resolution_alpha (see README.md's "Backtest plan").

Tests the one thing available data can actually test: whether live/probability.py's
resolution-probability model, fed historical Coinbase spot as the same proxy
live/spot_feed.py uses, corresponds to real outcomes for settled Kalshi crypto
interval markets. This imports and runs the exact live probability module
rather than reimplementing the math, so a pass here means "the live code's
model checks out against history," not just "some similar formula does."

What this does NOT (and structurally cannot, yet) validate:
  - Fill/liquidity economics. Kalshi's REST API only exposes the *current*
    order book, never a historical one -- there is no way to know what price
    or depth was actually available 90 seconds before close for a market that
    settled hours or days ago. This is the same "probable blocker" flagged in
    README.md's Data requirements section, still unresolved. This script
    reports probability-model calibration only, never P&L.
  - Sub-minute settlement dynamics. Coinbase's free historical-candles
    endpoint tops out at 1-minute granularity, vs. live's ~2-second spot
    polling, so the last-60-second variance-shrinkage regime in
    probability.py runs on coarser data here than it does live.

Usage:
    python backtest.py [--hours 24] [--decision-seconds 90,60,30] [--min-prob 0.97]
"""

import argparse
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "live"))

from config import INTERVAL_FREQUENCIES  # noqa: E402
from discovery import _extract_strike, _extract_underlying, _parse_time  # noqa: E402
from kalshi_gateway import fetch_markets, fetch_series  # noqa: E402
from probability import estimate_probability, realized_vol_per_sqrt_second  # noqa: E402

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"
CANDLE_GRANULARITY_SECONDS = 60
MAX_CANDLES_PER_REQUEST = 300  # Coinbase's per-request cap at 60s granularity = 5h
VOL_LOOKBACK_CANDLES = 30  # ~30 minutes of 1-minute candles feeding realized-vol estimate


@dataclass
class SettledMarket:
    ticker: str
    underlying: str
    direction: str
    strike: float
    open_time: datetime
    close_time: datetime
    result: str  # "yes" or "no"


def find_settled_crypto_markets(lookback_hours: float) -> list[SettledMarket]:
    """Mirrors live/discovery.py's series filter and strike parsing exactly,
    but against status="settled" markets instead of "open" ones, so the
    backtest population matches what the live system would actually consider.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    settled: list[SettledMarket] = []

    series_list = fetch_series(category="Crypto")
    qualifying = [s for s in series_list if s.get("frequency") in INTERVAL_FREQUENCIES and _extract_underlying(s)]
    print(f"scanning {len(qualifying)} qualifying crypto interval series for settled markets...")

    for series in qualifying:
        underlying = _extract_underlying(series)
        markets = None
        for attempt in range(5):
            try:
                markets = fetch_markets(series_ticker=series["ticker"], status="settled")
                break
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    backoff = 2.0 * (attempt + 1)
                    print(f"  {series['ticker']}: rate limited, retrying in {backoff:.0f}s (attempt {attempt + 1}/5)")
                    time.sleep(backoff)
                    continue
                print(f"  {series['ticker']}: fetch failed ({exc}), skipping")
                break
            except Exception as exc:
                print(f"  {series['ticker']}: fetch failed ({exc}), skipping")
                break
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
                open_time = _parse_time(market["open_time"])
                close_time = _parse_time(market["close_time"])
            except (KeyError, ValueError):
                continue
            if close_time < cutoff:
                continue
            settled.append(SettledMarket(
                ticker=market["ticker"], underlying=underlying, direction=direction,
                strike=strike, open_time=open_time, close_time=close_time, result=result,
            ))
            kept += 1
        if kept:
            print(f"  {series['ticker']} ({underlying}): {kept} settled markets in lookback window")

    return settled


def fetch_candles(product_id: str, start: datetime, end: datetime) -> list[tuple[float, float]]:
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
                COINBASE_CANDLES_URL.format(product_id=product_id),
                params={
                    "granularity": CANDLE_GRANULARITY_SECONDS,
                    "start": page_start.isoformat(),
                    "end": page_end.isoformat(),
                },
                headers={"User-Agent": "resolution_alpha-backtest"},
                timeout=20,
            )
            response.raise_for_status()
            for row in response.json():
                ts, _low, _high, _open, close, _volume = row
                # Coinbase timestamps a candle by its *bucket start*, but the
                # close price isn't knowable until the bucket ends -- label
                # it by bucket end (ts + granularity) so a later "keep
                # candles with timestamp <= decision_epoch" filter can't leak
                # up to 60s of future price info into a decision point.
                candles.append((float(ts) + CANDLE_GRANULARITY_SECONDS, float(close)))
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"    candle fetch failed for {product_id} [{page_start}..{page_end}]: {exc}")
        page_end = page_start
        time.sleep(0.1)  # be polite to Coinbase's public endpoint

    candles.sort(key=lambda c: c[0])
    return candles


@dataclass
class Trial:
    market: SettledMarket
    decision_seconds: float
    favored_side: str
    favored_probability: float
    correct: bool  # favored_side matched the actual result


def evaluate_market(
    market: SettledMarket, candles: list[tuple[float, float]], decision_seconds: float
) -> Trial | None:
    decision_epoch = market.close_time.timestamp() - decision_seconds
    history = [(ts, price) for ts, price in candles if ts <= decision_epoch]
    if len(history) < 3:
        return None
    history = history[-VOL_LOOKBACK_CANDLES:]

    sigma_log = realized_vol_per_sqrt_second(history)
    if not sigma_log or sigma_log <= 0:
        return None

    now_epoch, spot = history[-1]
    estimate = estimate_probability(
        direction=market.direction,
        strike=market.strike,
        spot=spot,
        seconds_to_close=decision_seconds,
        sigma_log_per_sqrt_second=sigma_log,
        spot_history=history,
        now_epoch=now_epoch,
    )
    return Trial(
        market=market,
        decision_seconds=decision_seconds,
        favored_side=estimate.favored_side,
        favored_probability=estimate.favored_probability,
        correct=(estimate.favored_side == market.result),
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
    settled = find_settled_crypto_markets(hours)
    print(f"\n{len(settled)} total settled crypto interval markets in the last {hours}h\n")
    if not settled:
        print("nothing to backtest -- widen --hours")
        return

    by_underlying: dict[str, list[SettledMarket]] = defaultdict(list)
    for m in settled:
        by_underlying[m.underlying].append(m)

    candles_by_underlying: dict[str, list[tuple[float, float]]] = {}
    for underlying, markets in by_underlying.items():
        product_id = None
        from config import KALSHI_UNDERLYING_TO_COINBASE_PRODUCT
        product_id = KALSHI_UNDERLYING_TO_COINBASE_PRODUCT.get(underlying)
        if not product_id:
            continue
        start = min(m.close_time for m in markets) - timedelta(minutes=VOL_LOOKBACK_CANDLES + 2)
        end = max(m.close_time for m in markets) + timedelta(minutes=1)
        print(f"fetching {underlying} ({product_id}) candles: {start} .. {end}")
        candles_by_underlying[underlying] = fetch_candles(product_id, start, end)

    for decision_seconds in decision_seconds_list:
        trials: list[Trial] = []
        for market in settled:
            candles = candles_by_underlying.get(market.underlying)
            if not candles:
                continue
            trial = evaluate_market(market, candles, decision_seconds)
            if trial is not None:
                trials.append(trial)

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
