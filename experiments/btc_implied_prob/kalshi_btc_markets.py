"""Discovers currently-open Kalshi BTC interval markets ("above"/"below" a
strike, resolving on a CF Benchmarks BRTI settlement average) to price
against the Deribit-implied model.

Scoped to BTC only and to series on a recurring interval cadence (Kalshi's
`frequency` field) -- confirmed live 2026-08-12: KXBTCD reports
frequency="hourly" with a 60s settlement_timer_seconds averaging window,
KXBTC15M reports "fifteen_min". "Range"-type markets (strike_type ==
"between", e.g. plain KXBTC) are out of scope -- _extract_strike returns
None for those and they're silently skipped. Mirrors the approach in
../resolution_alpha/live/discovery.py, narrowed to a single underlying.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import INTERVAL_FREQUENCIES
from kalshi_gateway import fetch_markets, fetch_series

logger = logging.getLogger("btc_implied_prob.kalshi_btc_markets")


@dataclass
class ActiveMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    direction: str  # "above" or "below"
    strike: float
    open_time: datetime
    close_time: datetime
    settlement_average_seconds: float
    yes_bid: float
    yes_ask: float


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_strike(market: dict) -> tuple[str, float] | None:
    strike_type = market.get("strike_type")
    if strike_type in ("greater", "greater_or_equal"):
        floor = market.get("floor_strike")
        return ("above", float(floor)) if floor is not None else None
    if strike_type in ("less", "less_or_equal"):
        cap = market.get("cap_strike")
        return ("below", float(cap)) if cap is not None else None
    return None


def find_btc_interval_series() -> list[dict]:
    """Returns BTC series on a recurring interval cadence."""
    series = fetch_series(category="Crypto")
    return [
        s for s in series
        if s.get("frequency") in INTERVAL_FREQUENCIES
        and "BTC" in [t.upper() for t in (s.get("tags") or [])]
    ]


def _fetch_markets_with_retry(series_ticker: str, status: str = "open", max_attempts: int = 3) -> list[dict] | None:
    """Retries a single rate-limited series with backoff instead of letting
    fetch_markets' raised HTTPError abort the whole discovery pass -- same
    fix as resolution_alpha/live/discovery.py's _fetch_markets_with_retry.
    `status` also lets backtest.py reuse this for status="settled" scans.
    """
    for attempt in range(max_attempts):
        try:
            return fetch_markets(series_ticker=series_ticker, status=status)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429 and attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("giving up on %s after %d attempt(s): %s", series_ticker, attempt + 1, exc)
            return None
    return None


def find_active_btc_markets() -> list[ActiveMarket]:
    """Discovers every currently-open BTC interval market. Safe to call
    repeatedly -- each call is a fresh scan with no caching.
    """
    active: list[ActiveMarket] = []
    for series in find_btc_interval_series():
        markets = _fetch_markets_with_retry(series["ticker"])
        if markets is None:
            continue
        for market in markets:
            parsed = _extract_strike(market)
            if parsed is None:
                continue
            direction, strike = parsed
            try:
                open_time = _parse_time(market["open_time"])
                close_time = _parse_time(market["close_time"])
            except (KeyError, ValueError):
                continue
            active.append(ActiveMarket(
                ticker=market["ticker"],
                event_ticker=market.get("event_ticker", ""),
                series_ticker=series["ticker"],
                direction=direction,
                strike=strike,
                open_time=open_time,
                close_time=close_time,
                settlement_average_seconds=float(market.get("settlement_timer_seconds") or 0.0),
                yes_bid=float(market.get("yes_bid_dollars") or 0.0),
                yes_ask=float(market.get("yes_ask_dollars") or 0.0),
            ))
    return active


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    for m in find_active_btc_markets():
        seconds_left = (m.close_time - now).total_seconds()
        print(
            f"{m.ticker:28s} {m.direction:5s} strike={m.strike:<12.2f} "
            f"bid={m.yes_bid:.2f} ask={m.yes_ask:.2f} closes_in={seconds_left:.0f}s"
        )
