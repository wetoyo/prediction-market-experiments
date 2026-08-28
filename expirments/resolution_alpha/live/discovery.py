"""Auto-discovers currently-open Kalshi crypto interval markets to trade.

Scans every series in the Crypto category, keeps the ones on a recurring
interval cadence (Kalshi's `frequency` field), and returns the currently
open market instances with the fields the strategy needs (strike,
direction, close time, underlying symbol). No hardcoded BTC/ETH list --
whatever qualifying series Kalshi has live shows up automatically.

Confirmed against the live API on 2026-08-04: series like KXBTC15M/KXETH15M
report frequency="fifteen_min", KXBTCD/KXETHD/KXXRPD/KXDOGED etc report
frequency="hourly". Each market's `floor_strike`/`cap_strike` + `strike_type`
gives the resolution strike; `close_time` is when the 60-second settlement
average (see ./README.md) ends.

"Range"-type markets (strike_type == "between", e.g. KXBTC "Bitcoin range")
are out of scope for v1 -- _extract_strike returns None for them and they're
silently skipped.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import INTERVAL_FREQUENCIES, KALSHI_UNDERLYING_TO_COINBASE_PRODUCT
from kalshi_gateway import fetch_markets, fetch_series

logger = logging.getLogger("resolution_alpha.discovery")

_FREQUENCY_TO_MINUTES = {"fifteen_min": 15, "thirty_min": 30, "hourly": 60}


@dataclass
class ActiveMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    underlying: str
    direction: str  # "above" or "below"
    strike: float
    open_time: datetime
    close_time: datetime
    interval_minutes: float


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_underlying(series: dict) -> str | None:
    for tag in series.get("tags") or []:
        symbol = tag.upper()
        if symbol in KALSHI_UNDERLYING_TO_COINBASE_PRODUCT:
            return symbol
    return None


def _extract_strike(market: dict) -> tuple[str, float] | None:
    strike_type = market.get("strike_type")
    if strike_type in ("greater", "greater_or_equal"):
        floor = market.get("floor_strike")
        return ("above", float(floor)) if floor is not None else None
    if strike_type in ("less", "less_or_equal"):
        cap = market.get("cap_strike")
        return ("below", float(cap)) if cap is not None else None
    return None


def find_crypto_interval_series(allowed_underlyings: frozenset[str] | None = None) -> list[dict]:
    """Returns Crypto-category series on a recurring interval cadence, with a
    resolvable underlying symbol (i.e. one we can get a spot price for).

    `allowed_underlyings`, when given, further restricts the result to series
    whose underlying is in that set. The live loop passes
    config.TRUSTED_SETTLEMENT_UNDERLYINGS here unless
    config.TRADE_UNSAFE_MARKETS is on, so the ~20 crypto series it would only
    ever skip aren't even discovered (and so never get spot-polled,
    order-book-subscribed, or evaluated). None means no such restriction --
    every qualifying series is returned, the original behavior.
    """
    series = fetch_series(category="Crypto")
    out = []
    for s in series:
        if s.get("frequency") not in INTERVAL_FREQUENCIES:
            continue
        underlying = _extract_underlying(s)
        if underlying is None:
            continue
        if allowed_underlyings is not None and underlying not in allowed_underlyings:
            continue
        out.append(s)
    return out


def _fetch_markets_with_retry(series_ticker: str, max_attempts: int = 3) -> list[dict] | None:
    """A single rate-limited series used to abort the *entire* discovery
    cycle (fetch_markets raises mid-pagination, uncaught, out of the loop in
    find_active_markets) -- observed live under concurrent load (dry-run
    loop + backtest.py hitting Kalshi's public API at the same time) on
    2026-08-05. Retries with backoff per series instead, and returns None
    (skip this series this cycle) rather than losing the whole discovery
    pass if Kalshi is still rate-limiting after retries.
    """
    for attempt in range(max_attempts):
        try:
            return fetch_markets(series_ticker=series_ticker, status="open")
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429 and attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("giving up on %s after %d attempt(s): %s", series_ticker, attempt + 1, exc)
            return None
    return None


def find_active_markets(allowed_underlyings: frozenset[str] | None = None) -> list[ActiveMarket]:
    """Discovers every currently-open market across all recurring crypto
    interval series. Safe to call repeatedly -- each call is a fresh scan
    with no caching, so freshly-opened market instances show up on the next
    call automatically.

    `allowed_underlyings` is forwarded to find_crypto_interval_series -- see
    there. Pass config.TRUSTED_SETTLEMENT_UNDERLYINGS (the live loop's
    default) to scan only trusted underlyings; None scans every one.
    """
    active: list[ActiveMarket] = []
    for series in find_crypto_interval_series(allowed_underlyings):
        underlying = _extract_underlying(series)
        # Kalshi's declared cadence, not (close_time - open_time): hourly series
        # pre-list occurrences days ahead, so that delta reflects how far in
        # advance a market was listed, not its actual settlement window.
        interval_minutes = _FREQUENCY_TO_MINUTES.get(series.get("frequency"), 0)
        markets = _fetch_markets_with_retry(series["ticker"])
        if markets is None:
            continue  # rate-limited even after retries -- skip this series for this cycle
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
                underlying=underlying,
                direction=direction,
                strike=strike,
                open_time=open_time,
                close_time=close_time,
                interval_minutes=interval_minutes,
            ))
    return active


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    for m in find_active_markets():
        seconds_left = (m.close_time - now).total_seconds()
        print(
            f"{m.ticker:30s} {m.underlying:5s} {m.direction:5s} "
            f"strike={m.strike:<12} interval={m.interval_minutes:.0f}m closes_in={seconds_left:.0f}s"
        )
