"""Kalshi settled-market history for the longshot_fade backtest, cached under data/.

Three layers, each resumable and cached (gzip JSON under data/, gitignored):

1. Universe: settled markets of a chosen set of series (sample.py picks
   them; the whole exchange is ~100k settled markets a day, mostly crypto
   ranges and auto-generated props, too many to page), from the live
   `/markets?status=settled&series_ticker=` (settled after the historical
   cutoff, 2026-08-25 as of 2026-10-24) and `/historical/markets?series_ticker=`
   (older; no time filter, so paged newest-first until settlement is before
   the window). Kept slim: no rules text.
2. Series metadata (`/series`): category, fee_type, fee_multiplier, frequency.
3. Hourly yes_bid / yes_ask candles for a sample of the universe
   (`sample.py` picks it) over the 30 hours before close. Live markets use
   the batch endpoint (100 markets, 10,000 candles per call); historical
   ones are one call per market.

All agents in this repo share one outbound IP, so every request goes
through one throttle (<= 3.5 requests/s across threads) and backs off
exponentially on 429 / 5xx.
"""

import gzip
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data"
MAX_RPS = 3.5
CANDLE_HOURS = 30

_lock = threading.Lock()
_next_slot = [0.0]
_session = requests.Session()


def _throttle():
    with _lock:
        now = time.monotonic()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + 1.0 / MAX_RPS
    if wait > 0:
        time.sleep(wait)


def get(path: str, **params) -> dict:
    """GET with the shared throttle and exponential backoff on 429/5xx/network errors."""
    for attempt in range(10):
        _throttle()
        try:
            resp = _session.get(f"{BASE_URL}{path}", params=params, timeout=60)
        except requests.RequestException as e:
            print(f"  [retry {attempt}] {path}: {type(e).__name__}", flush=True)
            time.sleep(min(2 ** attempt, 60))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            print(f"  [retry {attempt}] {path}: HTTP {resp.status_code}", flush=True)
            time.sleep(min(2 ** attempt, 60))
            continue
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"gave up after retries: {path} {params}")


def ts(iso: str | None) -> float | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _read(path: Path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _write(path: Path, obj):
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(obj, f)
    tmp.replace(path)


def slim(m: dict) -> dict | None:
    """The fields the backtest needs; None for voided / scalar markets."""
    if m.get("result") not in ("yes", "no") or m.get("market_type", "binary") != "binary":
        return None
    return {
        "ticker": m["ticker"],
        "event": m["event_ticker"],
        "series": m["event_ticker"].split("-")[0],
        "open_ts": ts(m.get("open_time")),
        "close_ts": ts(m.get("close_time")),
        "exp_ts": ts(m.get("expected_expiration_time")),
        "settled_ts": ts(m.get("settlement_ts")) or ts(m.get("close_time")),
        "result": m["result"],
        "volume": float(m.get("volume_fp") or m.get("volume") or 0),
        "early": bool(m.get("can_close_early")),
        "strike_type": m.get("strike_type"),
        "title": (m.get("title") or "")[:120],
        "sub": (m.get("yes_sub_title") or "")[:60],
    }


# ---------------------------------------------------------------- universe

def _series_markets(path: str, params: dict, start_ts: float, max_pages: int) -> tuple[list[dict], bool]:
    """Page one series newest-first until settlement < start_ts (or max_pages:
    returns truncated=True)."""
    rows_out, cursor = [], None
    for _ in range(max_pages):
        p = dict(params, limit=1000)
        if cursor:
            p["cursor"] = cursor
        d = get(path, **p)
        rows = d.get("markets", [])
        oldest = None
        for m in rows:
            st = ts(m.get("settlement_ts")) or ts(m.get("close_time"))
            if st is None:
                continue
            oldest = st if oldest is None else min(oldest, st)
            if st >= start_ts:
                s = slim(m)
                if s:
                    rows_out.append(s)
        cursor = d.get("cursor")
        if not rows or not cursor or (oldest is not None and oldest < start_ts):
            return rows_out, False
    return rows_out, True


def load_universe(series: list[str], start_ts: float, max_pages: dict | None = None,
                  name: str = "universe") -> dict:
    """series -> {"markets": [...], "truncated": bool}, settled since start_ts,
    live + historical. Checkpointed every 50 series."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{name}_{int(start_ts)}.json.gz"
    out = _read(path) if path.exists() else {}
    todo = [s for s in series if s not in out]
    print(f"universe: {len(out)} series cached, {len(todo)} to fetch", flush=True)
    for k, s in enumerate(todo):
        mp = (max_pages or {}).get(s, 10)
        live, t1 = _series_markets("/markets", {"series_ticker": s, "status": "settled"}, start_ts, mp)
        for m in live:
            m["historical"] = False
        hist, t2 = [], False
        # the live set reaches back to the cutoff; only go to /historical if
        # the live set wasn't truncated
        if not t1:
            hist, t2 = _series_markets("/historical/markets", {"series_ticker": s}, start_ts, mp)
        seen = {m["ticker"] for m in live}
        for m in hist:
            m["historical"] = True
        out[s] = {"markets": live + [m for m in hist if m["ticker"] not in seen], "truncated": t1 or t2}
        if k % 10 == 9:
            _write(path, out)
            n = sum(len(v["markets"]) for v in out.values())
            print(f"  {len(out)} series, {n} markets", flush=True)
    _write(path, out)
    return out


def load_series() -> dict:
    path = DATA_DIR / "series.json.gz"
    if path.exists():
        return _read(path)
    DATA_DIR.mkdir(exist_ok=True)
    d = get("/series", include_volume="true")
    out = {s["ticker"]: {"category": s.get("category") or "?", "fee_type": s.get("fee_type"),
                         "fee_multiplier": s.get("fee_multiplier", 1), "frequency": s.get("frequency"),
                         "tags": s.get("tags") or [], "title": s.get("title"),
                         "volume": float(s.get("volume_fp") or s.get("volume") or 0)}
           for s in d.get("series", [])}
    _write(path, out)
    return out


# ---------------------------------------------------------------- candles

def _num(side: dict, key: str):
    if not isinstance(side, dict):
        return None
    v = side.get(f"{key}_dollars", side.get(key))
    if v is None:
        return None
    v = float(v)
    return v / 100.0 if v > 1.0 else v  # very old responses quote cents


def parse_candles(cands: list[dict]) -> list[list]:
    """[end_ts, yes_bid_close, yes_ask_close, volume] per hour. A missing side
    is None (no quote that hour). Live and historical name fields differently."""
    rows = []
    for c in cands or []:
        bid = _num(c.get("yes_bid"), "close")
        ask = _num(c.get("yes_ask"), "close")
        vol = c.get("volume_fp", c.get("volume"))
        try:
            vol = float(vol) if vol is not None else 0.0
        except (TypeError, ValueError):
            vol = 0.0
        rows.append([int(c["end_period_ts"]), bid, ask, vol])
    return rows


def _candle_window(m: dict) -> tuple[int, int]:
    end = int(m["close_ts"]) + 3600
    start = max(int(m["open_ts"] or 0), int(m["close_ts"]) - CANDLE_HOURS * 3600)
    return start, end


def fetch_candles(markets: list[dict], name: str) -> dict:
    """ticker -> candle rows, resuming from data/candles_<name>.json.gz."""
    path = DATA_DIR / f"candles_{name}.json.gz"
    cache = _read(path) if path.exists() else {}
    todo = [m for m in markets if m["ticker"] not in cache]
    print(f"candles: {len(cache)} cached, {len(todo)} to fetch", flush=True)

    live = sorted((m for m in todo if not m["historical"]), key=lambda m: m["close_ts"])
    i = 0
    while i < len(live):
        chunk = [live[i]]
        lo, hi = _candle_window(live[i])
        j = i + 1
        while j < len(live) and len(chunk) < 100:
            s, e = _candle_window(live[j])
            nlo, nhi = min(lo, s), max(hi, e)
            if (len(chunk) + 1) * ((nhi - nlo) // 3600 + 1) > 9500:
                break
            chunk.append(live[j])
            lo, hi = nlo, nhi
            j += 1
        d = get("/markets/candlesticks", market_tickers=",".join(m["ticker"] for m in chunk),
                start_ts=lo, end_ts=hi, period_interval=60)
        for row in d.get("markets", []):
            cache[row["market_ticker"]] = parse_candles(row.get("candlesticks", []))
        for m in chunk:
            cache.setdefault(m["ticker"], [])
        i = j
        if (i // 100) % 20 == 0:
            _write(path, cache)
    _write(path, cache)

    def one(m):
        s, e = _candle_window(m)
        d = get(f"/historical/markets/{m['ticker']}/candlesticks", start_ts=s, end_ts=e, period_interval=60)
        return m["ticker"], parse_candles(d.get("candlesticks", []))

    hist = [m for m in todo if m["historical"]]
    with ThreadPoolExecutor(4) as ex:
        for k, (t, rows) in enumerate(ex.map(one, hist)):
            cache[t] = rows
            if k % 500 == 499:
                _write(path, cache)
                print(f"  historical candles {k + 1}/{len(hist)}", flush=True)
    _write(path, cache)
    return cache
