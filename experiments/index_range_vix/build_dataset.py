"""Build the backtest dataset: one row per (market, decision time) with the
executable Kalshi yes bid/ask at that minute, the strike geometry and the
settled result. Fetches whatever isn't cached yet (throttled, see
kalshi_data.py), newest events first, so an interrupted run still leaves a
contiguous recent window.

Scope (chosen before looking at any results, to bound the request count on a
shared IP):
  - daily close ladders KXINX / KXNASDAQ100: decision times 10:30, 12:30,
    14:30, 15:30 ET; strikes whose range touches +/-2% of the prior close.
  - hourly above/below KXINXU / KXNASDAQ100U: events closing 15:00 ET
    (decisions 10:30, 12:30, 14:30 -- decision times >= 30 min before close); strikes within +/-1% of
    the prior close, thinned to multiples of 25 (S&P) / 100 (NDX) -- adjacent
    5/10-point thresholds are near-duplicate bets.
  - window: --since (default 2025-10-27, one year). The shared IP gets ~1
    request/s through the 429s, so two years of all four series (~60k
    requests) wasn't feasible; Yahoo's 60m history would allow 2024-10-24 on.
Strike filters use the prior day's close only (no lookahead).

Usage: python build_dataset.py [--series KXINX,KXNASDAQ100] [--since 2024-10-28]
"""

import argparse
import gzip
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import kalshi_data as kd
import market_data as md

ET = ZoneInfo("America/New_York")
DATA_DIR = Path(__file__).resolve().parent / "data"
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

SERIES = {
    # series: (index, kind, band, thin)
    "KXINX": ("SPX", "daily", 0.02, None),
    "KXNASDAQ100": ("NDX", "daily", 0.02, None),
    "KXINXU": ("SPX", "hourly", 0.01, 25),
    "KXNASDAQ100U": ("NDX", "hourly", 0.01, 100),
}
DECISION_TIMES = [(10, 30), (12, 30), (14, 30), (15, 30)]
HOURLY_CLOSES = {15}
CUTOFF_TS = datetime(2026, 8, 25, tzinfo=ZoneInfo("UTC")).timestamp()


def parse_event(event_ticker: str):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})H(\d{2})(\d{2})$", event_ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    d = date(2000 + int(yy), MONTHS[mon], int(dd))
    close = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=ET)
    return d, close


def strikes(m: dict):
    """(lo, hi) of the YES region, None for unbounded."""
    st = m.get("strike_type")
    fl, cap = m.get("floor_strike"), m.get("cap_strike")
    if st == "between":
        return fl, cap
    if st in ("greater", "greater_or_equal"):
        return fl, None
    if st in ("less", "less_or_equal"):
        return None, cap
    return None


def prior_close(closes: dict, d: date):
    for back in range(1, 8):
        k = (d - timedelta(days=back)).isoformat()
        if k in closes:
            return closes[k]
    return None


def quote_at(rows: list, ts: float):
    best = None
    for end, bid, ask in rows:
        if end <= ts:
            best = (bid, ask)
        else:
            break
    return best


def build_series(series: str, since: date, verbose: bool = True) -> list[dict]:
    index, kind, band, thin = SERIES[series]
    closes = md.daily_close(index)
    events = [e for e in kd.list_events(series) if e["event_ticker"].startswith(series + "-")]
    out = []
    todo = []
    for e in events:
        p = parse_event(e["event_ticker"])
        if not p:
            continue
        d, close = p
        if d < since:
            continue
        if kind == "daily" and close.hour != 16:
            continue
        if kind == "hourly" and (close.hour not in HOURLY_CLOSES or close.minute != 0):
            continue
        todo.append((e["event_ticker"], d, close))
    todo.sort(key=lambda x: x[2], reverse=True)
    for n, (ev, d, close) in enumerate(todo):
        pc = prior_close(closes, d)
        if pc is None:
            continue
        dts = []
        for hh, mm in DECISION_TIMES:
            t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
            if (close - t).total_seconds() >= 1800:
                dts.append(int(t.timestamp()))
        if not dts:
            continue
        ms = kd.event_markets(ev)
        keep = []
        for m in ms:
            if m.get("result") not in ("yes", "no"):
                continue
            sk = strikes(m)
            if sk is None:
                continue
            lo, hi = sk
            a = lo if lo is not None else -1e18
            b = hi if hi is not None else 1e18
            if b < pc * (1 - band) or a > pc * (1 + band):
                continue
            if thin is not None:
                k = lo if lo is not None else hi
                if abs(round(k) % thin) > 0.5 and abs(round(k) % thin - thin) > 0.5:
                    continue
            keep.append(m)
        if not keep:
            continue
        close_ts = int(close.timestamp())
        historical = close_ts < CUTOFF_TS
        cand = {}
        if historical:
            for m in keep:
                open_ts = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
                start = max(open_ts, dts[0] - 20 * 3600)
                cand[m["ticker"]] = kd.market_candles(series, m["ticker"], start, dts[-1] + 60, True)
        else:
            start, end = dts[0] - 15 * 60, dts[-1] + 60
            per = max(1, 9000 // max(1, (end - start) // 60))
            for i in range(0, len(keep), per):
                chunk = [m["ticker"] for m in keep[i:i + per]]
                cand.update(kd.batch_candles(chunk, start, end))
        for m in keep:
            rows = cand.get(m["ticker"], [])
            lo, hi = strikes(m)
            for t in dts:
                q = quote_at(rows, t)
                if q is None:
                    continue
                out.append({
                    "series": series, "index": index, "event": ev, "ticker": m["ticker"], "date": d.isoformat(),
                    "close_ts": close_ts, "ts": t, "lo": lo, "hi": hi, "bid": q[0], "ask": q[1],
                    "result": 1 if m["result"] == "yes" else 0,
                    "expiration_value": float(m["expiration_value"]) if m.get("expiration_value") else None,
                })
        if verbose and n % 10 == 0:
            print(f"{series} {n + 1}/{len(todo)} {ev} markets={len(keep)} rows={len(out)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXINX,KXNASDAQ100,KXINXU,KXNASDAQ100U")
    ap.add_argument("--since", default="2025-10-27")
    args = ap.parse_args()
    since = date.fromisoformat(args.since)
    for s in args.series.split(","):
        rows = build_series(s, since)
        path = DATA_DIR / f"dataset_{s}.json.gz"
        with gzip.open(path, "wt") as f:
            json.dump(rows, f)
        print(f"wrote {path} rows={len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
