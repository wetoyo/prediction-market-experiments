"""NWS station observations -> hard bounds on a climate day's high and low.

The observed maximum so far is a floor on the day's final high (the high can
only go up), and the observed minimum so far is a ceiling on the final low.
That's all this module computes; it makes no forecast.

Readings come from each report's raw METAR, not the API's converted value,
so the precision is known:

- T-group (``T01330044`` = 13.3C): 0.1C, so +-0.05C.
- body group (``13/04``): whole degrees C, so +-0.5C (about +-0.9F).
- 6-hour max/min groups in the remarks (``10144`` = 6h max 14.4C,
  ``20089`` = 6h min 8.9C): 0.1C, used only when the whole 6-hour window
  sits inside the climate day.

Same endpoint as the ``nws`` vendor in the vendors repo
(``vendors/nws``, ``observations`` dataset), called directly so this
runner has no extra dependency.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from stations import Station

logger = logging.getLogger("weather_lock.nws_obs")

BASE_URL = "https://api.weather.gov"

_T_GROUP = re.compile(r"\bT([01])(\d{3})")
_BODY_TEMP = re.compile(r"\s(M?\d{2})/(M?\d{2})?(?=\s)")
_MAX6 = re.compile(r"\s1([01])(\d{3})(?=\s|$)")
_MIN6 = re.compile(r"\s2([01])(\d{3})(?=\s|$)")
_SIX_HOURS = timedelta(hours=6, minutes=10)  # small slack so a xx:51 report's window is treated conservatively


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _signed_tenths(sign: str, digits: str) -> float:
    v = int(digits) / 10.0
    return -v if sign == "1" else v


def _body_c(token: str) -> float:
    return -float(token[1:]) if token.startswith("M") else float(token)


@dataclass(frozen=True)
class Reading:
    """Bounds on a temperature, in F. `kind`: instant, max6 or min6."""

    kind: str
    time: datetime         # report time (UTC)
    lo_f: float
    hi_f: float
    source: str            # tgroup / body / api / group6


def parse_report(raw: str | None, when: datetime, api_value_c: float | None = None) -> list[Reading]:
    """Every reading in one METAR: the instant temperature, plus any 6-hour
    max/min group."""
    out: list[Reading] = []
    text = f" {raw.strip()} " if raw else ""
    body, _, remarks = text.partition(" RMK ")

    m = _T_GROUP.search(remarks) if remarks else None
    if m:
        c = _signed_tenths(m.group(1), m.group(2))
        out.append(Reading("instant", when, c_to_f(c - 0.05), c_to_f(c + 0.05), "tgroup"))
    else:
        b = _BODY_TEMP.search(body)
        if b:
            c = _body_c(b.group(1))
            out.append(Reading("instant", when, c_to_f(c - 0.5), c_to_f(c + 0.5), "body"))
        elif api_value_c is not None:
            out.append(Reading("instant", when, c_to_f(api_value_c - 0.5), c_to_f(api_value_c + 0.5), "api"))

    if remarks:
        rmk = f" {remarks} "
        for pattern, kind in ((_MAX6, "max6"), (_MIN6, "min6")):
            g = pattern.search(rmk)
            if g:
                c = _signed_tenths(g.group(1), g.group(2))
                out.append(Reading(kind, when, c_to_f(c - 0.05), c_to_f(c + 0.05), "group6"))
    return out


def fetch_readings(station: Station, hours: float = 50, user_agent: str = "weather-lock-runner/0.1") -> list[Reading]:
    """Readings from the last `hours` of reports at `station`."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"{BASE_URL}/stations/{station.icao}/observations",
        params={"start": start, "limit": 500},
        headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
        timeout=30,
    )
    r.raise_for_status()
    readings: list[Reading] = []
    for f in r.json().get("features", []):
        p = f.get("properties") or {}
        temp = p.get("temperature") or {}
        if temp.get("qualityControl") == "X":  # failed NWS quality control
            continue
        try:
            when = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        readings.extend(parse_report(p.get("rawMessage"), when, temp.get("value")))
    return readings


@dataclass(frozen=True)
class DayBounds:
    """What the observations prove about one climate day, in whole F as the
    climate report would print them. None = not enough readings yet."""

    station: str
    day: date
    high_at_least: int | None   # the final high is >= this
    low_at_most: int | None     # the final low is <= this
    n_readings: int
    last_report: datetime | None
    complete: bool              # the climate day has ended


def _kth(values: list[float], k: int, largest: bool) -> float | None:
    if len(values) < max(1, k):
        return None
    return sorted(values, reverse=largest)[max(1, k) - 1]


def day_bounds(station: Station, day: date, readings: list[Reading], min_confirming: int = 2,
               now: datetime | None = None) -> DayBounds:
    start, end = station.day_window(day)
    instants = [r for r in readings if r.kind == "instant" and start <= r.time < end]
    # A 6-hour group counts only if its whole window is inside the day.
    sixes = [r for r in readings if r.kind in ("max6", "min6") and r.time - _SIX_HOURS >= start and r.time <= end]

    high_lo = [r.lo_f for r in instants] + [r.lo_f for r in sixes if r.kind == "max6"]
    low_hi = [r.hi_f for r in instants] + [r.hi_f for r in sixes if r.kind == "min6"]

    floor_f = _kth(high_lo, min_confirming, largest=True)
    ceil_f = _kth(low_hi, min_confirming, largest=False)
    now = now or datetime.now(timezone.utc)
    times = [r.time for r in instants]
    return DayBounds(
        station=station.icao,
        day=day,
        # The report rounds the sensor reading to whole F (half up); the
        # sensor's true max is >= floor_f, so the printed high is >= round(floor_f).
        high_at_least=math.floor(floor_f + 0.5) if floor_f is not None else None,
        low_at_most=math.floor(ceil_f + 0.5) if ceil_f is not None else None,
        n_readings=len(instants),
        last_report=max(times) if times else None,
        complete=now >= end,
    )
