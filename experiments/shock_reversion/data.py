"""Backtest data for shock_reversion: a cross-category sample of settled
Kalshi markets and their hourly yes bid/ask candles, from the public REST API.

Everything is cached gzip-JSON under data/ (gitignored). A rerun reads the
cache and only touches the network for what is missing.

Sampling (see README "Data"):
  1. /series (one call): category, frequency, fee_type/fee_multiplier.
     Keep the categories whose underlying is not a continuously quoted
     price (drop Crypto, Financials, Commodities), drop hourly / 15-min /
     daily series (lifetime too short, mostly price-level ladders), and drop
     series whose title names a price level (gas, oil, index, yields, ...).
  2. /events?status=settled&min_close_ts=<window start> (paginated): every
     settled non-multivariate event with a market closing in the window.
     Live-set events come with their markets nested; events archived before
     the historical cutoff (/historical/cutoff, 2026-08-25 as of 2026-10-24)
     need /historical/markets?event_ticker=... one call each. To stop the
     big game-by-game sports series from swamping the sample, at most
     MAX_EVENTS_PER_SERIES events per series are taken (seeded random).
  3. Markets: settled yes/no, volume >= MIN_VOLUME, lifetime >= MIN_LIFE_DAYS,
     at most MAX_MARKETS_PER_EVENT per event (highest volume first).
  4. Hourly candles for each kept market over [max(open, window start),
     close]: /series/{s}/markets/{t}/candlesticks for the live set,
     /historical/markets/{t}/candlesticks for archived ones.

All requests go through one throttle (<= REQ_PER_SEC across threads, since
several agents share the outbound IP) with exponential backoff on 429/5xx.
"""

from __future__ import annotations

import gzip
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data"

REQ_PER_SEC = 3.5
WINDOW_START = "2025-10-24"           # ~12 months back from 2026-10-24
KEEP_CATEGORIES = {"Politics", "Elections", "Economics", "Entertainment", "Sports", "Climate and Weather",
                   "Science and Technology", "Companies", "World", "Health", "Social", "Transportation",
                   "Mentions", "Culture", "Education", "Business"}
DROP_FREQUENCIES = {"hourly", "fifteen_min", "daily"}
PRICE_LEVEL_RE = re.compile(
    r"\b(price|prices|gas|oil|gold|silver|index|s&p|nasdaq|dow|yield|treasury|stock|shares|bitcoin|btc|eth|"
    r"ethereum|crypto|euro|dollar|yen|forex|fx|exchange rate|close above|closing|market cap|wti|brent|"
    r"temperature|high temp|low temp)\b", re.I)
MAX_EVENTS_PER_SERIES = 12
MAX_MARKETS_PER_EVENT = 4
MIN_VOLUME = 2000.0
MIN_LIFE_DAYS = 3.0
SEED = 7

_session = requests.Session()
_lock = threading.Lock()
_last = [0.0]
_count = [0]


def _throttle() -> None:
    with _lock:
        wait = _last[0] + 1.0 / REQ_PER_SEC - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        _count[0] += 1


def get(path: str, params: dict | None = None, retries: int = 20) -> dict:
    for attempt in range(retries):
        _throttle()
        try:
            resp = _session.get(BASE_URL + path, params=params or {}, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(0.5 * 2 ** attempt, 60))
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(0.5 * 2 ** attempt, 60))
    raise RuntimeError(f"gave up: {path} {params}")


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def day_ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def load(name: str):
    path = DATA_DIR / name
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
        return json.load(f)


def save(name: str, obj) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(obj, f)
    tmp.replace(path)


def cached(name: str, fetch):
    obj = load(name)
    if obj is None:
        obj = fetch()
        save(name, obj)
    return obj


# ------------------------------------------------------------------ series

def load_series() -> dict:
    """series ticker -> {category, frequency, title, fee_type, fee_multiplier}."""
    def fetch():
        rows = get("/series").get("series", [])
        return {s["ticker"]: {k: s.get(k) for k in ("category", "frequency", "title", "fee_type", "fee_multiplier")}
                for s in rows}
    return cached("series.json.gz", fetch)


def series_eligible(s: dict) -> bool:
    if s.get("category") not in KEEP_CATEGORIES or s.get("frequency") in DROP_FREQUENCIES:
        return False
    return not PRICE_LEVEL_RE.search(s.get("title") or "")


def load_cutoff() -> float:
    d = cached("cutoff.json.gz", lambda: get("/historical/cutoff"))
    return ts(d["market_settled_ts"])


# ------------------------------------------------------------------ events

_MKT_KEEP = ("ticker", "event_ticker", "result", "open_time", "close_time", "expected_expiration_time",
             "occurrence_datetime", "yes_sub_title", "title")


def _slim_market(m: dict) -> dict:
    row = {k: m.get(k) for k in _MKT_KEEP}
    row["volume"] = float(m.get("volume_fp") or m.get("volume") or 0)
    return row


def load_events(series: dict, log=print) -> list[dict]:
    """Every settled event of an eligible series with a market closing after
    WINDOW_START. Live-set events carry `markets` (nested); archived ones
    carry none yet."""
    name = f"events_{WINDOW_START}.json.gz"
    got = load(name)
    if got is not None:
        return got
    part = load(name + ".part") or {"out": [], "cursor": None, "page": 0}
    out, cursor, page = part["out"], part["cursor"], part["page"]
    while page == 0 or cursor:
        params = {"status": "settled", "limit": 200, "min_close_ts": day_ts(WINDOW_START),
                  "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        d = get("/events", params)
        for e in d.get("events", []) or []:
            if not series_eligible(series.get(e["series_ticker"]) or {}):
                continue
            out.append({"event_ticker": e["event_ticker"], "series_ticker": e["series_ticker"],
                        "category": e.get("category"), "title": e.get("title"),
                        "markets": [_slim_market(m) for m in (e.get("markets") or [])]})
        cursor = d.get("cursor")
        page += 1
        if page % 100 == 0:
            save(name + ".part", {"out": out, "cursor": cursor, "page": page})
            log(f"  events: {page} pages, {len(out)} events")
        if not d.get("events"):
            break
    save(name, out)
    (DATA_DIR / (name + ".part")).unlink(missing_ok=True)
    return out


def sample_events(events: list[dict], series: dict) -> list[dict]:
    """Eligible-series events, at most MAX_EVENTS_PER_SERIES per series."""
    by_series: dict[str, list[dict]] = {}
    for e in events:
        s = series.get(e["series_ticker"])
        if s and series_eligible(s):
            by_series.setdefault(e["series_ticker"], []).append(e)
    rng = random.Random(SEED)
    out = []
    for st in sorted(by_series):
        evs = sorted(by_series[st], key=lambda e: e["event_ticker"])
        # prefer events whose nested markets already show volume; archived
        # events (no nested markets) are unknown, keep them eligible
        evs = [e for e in evs if not e["markets"] or max(m["volume"] for m in e["markets"]) >= MIN_VOLUME]
        rng.shuffle(evs)
        out.extend(evs[:MAX_EVENTS_PER_SERIES])
    return out


def load_event_markets(evs: list[dict], log=print) -> dict:
    """event ticker -> [slim market] for archived events (one call each)."""
    name = "hist_event_markets.json.gz"
    cache = load(name) or {}
    todo = [e for e in evs if not e["markets"] and e["event_ticker"] not in cache]
    log(f"historical event markets: {len(todo)} to fetch ({len(cache)} cached)")

    def one(e):
        d = get("/historical/markets", {"event_ticker": e["event_ticker"], "limit": 1000})
        return e["event_ticker"], [_slim_market(m) for m in d.get("markets", []) or []]

    with ThreadPoolExecutor(3) as ex:
        for i, (k, v) in enumerate(ex.map(one, todo)):
            cache[k] = v
            if i % 300 == 299:
                save(name, cache)
                log(f"  {i + 1}/{len(todo)}")
    save(name, cache)
    return cache


def select_markets(evs: list[dict], hist: dict, cutoff: float) -> list[dict]:
    w0 = day_ts(WINDOW_START)
    out = []
    for e in evs:
        ms = e["markets"] or hist.get(e["event_ticker"], [])
        keep = []
        for m in ms:
            if m.get("result") not in ("yes", "no") or not m.get("open_time") or not m.get("close_time"):
                continue
            o, c = ts(m["open_time"]), ts(m["close_time"])
            if c < w0 or m["volume"] < MIN_VOLUME or (c - max(o, w0)) < MIN_LIFE_DAYS * 86400:
                continue
            keep.append(dict(m, series_ticker=e["series_ticker"], category=e["category"],
                             historical=c < cutoff and not e["markets"]))
        keep.sort(key=lambda m: -m["volume"])
        out.extend(keep[:MAX_MARKETS_PER_EVENT])
    return out


# ------------------------------------------------------------------ candles

def _px(side: dict | None):
    if not side:
        return None
    v = side.get("close_dollars", side.get("close"))
    if v is None:
        return None
    v = float(v)
    return v / 100.0 if v > 1.0 else v


def parse_candles(cands: list[dict]) -> list[list]:
    """[end_ts, yes_bid_close, yes_ask_close, volume] per hour, dollars.
    None where that side of the book was empty."""
    rows = []
    for c in cands:
        vol = c.get("volume_fp", c.get("volume", 0)) or 0
        rows.append([int(c["end_period_ts"]), _px(c.get("yes_bid")), _px(c.get("yes_ask")), float(vol)])
    return rows


def fetch_candles(m: dict) -> list[list]:
    start = int(max(ts(m["open_time"]), day_ts(WINDOW_START)))
    end = int(ts(m["close_time"]))
    path = (f"/historical/markets/{m['ticker']}/candlesticks" if m["historical"]
            else f"/series/{m['series_ticker']}/markets/{m['ticker']}/candlesticks")
    rows, t = [], start
    while t < end:
        hi = min(end, t + 3000 * 3600)  # stay under the per-request candle cap
        d = get(path, {"start_ts": t, "end_ts": hi, "period_interval": 60})
        rows.extend(parse_candles(d.get("candlesticks", []) or []))
        t = hi
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r[0]):
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r)
    return out


def load_candles(markets: list[dict], log=print) -> dict:
    name = "candles.json.gz"
    cache = load(name) or {}
    todo = [m for m in markets if m["ticker"] not in cache]
    log(f"candles: {len(todo)} markets to fetch ({len(cache)} cached)")

    def one(m):
        try:
            return m["ticker"], fetch_candles(m)
        except Exception as e:  # one bad market shouldn't kill a long fetch
            log(f"  candles {m['ticker']}: {e}")
            return m["ticker"], None

    with ThreadPoolExecutor(3) as ex:
        for i, (k, v) in enumerate(ex.map(one, todo)):
            if v is not None:
                cache[k] = v
            if i % 300 == 299:
                save(name, cache)
                log(f"  {i + 1}/{len(todo)}")
    save(name, cache)
    return cache


def build_sample(log=print) -> tuple[list[dict], dict, dict]:
    """(markets, candles by ticker, series meta). The whole pipeline."""
    series = load_series()
    cutoff = load_cutoff()
    events = load_events(series, log)
    evs = sample_events(events, series)
    log(f"events: {len(events)} settled since {WINDOW_START} in eligible series; {len(evs)} sampled")
    hist = load_event_markets(evs, log)
    markets = select_markets(evs, hist, cutoff)
    log(f"markets: {len(markets)} pass volume/lifetime filters")
    candles = load_candles(markets, log)
    return markets, candles, series


if __name__ == "__main__":
    t0 = time.time()
    ms, cs, _ = build_sample(lambda s: print(s, flush=True))
    print(f"done: {len(ms)} markets, {sum(1 for m in ms if cs.get(m['ticker']))} with candles, "
          f"{_count[0]} requests, {time.time() - t0:.0f}s")
