"""KXBTCPERP history from Kalshi's public API, cached under data/.

Candles: 1-minute bid/ask OHLC (real quotes, so the spread is in the fills).
Funding: the perp's 8-hourly funding payments. History starts ~2026-06
(the perp's launch); earlier requests come back empty.

KXBTC15M ("BTC price up in next 15 mins?": YES if the 60s BRTI average at
close >= the one at open): every settled market and its 1-minute yes
bid/ask. Kalshi splits these between the live endpoints (markets settled
after the historical cutoff, ~2026-08-23 as of 2026-10-23) and /historical/
(older ones). The live batch candlestick endpoint caps a request at 100
markets and 10,000 candles in total (markets x the minutes in the range), so
~25 consecutive markets per call. The historical one is per market.
"""

import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data"


def _cached(name: str, fetch):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    if path.exists():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    rows = fetch()
    with gzip.open(path, "wt") as f:
        json.dump(rows, f)
    return rows


def fetch_candles(ticker: str, start_ts: int, end_ts: int) -> list[list[float]]:
    """[end_ts, bid_open, bid_high, bid_low, bid_close, ask_open, ask_high,
    ask_low, ask_close] per minute, ascending. A day per request."""
    rows = []
    t = start_ts
    while t < end_ts:
        chunk_end = min(t + 86400, end_ts)
        for attempt in range(5):
            try:
                resp = requests.get(f"{BASE_URL}/margin/markets/{ticker}/candlesticks",
                                    params={"period_interval": 1, "start_ts": t, "end_ts": chunk_end}, timeout=30)
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        for c in resp.json().get("candlesticks", []):
            try:
                b, a = c["bid"], c["ask"]
                row = [float(c["end_period_ts"])] + [float(b[k]) for k in ("open", "high", "low", "close")] \
                    + [float(a[k]) for k in ("open", "high", "low", "close")]
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < row[4] <= row[8] and 0 < row[1] <= row[5]:
                rows.append(row)
        t = chunk_end
        time.sleep(0.1)
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_funding(ticker: str, start_ts: int, end_ts: int) -> list[list[float]]:
    """[ts, rate, mark per contract] per funding event, ascending."""
    rows = {}
    t = start_ts
    while t < end_ts:
        chunk_end = min(t + 15 * 86400, end_ts)
        resp = requests.get(f"{BASE_URL}/margin/funding_rates/historical",
                            params={"ticker": ticker, "start_ts": t, "end_ts": chunk_end}, timeout=30)
        resp.raise_for_status()
        for f in resp.json().get("funding_rates", []):
            ts = datetime.fromisoformat(f["funding_time"].replace("Z", "+00:00")).timestamp()
            rows[ts] = [ts, float(f["funding_rate"]), float(f["mark_price"])]
        t = chunk_end
    return sorted(rows.values())


def _get(path: str, **params) -> dict:
    for attempt in range(8):
        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=60)
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            if attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"rate-limited: {path}")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fetch_15m_markets(series: str, start_ts: int, end_ts: int) -> list[dict]:
    """Settled markets with open_ts/close_ts in [start_ts, end_ts], ascending."""
    out = {}
    for path in ("/markets", "/historical/markets"):
        cursor = None
        while True:
            params = {"series_ticker": series, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = _get(path, **params)
            rows = d.get("markets", [])
            for m in rows:
                if m.get("result") not in ("yes", "no"):
                    continue
                o, c = _ts(m["open_time"]), _ts(m["close_time"])
                if start_ts <= o and c <= end_ts:
                    out[m["ticker"]] = {"ticker": m["ticker"], "open_ts": o, "close_ts": c, "result": m["result"],
                                        "historical": path != "/markets"}
            cursor = d.get("cursor")
            if not cursor or not rows or min(_ts(m["close_time"]) for m in rows) < start_ts:
                break
            time.sleep(0.2)
    return sorted(out.values(), key=lambda m: m["open_ts"])


def _parse_market_candles(cands: list[dict]) -> list[list[float]]:
    """[end_ts, yes_bid_close, yes_ask_close] per minute (live and historical
    responses name the fields differently)."""
    rows = []
    for c in cands:
        try:
            b, a = c["yes_bid"], c["yes_ask"]
            bid = float(b.get("close_dollars", b.get("close")))
            ask = float(a.get("close_dollars", a.get("close")))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append([float(c["end_period_ts"]), bid, ask])
    return rows


def fetch_15m_candles(markets: list[dict], cache: dict, save, batch: int = 25) -> dict:
    """ticker -> [[end_ts, yes_bid, yes_ask], ...]; resumes from `cache`."""
    todo = [m for m in markets if m["ticker"] not in cache]
    live = [m for m in todo if not m["historical"]]
    for i in range(0, len(live), batch):
        chunk = live[i:i + batch]
        lo, hi = int(chunk[0]["open_ts"]), int(chunk[-1]["close_ts"])
        while len(chunk) * (hi - lo) / 60 > 10000:
            chunk = chunk[:-1]
            hi = int(chunk[-1]["close_ts"])
        d = _get("/markets/candlesticks", market_tickers=",".join(m["ticker"] for m in chunk), start_ts=lo,
                 end_ts=hi, period_interval=1)
        for row in d.get("markets", []):
            cache[row["market_ticker"]] = _parse_market_candles(row.get("candlesticks", []))
        for m in chunk:
            cache.setdefault(m["ticker"], [])
        if i % (batch * 40) == 0:
            save(cache)
        time.sleep(0.15)

    def one(m):
        d = _get(f"/historical/markets/{m['ticker']}/candlesticks", start_ts=int(m["open_ts"]),
                 end_ts=int(m["close_ts"]), period_interval=1)
        return m["ticker"], _parse_market_candles(d.get("candlesticks", []))

    hist = [m for m in todo if m["historical"]]
    with ThreadPoolExecutor(4) as ex:
        for j, (t, rows) in enumerate(ex.map(one, hist)):
            cache[t] = rows
            if j % 500 == 0:
                save(cache)
    save(cache)
    return cache


def load_15m(series: str, start_ts: int, end_ts: int) -> tuple[list[dict], dict]:
    key = f"{series}_{start_ts}_{end_ts}"
    markets = _cached(f"{key}_markets.json.gz", lambda: fetch_15m_markets(series, start_ts, end_ts))
    path = DATA_DIR / f"{key}_candles.json.gz"
    cache = {}
    if path.exists():
        with gzip.open(path, "rt") as f:
            cache = json.load(f)

    def save(c):
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            json.dump(c, f)
        tmp.replace(path)

    if any(m["ticker"] not in cache for m in markets):
        cache = fetch_15m_candles(markets, cache, save)
    return markets, cache


def clean(candles: list[list[float]], max_spread: float = 0.005, max_wick: float = 0.05) -> list[list[float]]:
    """Drop the empty-book placeholders Kalshi leaves in the bid/ask
    high/low (a bid low of 0, an ask high of ~9.2e14 -- int64 max over 1e4),
    seen in the perp's first days. An open or a wick more than `max_wick` beyond the
    candle's own open/close on that side falls back to the open/close, and a
    minute closing with a spread wider than `max_spread` is dropped."""
    out = []
    for ts, bo, bh, bl, bc, ao, ah, al, ac in candles:
        if (ac - bc) / bc > max_spread:
            continue
        bo = bo if abs(bo / bc - 1) <= max_wick else bc
        ao = ao if abs(ao / ac - 1) <= max_wick else ac
        lo_b, hi_b = min(bo, bc), max(bo, bc)
        lo_a, hi_a = min(ao, ac), max(ao, ac)
        bl = bl if bl >= lo_b * (1 - max_wick) else lo_b
        bh = bh if bh <= hi_b * (1 + max_wick) else hi_b
        al = al if al >= lo_a * (1 - max_wick) else lo_a
        ah = ah if ah <= hi_a * (1 + max_wick) else hi_a
        out.append([ts, bo, bh, bl, bc, ao, ah, al, ac])
    return out


def load(ticker: str, start_ts: int, end_ts: int) -> tuple[list[list[float]], list[list[float]]]:
    key = f"{ticker}_{start_ts}_{end_ts}"
    candles = _cached(f"{key}_candles.json.gz", lambda: fetch_candles(ticker, start_ts, end_ts))
    funding = _cached(f"{key}_funding.json.gz", lambda: fetch_funding(ticker, start_ts, end_ts))
    return clean(candles), funding
