"""Finds Kalshi's open daily high/low temperature markets and parses each
into a TempMarket (station, kind, climate day, strike, quotes).

Series are discovered from the "Climate and Weather" category, keeping daily
KXHIGH*/KXLOW* series. The settlement station and whether it's a high or a
low come from each market's rules_primary ("If the maximum temperature
recorded at New York City (CLINYC) for Nov 8, 2026, is greater than 68...").

Strike shapes (confirmed on KXHIGHNY 2026-11-08):
- ``greater`` floor f: YES if the value > f ("69 or above" for f=68).
- ``less`` cap c: YES if the value < c ("60 or below" for c=61).
- ``between`` floor f, cap c: YES if f <= value <= c ("61 to 62").
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import requests

from kalshi_gateway import BASE_URL
from stations import Station, station_for_cli

logger = logging.getLogger("weather_lock.discovery")

CATEGORY = "Climate and Weather"
_SERIES_RE = re.compile(r"^KX(HIGH|LOW)")
_CLI_RE = re.compile(r"\((CLI[A-Z]{3})\)")
_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_date(event_ticker: str) -> date | None:
    """KXHIGHNY-26NOV08 -> 2026-11-08."""
    m = _DATE_RE.search(event_ticker.upper())
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def kind_from_rules(rules: str) -> str | None:
    text = rules.lower()
    if "maximum temperature" in text or "highest temperature" in text:
        return "high"
    if "minimum temperature" in text or "lowest temperature" in text:
        return "low"
    return None


@dataclass
class TempMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    station: Station
    kind: str                  # "high" or "low"
    day: date
    strike_type: str           # greater / less / between
    floor: float | None
    cap: float | None
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float
    yes_ask_size: float
    tick: float
    close_time: datetime
    settle_time: datetime      # expected_expiration_time, when the cash comes back
    exchange_index: int | None
    subtitle: str

    def ask(self, side: str) -> tuple[float | None, float]:
        """Best price to buy `side` and the size there."""
        if side == "yes":
            return self.yes_ask, self.yes_ask_size
        if self.yes_bid is None or self.yes_bid <= 0:
            return None, 0.0
        return round(1.0 - self.yes_bid, 4), self.yes_bid_size


def parse_market(m: dict, series_ticker: str) -> TempMarket | None:
    rules = m.get("rules_primary") or ""
    cli = _CLI_RE.search(rules)
    station = station_for_cli(cli.group(1)) if cli else None
    kind = kind_from_rules(rules)
    day = event_date(m.get("event_ticker", ""))
    strike_type = m.get("strike_type")
    if station is None or kind is None or day is None or strike_type not in ("greater", "less", "between"):
        return None
    try:
        tick = float((m.get("price_ranges") or [{}])[0].get("step") or 0.01)
    except (TypeError, ValueError):
        tick = 0.01
    return TempMarket(
        ticker=m["ticker"],
        event_ticker=m["event_ticker"],
        series_ticker=series_ticker,
        station=station,
        kind=kind,
        day=day,
        strike_type=strike_type,
        floor=_f(m.get("floor_strike")),
        cap=_f(m.get("cap_strike")),
        yes_bid=_f(m.get("yes_bid_dollars")),
        yes_ask=_f(m.get("yes_ask_dollars")),
        yes_bid_size=_f(m.get("yes_bid_size_fp")) or 0.0,
        yes_ask_size=_f(m.get("yes_ask_size_fp")) or 0.0,
        tick=tick,
        close_time=_parse_time(m["close_time"]),
        settle_time=_parse_time(m.get("expected_expiration_time") or m["close_time"]),
        exchange_index=m.get("exchange_index"),
        subtitle=m.get("yes_sub_title") or "",
    )


def find_temperature_series(allowlist: tuple[str, ...] = (), blocklist: tuple[str, ...] = ()) -> list[str]:
    series = _get("/series", {"category": CATEGORY}).get("series", [])
    out = []
    for s in series:
        t = s.get("ticker", "").upper()
        if s.get("frequency") != "daily" or not _SERIES_RE.match(t):
            continue
        if allowlist and t not in allowlist:
            continue
        if t in blocklist:
            continue
        out.append(t)
    return sorted(out)


def fetch_open_markets(series_ticker: str) -> list[TempMarket]:
    markets: list[TempMarket] = []
    cursor = None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        for m in data.get("markets", []):
            parsed = parse_market(m, series_ticker)
            if parsed is not None:
                markets.append(parsed)
        cursor = data.get("cursor")
        if not cursor:
            return markets


def find_open_markets(series: list[str]) -> list[TempMarket]:
    out: list[TempMarket] = []
    for s in series:
        try:
            out.extend(fetch_open_markets(s))
        except Exception:
            logger.exception("%s: market fetch failed this tick", s)
    return out
