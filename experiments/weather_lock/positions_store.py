"""This runner's open positions, keyed by market ticker, persisted between
ticks and restarts so a looping run never re-buys past its caps.

Each entry: side, contracts, cost (upper bound: filled x limit + fee),
event_ticker, settle_time. Entries drop out a few hours after settle_time,
when the cash is back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("weather_lock.positions_store")

_PRUNE_AFTER = timedelta(hours=6)


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        logger.exception("could not read %s -- starting with no tracked positions", path)
        return {}
    logger.info("loaded %d tracked position(s) from %s", len(data), path)
    return data


def save(path: str, positions: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(positions, indent=2, sort_keys=True))
    tmp.replace(p)


def add_fill(positions: dict, ticker: str, side: str, contracts: float, cost: float,
             event_ticker: str, settle_time: datetime) -> None:
    entry = positions.setdefault(ticker, {
        "side": side, "contracts": 0.0, "cost": 0.0,
        "event_ticker": event_ticker, "settle_time": settle_time.isoformat(),
    })
    entry["contracts"] = round(entry["contracts"] + contracts, 4)
    entry["cost"] = round(entry["cost"] + cost, 4)


def prune_settled(positions: dict, now: datetime) -> None:
    for ticker in list(positions):
        try:
            settle = datetime.fromisoformat(positions[ticker]["settle_time"])
        except (KeyError, ValueError):
            continue
        if now >= settle + _PRUNE_AFTER:
            logger.info("%s: settled, no longer tracked", ticker)
            del positions[ticker]


def event_cost(positions: dict, event_ticker: str) -> float:
    return sum(p["cost"] for p in positions.values() if p.get("event_ticker") == event_ticker)


def total_cost(positions: dict) -> float:
    return sum(p["cost"] for p in positions.values())


def reconcile(positions: dict, manager, prefixes: tuple[str, ...] = ("KXHIGH", "KXLOW")) -> None:
    """Adds any real temperature-market position this runner has no record
    of (a crash between fill and save). Live only; best effort."""
    if manager.dry_run:
        return
    try:
        real = manager.get_positions()
    except Exception:
        logger.exception("could not fetch Kalshi positions -- continuing with local state")
        return
    for mp in real.get("market_positions", []):
        ticker = mp.get("ticker") or ""
        if not ticker.startswith(prefixes) or ticker in positions:
            continue
        try:
            pos = float(mp.get("position_fp") or 0.0)
        except (TypeError, ValueError):
            continue
        if pos == 0:
            continue
        cost = float(mp.get("total_traded_dollars") or 0.0) or abs(pos)
        logger.warning("%s: untracked Kalshi position %.2f, adding it (cost $%.2f)", ticker, pos, cost)
        positions[ticker] = {
            "side": "yes" if pos > 0 else "no", "contracts": abs(pos), "cost": cost,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "settle_time": (datetime.now(timezone.utc) + timedelta(hours=36)).isoformat(),
            "reconciled": True,
        }
