"""Local persistence + Kalshi-side reconciliation for strategy.py's
open_positions dict.

Added 2026-08-15 after a live incident: open_positions was tracked only in
process memory, and only when config.ENABLE_TRAILING_EXIT was on -- with it
off (the default), _execute had no record of what it already held, so it
re-bought the same handful of qualifying tickers on every single loop tick.
A process left running for ~80 minutes accumulated ~750 contracts across 7
strikes in one event before anyone noticed, paying a separate fee each time.
open_positions is now always tracked and always persisted here, so a crash
or restart doesn't forget what's already open (see save/load below), and
reconcile_with_kalshi below cross-checks against the real account on startup
to pick up any position this process still has no local record of (e.g. one
opened before this file existed, or by a different process entirely).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from kalshi_btc_markets import ActiveMarket
from order_manager import OrderManager

logger = logging.getLogger("btc_implied_prob.positions_store")


def _market_to_dict(market: ActiveMarket) -> dict:
    return {
        "ticker": market.ticker, "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker, "direction": market.direction,
        "strike": market.strike, "open_time": market.open_time.isoformat(),
        "close_time": market.close_time.isoformat(),
        "settlement_average_seconds": market.settlement_average_seconds,
    }


def _market_from_dict(d: dict) -> ActiveMarket:
    return ActiveMarket(
        ticker=d["ticker"], event_ticker=d["event_ticker"], series_ticker=d["series_ticker"],
        direction=d["direction"], strike=d["strike"],
        open_time=datetime.fromisoformat(d["open_time"]), close_time=datetime.fromisoformat(d["close_time"]),
        settlement_average_seconds=d["settlement_average_seconds"],
        # Stale by construction -- whatever quote was live when this was saved is meaningless
        # by the time it's loaded. _check_exit_conditions always re-fetches live quotes from
        # that tick's own markets_by_ticker rather than trusting a position's stored market.
        yes_bid=0.0, yes_ask=0.0,
    )


def save(path: str, open_positions: dict) -> None:
    payload = {
        ticker: {
            "market": _market_to_dict(p["market"]), "side": p["side"], "contracts": p["contracts"],
            "entry_edge": p["entry_edge"], "entry_price": p["entry_price"], "peak_price": p["peak_price"],
            "reconciled": p.get("reconciled", False),
            # A live resting take-profit order (see strategy.py's _maintain_take_profit_order)
            # outlives a process restart -- persisting the target price we last set it to means
            # a reloaded position doesn't immediately reprice/replace a still-correct order.
            "tp_order_price": p.get("tp_order_price"),
        }
        for ticker, p in open_positions.items()
    }
    Path(path).write_text(json.dumps(payload, indent=2))


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.exception("failed to load positions state from %s -- starting with no tracked positions", path)
        return {}

    open_positions = {}
    for ticker, entry in raw.items():
        open_positions[ticker] = {
            "market": _market_from_dict(entry["market"]), "side": entry["side"],
            "contracts": entry["contracts"], "entry_edge": entry["entry_edge"],
            "entry_price": entry["entry_price"], "peak_price": entry["peak_price"],
            "reconciled": entry.get("reconciled", False),
            "tp_order_price": entry.get("tp_order_price"),
        }
    logger.info("loaded %d tracked position(s) from %s", len(open_positions), path)
    return open_positions


def reconcile_with_kalshi(open_positions: dict, manager: OrderManager, markets_by_ticker: dict) -> None:
    """Adds any real non-zero Kalshi position this process has no local
    record of, mutating open_positions in place. Meant to be called once,
    early, on startup (before the first _execute) -- not every tick, since
    it's an extra authenticated API call and every position it could find is
    also one _execute would otherwise place fresh entry-tracking for anyway.

    Best-effort only: without our own entry_edge, a reconciled entry can't
    know what edge originally justified the trade, so entry_edge is set to
    None (see strategy.py's exit-trigger log line, which handles that) and
    peak_price defaults to Kalshi's own average cost (total_traded_dollars /
    contracts) rather than a real observed peak -- the closest available
    stand-in for entry_price, but not a substitute for having tracked it live.
    Flagged via "reconciled": True. No-op in dry-run -- there's no real
    account position to reconcile against.

    A position whose ticker isn't in this tick's markets_by_ticker (already
    closed, or outside INTERVAL_FREQUENCIES/BTC scope) can't be reconciled --
    _check_exit_conditions needs a real ActiveMarket for direction/strike/
    close_time, which only a fresh discovery-pass market has. Logged and left
    untracked; harmless, since a market this process doesn't scan was never
    going to be re-bought by the bug this exists to guard against anyway.
    """
    if manager.dry_run:
        return
    try:
        positions = manager.get_positions()
    except Exception:
        logger.exception("failed to fetch Kalshi positions for reconciliation -- continuing with local state only")
        return

    for mp in positions.get("market_positions", []):
        ticker = mp["ticker"]
        position_fp = float(mp["position_fp"])
        if position_fp == 0.0 or ticker in open_positions:
            continue

        market = markets_by_ticker.get(ticker)
        if market is None:
            logger.warning(
                "%s: Kalshi shows a %.2f-contract position with no local record, but it's not in this tick's "
                "open-market scan -- can't reconcile (need a live ActiveMarket for direction/strike/close_time), "
                "leaving untracked", ticker, position_fp,
            )
            continue

        contracts = abs(position_fp)
        side = "yes" if position_fp > 0 else "no"
        total_cost = float(mp.get("total_traded_dollars", 0.0))
        avg_price = total_cost / contracts if contracts else 0.0
        logger.warning(
            "%s: reconciled untracked Kalshi position -- %s x%.2f @ avg cost %.4f (entry_edge unknown, "
            "using avg cost as both entry_price and peak_price)", ticker, side, contracts, avg_price,
        )
        open_positions[ticker] = {
            "market": market, "side": side, "contracts": contracts,
            "entry_edge": None, "entry_price": avg_price, "peak_price": avg_price,
            "reconciled": True,
            # None, not adopted from any pre-existing resting order on Kalshi -- there
            # shouldn't be one for a genuinely-untracked position (this feature is what
            # places them, and an untracked position predates this feature by definition),
            # and _maintain_take_profit_order self-heals harmlessly even if there is one
            # (None != any real target -> cancel-and-replace on the first tick that sees it).
            "tp_order_price": None,
        }
