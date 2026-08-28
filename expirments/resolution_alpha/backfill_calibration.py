"""Backfills calibration_db.py's dataset from already-settled markets, reusing
backtest.py's discovery/candle-fetching (same settled-market population, same
Coinbase 1-minute candle source) but persisting every (market, decision point)
trial instead of just printing a summary, and evaluating a much finer grid of
decision points -- concentrated in 0-90s before close, resolution_alpha's
actual entry regime (median observed live entry ~22s before close), not a
broad/generic horizon.

Stores raw z, not just favored_probability -- see fit_calibration.py's
docstring for why bucketing on probability hides most of the tail once z
saturates the Gaussian CDF (~z=10+).

Usage:
    python backfill_calibration.py [--hours 72] [--decision-seconds 5,10,...,90] [--db calibration.db]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibration_db  # noqa: E402
from backtest import fetch_candles, find_settled_crypto_markets  # noqa: E402
from config import KALSHI_UNDERLYING_TO_COINBASE_PRODUCT  # noqa: E402
from probability import estimate_probability, realized_vol_per_sqrt_second  # noqa: E402

VOL_LOOKBACK_CANDLES = 30
DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "samples.db")


def _default_decision_seconds() -> list[float]:
    # Dense near close (where resolution_alpha actually trades), sparser
    # further out -- no point spending API/compute budget finely resolving a
    # regime this strategy never enters at.
    return [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90]


def backfill(hours: float, decision_seconds_list: list[float], db_path: str) -> None:
    calibration_db.init_db(db_path)

    settled = find_settled_crypto_markets(hours)
    print(f"\n{len(settled)} total settled crypto interval markets in the last {hours}h")
    if not settled:
        print("nothing to backfill -- widen --hours")
        return

    by_underlying: dict[str, list] = {}
    for m in settled:
        by_underlying.setdefault(m.underlying, []).append(m)

    candles_by_underlying: dict[str, list[tuple[float, float]]] = {}
    for underlying, markets in by_underlying.items():
        product_id = KALSHI_UNDERLYING_TO_COINBASE_PRODUCT.get(underlying)
        if not product_id:
            continue
        start = min(m.close_time for m in markets) - timedelta(minutes=VOL_LOOKBACK_CANDLES + 2)
        end = max(m.close_time for m in markets) + timedelta(minutes=1)
        print(f"fetching {underlying} ({product_id}) candles: {start} .. {end}")
        candles_by_underlying[underlying] = fetch_candles(product_id, start, end)

    total_written = 0
    for market in settled:
        candles = candles_by_underlying.get(market.underlying)
        if not candles:
            continue

        for decision_seconds in decision_seconds_list:
            decision_epoch = market.close_time.timestamp() - decision_seconds
            history = [(ts, price) for ts, price in candles if ts <= decision_epoch]
            if len(history) < 3:
                continue
            history = history[-VOL_LOOKBACK_CANDLES:]

            sigma_log = realized_vol_per_sqrt_second(history)
            if not sigma_log or sigma_log <= 0:
                continue

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

            calibration_db.record_sample(
                db_path,
                source="backfill",
                ticker=market.ticker,
                underlying=market.underlying,
                direction=market.direction,
                strike=market.strike,
                close_time=market.close_time,
                decision_seconds=decision_seconds,
                sample_time=datetime.fromtimestamp(now_epoch, tz=timezone.utc),
                spot=spot,
                settlement_estimate=estimate.settlement_estimate,
                sigma_used=estimate.sigma_used,
                z=estimate.z,
                favored_side=estimate.favored_side,
                favored_probability=estimate.favored_probability,
                resolution=market.result,  # already known -- settled market
            )
            total_written += 1

    print(f"\nwrote {total_written} calibration samples to {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--decision-seconds", type=str, default=None, help="comma-separated, default is a dense 0-90s grid")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    decision_points = (
        [float(x) for x in args.decision_seconds.split(",")]
        if args.decision_seconds else _default_decision_seconds()
    )
    backfill(args.hours, decision_points, args.db)
