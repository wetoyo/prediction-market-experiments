"""Standalone, no-Kalshi-auth-needed live poller that grows calibration_db.py's
dataset going forward, complementing backfill_calibration.py's historical
replay with native ~2.5s-granularity live samples -- finer than backfill's
1-minute Coinbase candles, which matters most in exactly the last 30-60s
where that granularity is coarsest.

Not part of the live trading loop: read-only against Kalshi's public market
endpoints (discovery, single-market lookup) and Coinbase's public spot
endpoint, no credentials, never places an order. Safe to run continuously and
indefinitely, independent of whether the live trader is running.

Mirrors live/runner.py's own polling shape (continuous spot refresh for every
discovered underlying so volatility history is warm before a market enters
the decision window, not gated by the window itself) but only writes a
sample when a market IS inside DECISION_WINDOW_SECONDS -- resolution_alpha's
actual entry regime (median observed live entry ~22s before close), not a
generic/longer horizon. Unlike backfill_calibration.py's fixed decision-second
grid, this records whatever seconds_to_expiry a real tick happens to land on.

Built 2026-08-13 during a scheduled Kalshi API outage -- could NOT be
smoke-tested against the live API before that outage started. Every network
call follows this codebase's established retry/skip-don't-crash pattern (see
discovery.py's _fetch_markets_with_retry): the expectation is it sits idle
while Kalshi is down (catching and logging failures, writing nothing each
tick) and starts collecting for real once the API is back, but that
expectation is unverified until it's actually run against a live endpoint --
check logs after the outage ends before trusting this is working.

Usage:
    python collect_calibration_live.py [--db samples.db]
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "live"))

import calibration_db  # noqa: E402
from discovery import find_active_markets  # noqa: E402
from kalshi_gateway import fetch_market  # noqa: E402
from probability import estimate_probability, realized_vol_per_sqrt_second  # noqa: E402
from spot_feed import SpotFeed  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("resolution_alpha.collect_calibration_live")

DECISION_WINDOW_SECONDS = 90  # matches resolution_alpha's actual entry regime, see calibration_db.py
POLL_INTERVAL_SECONDS = 2.5  # matches live/runner.py's tick cadence
DISCOVERY_INTERVAL_SECONDS = 30
RESOLUTION_CHECK_INTERVAL_SECONDS = 120
DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "samples.db")


def _check_pending_resolutions(db_path: str) -> None:
    """Same approach as live/check_resolutions.py's check_all_pending, against
    this script's own dataset instead -- kept separate rather than imported,
    since that module's db_path defaulting (config.SAMPLING_DB_PATH) points at
    the live trading dataset, not this one.
    """
    tickers = calibration_db.pending_tickers(db_path)
    if not tickers:
        return
    logger.info("checking %d pending ticker(s) for resolution", len(tickers))
    resolved = 0
    for ticker in tickers:
        try:
            market = fetch_market(ticker)
        except Exception:
            logger.debug("failed to fetch %s, leaving pending", ticker)
            continue
        status = market.get("status")
        result = market.get("result")
        if status == "finalized" and result in ("yes", "no"):
            calibration_db.record_resolution(db_path, ticker, result)
            resolved += 1
        time.sleep(0.05)  # light rate-limit courtesy, same as check_resolutions.py
    if resolved:
        logger.info("resolved %d ticker(s)", resolved)


def run_forever(db_path: str) -> None:
    calibration_db.init_db(db_path)
    spot_feed = SpotFeed()

    active_markets = []
    last_discovery = 0.0
    last_resolution_check = 0.0
    samples_written = 0
    last_stats_log = time.time()

    logger.info("calibration live collector starting, db=%s", db_path)

    while True:
        now_ts = time.time()

        if now_ts - last_discovery >= DISCOVERY_INTERVAL_SECONDS:
            try:
                active_markets = find_active_markets()
                logger.info("discovered %d active markets", len(active_markets))
            except Exception:
                logger.exception("discovery failed, keeping previous market list")
            last_discovery = now_ts

        underlyings = {m.underlying for m in active_markets}
        for underlying in underlyings:
            spot_feed.poll(underlying)  # None on failure -- caught inside SpotFeed itself

        now = datetime.now(timezone.utc)
        for market in active_markets:
            seconds_left = (market.close_time - now).total_seconds()
            if not (0 < seconds_left <= DECISION_WINDOW_SECONDS):
                continue

            history = spot_feed.history(market.underlying)
            sigma_log = realized_vol_per_sqrt_second(history)
            if sigma_log is None:
                continue
            latest = spot_feed.latest(market.underlying)
            if latest is None:
                continue
            now_epoch, spot = latest

            try:
                estimate = estimate_probability(
                    direction=market.direction, strike=market.strike, spot=spot,
                    seconds_to_close=seconds_left, sigma_log_per_sqrt_second=sigma_log,
                    spot_history=history, now_epoch=now_epoch,
                )
            except Exception:
                logger.exception("probability estimate failed for %s, skipping", market.ticker)
                continue

            try:
                calibration_db.record_sample(
                    db_path,
                    source="live",
                    ticker=market.ticker,
                    underlying=market.underlying,
                    direction=market.direction,
                    strike=market.strike,
                    close_time=market.close_time,
                    decision_seconds=seconds_left,
                    sample_time=now,
                    spot=spot,
                    settlement_estimate=estimate.settlement_estimate,
                    sigma_used=estimate.sigma_used,
                    z=estimate.z,
                    favored_side=estimate.favored_side,
                    favored_probability=estimate.favored_probability,
                )
                samples_written += 1
            except Exception:
                logger.exception("failed to record calibration sample for %s", market.ticker)

        if now_ts - last_resolution_check >= RESOLUTION_CHECK_INTERVAL_SECONDS:
            try:
                _check_pending_resolutions(db_path)
            except Exception:
                logger.exception("resolution check pass failed")
            last_resolution_check = now_ts

        if now_ts - last_stats_log >= 300:
            logger.info("samples written this run: %d", samples_written)
            last_stats_log = now_ts

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    try:
        run_forever(args.db)
    except KeyboardInterrupt:
        logger.info("stopped")
