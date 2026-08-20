"""Standalone companion to sampling.py: fills in the actual outcome for
every sampled market that has since settled. Run this periodically (no
built-in scheduling -- invoke by hand or via cron/Task Scheduler); each run
processes every ticker in the DB that's still missing a resolution and exits.

Not part of the live trading loop -- read-only against Kalshi's public
market endpoint (no auth needed), and only ever writes to the sampling DB,
never touches order placement or the runner's own state.
"""

import logging
import sys
import time

import config
import sampling
from fees import estimate_fee_dollars
from kalshi_gateway import fetch_market

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("resolution_alpha.check_resolutions")


def check_all_pending(db_path: str) -> tuple[int, int]:
    """Returns (resolved_count, still_pending_count)."""
    tickers = sampling.pending_tickers(db_path)
    logger.info("checking %d pending ticker(s)", len(tickers))

    resolved = 0
    still_pending = 0
    for ticker in tickers:
        try:
            market = fetch_market(ticker)
        except Exception:
            logger.exception("failed to fetch %s, leaving pending", ticker)
            still_pending += 1
            continue

        status = market.get("status")
        result = market.get("result")
        if status == "finalized" and result in ("yes", "no"):
            rows_updated = sampling.record_resolution(db_path, ticker, result)
            logger.info("%s resolved %s (%d sample row(s) updated)", ticker, result, rows_updated)
            resolved += 1
        else:
            logger.debug("%s not yet finalized (status=%s)", ticker, status)
            still_pending += 1

        time.sleep(0.1)  # light rate-limit courtesy -- public endpoint, but no need to hammer it

    logger.info("done: %d resolved, %d still pending", resolved, still_pending)
    return resolved, still_pending


def backfill_edge(db_path: str) -> int:
    """Fills in edge_per_contract for every sample row that has a
    market_price but no edge yet. Not computed live in runner.py because the
    real edge_per_contract depends on the actual price/size walked through
    the order book at trade time (see _size_for_edge in runner.py), which
    most sampled rows never got -- they were skipped candidates, not trades.
    This is a proxy instead, using the same formula against the row's
    top-of-book market_price:

        effective_probability = min(model_prob, market_price + MAX_TRUSTED_EDGE_PROB)
        edge_per_contract = effective_probability - market_price - fee_per_contract

    fee_per_contract uses estimate_fee_dollars(market_price, contracts=1) --
    a rate-only estimate, since we don't know what size a real order would
    have used for a candidate that was never traded. Deliberately run here,
    not in the live loop: it's pure local computation over already-stored
    columns, no API calls, so there's no reason to pay for it on the hot
    path when this script already runs periodically after the fact.

    MAINTENANCE WARNING: mirrors evaluate_and_maybe_trade's edge formula in
    runner.py rather than sharing it (that function computes edge from an
    actual walked fill, not a bare top-of-book price, so it isn't reusable
    here as-is). If MAX_TRUSTED_EDGE_PROB's role or the edge formula changes
    there, mirror the change here too, or this column will silently drift
    from what the live trader actually means by "edge". Purely a research
    feature either way -- can never affect a real trade.
    """
    rows = sampling.rows_missing_edge(db_path)
    for sample_id, model_prob, market_price in rows:
        effective_probability = min(model_prob, market_price + config.MAX_TRUSTED_EDGE_PROB)
        fee_per_contract = estimate_fee_dollars(market_price, 1)
        edge_per_contract = effective_probability - market_price - fee_per_contract
        sampling.record_edge(db_path, sample_id, edge_per_contract)
    logger.info("backfilled edge_per_contract for %d row(s)", len(rows))
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = config.SAMPLING_DB_PATH
    # Ensures schema migrations (e.g. edge_per_contract, added 2026-08-07)
    # are applied regardless of whether the live runner has happened to run
    # with SAMPLING_ENABLED=true since -- runner.py only calls init_db() in
    # that case (see run_forever()), which this standalone script can't rely
    # on: real incident, 2026-08-07, sampling was off for a restart and
    # backfill_edge crashed with "no such column: edge_per_contract" against
    # an unmigrated DB. This script owns its own schema instead.
    sampling.init_db(db_path)
    check_all_pending(db_path)
    backfill_edge(db_path)
