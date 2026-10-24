"""Kalshi public REST reader for the index series, throttled and cached.

Everything goes through `get()`, which spaces requests at least
`MIN_INTERVAL` seconds apart (<= ~3.3 req/s -- the repo's agents share one
outbound IP and Kalshi's public limit), backs off exponentially on 429/5xx,
and caches every successful JSON response under data/kalshi/ keyed on the
path + params, so a re-run never refetches.

Kalshi splits settled markets at the historical cutoff (`/historical/cutoff`,
2026-08-25 as of 2026-10-24): markets settled after it are under /markets and
/series/{s}/markets/{t}/candlesticks (or the batch /markets/candlesticks);
older ones under /historical/markets and /historical/markets/{t}/candlesticks.
/events lists every event regardless of the cutoff.
"""

import gzip
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data" / "kalshi"
MIN_INTERVAL = 0.3

_lock = threading.Lock()
_last = [0.0]


def _cache_path(path: str, params: dict) -> Path:
    key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    h = hashlib.sha1(key.encode()).hexdigest()[:20]
    safe = path.strip("/").replace("/", "_")[:80]
    return DATA_DIR / f"{safe}_{h}.json.gz"


def _throttle():
    with _lock:
        wait = _last[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def get(path: str, cache: bool = True, **params) -> dict:
    cp = _cache_path(path, params)
    if cache and cp.exists():
        with gzip.open(cp, "rt") as f:
            return json.load(f)
    if os.environ.get("IRV_CACHE_ONLY"):
        return {}
    delay = 1.0
    for attempt in range(12):
        _throttle()
        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=60)
        except requests.RequestException:
            time.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        if resp.status_code == 404:
            data = {"_status": 404}
        else:
            resp.raise_for_status()
            data = resp.json()
        if cache:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cp.with_suffix(".tmp")
            with gzip.open(tmp, "wt") as f:
                json.dump(data, f)
            tmp.replace(cp)
        return data
    raise RuntimeError(f"gave up on {path} {params}")


def list_events(series: str) -> list[dict]:
    """Every settled event of a series (cached per page; the first page is
    re-fetched each day so new events show up)."""
    out, cursor = [], None
    day = time.strftime("%Y-%m-%d")
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        else:
            params["_day"] = day  # cache-buster for page 1 only (ignored by the API)
        d = get("/events", **params)
        evs = d.get("events", [])
        out.extend(evs)
        cursor = d.get("cursor")
        if not cursor or not evs:
            break
    return out


def event_markets(event_ticker: str) -> list[dict]:
    """All markets of one settled event, from the live endpoint, falling back
    to /historical/markets for events before the cutoff."""
    d = get("/markets", event_ticker=event_ticker, limit=1000)
    ms = d.get("markets", [])
    if not ms:
        d = get("/historical/markets", event_ticker=event_ticker, limit=1000)
        ms = d.get("markets", [])
    return ms


def parse_candles(cands: list[dict]) -> list[list[float]]:
    """[end_ts, yes_bid_close, yes_ask_close] per period (live and historical
    responses name the fields differently: close_dollars vs close)."""
    rows = []
    for c in cands:
        try:
            b, a = c["yes_bid"], c["yes_ask"]
            bid = b.get("close_dollars", b.get("close"))
            ask = a.get("close_dollars", a.get("close"))
            if bid is None or ask is None:
                continue
            bid, ask = float(bid), float(ask)
            if bid > 1.0 or ask > 1.0:  # historical endpoint in cents
                bid, ask = bid / 100.0, ask / 100.0
        except (KeyError, TypeError, ValueError):
            continue
        rows.append([float(c["end_period_ts"]), bid, ask])
    return rows


def market_candles(series: str, ticker: str, start_ts: int, end_ts: int, historical: bool,
                   period: int = 1) -> list[list[float]]:
    if historical:
        d = get(f"/historical/markets/{ticker}/candlesticks", start_ts=start_ts, end_ts=end_ts,
                period_interval=period)
    else:
        d = get(f"/series/{series}/markets/{ticker}/candlesticks", start_ts=start_ts, end_ts=end_ts,
                period_interval=period)
    return parse_candles(d.get("candlesticks", []))


def batch_candles(tickers: list[str], start_ts: int, end_ts: int, period: int = 1) -> dict:
    """ticker -> rows via the live batch endpoint (<=100 tickers, <=10k candles)."""
    d = get("/markets/candlesticks", market_tickers=",".join(tickers), start_ts=start_ts, end_ts=end_ts,
            period_interval=period)
    out = {}
    for row in d.get("markets", []):
        out[row["market_ticker"]] = parse_candles(row.get("candlesticks", []))
    return out
