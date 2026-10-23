"""KXBTCPERP history from Kalshi's public API, cached under data/.

Candles: 1-minute bid/ask OHLC (real quotes, so the spread is in the fills).
Funding: the perp's 8-hourly funding payments. History starts ~2026-06
(the perp's launch); earlier requests come back empty.
"""

import gzip
import json
import time
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
