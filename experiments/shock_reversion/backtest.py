"""shock_reversion backtest: fade (or follow) large, fast mid moves on
non-price-level Kalshi markets, on real hourly yes bid/ask candles.

Mechanics (no lookahead):
  * Quotes: hourly candle closes, forward-filled across hours with no
    candle (a resting quote persists). A side at bid <= 0 or ask >= 1 is
    treated as empty. Mid needs both sides.
  * Shock at candle t (fully closed): |mid_t - mid_{t-k}| >= X, k = WINDOW
    hours. Volume during the move = contracts traded in candles t-k+1..t.
  * Entry at the NEXT candle (t+1) close -- an hour after the shock was
    visible -- at the executable price: buy YES at yes_ask, buy NO at
    1 - yes_bid. "fade" buys the side that just fell; "momentum" the side
    that just rose.
  * Exit HORIZON hours after entry at the other side of the book (sell YES
    at yes_bid, sell NO at 1 - yes_ask), or at the settlement value if the
    market closes first. One open position per market at a time.
  * Fees: Kalshi taker fee (fees.py, x the series fee_multiplier) on entry,
    and on exit when exiting before settlement. Slippage s per side adds to
    the entry price and comes off a pre-settlement exit.
  * ROI = sum(P&L) / sum(capital risked), capital = entry price + entry fee.

Train/test: split by entry time at SPLIT_DATE (chosen from shock counts
only, before looking at any P&L: ~65% of base-config shocks fall before it).
The config grid is searched on train only; the winner runs on test once.
CIs bootstrap over events (all markets of one event move together).

Usage:
    python data.py                 # fetch / refresh the cached sample
    python backtest.py             # full report (train search -> test)
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data  # noqa: E402
from fees import estimate_fee_dollars  # noqa: E402

H = 3600
ORDER_SIZE = 10          # contracts per order for the fee rounding (ceil to $0.0001)
SPLIT_FRAC = 0.65
BOOT = 2000


def fee_per_contract(price: float, mult: float = 1.0) -> float:
    if mult <= 0:
        return 0.0
    return estimate_fee_dollars(price, ORDER_SIZE, 0.07 * mult) / ORDER_SIZE


# ------------------------------------------------------------------ quotes

@dataclass
class Series:
    """Hour-aligned forward-filled quotes for one market."""
    t0: int
    bid: np.ndarray      # nan = no bid
    ask: np.ndarray      # nan = no ask
    vol: np.ndarray      # contracts traded in that hour (0 if no candle)

    def idx(self, ts: int) -> int:
        return (ts - self.t0) // H


def build_quotes(rows: list[list]) -> Series | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r[0])
    t0, t1 = int(rows[0][0]), int(rows[-1][0])
    n = (t1 - t0) // H + 1
    bid = np.full(n, np.nan)
    ask = np.full(n, np.nan)
    vol = np.zeros(n)
    seen = np.zeros(n, bool)
    for ts, b, a, v in rows:
        i = (int(ts) - t0) // H
        if i < 0 or i >= n:
            continue
        bid[i] = b if (b is not None and b > 0) else np.nan
        ask[i] = a if (a is not None and a < 1) else np.nan
        vol[i] += v or 0.0
        seen[i] = True
    # forward-fill hours with no candle (not hours whose candle said "empty")
    for i in range(1, n):
        if not seen[i]:
            bid[i], ask[i] = bid[i - 1], ask[i - 1]
    return Series(t0, bid, ask, vol)


# ------------------------------------------------------------------ shocks

@dataclass
class Shock:
    ticker: str
    event: str
    series: str
    category: str
    t_entry: int          # entry candle close ts
    i_entry: int
    move: float           # signed mid change over the window (dollars)
    mid_before: float
    mid_after: float
    move_vol: float       # contracts traded during the move
    vol_24h: float        # contracts traded in the 24h before the shock
    entry_vol: float      # contracts traded in the entry hour (capacity proxy)
    spread: float         # spread at the shock candle
    hours_to_close: float  # from entry to the earlier of close / expected expiration


def find_shocks(m: dict, q: Series, window: int, thresh: float, close_ts: int) -> list[Shock]:
    mid = (q.bid + q.ask) / 2.0
    out = []
    n = len(mid)
    cum = np.concatenate([[0.0], np.cumsum(q.vol)])
    for t in range(window, n - 1):
        a, b = mid[t - window], mid[t]
        if math.isnan(a) or math.isnan(b) or abs(b - a) < thresh - 1e-9:
            continue
        ie = t + 1
        te = q.t0 + ie * H
        if te >= close_ts:
            continue
        out.append(Shock(
            ticker=m["ticker"], event=m["event_ticker"], series=m["series_ticker"], category=m["category"] or "?",
            t_entry=te, i_entry=ie, move=b - a, mid_before=a, mid_after=b,
            move_vol=float(cum[t + 1] - cum[t - window + 1]),
            vol_24h=float(cum[t + 1] - cum[max(0, t - 23)]),
            entry_vol=float(q.vol[ie]), spread=float(q.ask[t] - q.bid[t]),
            hours_to_close=(close_ts - te) / H))
    return out


def market_close_ts(m: dict) -> int:
    c = data.ts(m["close_time"])
    for k in ("expected_expiration_time", "occurrence_datetime"):
        if m.get(k):
            try:
                c = min(c, data.ts(m[k]))
            except ValueError:
                pass
    return int(c)


# ------------------------------------------------------------------ trades

@dataclass
class Trade:
    shock: Shock
    side: str             # "yes" | "no"
    entry: float          # price paid per contract (before slippage)
    exit: float           # proceeds per contract (settlement 0/1 or book)
    settled: bool
    fee_in: float
    fee_out: float

    def pnl(self, slip: float = 0.0) -> float:
        e = self.entry + slip
        x = self.exit if self.settled else self.exit - slip
        return x - e - self.fee_in - self.fee_out

    def capital(self, slip: float = 0.0) -> float:
        return self.entry + slip + self.fee_in


def simulate(m: dict, q: Series, shocks: list[Shock], direction: str, horizon: int, mult: float,
             real_close: int) -> list[Trade]:
    """Turn shocks into trades, one open position per market at a time."""
    out = []
    busy_until = -1
    result = 1.0 if m["result"] == "yes" else 0.0
    for s in shocks:
        if s.t_entry < busy_until:
            continue
        rose = s.move > 0
        buy_yes = (not rose) if direction == "fade" else rose
        i = s.i_entry
        if buy_yes:
            px = q.ask[i]
        else:
            px = 1.0 - q.bid[i] if not math.isnan(q.bid[i]) else np.nan
        if math.isnan(px) or px <= 0 or px >= 1:
            continue
        t_exit = s.t_entry + horizon * H
        if t_exit >= real_close or q.idx(t_exit) >= len(q.bid):
            xv = result if buy_yes else 1.0 - result
            settled = True
            busy_until = real_close
        else:
            j = q.idx(t_exit)
            xv = q.bid[j] if buy_yes else (1.0 - q.ask[j] if not math.isnan(q.ask[j]) else np.nan)
            if math.isnan(xv):
                xv = 0.0  # nobody to sell to: worst case, mark at zero
            settled = False
            busy_until = t_exit
        out.append(Trade(s, "yes" if buy_yes else "no", float(px), float(xv), settled,
                         fee_per_contract(px, mult), 0.0 if settled else fee_per_contract(xv, mult)))
    return out


# ------------------------------------------------------------------ configs

@dataclass(frozen=True)
class Config:
    window: int = 3
    thresh: float = 0.10
    horizon: int = 24
    direction: str = "fade"
    low_vol: bool = False          # move volume below the train median for its window/thresh
    mid_band: bool = False          # pre-move mid in [0.15, 0.85]
    far_close: bool = False         # >= 48h from entry to close/expected expiration
    max_spread: float = 0.10

    def label(self) -> str:
        return (f"{self.direction} k={self.window}h X={self.thresh * 100:.0f}c H={self.horizon}h"
                f"{' lowvol' if self.low_vol else ''}{' mid' if self.mid_band else ''}"
                f"{' far' if self.far_close else ''}")


GRID = dict(window=[1, 3], thresh=[0.05, 0.10, 0.15], horizon=[6, 24, 72], direction=["fade", "momentum"],
            low_vol=[False, True], mid_band=[False, True], far_close=[False, True])


def grid_configs() -> list[Config]:
    keys = list(GRID)
    return [Config(**dict(zip(keys, vals))) for vals in itertools.product(*(GRID[k] for k in keys))]


class Universe:
    """Markets + quotes, with shocks cached per (window, thresh)."""

    def __init__(self, markets: list[dict], candles: dict, series: dict):
        self.markets, self.quotes, self.mult, self.closes, self.real_closes = [], {}, {}, {}, {}
        for m in markets:
            q = build_quotes(candles.get(m["ticker"]) or [])
            if q is None or len(q.bid) < 24:
                continue
            self.markets.append(m)
            self.quotes[m["ticker"]] = q
            s = series.get(m["series_ticker"]) or {}
            self.mult[m["ticker"]] = float(s.get("fee_multiplier") if s.get("fee_multiplier") is not None else 1)
            self.closes[m["ticker"]] = market_close_ts(m)
            self.real_closes[m["ticker"]] = int(data.ts(m["close_time"]))
        self._shocks: dict = {}
        self.vol_median: dict = {}

    def shocks(self, window: int, thresh: float) -> dict[str, list[Shock]]:
        key = (window, thresh)
        if key not in self._shocks:
            self._shocks[key] = {m["ticker"]: find_shocks(m, self.quotes[m["ticker"]], window, thresh,
                                                          self.closes[m["ticker"]])
                                 for m in self.markets}
        return self._shocks[key]

    def set_vol_medians(self, split_ts: int) -> None:
        """Median move volume per (window, thresh), from train shocks only."""
        for w in GRID["window"]:
            for x in GRID["thresh"]:
                v = [s.move_vol for ss in self.shocks(w, x).values() for s in ss if s.t_entry < split_ts]
                self.vol_median[(w, x)] = float(np.median(v)) if v else 0.0

    def run(self, cfg: Config, lo: int = -1, hi: int = 2 ** 62, categories: set | None = None) -> list[Trade]:
        out = []
        med = self.vol_median.get((cfg.window, cfg.thresh), 0.0)
        for m in self.markets:
            if categories is not None and (m["category"] or "?") not in categories:
                continue
            ss = []
            for s in self.shocks(cfg.window, cfg.thresh)[m["ticker"]]:
                if not (lo <= s.t_entry < hi) or s.spread > cfg.max_spread:
                    continue
                if cfg.low_vol and s.move_vol >= med:
                    continue
                if cfg.mid_band and not (0.15 <= s.mid_before <= 0.85):
                    continue
                if cfg.far_close and s.hours_to_close < 48:
                    continue
                ss.append(s)
            out += simulate(m, self.quotes[m["ticker"]], ss, cfg.direction, cfg.horizon, self.mult[m["ticker"]],
                            self.real_closes[m["ticker"]])
        return out


# ------------------------------------------------------------------ stats

def roi(trades: list[Trade], slip: float = 0.0) -> float:
    cap = sum(t.capital(slip) for t in trades)
    return sum(t.pnl(slip) for t in trades) / cap if cap else float("nan")


def boot_ci(trades: list[Trade], slip: float = 0.0, reps: int = BOOT, seed: int = 1) -> tuple[float, float]:
    by_ev = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        by_ev[t.shock.event][0] += t.pnl(slip)
        by_ev[t.shock.event][1] += t.capital(slip)
    if len(by_ev) < 2:
        return float("nan"), float("nan")
    arr = np.array(list(by_ev.values()))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(reps, len(arr)))
    p, c = arr[idx, 0].sum(1), arr[idx, 1].sum(1)
    r = p / c
    return float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))


def event_t(trades: list[Trade]) -> float:
    """Clustered t-stat of ROI (event sums), for ranking configs on train."""
    by_ev = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        by_ev[t.shock.event][0] += t.pnl()
        by_ev[t.shock.event][1] += t.capital()
    if len(by_ev) < 10:
        return float("nan")
    arr = np.array(list(by_ev.values()))
    r = arr[:, 0].sum() / arr[:, 1].sum()
    resid = arr[:, 0] - r * arr[:, 1]
    se = math.sqrt((resid ** 2).sum()) / arr[:, 1].sum()
    return r / se if se > 0 else float("nan")


def summary(trades: list[Trade]) -> dict:
    evs = {t.shock.event for t in trades}
    lo, hi = boot_ci(trades)
    return dict(n=len(trades), events=len(evs), roi=roi(trades), lo=lo, hi=hi, roi1=roi(trades, 0.01),
                roi2=roi(trades, 0.02), win=float(np.mean([t.pnl() > 0 for t in trades])) if trades else float("nan"),
                pnl=sum(t.pnl() for t in trades), settled=float(np.mean([t.settled for t in trades])) if trades else 0,
                entry_vol=float(np.median([t.shock.entry_vol for t in trades])) if trades else float("nan"),
                vol24=float(np.median([t.shock.vol_24h for t in trades])) if trades else float("nan"))


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    return (f"n={s['n']:5d} ev={s['events']:4d} ROI={s['roi'] * 100:+6.2f}% CI[{s['lo'] * 100:+6.2f},"
            f"{s['hi'] * 100:+6.2f}] +1c={s['roi1'] * 100:+6.2f}% +2c={s['roi2'] * 100:+6.2f}% "
            f"win={s['win'] * 100:4.1f}% settled={s['settled'] * 100:3.0f}%")


def drop_best_events(trades: list[Trade], k: int = 5) -> list[Trade]:
    by_ev = defaultdict(float)
    for t in trades:
        by_ev[t.shock.event] += t.pnl()
    best = set(sorted(by_ev, key=lambda e: -by_ev[e])[:k])
    return [t for t in trades if t.shock.event not in best]


def by_key(trades: list[Trade], key) -> dict:
    g = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return dict(sorted(g.items(), key=lambda kv: str(kv[0])))


def bucket(v: float, edges: list[float], labels: list[str]) -> str:
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ report

def choose_split(u: Universe) -> int:
    base = sorted(s.t_entry for ss in u.shocks(3, 0.10).values() for s in ss)
    return int(base[int(len(base) * SPLIT_FRAC)])


def conditional_tables(u: Universe, split: int, out) -> None:
    """Train-only diagnostics for both directions at the base window/thresh."""
    for direction in ("fade", "momentum"):
        tr = u.run(Config(window=3, thresh=0.05, horizon=24, direction=direction), hi=split)
        out(f"\n### train, {direction}, k=3h X>=5c H=24h: conditional ROI (no slippage)")
        tables = {
            "move size": lambda t: bucket(abs(t.shock.move), [0.10, 0.15, 0.25], ["5-10c", "10-15c", "15-25c", "25c+"]),
            "move volume vs median": lambda t: "low" if t.shock.move_vol < u.vol_median[(3, 0.05)] else "high",
            "pre-move mid": lambda t: bucket(t.shock.mid_before, [0.15, 0.40, 0.60, 0.85],
                                             ["<15c", "15-40c", "40-60c", "60-85c", ">85c"]),
            "hours to close": lambda t: bucket(t.shock.hours_to_close, [24, 72, 24 * 14],
                                               ["<1d", "1-3d", "3-14d", ">14d"]),
            "move direction": lambda t: "up" if t.shock.move > 0 else "down",
            "category": lambda t: t.shock.category,
        }
        for name, fn in tables.items():
            out(f"  by {name}:")
            for k, ts_ in by_key(tr, fn).items():
                if len(ts_) >= 20:
                    out(f"    {k:<24} {fmt(summary(ts_))}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-train-trades", type=int, default=250)
    ap.add_argument("--no-tables", action="store_true")
    args = ap.parse_args(argv)
    out = lambda s="": print(s, flush=True)  # noqa: E731

    markets, candles, series = data.build_sample(lambda s: None)
    u = Universe(markets, candles, series)
    split = choose_split(u)
    u.set_vol_medians(split)
    evs = {m["event_ticker"] for m in u.markets}
    cats = defaultdict(lambda: [0, set()])
    for m in u.markets:
        cats[m["category"]][0] += 1
        cats[m["category"]][1].add(m["event_ticker"])
    first = min(q.t0 for q in u.quotes.values())
    last = max(q.t0 + len(q.bid) * H for q in u.quotes.values())
    out(f"# shock_reversion backtest\n")
    out(f"sample: {len(u.markets)} markets, {len(evs)} events, {len({m['series_ticker'] for m in u.markets})} series, "
        f"candles {iso(first)} .. {iso(last)}; split at {iso(split)} (train before, test after)")
    for c, (n, e) in sorted(cats.items(), key=lambda kv: -kv[1][0]):
        out(f"  {c:<24} {n:5d} markets {len(e):5d} events")

    out("\n## baseline (no filters), both directions, train | test")
    for w, x, h in [(1, 0.05, 6), (3, 0.05, 24), (3, 0.10, 24), (3, 0.15, 24), (3, 0.10, 72)]:
        for d in ("fade", "momentum"):
            c = Config(window=w, thresh=x, horizon=h, direction=d)
            out(f"  {c.label():<34} train {fmt(summary(u.run(c, hi=split)))}")
            out(f"  {'':<34} test  {fmt(summary(u.run(c, lo=split)))}")

    if not args.no_tables:
        conditional_tables(u, split, out)

    configs = grid_configs()
    out(f"\n## config search on train: {len(configs)} configs "
        f"(min {args.min_train_trades} train trades; ranked by event-clustered t of ROI)")
    scored = []
    for c in configs:
        tr = u.run(c, hi=split)
        if len(tr) < args.min_train_trades:
            continue
        scored.append((event_t(tr), c, summary(tr)))
    scored.sort(key=lambda r: -(r[0] if not math.isnan(r[0]) else -1e9))
    n_pos = sum(1 for r in scored if r[2]["roi"] > 0)
    out(f"  {len(scored)} configs had enough trades; {n_pos} had train ROI > 0")
    for tstat, c, s in scored[:10]:
        out(f"  t={tstat:+5.2f} {c.label():<40} {fmt(s)}")
    for d in ("fade", "momentum"):
        best_d = next((r for r in scored if r[1].direction == d), None)
        if best_d:
            out(f"  best {d}: t={best_d[0]:+.2f} {best_d[1].label()}")

    best = scored[0][1]
    out(f"\n## held-out test, chosen config: {best.label()}")
    te = u.run(best, lo=split)
    s = summary(te)
    out(f"  {fmt(s)}")
    out(f"  median contracts traded in the entry hour: {s['entry_vol']:.0f}; in the 24h before the shock: "
        f"{s['vol24']:.0f}")
    dropped = drop_best_events(te)
    out(f"  best 5 events removed: {fmt(summary(dropped))}")
    out("  by category:")
    for k, ts_ in by_key(te, lambda t: t.shock.category).items():
        out(f"    {k:<24} {fmt(summary(ts_))}")
    for d in ("fade", "momentum"):
        best_d = next((r for r in scored if r[1].direction == d), None)
        if best_d and best_d[1] != best:
            out(f"  (for reference, best {d} on train, on test: {fmt(summary(u.run(best_d[1], lo=split)))})")

    # pass criteria
    ok_a = s["n"] >= 150 and s["events"] >= 40
    ok_b = s["roi"] > 0 and s["lo"] > 0
    ok_c = s["roi1"] > 0
    cat_rois = {k: roi(v) for k, v in by_key(te, lambda t: t.shock.category).items() if len(v) >= 20}
    ok_d = roi(dropped) > 0 and sum(1 for r in cat_rois.values() if r > 0) >= max(1, len(cat_rois) // 2)
    out("\n## pass criteria (test)")
    out(f"  (a) >=150 trades over >=40 events: {'PASS' if ok_a else 'FAIL'} ({s['n']} trades, {s['events']} events)")
    out(f"  (b) ROI > 0, 95% event-bootstrap CI excludes 0: {'PASS' if ok_b else 'FAIL'} "
        f"({s['roi'] * 100:+.2f}% [{s['lo'] * 100:+.2f}, {s['hi'] * 100:+.2f}])")
    out(f"  (c) positive at +1c/side: {'PASS' if ok_c else 'FAIL'} ({s['roi1'] * 100:+.2f}%)")
    out(f"  (d) not one category / few events: {'PASS' if ok_d else 'FAIL'} (best-5-removed "
        f"{roi(dropped) * 100:+.2f}%, categories positive {sum(1 for r in cat_rois.values() if r > 0)}/{len(cat_rois)})")
    out(f"  (e) capacity: median {s['entry_vol']:.0f} contracts traded in the entry hour")
    out(f"  VERDICT: {'PASS' if ok_a and ok_b and ok_c and ok_d else 'FAIL'}")


if __name__ == "__main__":
    main()
