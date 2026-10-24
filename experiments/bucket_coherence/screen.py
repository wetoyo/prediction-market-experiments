"""Candle-based screen for full-set arbitrage on Kalshi bucket ladders.

For a sample of settled, mutually exclusive ladder events over the window,
fetch every bucket's 1-minute yes bid/ask candles, forward-fill each bucket's
last close onto a common minute grid, and at every minute where all buckets
are open and quoted compute

    YES-set edge = 1 - sum(ask_i + fee(ask_i))
    NO-set edge  = (n - 1) - sum((1 - bid_i) + fee(1 - bid_i))

Kalshi emits a candle only for minutes in which a bucket's quote (or a
trade) changed, so the forward-filled value is the bucket's quote as of that
minute's end. Within one minute the buckets' closes are not simultaneous;
a run of positive edge that lasts several minutes is evidence the gap was
really there. A run during which no bucket's quote changed at all ("static")
means the sum held over a full minute of unchanged books, i.e. it was
simultaneously on the screen.

Windows: each event from max(last bucket listed, close - 48h) to the first
bucket's close. Events settled before Kalshi's historical cutoff
(2026-08-25) come from /historical/ one bucket at a time, so the early half
of the window is sampled more thinly.

    python screen.py                 # fetch (cached) + summarize
    python screen.py --summary-only  # just re-summarize cached event results
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kalshi_api as api  # noqa: E402
import ladders  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
EVENTS_DIR = DATA_DIR / "events"

W0 = datetime(2026, 6, 24, tzinfo=timezone.utc)
MID = datetime(2026, 8, 25, tzinfo=timezone.utc)   # = Kalshi's historical cutoff; recent half after
W1 = datetime(2026, 10, 24, tzinfo=timezone.utc)
WINDOW_CAP_S = 48 * 3600
BATCH_CANDLE_CAP = 10000   # live batch endpoint: markets x minutes per request
BATCH_MARKET_CAP = 100

# series -> (group, recent-half sample size, early-half sample size)
SERIES = {
    "KXBTC": ("crypto_hourly", 240, 4),
    "KXETH": ("crypto_hourly", 240, 2),
    "KXXRP": ("crypto_hourly", 240, 8),
    "KXINX": ("index_daily", 999, 10),
    "KXNASDAQ100": ("index_daily", 999, 10),
    "KXEURUSD": ("fx_daily", 999, 10),
    "KXUSDJPY": ("fx_daily", 999, 10),
    "KXHIGHNY": ("weather", 999, 15),
    "KXHIGHCHI": ("weather", 999, 15),
    "KXHIGHLAX": ("weather", 999, 15),
    "KXHIGHMIA": ("weather", 999, 15),
    "KXHIGHAUS": ("weather", 999, 15),
    "KXHIGHDEN": ("weather", 999, 15),
    "KXHIGHPHIL": ("weather", 999, 15),
    "KXHIGHTDC": ("weather", 999, 15),
    "KXLOWTNYC": ("weather", 999, 15),
    "KXNFLGAME": ("sports", 100, 20),
    "KXMLBGAME": ("sports", 100, 20),
    "KXNHLGAME": ("sports", 100, 20),
    "KXNBAGAME": ("sports", 100, 20),
    "KXNCAAFGAME": ("sports", 100, 20),
    "KXEPLGAME": ("sports", 100, 20),
    "KXATPMATCH": ("sports", 100, 20),
    "KXFEDDECISION": ("econ", 999, 10),
}

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def ticker_date(event_ticker: str) -> datetime | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


# ---- discovery --------------------------------------------------------------

def list_events(series: str) -> list[dict]:
    """Settled, mutually exclusive events of `series` dated inside the window,
    newest first (the /events listing includes historical events, without
    nested markets)."""
    out, cursor = [], None
    for _ in range(200):
        d = api.get("/events", series_ticker=series, status="settled", limit=200, cursor=cursor)
        rows = d.get("events", []) or []
        for e in rows:
            day = ticker_date(e["event_ticker"])
            if day is None and e.get("strike_date"):
                day = datetime.fromtimestamp(ts(e["strike_date"]), timezone.utc)
            if day is None or not e.get("mutually_exclusive"):
                continue
            if W0 <= day < W1:
                out.append({"event": e["event_ticker"], "day": day.timestamp()})
        cursor = d.get("cursor")
        days = [ticker_date(e["event_ticker"]) for e in rows]
        days = [x for x in days if x]
        if not cursor or not rows or (days and min(days) < W0):
            break
    return out


def sample(events: list[dict], k: int) -> list[dict]:
    events = sorted(events, key=lambda e: (e["day"], e["event"]))
    if len(events) <= k:
        return events
    step = len(events) / k
    return [events[int(i * step)] for i in range(k)]


def event_markets(event: str) -> tuple[list[dict], bool]:
    rows = api.paginate("/markets", "markets", event_ticker=event, limit=1000)
    hist = False
    if not rows:
        rows = api.paginate("/historical/markets", "markets", event_ticker=event, limit=1000)
        hist = True
    ms = [{"ticker": m["ticker"], "open_ts": ts(m.get("open_time")), "close_ts": ts(m.get("close_time")),
           "result": m.get("result") or "", "status": m.get("status")} for m in rows]
    return ms, hist


# ---- candles ----------------------------------------------------------------

def _num(side: dict | None, default: float) -> float:
    if not side:
        return default
    v = side.get("close_dollars", side.get("close"))
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _parse(cands: list[dict]) -> list[list[float]]:
    """[end_ts, yes_bid_close, yes_ask_close]; a missing bid is 0 and a missing
    ask 1 (nothing to buy)."""
    out = []
    for c in cands or []:
        out.append([float(c["end_period_ts"]), _num(c.get("yes_bid"), ladders.NO_BID),
                    _num(c.get("yes_ask"), ladders.NO_ASK)])
    return out


def fetch_candles(tickers: list[str], lo: int, hi: int, hist: bool, period: int = 1) -> dict[str, list]:
    out: dict[str, list] = {t: [] for t in tickers}
    if hist:
        for t in tickers:
            d = api.get(f"/historical/markets/{t}/candlesticks", start_ts=lo, end_ts=hi, period_interval=period)
            out[t] = _parse(d.get("candlesticks"))
        return out
    g = min(len(tickers), BATCH_MARKET_CAP)
    chunk = max(1, BATCH_CANDLE_CAP // g - 2) * 60 * period
    for i in range(0, len(tickers), g):
        group = tickers[i:i + g]
        t0 = lo
        while t0 < hi:
            t1 = min(hi, t0 + chunk)
            d = api.get("/markets/candlesticks", market_tickers=",".join(group), start_ts=t0, end_ts=t1,
                        period_interval=period)
            for row in d.get("markets", []) or []:
                out.setdefault(row["market_ticker"], []).extend(_parse(row.get("candlesticks")))
            t0 = t1
    for t in out:
        seen, rows = set(), []
        for r in sorted(out[t]):
            if r[0] not in seen:
                seen.add(r[0])
                rows.append(r)
        out[t] = rows
    return out


# ---- per-event computation ----------------------------------------------------

def grid_quotes(candles: dict[str, list], seed: dict[str, list[float]], g0: int, g1: int):
    """Forward-filled bid/ask matrices [n_markets, n_minutes] (NaN = no quote
    yet) and a per-minute count of quote changes."""
    grid = np.arange(g0, g1 + 1, 60, dtype=float)
    tickers = list(candles)
    bids = np.full((len(tickers), len(grid)), np.nan)
    asks = np.full((len(tickers), len(grid)), np.nan)
    changes = np.zeros(len(grid), dtype=int)
    for i, t in enumerate(tickers):
        rows = candles[t]
        if seed.get(t):
            rows = [[g0 - 60.0, seed[t][0], seed[t][1]]] + [r for r in rows if r[0] > g0 - 60]
        if not rows:
            continue
        arr = np.array(rows)
        idx = np.searchsorted(arr[:, 0], grid, side="right") - 1
        ok = idx >= 0
        bids[i, ok] = arr[idx[ok], 1]
        asks[i, ok] = arr[idx[ok], 2]
        # count quote changes landing in each grid minute (after the first)
        hit = np.searchsorted(grid, arr[:, 0], side="left")
        for h, prev, cur in zip(hit[1:], arr[:-1], arr[1:]):
            if h < len(grid) and (prev[1] != cur[1] or prev[2] != cur[2]):
                changes[h] += 1
    return grid, tickers, bids, asks, changes


def edges_from_grid(bids: np.ndarray, asks: np.ndarray, fee_mult: float = 1.0):
    """Vectorised yes_set_edge / no_set_edge per minute (NaN where any bucket
    has no quote yet, -inf where any leg can't be bought)."""
    n = bids.shape[0]
    fee = lambda p: 0.07 * fee_mult * p * (1 - p)  # noqa: E731
    with np.errstate(invalid="ignore"):
        ye = 1.0 - np.sum(asks + fee(asks), axis=0)
        ye[np.any((asks >= 1.0) | (asks <= 0.0), axis=0)] = -np.inf
        no = 1.0 - bids
        ne = (n - 1) - np.sum(no + fee(no), axis=0)
        ne[np.any((bids <= 0.0) | (bids >= 1.0), axis=0)] = -np.inf
    missing = np.any(np.isnan(asks), axis=0) | np.any(np.isnan(bids), axis=0)
    ye[missing] = np.nan
    ne[missing] = np.nan
    return ye, ne


def summarize_runs(grid, edge, changes) -> list[list]:
    """[[start_ts, minutes, mean_edge, max_edge, quote_changes_inside]] for
    every maximal run of edge > 0."""
    e = np.where(np.isnan(edge), -np.inf, edge)
    out = []
    for s, n in ladders.runs(list(e), 0.0):
        seg = edge[s:s + n]
        inside = int(changes[s + 1:s + n].sum()) if n > 1 else 0
        out.append([float(grid[s]), int(n), float(np.mean(seg)), float(np.max(seg)), inside])
    return out


def process_event(series: str, ev: dict, fee_mult: float) -> dict | None:
    path = EVENTS_DIR / f"{ev['event']}.json"
    if path.exists():
        return json.loads(path.read_text())
    ms, hist = event_markets(ev["event"])
    res = {"series": series, "group": SERIES[series][0], "event": ev["event"], "hist": hist, "n": len(ms)}
    if len(ms) < 2 or any(m["open_ts"] is None or m["close_ts"] is None for m in ms):
        res["skip"] = "markets"
        path.write_text(json.dumps(res))
        return res
    res["exh"] = ladders.exhaustiveness(ms)
    close = min(m["close_ts"] for m in ms)
    latest_open = max(m["open_ts"] for m in ms)
    res["close_ts"] = close
    lo = int(max(latest_open, close - WINDOW_CAP_S))
    hi = int(close)
    if hi - lo < 120:
        res["skip"] = "window"
        path.write_text(json.dumps(res))
        return res
    tickers = [m["ticker"] for m in ms]
    candles = fetch_candles(tickers, lo, hi, hist)
    seed = {}
    if lo > latest_open + 60:  # seed each bucket's quote as of the window start from hourly candles
        hc = fetch_candles(tickers, int(latest_open), lo, hist, period=60)
        seed = {t: rows[-1][1:] for t, rows in hc.items() if rows}
    g0 = int(math.ceil(lo / 60) * 60)
    g1 = int(math.floor(hi / 60) * 60)
    grid, tick, bids, asks, changes = grid_quotes(candles, seed, g0, g1)
    ye, ne = edges_from_grid(bids, asks, fee_mult)
    yg, ng = edges_from_grid(bids, asks, 0.0)  # before fees, for context
    valid = ~np.isnan(ye)
    res.update({
        "minutes": int(len(grid)), "valid_minutes": int(valid.sum()),
        "yes_runs": summarize_runs(grid, ye, changes), "no_runs": summarize_runs(grid, ne, changes),
        "yes_gross_min": int(np.sum(yg > 0)), "no_gross_min": int(np.sum(ng > 0)),
        "yes_gross_events_persist": any(n >= 3 for _, n in ladders.runs(list(np.nan_to_num(yg, nan=-1.0)))),
        "no_gross_events_persist": any(n >= 3 for _, n in ladders.runs(list(np.nan_to_num(ng, nan=-1.0)))),
        "best_yes": float(np.nanmax(ye)) if valid.any() else None,
        "best_no": float(np.nanmax(ne)) if valid.any() else None,
        "median_yes_cost": float(np.nanmedian(1 - ye[np.isfinite(ye)])) if np.isfinite(ye).any() else None,
    })
    path.write_text(json.dumps(res))
    return res


# ---- driver -------------------------------------------------------------------

def collect(workers: int = 4) -> list[dict]:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for series, (group, k_recent, k_early) in SERIES.items():
        try:
            meta = api.get(f"/series/{series}")["series"]
            fee_mult = float(meta.get("fee_multiplier") or 1.0)
        except Exception:  # noqa: BLE001
            fee_mult = 1.0
        evs = list_events(series)
        recent = [e for e in evs if e["day"] >= MID.timestamp()]
        early = [e for e in evs if e["day"] < MID.timestamp()]
        chosen = sample(recent, k_recent) + sample(early, k_early)
        print(f"{series}: {len(evs)} events in window, sampling {len(chosen)}", flush=True)
        jobs += [(series, e, fee_mult) for e in chosen]
    out = []
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(process_event, *j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
                if r:
                    out.append(r)
            except Exception as e:  # noqa: BLE001
                print("ERR", futs[f][1]["event"], e, flush=True)
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} events, {api.STATS}, {time.time() - t0:.0f}s", flush=True)
    return out


def load_results() -> list[dict]:
    return [json.loads(p.read_text()) for p in EVENTS_DIR.glob("*.json")]


def summarize(results: list[dict], min_run: int = 3) -> dict:
    """Per series/half: events, exhaustiveness, apparent arbs (any minute
    > 0), persistent arbs (a run >= min_run minutes), static runs, edges."""
    mid = MID.timestamp()
    table = defaultdict(lambda: defaultdict(list))
    crit_events, crit_edges = [], []
    for r in results:
        if r.get("skip") or r.get("valid_minutes", 0) == 0 or r.get("series") not in SERIES:
            continue
        half = "recent" if r["close_ts"] >= mid else "early"
        key = (r["series"], half)
        t = table[key]
        t["events"].append(r["event"])
        t["clean"].append(r["exh"]["clean"])
        t["valid_min"].append(r["valid_minutes"])
        for side in ("yes", "no"):
            rr = r[f"{side}_runs"]
            t[f"{side}_any"].append(bool(rr))
            t[f"{side}_arb_min"].append(sum(x[1] for x in rr))
            pers = [x for x in rr if x[1] >= min_run]
            t[f"{side}_persist"].append(bool(pers))
            t[f"{side}_static"].append(any(x[4] == 0 and x[1] >= 2 for x in rr))
            t[f"{side}_gross_persist"].append(bool(r.get(f"{side}_gross_events_persist")))
            if r.get(f"best_{side}") is not None and math.isfinite(r[f"best_{side}"]):
                t[f"{side}_best"].append(r[f"best_{side}"])
            if pers:
                w = sum(x[1] for x in pers)
                ev_edge = sum(x[1] * x[2] for x in pers) / w
                t[f"{side}_persist_edge"].append(ev_edge)
                t[f"{side}_persist_maxlen"].append(max(x[1] for x in pers))
        if half == "recent":
            pers = [x for side in ("yes", "no") for x in r[f"{side}_runs"] if x[1] >= min_run]
            if pers and r["exh"]["clean"]:
                crit_events.append(r["event"])
                w = sum(x[1] for x in pers)
                crit_edges.append(sum(x[1] * x[2] for x in pers) / w)
    rows = []
    for (series, half), t in sorted(table.items(), key=lambda kv: (SERIES[kv[0][0]][0], kv[0][0], kv[0][1])):
        n = len(t["events"])
        row = {"series": series, "half": half, "events": n, "clean": sum(t["clean"]),
               "valid_hours": round(sum(t["valid_min"]) / 60)}
        for side in ("yes", "no"):
            row[f"{side}_any"] = sum(t[f"{side}_any"])
            row[f"{side}_arb_min"] = sum(t[f"{side}_arb_min"])
            row[f"{side}_persist"] = sum(t[f"{side}_persist"])
            row[f"{side}_static"] = sum(t[f"{side}_static"])
            row[f"{side}_gross_persist"] = sum(t[f"{side}_gross_persist"])
            b = t[f"{side}_best"]
            row[f"{side}_best_med_c"] = round(100 * statistics.median(b), 2) if b else None
            pe = t[f"{side}_persist_edge"]
            row[f"{side}_edge_mean_c"] = round(100 * statistics.mean(pe), 2) if pe else None
            row[f"{side}_maxlen"] = max(t[f"{side}_persist_maxlen"]) if pe else None
        rows.append(row)
    return {"rows": rows, "criterion_a": {
        "recent_events_with_persistent_arb": len(crit_events),
        "mean_edge_c": round(100 * statistics.mean(crit_edges), 3) if crit_edges else None,
        "median_edge_c": round(100 * statistics.median(crit_edges), 3) if crit_edges else None,
        "passes": len(crit_events) >= 30 and bool(crit_edges) and statistics.mean(crit_edges) >= 0.01,
        "events": sorted(crit_events)}}


def print_summary(s: dict) -> None:
    cols = ["series", "half", "events", "clean", "valid_hours", "yes_best_med_c", "yes_gross_persist", "yes_any",
            "yes_persist", "yes_static", "yes_edge_mean_c", "yes_maxlen", "no_best_med_c", "no_gross_persist",
            "no_any", "no_persist", "no_static", "no_edge_mean_c", "no_maxlen"]
    print(" | ".join(cols))
    for r in s["rows"]:
        print(" | ".join(str(r.get(c)) for c in cols))
    a = dict(s["criterion_a"])
    ev = a.pop("events")
    print("criterion (a):", a)
    print("events:", ev[:60])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if not args.summary_only:
        collect(args.workers)
    s = summarize(load_results())
    (DATA_DIR / "summary.json").write_text(json.dumps(s, indent=1))
    print_summary(s)
