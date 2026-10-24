"""longshot_fade backtest: buy the expensive side of cheap-longshot Kalshi
markets H hours before close, hold to settlement.

    python fetch_universe.py      # 1. settled universe of 503 series (cached)
    python fetch_candles.py       # 2. hourly bid/ask candles for the sample (cached)
    python backtest.py            # 3. grid on train, chosen config once on test, pass/fail
    python backtest.py --describe # calibration tables only

Mechanics (no lookahead):
- entry time = the event's last close_time - H hours, H in HORIZONS, and
  single-market events are dropped (see load_table: a market's own close
  can leak its outcome, because many close early when the outcome happens).
  `--ref market --single` runs the naive per-market clock as a check.
- the quote is the latest hourly candle that ended at or before entry (its
  closing yes_bid / yes_ask). A market without a two-sided quote then (bid
  0 or ask 1) is skipped, as is one that had already closed before entry
  (a live trader would see it closed).
- side "no": the longshot is YES (yes mid in the band); buy NO at
  1 - yes_bid. side "yes": the longshot is NO (1 - yes mid in the band);
  buy YES at yes_ask. side "any": both.
- fee: fees.estimate_fee_dollars at the series' rate (0.07 x fee_multiplier)
  on a FEE_LOT-contract order, per contract. Sensitivities: +1c / +2c
  slippage on the fill, and the cent-rounded fee on a 1-lot.
- one contract per market per config; ROI = sum(pnl) / sum(cost), cost =
  fill + fee. Bootstrap CIs resample whole events.
- split by settlement time: the first TRAIN_FRAC of the sample (by count)
  is train. Every filter is chosen on train; the chosen config runs once on test.
"""

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

import data
import fees
import sample

HORIZONS = (1, 6, 24)
TRAIN_FRAC = 0.65
FEE_LOT = 10
BOOT = 2000
SEED = 11

BANDS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.15), (0.01, 0.10), (0.01, 0.15),
         (0.03, 0.10), (0.03, 0.15), (0.05, 0.15)]
SIDES = ("no", "yes", "any")
MIN_VOL_BEFORE = (0, 100, 1000)
MAX_SPREAD = (0.04, 0.10)
MIN_TRAIN_TRADES = 300
MIN_TRAIN_EVENTS = 60

# pass criteria (fixed before running the test period)
PASS_MIN_TRADES = 150
PASS_MIN_EVENTS = 40


# ---------------------------------------------------------------- rows

def quote_at(candles: list, t: float):
    """(end_ts, bid, ask, vol_24h_before) of the latest candle ending at or
    before t, or None. Candles are [end_ts, bid, ask, volume], ascending."""
    best = None
    vol = 0.0
    for row in candles:
        if row[0] > t:
            break
        best = row
        if row[0] > t - 24 * 3600:
            vol += row[3] or 0.0
    if best is None:
        return None
    return best[0], best[1], best[2], vol


def build_rows(markets: list[dict], candles: dict, series: dict, event_close: dict | None = None) -> list[dict]:
    """One row per (market, horizon) with a two-sided quote at entry.

    The entry clock counts back from the market's own close_time, or, with
    `event_close`, from the last close_time of any market in its event (so a
    ladder rung that closed early because it hit is never the reference for
    its own entry; it is skipped if it had closed before entry)."""
    out = []
    for m in markets:
        cs = candles.get(m["ticker"]) or []
        if not cs:
            continue
        meta = series.get(m["series"], {})
        rate = fees.fee_rate_for_series(meta.get("fee_type"), meta.get("fee_multiplier"))
        ref = event_close.get(m["event"], m["close_ts"]) if event_close else m["close_ts"]
        for h in HORIZONS:
            t = ref - h * 3600
            if (m["open_ts"] and m["open_ts"] > t) or m["close_ts"] <= t:
                continue
            q = quote_at(cs, t)
            if q is None:
                continue
            qt, bid, ask, vol24 = q
            if bid is None or ask is None or bid <= 0 or ask >= 1 or ask < bid:
                continue
            out.append({
                "ticker": m["ticker"], "event": m["event"], "series": m["series"],
                "category": meta.get("category", "?"), "mech": sample.is_mechanical(meta),
                "settled_ts": m["settled_ts"], "h": h, "bid": bid, "ask": ask,
                "mid": (bid + ask) / 2, "spread": ask - bid, "stale_h": (t - qt) / 3600,
                "yes": m["result"] == "yes", "vol24": vol24, "volume": m["volume"], "fee_rate": rate,
                "early": bool(m.get("early")),
            })
    return out


# ---------------------------------------------------------------- trades

class Table:
    """Column arrays over rows, for fast config filtering."""
    def __init__(self, rows: list[dict]):
        self.n = len(rows)
        for k in ("bid", "ask", "mid", "spread", "vol24", "volume", "fee_rate", "settled_ts", "stale_h"):
            setattr(self, k, np.array([r[k] for r in rows], dtype=float))
        self.h = np.array([r["h"] for r in rows], dtype=int)
        self.yes = np.array([r["yes"] for r in rows], dtype=bool)
        self.mech = np.array([r["mech"] for r in rows], dtype=bool)
        self.category = np.array([r["category"] for r in rows])
        self.series = np.array([r["series"] for r in rows])
        self.event = np.array([r["event"] for r in rows])
        self.ticker = np.array([r["ticker"] for r in rows])
        _, self.event_id = np.unique(self.event, return_inverse=True)


def fee_per_contract(price: np.ndarray, rate: np.ndarray, cent_1lot: bool = False) -> np.ndarray:
    """Vectorised fees.estimate_fee_dollars (same formula and rounding; the
    tests check they agree) on a FEE_LOT order, per contract; or the
    cent-rounded fee on a 1-lot."""
    if cent_1lot:
        return np.ceil(np.round(rate * price * (1 - price) * 100, 9)) / 100
    return np.ceil(rate * FEE_LOT * price * (1 - price) * 10000) / 10000 / FEE_LOT


def trades(tb: Table, mask: np.ndarray, side: str, slip: float = 0.0, cent_1lot: bool = False):
    """(idx, price, cost, pnl, side_yes) for rows in mask. side_yes: True where YES was bought."""
    idx = np.nonzero(mask)[0]
    if side == "no":
        buy_yes = np.zeros(len(idx), bool)
    elif side == "yes":
        buy_yes = np.ones(len(idx), bool)
    else:  # any: buy whichever side is the favourite
        buy_yes = tb.mid[idx] > 0.5
    price = np.where(buy_yes, tb.ask[idx], 1 - tb.bid[idx]) + slip
    price = np.minimum(price, 1.0)  # slippage past $1 can't be paid: a 1c-bid longshot leaves nothing
    fee = fee_per_contract(price, tb.fee_rate[idx], cent_1lot)
    cost = price + fee
    win = np.where(buy_yes, tb.yes[idx], ~tb.yes[idx])
    pnl = win.astype(float) - cost
    return idx, price, cost, pnl, buy_yes


def band_mask(tb: Table, side: str, lo: float, hi: float) -> np.ndarray:
    q_yes = tb.mid            # longshot prob when the longshot is YES
    q_no = 1 - tb.mid         # ... when it is NO
    m_no = (q_yes >= lo) & (q_yes <= hi)
    m_yes = (q_no >= lo) & (q_no <= hi)
    return {"no": m_no, "yes": m_yes, "any": m_no | m_yes}[side]


def roi(cost, pnl) -> float:
    c = cost.sum()
    return float(pnl.sum() / c) if c > 0 else float("nan")


def cluster_stats(event_id, cost, pnl):
    """ROI and its event-clustered standard error (delta method)."""
    if len(cost) == 0:
        return float("nan"), float("nan")
    r = roi(cost, pnl)
    ev, inv = np.unique(event_id, return_inverse=True)
    resid = np.bincount(inv, weights=pnl - r * cost)
    se = math.sqrt((resid ** 2).sum()) / cost.sum()
    return r, se


def boot_ci(event_id, cost, pnl, n=BOOT, seed=SEED):
    """95% CI of ROI, resampling whole events with replacement."""
    if len(cost) == 0:
        return float("nan"), float("nan")
    ev, inv = np.unique(event_id, return_inverse=True)
    c = np.bincount(inv, weights=cost)
    p = np.bincount(inv, weights=pnl)
    rng = np.random.default_rng(seed)
    k = len(ev)
    draws = rng.integers(0, k, size=(n, k))
    rs = p[draws].sum(1) / c[draws].sum(1)
    return float(np.percentile(rs, 2.5)), float(np.percentile(rs, 97.5))


# ---------------------------------------------------------------- configs

@dataclass(frozen=True)
class Config:
    side: str
    lo: float
    hi: float
    h: int
    universe: str        # "all" (non-mechanical), "mech", or a category name
    min_vol24: float
    max_spread: float

    def label(self):
        return (f"{self.side} band {self.lo:.2f}-{self.hi:.2f} H={self.h}h {self.universe} "
                f"vol24>={self.min_vol24:g} spread<={self.max_spread:.2f}")


def config_mask(tb: Table, cfg: Config, period: np.ndarray) -> np.ndarray:
    m = period & (tb.h == cfg.h) & band_mask(tb, cfg.side, cfg.lo, cfg.hi)
    m &= (tb.vol24 >= cfg.min_vol24) & (tb.spread <= cfg.max_spread + 1e-9)
    if cfg.universe == "all":
        m &= ~tb.mech
    elif cfg.universe == "mech":
        m &= tb.mech
    else:
        m &= (tb.category == cfg.universe) & ~tb.mech
    return m


def evaluate(tb: Table, cfg: Config, period: np.ndarray, slip=0.0, cent_1lot=False, ci=False) -> dict:
    idx, price, cost, pnl, _ = trades(tb, config_mask(tb, cfg, period), cfg.side, slip, cent_1lot)
    ev = tb.event_id[idx]
    r, se = cluster_stats(ev, cost, pnl)
    out = {"n": len(idx), "events": len(np.unique(ev)), "roi": r, "se": se,
           "lb": r - 1.96 * se if len(idx) else float("nan"),
           "win": float(np.mean(pnl + cost > 0.5)) if len(idx) else float("nan"),  # contract paid $1
           "avg_price": float(price.mean()) if len(idx) else float("nan"),
           "pnl": float(pnl.sum())}
    if ci:
        out["ci"] = boot_ci(ev, cost, pnl)
    return out


def grid(tb: Table, period: np.ndarray) -> list[tuple[Config, dict]]:
    cats = sorted(set(tb.category[~tb.mech]))
    res = []
    for side in SIDES:
        for lo, hi in BANDS:
            for h in HORIZONS:
                for uni in ["all", *cats]:
                    for mv in MIN_VOL_BEFORE:
                        for sp in MAX_SPREAD:
                            cfg = Config(side, lo, hi, h, uni, mv, sp)
                            res.append((cfg, evaluate(tb, cfg, period)))
    return res


def choose(results) -> tuple[Config, dict] | None:
    """Train-only rule, fixed in advance: among configs with enough train
    trades and events, the highest event-clustered 95% lower bound on ROI."""
    ok = [(c, r) for c, r in results if r["n"] >= MIN_TRAIN_TRADES and r["events"] >= MIN_TRAIN_EVENTS]
    if not ok:
        return None
    return max(ok, key=lambda cr: cr[1]["lb"])


# ---------------------------------------------------------------- reports

def split(tb: Table):
    cut = float(np.quantile(tb.settled_ts, TRAIN_FRAC))
    return tb.settled_ts < cut, tb.settled_ts >= cut, cut


def fmt(r: dict) -> str:
    s = (f"n={r['n']:5d} ev={r['events']:4d} roi={r['roi'] * 100:+6.2f}% (se {r['se'] * 100:4.2f}) "
         f"win={r['win'] * 100:5.1f}% px={r['avg_price']:.3f}")
    if "ci" in r:
        s += f" CI[{r['ci'][0] * 100:+.2f}%, {r['ci'][1] * 100:+.2f}%]"
    return s


def describe(tb: Table, period: np.ndarray, title: str):
    """Calibration: realised YES rate against the yes mid, by bucket and H."""
    print(f"\n== calibration ({title}): yes mid bucket -> realised YES rate, n ==")
    edges = [0, .03, .05, .10, .15, .25, .50, .75, .85, .90, .95, .97, 1.0]
    for mech in (False, True):
        print(f"-- {'mechanical' if mech else 'non-mechanical'} --")
        print("bucket       " + "  ".join(f"H={h:>2}h            " for h in HORIZONS))
        for a, b in zip(edges[:-1], edges[1:]):
            cells = []
            for h in HORIZONS:
                m = period & (tb.h == h) & (tb.mech == mech) & (tb.mid >= a) & (tb.mid < b)
                n = int(m.sum())
                cells.append(f"{tb.mid[m].mean() if n else 0:.3f}->{tb.yes[m].mean() if n else 0:.3f} n={n:<5d}")
            print(f"{a:.2f}-{b:.2f}    " + "  ".join(cells))


def pass_report(tb: Table, cfg: Config, test: np.ndarray) -> dict:
    """Run the chosen config once on test and check the five criteria."""
    base = evaluate(tb, cfg, test, ci=True)
    s1 = evaluate(tb, cfg, test, slip=0.01)
    s2 = evaluate(tb, cfg, test, slip=0.02)
    cent = evaluate(tb, cfg, test, cent_1lot=True)
    idx, price, cost, pnl, _ = trades(tb, config_mask(tb, cfg, test), cfg.side)
    print(f"\n== TEST (once): {cfg.label()} ==")
    print(f"  base          {fmt(base)}")
    print(f"  +1c slippage  {fmt(s1)}")
    print(f"  +2c slippage  {fmt(s2)}")
    print(f"  cent fee 1lot {fmt(cent)}")
    # per category / series
    print("  per category:")
    for cat in sorted(set(tb.category[idx])):
        k = tb.category[idx] == cat
        print(f"    {cat:24s} n={k.sum():5d} ev={len(np.unique(tb.event_id[idx][k])):4d} "
              f"roi={roi(cost[k], pnl[k]) * 100:+6.2f}% pnl={pnl[k].sum():+.2f}")
    ser = defaultdict(float)
    for s, p in zip(tb.series[idx], pnl):
        ser[s] += p
    top_series = sorted(ser.items(), key=lambda kv: -kv[1])[:5]
    print("  top series by pnl:", ", ".join(f"{s} {p:+.2f}" for s, p in top_series))
    # best 5 events removed
    ev_pnl = defaultdict(float)
    for e, p in zip(tb.event_id[idx], pnl):
        ev_pnl[e] += p
    best5 = set(sorted(ev_pnl, key=lambda e: -ev_pnl[e])[:5])
    keep = np.array([e not in best5 for e in tb.event_id[idx]])
    ex5 = roi(cost[keep], pnl[keep]) if keep.any() else float("nan")
    top_s = top_series[0][0] if top_series else None
    keep_s = tb.series[idx] != top_s
    ex_series = roi(cost[keep_s], pnl[keep_s]) if keep_s.any() else float("nan")
    print(f"  ROI without best 5 events: {ex5 * 100:+.2f}%; without top series {top_s}: {ex_series * 100:+.2f}%")
    # capacity
    v24 = tb.vol24[idx]
    print(f"  capacity: median contracts traded in the 24h before entry {np.median(v24):.0f} "
          f"(p25 {np.percentile(v24, 25):.0f}, p75 {np.percentile(v24, 75):.0f}); "
          f"median final market volume {np.median(tb.volume[idx]):.0f}")
    crit = {
        "a_size": base["n"] >= PASS_MIN_TRADES and base["events"] >= PASS_MIN_EVENTS,
        "b_roi_ci": base["roi"] > 0 and base["ci"][0] > 0,
        "c_slip1": s1["roi"] > 0,
        "d_concentration": ex5 > 0 and ex_series > 0,
        "e_capacity_median_vol24": float(np.median(v24)),
    }
    crit["PASS"] = crit["a_size"] and crit["b_roi_ci"] and crit["c_slip1"] and crit["d_concentration"]
    print("  criteria:", crit)
    return {"base": base, "s1": s1, "s2": s2, "cent": cent, "ex5": ex5, "ex_series": ex_series, "crit": crit}


def load_table(ref: str = "event", single: bool = False) -> Table:
    """ref="event" (default): H counts back from the event's last close, and
    single-market events are dropped unless `single`. Why: many markets
    close early when their outcome happens ("closes early if the event
    occurs"), so counting back from a market's own close_time would know the
    outcome was coming. In a multi-market event the last close is shared
    (a game ending, a data release, a ladder's expiry) and doesn't say which
    market won; a single-market event's close can. ref="market" counts back
    from each market's own close (the naive clock, reported as a check)."""
    import fetch_candles
    markets, candles, series = fetch_candles.load()
    event_close, event_n = defaultdict(float), defaultdict(int)
    for m in fetch_candles.load_universe_markets():
        event_close[m["event"]] = max(event_close[m["event"]], m["close_ts"])
        event_n[m["event"]] += 1
    if not single:
        markets = [m for m in markets if event_n[m["event"]] >= 2]
    return Table(build_rows(markets, candles, series, event_close if ref == "event" else None))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--train-only", action="store_true", help="never touch the test period (for debugging)")
    ap.add_argument("--ref", choices=("market", "event"), default="event",
                    help="count H back from the event's last close (default) or the market's own close")
    ap.add_argument("--single", action="store_true", help="keep single-market events (their close can leak the outcome)")
    args = ap.parse_args()
    tb = load_table(args.ref, args.single)
    print(f"clock: H before the {args.ref}'s close; single-market events {'kept' if args.single else 'dropped'}")
    train, test, cut = split(tb)
    print(f"rows (market x horizon with a two-sided quote): {tb.n}; markets {len(set(tb.ticker))}, "
          f"events {len(set(tb.event))}, series {len(set(tb.series))}")
    print(f"settled {data.iso(tb.settled_ts.min())} .. {data.iso(tb.settled_ts.max())}; "
          f"train < {data.iso(cut)} <= test")
    if args.train_only:
        test = np.zeros(tb.n, bool)
    describe(tb, train, "train")
    if args.describe:
        return

    # the literal idea, unoptimised, on train and test (reported, not selected)
    print("\n== fixed headline configs (not optimised) ==")
    for side, lo, hi in (("no", 0.01, 0.10), ("no", 0.01, 0.15), ("yes", 0.01, 0.10), ("yes", 0.01, 0.15)):
        for h in HORIZONS:
            for uni in ("all", "mech"):
                cfg = Config(side, lo, hi, h, uni, 0, 0.10)
                print(f"  {cfg.label():70s} TRAIN {fmt(evaluate(tb, cfg, train))}")
                if test.any():
                    print(f"  {'':70s} TEST  {fmt(evaluate(tb, cfg, test, ci=True))}")

    res = grid(tb, train)
    ranked = sorted([cr for cr in res if cr[1]["n"] >= MIN_TRAIN_TRADES and cr[1]["events"] >= MIN_TRAIN_EVENTS],
                    key=lambda cr: -cr[1]["lb"])
    print(f"\n== train grid: {len(res)} configs, {len(ranked)} with >= {MIN_TRAIN_TRADES} trades / "
          f">= {MIN_TRAIN_EVENTS} events; top 15 by clustered lower bound ==")
    for c, r in ranked[:15]:
        print(f"  {c.label():70s} {fmt(r)}")
    pos = sum(1 for _, r in ranked if r["roi"] > 0)
    print(f"  configs with positive train ROI: {pos}/{len(ranked)}")
    # by category, best train config per category (diagnostic)
    print("\n== per-category: best train config (diagnostic) and its test result ==")
    for cat in sorted(set(c.universe for c, _ in ranked)):
        cr = [x for x in ranked if x[0].universe == cat]
        if cr:
            c, r = cr[0]
            print(f"  {c.label():70s} TRAIN {fmt(r)}")
            if test.any():
                print(f"  {'':70s} TEST  {fmt(evaluate(tb, c, test))}")

    chosen = choose(res)
    if chosen is None:
        print("no config met the train size floor")
        return
    cfg, r = chosen
    print(f"\n== chosen on train: {cfg.label()} ==\n  TRAIN {fmt(r)}")
    if test.any():
        pass_report(tb, cfg, test)


if __name__ == "__main__":
    main()
