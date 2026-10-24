"""Underlying index level and implied vol (VIX / VXN), from Yahoo Finance's
public chart API, cached under data/yahoo/.

Sources tried (2026-10-24): CBOE's CDN CSV returns 307 (blocked); FRED's
fredgraph CSV works but is daily close only; Stooq returns nothing. Yahoo's
v8 chart endpoint serves 60-minute bars for the last 730 days for ^GSPC,
^NDX, ^VIX and ^VXN, which is what the backtest uses -- intraday spot and
intraday vol at the decision time, not the prior day's close.

Bar timing (verified on the raw timestamps): ^GSPC / ^NDX / ^VXN bars start
at :30 during the cash session (09:30, 10:30, ... 15:30 ET) and the last one
is cut at 16:00; ^VIX bars start on the hour and include extended hours.
A bar's close is only known at its END, so `value_at(ts)` returns the close
of the latest bar whose end is <= ts. No lookahead.
"""

import bisect
import gzip
import json
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent / "data" / "yahoo"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0"}

SPOT_SYMBOL = {"SPX": "^GSPC", "NDX": "^NDX"}
VOL_SYMBOL = {"SPX": "^VIX", "NDX": "^VXN"}


def _fetch(sym: str, interval: str, rng: str, cache_tag: str) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{sym.strip('^')}_{interval}_{rng}_{cache_tag}.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    delay = 2.0
    for _ in range(6):
        r = requests.get(URL.format(sym=sym), params={"interval": interval, "range": rng}, headers=HEADERS,
                         timeout=30)
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            with gzip.open(path, "wt") as f:
                json.dump(data, f)
            return data
        time.sleep(delay)
        delay *= 2
    r.raise_for_status()
    raise RuntimeError(sym)


class Series:
    """(end_ts, close) pairs, ascending."""

    def __init__(self, ends: list[float], closes: list[float]):
        self.ends, self.closes = ends, closes

    def value_at(self, ts: float, max_stale: float = 3 * 3600):
        i = bisect.bisect_right(self.ends, ts) - 1
        if i < 0 or ts - self.ends[i] > max_stale:
            return None
        return self.closes[i]


def _bars(data: dict, bar_seconds: int, session_close_cut: bool) -> Series:
    ts = data.get("timestamp") or []
    closes = data["indicators"]["quote"][0]["close"]
    pairs = []
    for i, (t, c) in enumerate(zip(ts, closes)):
        if c is None:
            continue
        end = t + bar_seconds
        if session_close_cut and (t % 3600) == 0:
            end = t  # the 16:00 "close" stamp: an instant, not a bar
        elif i + 1 < len(ts) and ts[i + 1] < end:
            end = ts[i + 1]  # the 15:30 bar is cut at 16:00
        pairs.append((end, float(c)))
    pairs.sort()
    # Yahoo emits a trailing "close" stamp (e.g. 20:00 UTC) with the session close;
    # de-duplicate equal ends keeping the last.
    ends, vals = [], []
    for e, v in pairs:
        if ends and ends[-1] == e:
            vals[-1] = v
        else:
            ends.append(e)
            vals.append(v)
    return Series(ends, vals)


def intraday(index: str, cache_tag: str = "2026-10-24") -> tuple[Series, Series]:
    """(spot, vol) 60-minute series for 'SPX' or 'NDX'. `cache_tag` pins the
    snapshot (the backtest uses the one fetched on 2026-10-24)."""
    spot = _bars(_fetch(SPOT_SYMBOL[index], "60m", "2y", cache_tag), 3600, True)
    vsym = VOL_SYMBOL[index]
    vol_raw = _fetch(vsym, "60m", "2y", cache_tag)
    # ^VIX bars start on the hour (extended hours); ^VXN at :30 like the index.
    vol = _bars(vol_raw, 3600, vsym != "^VIX")
    return spot, vol


def daily_close(index: str, cache_tag: str = "2026-10-24") -> dict:
    """date 'YYYY-MM-DD' (exchange tz) -> close."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    data = _fetch(SPOT_SYMBOL[index], "1d", "5y", cache_tag)
    tz = ZoneInfo("America/New_York")
    out = {}
    for t, c in zip(data["timestamp"], data["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")] = float(c)
    return out


if __name__ == "__main__":
    from datetime import datetime, timezone
    for idx in ("SPX", "NDX"):
        s, v = intraday(idx)
        print(idx, len(s.ends), "spot bars", len(v.ends), "vol bars")
        for e, c in list(zip(s.ends, s.closes))[-9:]:
            print("  spot", datetime.fromtimestamp(e, timezone.utc), c)
        for e, c in list(zip(v.ends, v.closes))[-4:]:
            print("  vol ", datetime.fromtimestamp(e, timezone.utc), c)
