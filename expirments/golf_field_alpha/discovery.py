"""Discovers currently-open Kalshi golf outright-winner events (one golf
tournament = one event, one binary YES/NO market per entrant, exactly one
resolving YES) and packages each as a GolfEvent with its field of players
and their live quotes.

Scoped to the series in config.WINNER_SERIES -- a hardcoded-but-overridable
whitelist of outright-winner series (see that config's docstring for why a
whitelist rather than an API-field filter). Mirrors the discovery pattern
in ../resolution_alpha/live/discovery.py and
../btc_implied_prob/kalshi_btc_markets.py, adapted from "recurring interval
series" to "tournament winner series".

Player identity comes from `yes_sub_title` (e.g. "Scottie Scheffler"),
falling back to `custom_strike["Golfer"]`. Quotes come from
`yes_bid_dollars` / `yes_ask_dollars` (the price to BUY one YES contract is
`yes_ask`); `last_price_dollars` is a fallback single-price signal used for
the de-vig when a two-sided quote is missing.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import WINNER_SERIES
from kalshi_gateway import fetch_markets, fetch_series

logger = logging.getLogger("golf_field_alpha.discovery")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _first_float(*values) -> float | None:
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        return f
    return None


@dataclass
class PlayerMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    name: str
    yes_bid: float | None   # best bid to sell YES into (dollars, 0-1)
    yes_ask: float | None   # best ask to buy YES at    (dollars, 0-1)
    last_price: float | None

    @property
    def buy_price(self) -> float | None:
        """What it costs to buy one YES contract right now, or None if there
        is no usable ask.
        """
        if self.yes_ask is not None and 0.0 < self.yes_ask < 1.0:
            return self.yes_ask
        return None

    @property
    def implied(self) -> float | None:
        """A single raw win-probability estimate for the de-vig: the two-
        sided mid if both quotes are present, else the last trade price.
        None if neither is usable.
        """
        if (
            self.yes_bid is not None and self.yes_ask is not None
            and 0.0 <= self.yes_bid < 1.0 and 0.0 < self.yes_ask <= 1.0
            and self.yes_ask >= self.yes_bid
        ):
            return (self.yes_bid + self.yes_ask) / 2.0
        if self.last_price is not None and 0.0 < self.last_price < 1.0:
            return self.last_price
        return None


@dataclass
class GolfEvent:
    event_ticker: str
    series_ticker: str
    title: str
    close_time: datetime
    players: list[PlayerMarket]

    def seconds_to_close(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.close_time - now).total_seconds()


_TITLE_RE = re.compile(r"^Will\s+.+?\s+win\s+(?:the\s+)?(.*?)\??$", re.IGNORECASE)


def _tournament_title(market_title: str, event_ticker: str) -> str:
    """Per-player market titles read "Will <player> win the <Tournament>?" --
    strip the player-specific framing down to just the tournament name for
    the event-level label. Falls back to the raw title, then the ticker.
    """
    if not market_title:
        return event_ticker
    m = _TITLE_RE.match(market_title.strip())
    if m and m.group(1):
        return m.group(1).strip()
    return market_title


def _player_name(market: dict) -> str:
    name = market.get("yes_sub_title")
    if name:
        return name
    strike = market.get("custom_strike") or {}
    for v in strike.values():
        if v:
            return str(v)
    return market.get("ticker", "?")


def _fetch_markets_with_retry(series_ticker: str, status: str = "open", max_attempts: int = 3) -> list[dict] | None:
    """Retries a rate-limited series with backoff instead of letting
    fetch_markets' raised HTTPError abort the whole discovery pass -- same
    fix as the crypto experiments' discovery modules.
    """
    for attempt in range(max_attempts):
        try:
            return fetch_markets(series_ticker=series_ticker, status=status, max_pages=25)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429 and attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("giving up on %s after %d attempt(s): %s", series_ticker, attempt + 1, exc)
            return None
    return None


def find_winner_series() -> list[dict]:
    """The subset of config.WINNER_SERIES that actually exists in Kalshi's
    Sports catalogue right now (a renamed/retired ticker in the whitelist
    is silently dropped rather than erroring).
    """
    want = {s.upper() for s in WINNER_SERIES}
    catalogue = {s.get("ticker", "").upper(): s for s in fetch_series(category="Sports")}
    found = [catalogue[t] for t in want if t in catalogue]
    missing = want - set(catalogue)
    if missing:
        logger.info("WINNER_SERIES not found in Kalshi catalogue (skipped): %s", ", ".join(sorted(missing)))
    return found


def find_open_golf_events() -> list[GolfEvent]:
    """Every currently-open outright-winner event across the whitelisted
    series, each with its full field of PlayerMarkets. Safe to call
    repeatedly -- a fresh scan each time, no caching.
    """
    events: dict[str, GolfEvent] = {}
    for series in find_winner_series():
        series_ticker = series["ticker"]
        markets = _fetch_markets_with_retry(series_ticker, status="open")
        if not markets:
            continue
        for market in markets:
            event_ticker = market.get("event_ticker")
            if not event_ticker:
                continue
            try:
                close_time = _parse_time(market["close_time"])
            except (KeyError, ValueError):
                continue

            player = PlayerMarket(
                ticker=market["ticker"],
                event_ticker=event_ticker,
                series_ticker=series_ticker,
                name=_player_name(market),
                yes_bid=_first_float(market.get("yes_bid_dollars")),
                yes_ask=_first_float(market.get("yes_ask_dollars")),
                last_price=_first_float(market.get("last_price_dollars"), market.get("previous_price_dollars")),
            )

            ev = events.get(event_ticker)
            if ev is None:
                events[event_ticker] = GolfEvent(
                    event_ticker=event_ticker,
                    series_ticker=series_ticker,
                    title=_tournament_title(market.get("title", ""), event_ticker),
                    # Player markets in one event can carry slightly different
                    # close_times (per-player early close on withdrawal); the
                    # event closes when the tournament resolves, so take the
                    # latest as the event close.
                    close_time=close_time,
                    players=[player],
                )
            else:
                ev.players.append(player)
                if close_time > ev.close_time:
                    ev.close_time = close_time

    return sorted(events.values(), key=lambda e: e.close_time)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = datetime.now(timezone.utc)
    for ev in find_open_golf_events():
        quoted = [p for p in ev.players if p.implied is not None]
        overround = sum(p.implied for p in quoted) if quoted else 0.0
        hrs = ev.seconds_to_close(now) / 3600.0
        print(
            f"{ev.event_ticker:32s} {ev.series_ticker:14s} players={len(ev.players):3d} "
            f"quoted={len(quoted):3d} overround={overround:.3f} closes_in={hrs:6.1f}h  {ev.title}"
        )
        top = sorted(quoted, key=lambda p: -(p.implied or 0))[:5]
        for p in top:
            print(f"    {p.name:26s} bid={p.yes_bid} ask={p.yes_ask} last={p.last_price} implied={p.implied:.3f}")
