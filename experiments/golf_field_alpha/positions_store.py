"""Local persistence + Kalshi-side reconciliation for strategy.py's
open_positions dict (keyed by player-market ticker).

Same rationale as ../btc_implied_prob/positions_store.py, which exists
because of a real incident there: open_positions tracked only in process
memory meant a looping --execute run re-bought the same legs on every tick
(~750 contracts across 7 strikes in ~80 minutes before anyone noticed).
Here the blast radius is larger -- a basket is 20-40 legs, and re-buying
the whole basket every tick would burn fees and oversize fast -- so the
dedup, persistence, and startup reconciliation matter more, not less.

A golf basket has no exit logic (it rides to the tournament resolution),
so a tracked position only needs enough to (a) skip re-buying a leg
already held and (b) survive a restart. No peak-price / take-profit state.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from discovery import GolfEvent, PlayerMarket
from order_manager import OrderManager

logger = logging.getLogger("golf_field_alpha.positions_store")


def save(path: str, open_positions: dict) -> None:
    payload = {
        ticker: {
            "event_ticker": p["event_ticker"],
            "series_ticker": p.get("series_ticker", ""),
            "name": p["name"],
            "side": p["side"],
            "contracts": p["contracts"],
            "entry_price": p["entry_price"],
            "entry_edge": p["entry_edge"],
            "entry_fair": p.get("entry_fair"),
            "method": p.get("method", ""),
            "close_time": p["close_time"],
            "reconciled": p.get("reconciled", False),
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
            "event_ticker": entry["event_ticker"],
            "series_ticker": entry.get("series_ticker", ""),
            "name": entry["name"],
            "side": entry["side"],
            "contracts": entry["contracts"],
            "entry_price": entry["entry_price"],
            "entry_edge": entry["entry_edge"],
            "entry_fair": entry.get("entry_fair"),
            "method": entry.get("method", ""),
            "close_time": entry["close_time"],
            "reconciled": entry.get("reconciled", False),
        }
    logger.info("loaded %d tracked leg(s) from %s", len(open_positions), path)
    return open_positions


def prune_closed(open_positions: dict, now: datetime) -> None:
    """Drops legs whose event close_time has passed -- a resolved position
    is dead weight in the tracked dict (no exit logic re-checks it).
    """
    for ticker in list(open_positions):
        try:
            close_time = datetime.fromisoformat(open_positions[ticker]["close_time"])
        except (KeyError, ValueError):
            continue
        if close_time <= now:
            logger.info("%s: event closed, dropping from tracked positions", ticker)
            del open_positions[ticker]


def reconcile_with_kalshi(open_positions: dict, manager: OrderManager, events: list[GolfEvent]) -> None:
    """Adds any real non-zero Kalshi YES position this process has no local
    record of. Called once on startup before the first --execute. Best-
    effort: without our own entry data, entry_edge/entry_fair are None and
    entry_price is Kalshi's average cost basis. No-op in dry-run.

    A position whose ticker isn't in the current open-event scan can't be
    reconciled (we need its event/close_time) -- logged and left untracked;
    harmless, since a market we don't scan won't be re-bought anyway.
    """
    if manager.dry_run:
        return
    try:
        positions = manager.get_positions()
    except Exception:
        logger.exception("failed to fetch Kalshi positions for reconciliation -- continuing with local state only")
        return

    players_by_ticker: dict[str, tuple[GolfEvent, PlayerMarket]] = {}
    for ev in events:
        for pl in ev.players:
            players_by_ticker[pl.ticker] = (ev, pl)

    for mp in positions.get("market_positions", []):
        ticker = mp.get("ticker")
        try:
            position_fp = float(mp.get("position_fp", 0.0))
        except (TypeError, ValueError):
            continue
        if position_fp == 0.0 or ticker in open_positions:
            continue

        match = players_by_ticker.get(ticker)
        if match is None:
            logger.warning(
                "%s: Kalshi shows a %.2f-contract position with no local record, not in the current open-event "
                "scan -- leaving untracked", ticker, position_fp,
            )
            continue

        ev, pl = match
        contracts = abs(position_fp)
        side = "yes" if position_fp > 0 else "no"
        total_cost = float(mp.get("total_traded_dollars", 0.0) or 0.0)
        avg_price = total_cost / contracts if contracts else 0.0
        logger.warning(
            "%s: reconciled untracked Kalshi position -- %s x%.2f @ avg cost %.4f (entry edge unknown)",
            ticker, side, contracts, avg_price,
        )
        open_positions[ticker] = {
            "event_ticker": ev.event_ticker,
            "series_ticker": ev.series_ticker,
            "name": pl.name,
            "side": side,
            "contracts": contracts,
            "entry_price": avg_price,
            "entry_edge": None,
            "entry_fair": None,
            "method": "reconciled",
            "close_time": ev.close_time.isoformat(),
            "reconciled": True,
        }
