"""Backtest data: Kalshi daily high-temperature markets + IEM MOS archive.

Everything is cached gzip-JSON under data/ (gitignored); a rerun reads the
cache and touches the network only for what is missing.

Kalshi (public REST, no auth):
  * settled markets per series from /markets (settled after the historical
    cutoff, 2026-08-25 as of 2026-10-24) and /historical/markets (older);
  * hourly yes bid/ask candles per market from
    /series/{s}/markets/{t}/candlesticks or /historical/markets/{t}/candlesticks.
  The settled market's `expiration_value` is the official daily max the
  event settled on, and `result` says which bucket won.

IEM (mesonet.agron.iastate.edu): the MOS archive, one CSV per station/model/
month. `n_x` on the 00Z/12Z rows is the max (daytime) or min (night) temp
forecast; we keep only the daytime max rows (valid 00Z after a local day,
see `mos_max_forecasts`).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
IEM_MOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
DATA_DIR = Path(__file__).resolve().parent / "data"

_session = requests.Session()


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _get(url: str, params: dict, retries: int = 8, as_json: bool = True):
    for attempt in range(retries):
        try:
            resp = _session.get(url, params=params, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(0.25 * 2 ** attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"gave up: {url} {params}")


def _load(name: str):
    path = DATA_DIR / name
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _age_hours(name: str) -> float:
    path = DATA_DIR / name
    return (time.time() - path.stat().st_mtime) / 3600 if path.exists() else math.inf


def _save(name: str, obj) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(obj, f)
    tmp.replace(path)


# ---------------------------------------------------------------- Kalshi

_KEEP = ("ticker", "event_ticker", "strike_type", "floor_strike", "cap_strike", "result", "expiration_value",
         "open_time", "close_time", "yes_sub_title", "rules_primary")


def fetch_series_markets(series: str) -> list[dict]:
    """Every settled market of `series` (live + historical), deduped by ticker."""
    out: dict[str, dict] = {}
    for path, extra in (("/markets", {"status": "settled"}), ("/historical/markets", {})):
        cursor = None
        while True:
            params = {"series_ticker": series, "limit": 1000, **extra}
            if cursor:
                params["cursor"] = cursor
            d = _get(BASE_URL + path, params)
            rows = d.get("markets", []) or []
            for m in rows:
                if m.get("result") not in ("yes", "no"):
                    continue
                row = {k: m.get(k) for k in _KEEP}
                row["volume"] = float(m.get("volume_fp") or m.get("volume") or 0)
                row["historical"] = path != "/markets"
                out[m["ticker"]] = row
            cursor = d.get("cursor")
            if not cursor or not rows:
                break
            time.sleep(0.25)
    return sorted(out.values(), key=lambda m: (m["close_time"], m["ticker"]))


def load_series_markets(series: str, refresh: bool = False) -> list[dict]:
    name = f"markets_{series}.json.gz"
    rows = None if refresh else _load(name)
    if rows is None:
        rows = fetch_series_markets(series)
        _save(name, rows)
    return rows


def _parse_candles(cands: list[dict]) -> list[list]:
    """[end_ts, bid_close, ask_close, volume] per hour. Prices in dollars;
    None where the side was empty. Live and historical responses name the
    fields differently (close_dollars vs close)."""
    rows = []
    for c in cands:
        def px(side):
            s = c.get(side) or {}
            v = s.get("close_dollars", s.get("close"))
            if v is None:
                return None
            v = float(v)
            return v / 100.0 if v > 1.0 else v  # cents in older payloads
        vol = c.get("volume_fp", c.get("volume", 0)) or 0
        rows.append([float(c["end_period_ts"]), px("yes_bid"), px("yes_ask"), float(vol)])
    return rows


def fetch_market_candles(series: str, m: dict) -> list[list]:
    start = int(_ts(m["open_time"]))
    end = int(_ts(m["close_time"]))
    path = (f"/historical/markets/{m['ticker']}/candlesticks" if m["historical"]
            else f"/series/{series}/markets/{m['ticker']}/candlesticks")
    d = _get(BASE_URL + path, {"start_ts": start, "end_ts": end, "period_interval": 60})
    return _parse_candles(d.get("candlesticks", []) or [])


def fetch_event_candles(series: str, ms: list[dict]) -> dict:
    """All markets of one live (non-historical) event in one request."""
    start = min(int(_ts(m["open_time"])) for m in ms)
    end = max(int(_ts(m["close_time"])) for m in ms)
    d = _get(f"{BASE_URL}/series/{series}/events/{ms[0]['event_ticker']}/candlesticks",
             {"start_ts": start, "end_ts": end, "period_interval": 60})
    return {t: _parse_candles(c or []) for t, c in zip(d.get("market_tickers", []), d.get("market_candlesticks", []))}


def load_all_candles(markets_by_series: dict, pace: float = 0.05, log=print) -> dict:
    """series -> ticker -> candles, filling the per-series caches.

    Kalshi's public endpoints allow ~2.5-3 requests/s from one IP, and the
    historical candle endpoint is one market per request, so this is the
    slow part (~0.4s/market). Live events go one request per event. Work
    runs newest event first across all series, so a partial fetch is a
    contiguous recent block; progress is saved every ~300 requests."""
    caches = {s: _load(f"candles_{s}.json.gz") or {} for s in markets_by_series}
    jobs = []  # (event_date, series, [markets]) -- one request each
    for s, ms in markets_by_series.items():
        todo = [m for m in ms if m["ticker"] not in caches[s]]
        live: dict[str, list] = {}
        for m in todo:
            if m["historical"]:
                jobs.append((event_date(m["event_ticker"]), s, [m]))
            else:
                live.setdefault(m["event_ticker"], []).append(m)
        jobs += [(event_date(e), s, v) for e, v in live.items()]
    jobs.sort(key=lambda j: j[0], reverse=True)
    log(f"candles: {len(jobs)} requests to make")
    dirty = set()
    for i, (_d, s, ms) in enumerate(jobs):
        try:
            if len(ms) == 1 and ms[0]["historical"]:
                caches[s][ms[0]["ticker"]] = fetch_market_candles(s, ms[0])
            else:
                got = fetch_event_candles(s, ms)
                for m in ms:
                    caches[s][m["ticker"]] = got.get(m["ticker"], [])
            dirty.add(s)
        except Exception as e:  # one bad market shouldn't kill a long fetch
            log(f"  candles {ms[0]['ticker']}: {e}")
        time.sleep(pace)
        if i % 300 == 299 or i == len(jobs) - 1:
            for d in dirty:
                _save(f"candles_{d}.json.gz", caches[d])
            dirty.clear()
            log(f"  {i + 1}/{len(jobs)} (at {_d})")
    return caches


def load_candles(series: str) -> dict:
    return _load(f"candles_{series}.json.gz") or {}


# ---------------------------------------------------------------- IEM CLI

IEM_CLI = "https://mesonet.agron.iastate.edu/json/cli.py"


def load_cli(station: str, years: list[int]) -> dict:
    """local date iso -> observed daily max (F) from the NWS CLI reports IEM
    archives. Cached per year; the current year is refetched when >6h old."""
    out = {}
    this_year = datetime.now(timezone.utc).year
    for y in years:
        name = f"cli_{station}_{y}.json.gz"
        rows = _load(name)
        if rows is None or (y == this_year and _age_hours(name) > 6):
            d = _get(IEM_CLI, {"station": station, "year": y})
            rows = {r["valid"]: r["high"] for r in d.get("results", []) if isinstance(r.get("high"), (int, float))}
            _save(name, rows)
            time.sleep(0.5)
        out.update(rows)
    return out


# ---------------------------------------------------------------- IEM MOS

def fetch_mos_month(station: str, model: str, year: int, month: int) -> list[dict]:
    sts = f"{year:04d}-{month:02d}-01T00:00Z"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    ets = f"{ny:04d}-{nm:02d}-01T00:00Z"
    text = _get(IEM_MOS, {"station": station, "model": model, "sts": sts, "ets": ets, "format": "csv"},
                as_json=False)
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        # GFS/NAM MOS: n_x. NBM (NBS/NBE): txn, plus xnd = NBM's own
        # standard deviation of that max/min.
        nx = (r.get("n_x") or r.get("txn") or "").strip()
        if not nx:
            continue
        row = {"runtime": r["runtime"], "ftime": r["ftime"], "n_x": float(nx)}
        sd = (r.get("xnd") or "").strip()
        if sd:
            row["sd"] = float(sd)
        rows.append(row)
    return rows


def load_mos(station: str, model: str, months: list[tuple[int, int]], log=print) -> list[dict]:
    """All n_x rows for station/model over `months` (runtime month). Cached
    per month; the current month is always refetched."""
    out = []
    now = datetime.now(timezone.utc)
    for y, mth in months:
        name = f"mos_{station}_{model}_{y:04d}{mth:02d}.json.gz"
        rows = _load(name)
        if rows is None or ((y, mth) == (now.year, now.month) and _age_hours(name) > 6):
            rows = fetch_mos_month(station, model, y, mth)
            _save(name, rows)
            time.sleep(0.5)
        out.extend(rows)
    return out


def mos_max_forecasts(rows: list[dict]) -> dict:
    """runtime_ts -> {local_date_iso: (max forecast, sd or None)} from n_x rows.

    MOS max temps are the daytime max for the local calendar day and carry
    ftime = 00Z of the following UTC day (verifying 7am-7pm local). We assign
    each 00Z n_x row to the local date of (ftime - 12h), i.e. the date of the
    afternoon it covers; the 12Z rows are the overnight min and are dropped.
    """
    out: dict[float, dict[str, float]] = {}
    for r in rows:
        ft = datetime.strptime(r["ftime"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if ft.hour != 0:
            continue
        rt = datetime.strptime(r["runtime"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
        local_day = datetime.fromtimestamp(ft.timestamp() - 12 * 3600, timezone.utc).date().isoformat()
        out.setdefault(rt, {})[local_day] = (r["n_x"], r.get("sd"))
    return out


# ---------------------------------------------------------------- fetch all

def _months(start: str, end: str) -> list[tuple[int, int]]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while (y, m) <= (int(end[:4]), int(end[5:7])):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def event_date(event_ticker: str) -> str:
    """KXHIGHNY-26MAY10 -> 2026-05-10."""
    code = event_ticker.rsplit("-", 1)[1]
    return datetime.strptime(code, "%y%b%d").date().isoformat()


def backtest_markets(series: str) -> list[dict]:
    import config
    return [m for m in load_series_markets(series)
            if config.BACKTEST_START <= event_date(m["event_ticker"]) <= config.BACKTEST_END]


def fetch_all(what: str = "all") -> None:
    import config
    if what in ("all", "wx"):
        months = _months(config.ERROR_HISTORY_START, config.BACKTEST_END)
        years = sorted({y for y, _ in months})
        from concurrent.futures import ThreadPoolExecutor
        stations = [st for st, _tz in config.CITIES.values()]
        jobs = [(st, mdl, [ym]) for st in stations for mdl in config.MOS_MODELS for ym in months]
        with ThreadPoolExecutor(2) as ex:  # IEM is slow (~3-5s/request), not rate limited
            list(ex.map(lambda st: load_cli(st, years), stations))
            def safe(j):
                try:
                    return load_mos(*j)
                except Exception as e:  # IEM times out now and then; rerun to fill gaps
                    print(f"  mos {j[0]} {j[1]} {j[2]}: {e}", flush=True)

            for i, _ in enumerate(ex.map(safe, jobs)):
                if i % 200 == 199:
                    print(f"wx: {i + 1}/{len(jobs)} station-model-months", flush=True)
    if what in ("all", "kalshi"):
        load_all_candles({s: backtest_markets(s) for s in config.CITIES})


if __name__ == "__main__":
    import sys
    fetch_all(sys.argv[1] if len(sys.argv) > 1 else "all")
