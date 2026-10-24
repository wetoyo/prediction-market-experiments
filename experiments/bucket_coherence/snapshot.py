"""Live order-book snapshots of open bucket ladders: the true simultaneous
full-set cost, with depth.

Each sweep lists the open, mutually exclusive events of every series in
screen.SERIES (the soonest-closing few per series), then pulls every bucket's
book with the batch endpoint `/markets/orderbooks` (100 tickers per call, so
a ladder of <= 100 buckets is one simultaneous snapshot; KXBTC's 188 and
KXETH's 300 take 2-3 calls a fraction of a second apart). For each ladder it
walks the YES asks (= 1 - NO bids) and the NO asks (= 1 - YES bids) of all
buckets together and records the top-of-book edge per set after taker fees,
the size there, and the most whole sets that still net > 0 sweeping deeper.

    python snapshot.py --sweeps 6 --interval 300   # ~25 minutes
    python snapshot.py --summary-only

Raw books are saved under data/snapshots/ (gitignored). Runs at a low request
rate (BCO_MAX_RPS, default 1/s here) so it can share the IP with other work.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("BCO_MAX_RPS", "1.0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kalshi_api as api  # noqa: E402
import ladders  # noqa: E402
from screen import SERIES  # noqa: E402

SNAP_DIR = Path(__file__).resolve().parent / "data" / "snapshots"
EVENTS_PER_SERIES = 3
MAX_HORIZON_S = 8 * 86400


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def open_ladders(series_list=None, per_series: int = EVENTS_PER_SERIES) -> list[dict]:
    """[{series, event, tickers, close_ts}] for open ME events whose buckets
    are all active, soonest close first, at most `per_series` each."""
    now = time.time()
    out = []
    for s in series_list or SERIES:
        d = api.get("/events", cache=False, series_ticker=s, status="open", with_nested_markets="true", limit=50)
        cand = []
        for e in d.get("events", []) or []:
            ms = e.get("markets") or []
            if not e.get("mutually_exclusive") or len(ms) < 2:
                continue
            if any(m.get("status") != "active" for m in ms):
                continue
            close = min(_ts(m["close_time"]) for m in ms)
            if close - now > MAX_HORIZON_S or close <= now:
                continue
            cand.append({"series": s, "event": e["event_ticker"], "tickers": [m["ticker"] for m in ms],
                         "close_ts": close})
        out += sorted(cand, key=lambda x: x["close_ts"])[:per_series]
    return out


def fetch_books(tickers: list[str]) -> tuple[dict, float]:
    """ticker -> {"yes": [(p, size)], "no": [(p, size)]} (bids), and the
    seconds between the first and last batch call."""
    books, t_first, t_last = {}, None, None
    for i in range(0, len(tickers), 100):
        d = api.get("/markets/orderbooks", cache=False, tickers=tickers[i:i + 100])
        t_last = time.time()
        t_first = t_first or t_last  # span = first response -> last response; 0 for one call
        for ob in d.get("orderbooks", []) or []:
            o = ob.get("orderbook_fp") or ob.get("orderbook") or {}
            books[ob["ticker"]] = {
                side: [(float(p), float(q)) for p, q in (o.get(f"{side}_dollars") or [])] for side in ("yes", "no")}
    return books, (t_last - t_first) if t_first else 0.0


def evaluate(books: dict, tickers: list[str], fee_mult: float = 1.0) -> dict:
    """Full-set economics of one ladder from its books."""
    n = len(tickers)
    if any(t not in books for t in tickers):
        return {"n": n, "error": "missing books"}
    yes_asks = [ladders.asks_from_book(books[t]["no"]) for t in tickers]
    no_asks = [ladders.asks_from_book(books[t]["yes"]) for t in tickers]
    y = ladders.full_set_depth(yes_asks, 1.0, fee_mult)
    no = ladders.full_set_depth(no_asks, n - 1.0, fee_mult)
    top_yes_cost = sum(a[0][0] for a in yes_asks) if all(yes_asks) else None
    top_no_bidsum = sum(max(p for p, _ in books[t]["yes"]) for t in tickers) if all(
        books[t]["yes"] for t in tickers) else None
    return {"n": n, "yes_top_edge": y["top_edge"], "yes_top_size": y["top_size"], "yes_sets": y["sets"],
            "yes_profit": y["profit"], "no_top_edge": no["top_edge"], "no_top_size": no["top_size"],
            "no_sets": no["sets"], "no_profit": no["profit"], "sum_yes_ask": top_yes_cost,
            "sum_yes_bid": top_no_bidsum, "missing_yes_ask": sum(1 for a in yes_asks if not a),
            "missing_no_ask": sum(1 for a in no_asks if not a)}


def sweep() -> list[dict]:
    ts = datetime.now(timezone.utc)
    rows, raw = [], {}
    for lad in open_ladders():
        books, span = fetch_books(lad["tickers"])
        r = {"ts": ts.isoformat(), "series": lad["series"], "event": lad["event"], "span_s": round(span, 2),
             "mins_to_close": round((lad["close_ts"] - time.time()) / 60, 1), **evaluate(books, lad["tickers"])}
        rows.append(r)
        raw[lad["event"]] = {"tickers": lad["tickers"], "books": books}
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(SNAP_DIR / f"{ts:%Y%m%dT%H%M%S}.json.gz", "wt") as f:
        json.dump({"rows": rows, "raw": raw}, f)
    return rows


def load_rows() -> list[dict]:
    rows = []
    for p in sorted(SNAP_DIR.glob("*.json.gz")):
        with gzip.open(p, "rt") as f:
            rows += json.load(f)["rows"]
    return rows


def summarize(rows: list[dict]) -> None:
    sweeps = sorted({r["ts"] for r in rows})
    print(f"{len(sweeps)} sweeps, {len(rows)} ladder snapshots, {len({r['event'] for r in rows})} events")
    by = {}
    for r in rows:
        if "error" in r:
            continue
        by.setdefault(r["series"], []).append(r)
    print("series | snaps | n | best yes edge c | median sum(yes ask) | best no edge c | median sum(yes bid) | "
          "arb snaps (>=1 set) | arb snaps (>=5 sets)")
    for s, rs in by.items():
        def best(k):
            v = [r[k] for r in rs if r[k] is not None and r[k] > -1e9]
            return round(100 * max(v), 2) if v else None

        def med(k):
            v = sorted(r[k] for r in rs if r[k] is not None)
            return round(v[len(v) // 2], 3) if v else None
        arb1 = sum(1 for r in rs if r["yes_sets"] >= 1 or r["no_sets"] >= 1)
        arb5 = sum(1 for r in rs if r["yes_sets"] >= 5 or r["no_sets"] >= 5)
        print(f"{s} | {len(rs)} | {rs[0]['n']} | {best('yes_top_edge')} | {med('sum_yes_ask')} | "
              f"{best('no_top_edge')} | {med('sum_yes_bid')} | {arb1} | {arb5}")
    hits = [r for r in rows if "error" not in r and (r["yes_sets"] >= 1 or r["no_sets"] >= 1)]
    for r in hits:
        print("ARB", r)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweeps", type=int, default=6)
    ap.add_argument("--interval", type=float, default=300)
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args()
    if not a.summary_only:
        for i in range(a.sweeps):
            t0 = time.time()
            rows = sweep()
            print(f"sweep {i + 1}/{a.sweeps}: {len(rows)} ladders in {time.time() - t0:.0f}s, {api.STATS}", flush=True)
            if i + 1 < a.sweeps:
                time.sleep(max(0.0, a.interval - (time.time() - t0)))
    summarize(load_rows())
