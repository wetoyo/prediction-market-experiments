"""Backtest: Kalshi S&P 500 / Nasdaq-100 range and above/below markets
priced off VIX / VXN.

For each (market, decision time) row from build_dataset.py:
  spot  S   = ^GSPC / ^NDX close of the latest 60m Yahoo bar ended at or
              before the decision time (bars end at :30, decisions are at :30,
              so the spot is the index at the decision minute);
  vol   V   = ^VIX / ^VXN, latest 60m bar ended at or before the decision
              (VIX bars end on the hour: <= 30 min stale);
  clock T   = (close - now) / 6.5h / 252 trading years;
  sigma     = k * V / 100, with k fit on TRAIN days only, per (index,
              decision time, close hour) -- the intraday vol shape and the
              VIX-vs-realized premium in one number:
              k^2 = mean( ln(X/S)^2 / ((V/100)^2 T) ) over train events,
              X = Kalshi's own settlement value (expiration_value);
  p         = lognormal digital P(lo <= X <= hi) (black_scholes.py).
Trade: buy YES at the minute's yes_ask if p - ask - fee >= thr; buy NO at
1 - yes_bid if (1 - p) - (1 - bid) - fee >= thr. One contract per signal
(flat stake), held to settlement. thr / min price fit on train, test reported
once. Fee = ceil(0.07 n p (1-p) * 100)/100 per n-contract order (n = 10).

    python backtest.py              # train fit + grid, test report
    python backtest.py --spot ladder  # robustness: spot implied by the KXINX/KXNASDAQ100 ladder
"""

import argparse
import gzip
import json
import math
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import black_scholes as bs
import fees
import market_data as md

ET = ZoneInfo("America/New_York")
DATA_DIR = Path(__file__).resolve().parent / "data"
SERIES = ["KXINX", "KXNASDAQ100", "KXINXU", "KXNASDAQ100U"]
TRAIN_FRACTION = 0.65
CONTRACTS_PER_ORDER = 10
THR_GRID = [0.02, 0.04, 0.06, 0.08, 0.10, 0.15]
MIN_PX_GRID = [0.01, 0.05, 0.10]
MIN_TRAIN_TRADES = 300
BOOT = 2000
SESSION_SECONDS = 6.5 * 3600


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def load_rows(series_list) -> list[dict]:
    rows = []
    for s in series_list:
        p = DATA_DIR / f"dataset_{s}.json.gz"
        if not p.exists():
            print(f"(missing {p.name}, skipped)")
            continue
        with gzip.open(p, "rt") as f:
            rows.extend(json.load(f))
    return rows


def attach_market_inputs(rows: list[dict]) -> list[dict]:
    feeds = {idx: md.intraday(idx) for idx in ("SPX", "NDX")}
    out = []
    for r in rows:
        spot_s, vol_s = feeds[r["index"]]
        S = spot_s.value_at(r["ts"], max_stale=1800)
        V = vol_s.value_at(r["ts"], max_stale=5400)
        if S is None or V is None or r.get("expiration_value") is None:
            continue
        bid, ask = r["bid"], r["ask"]
        if not (0.0 <= bid < ask <= 1.0):
            continue
        r = dict(r)
        r["S"], r["V"] = S, V
        r["T"] = (r["close_ts"] - r["ts"]) / SESSION_SECONDS / 252.0
        r["combo"] = (r["index"], hhmm(r["ts"]), hhmm(r["close_ts"]))
        out.append(r)
    return out


def ladder_spot(rows: list[dict]) -> dict:
    """(event, ts) -> spot implied by the daily bucket ladder: the level where
    the cumulative mid-price mass crosses 0.5 (linear within the bucket).
    Uses only that minute's quotes, so lookahead-free."""
    by = defaultdict(list)
    for r in rows:
        if r["series"] in ("KXINX", "KXNASDAQ100"):
            by[(r["date"], r["index"], r["ts"])].append(r)
    out = {}
    for key, rs in by.items():
        bk = [r for r in rs if r["lo"] is not None and r["hi"] is not None]
        if len(bk) < 5:
            continue
        bk.sort(key=lambda r: r["lo"])
        mids = [(r["lo"], r["hi"], (r["bid"] + r["ask"]) / 2) for r in bk]
        tot = sum(m for _, _, m in mids)
        if tot <= 0.5:
            continue
        acc = 0.0
        for lo, hi, m in mids:
            if acc + m >= tot / 2:
                out[key] = lo + (hi - lo) * ((tot / 2 - acc) / m if m > 0 else 0.5)
                break
            acc += m
    return out


def split_days(rows):
    days = sorted({r["date"] for r in rows})
    cut = days[int(len(days) * TRAIN_FRACTION)]
    return cut, days


def fit_k(train_rows) -> dict:
    """k per combo from one observation per (event, decision time)."""
    seen = {}
    for r in train_rows:
        seen[(r["event"], r["ts"])] = r
    acc = defaultdict(list)
    for r in seen.values():
        v = (r["V"] / 100.0) ** 2 * r["T"]
        if v <= 0:
            continue
        acc[r["combo"]].append(math.log(r["expiration_value"] / r["S"]) ** 2 / v)
    allv = [x for xs in acc.values() for x in xs]
    k_all = math.sqrt(sum(allv) / len(allv)) if allv else 1.0
    return {c: (math.sqrt(sum(xs) / len(xs)) if len(xs) >= 20 else k_all) for c, xs in acc.items()} | {"_all": k_all}


def model_prob(r, k: dict, spot=None) -> float:
    S = spot if spot is not None else r["S"]
    sigma = k.get(r["combo"], k["_all"]) * r["V"] / 100.0
    return bs.prob_range(S, r["lo"], r["hi"], sigma, r["T"])


def fee(px: float) -> float:
    return fees.fee_per_contract_cent_rounded(px, CONTRACTS_PER_ORDER)


def signals(rows, k, thr, min_px, spot_map=None):
    out = []
    for r in rows:
        spot = None
        if spot_map is not None:
            spot = spot_map.get((r["date"], r["index"], r["ts"]))
            if spot is None:
                continue
        p = model_prob(r, k, spot)
        bid, ask = r["bid"], r["ask"]
        cand = []
        if 0 < ask < 1:
            cand.append(("yes", p, ask))
        if 0 < bid < 1:
            cand.append(("no", 1 - p, 1 - bid))
        best = None
        for side, q, px in cand:
            if px < min_px or px > 1 - 0.01:
                continue
            edge = q - px - fee(px)
            if edge >= thr and (best is None or edge > best[3]):
                best = (side, q, px, edge)
        if best:
            side, q, px, edge = best
            win = r["result"] if side == "yes" else 1 - r["result"]
            out.append({"date": r["date"], "series": r["series"], "dt": hhmm(r["ts"]), "side": side, "px": px,
                        "q": q, "edge": edge, "win": win, "ticker": r["ticker"]})
    return out


def pnl(trades, slip=0.0, n_per_order=CONTRACTS_PER_ORDER):
    tot_pnl = tot_cost = 0.0
    for t in trades:
        px = min(0.99, t["px"] + slip)
        f = fees.fee_per_contract_cent_rounded(px, n_per_order)
        tot_cost += px + f
        tot_pnl += t["win"] - px - f
    return tot_pnl, tot_cost


def roi(trades, slip=0.0, n_per_order=CONTRACTS_PER_ORDER):
    p, c = pnl(trades, slip, n_per_order)
    return p / c if c else float("nan")


def boot_ci(trades, slip=0.0, seed=7):
    by = defaultdict(list)
    for t in trades:
        by[t["date"]].append(t)
    days = list(by)
    if not days:
        return float("nan"), float("nan")
    agg = {d: pnl(by[d], slip) for d in days}
    rng = random.Random(seed)
    stats = []
    for _ in range(BOOT):
        P = C = 0.0
        for _ in days:
            p, c = agg[rng.choice(days)]
            P += p
            C += c
        stats.append(P / C if C else 0.0)
    stats.sort()
    return stats[int(0.025 * BOOT)], stats[int(0.975 * BOOT)]


def drop_best_days(trades, n=5):
    by = defaultdict(float)
    for t in trades:
        by[t["date"]] += pnl([t])[0]
    best = set(sorted(by, key=by.get, reverse=True)[:n])
    return [t for t in trades if t["date"] not in best]


def calibration(rows, k, spot_map=None):
    n = 0
    bm = bk = lm = lk = 0.0
    eps = 1e-4
    for r in rows:
        spot = None
        if spot_map is not None:
            spot = spot_map.get((r["date"], r["index"], r["ts"]))
            if spot is None:
                continue
        if r["bid"] <= 0 and r["ask"] >= 1:
            continue
        p = model_prob(r, k, spot)
        m = (r["bid"] + r["ask"]) / 2
        y = r["result"]
        bm += (m - y) ** 2
        bk += (p - y) ** 2
        pc, mc = min(max(p, eps), 1 - eps), min(max(m, eps), 1 - eps)
        lk -= y * math.log(pc) + (1 - y) * math.log(1 - pc)
        lm -= y * math.log(mc) + (1 - y) * math.log(1 - mc)
        n += 1
    return {"n": n, "brier_model": bk / n, "brier_market": bm / n, "logloss_model": lk / n, "logloss_market": lm / n}


def implied_vol_diagnostic(rows, k, label):
    """Fit the Kalshi daily ladder's own vol (as a multiple of VIX/VXN) at each
    decision minute, by least squares of model probs on bucket mids at the
    Yahoo spot; then score both vols on the realized log return to the
    settlement (Gaussian log-likelihood per observation). Answers "who had the
    better variance forecast: VIX x k (fit on train) or the Kalshi ladder?"."""
    by = defaultdict(list)
    for r in rows:
        if r["series"] in ("KXINX", "KXNASDAQ100"):
            by[(r["event"], r["ts"])].append(r)
    grid = [0.10 + 0.02 * i for i in range(146)]
    ll_v = ll_m = 0.0
    ratios = []
    n = 0
    for key, rs in by.items():
        rs = [r for r in rs if not (r["bid"] <= 0 and r["ask"] >= 1)]
        if len(rs) < 5:
            continue
        r0 = rs[0]
        best = None
        for g in grid:
            sig = g * r0["V"] / 100
            e = sum((bs.prob_range(r["S"], r["lo"], r["hi"], sig, r["T"]) - (r["bid"] + r["ask"]) / 2) ** 2 for r in rs)
            if best is None or e < best[0]:
                best = (e, g)
        k_m = best[1]
        k_v = k.get(r0["combo"], k["_all"])
        ratios.append(k_m / k_v)
        x = math.log(r0["expiration_value"] / r0["S"])
        for kk, which in ((k_v, "v"), (k_m, "m")):
            var = (kk * r0["V"] / 100) ** 2 * r0["T"]
            ll = -0.5 * math.log(2 * math.pi * var) - x * x / (2 * var)
            if which == "v":
                ll_v += ll
            else:
                ll_m += ll
        n += 1
    if n:
        ratios.sort()
        print(f"  {label}: n={n} ladder-implied k / VIX-model k: median={ratios[n // 2]:.2f} "
              f"[p10 {ratios[n // 10]:.2f}, p90 {ratios[9 * n // 10]:.2f}] | realized-return loglik/obs: "
              f"VIX model={ll_v / n:.3f} ladder={ll_m / n:.3f}")


def summarize(label, trades):
    days = len({t["date"] for t in trades})
    lo, hi = boot_ci(trades)
    p, c = pnl(trades)
    print(f"  {label:28s} trades={len(trades):5d} days={days:4d} cost=${c:8.2f} pnl=${p:+8.2f} "
          f"ROI={roi(trades):+.3f} CI95=[{lo:+.3f},{hi:+.3f}] +1c={roi(trades, 0.01):+.3f} "
          f"+2c={roi(trades, 0.02):+.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default=",".join(SERIES))
    ap.add_argument("--spot", choices=["yahoo", "ladder"], default="yahoo")
    args = ap.parse_args()

    rows = attach_market_inputs(load_rows(args.series.split(",")))
    spot_map = ladder_spot(rows) if args.spot == "ladder" else None
    if spot_map is not None:
        rows = [r for r in rows if r["series"] in ("KXINX", "KXNASDAQ100")]
    cut, days = split_days(rows)
    train = [r for r in rows if r["date"] < cut]
    test = [r for r in rows if r["date"] >= cut]
    ntrain_days = len({r["date"] for r in train})
    print(f"rows={len(rows)} days={len(days)} {days[0]}..{days[-1]}  train<{cut} ({ntrain_days} days, "
          f"{len(train)} rows)  test>={cut} ({len(days) - ntrain_days} days, {len(test)} rows)")
    for s in sorted({r["series"] for r in rows}):
        rs = [r for r in rows if r["series"] == s]
        print(f"  {s:13s} rows={len(rs):6d} events={len({r['event'] for r in rs}):4d} "
              f"days={len({r['date'] for r in rs})}")

    k = fit_k(train)
    print("k (sigma = k * VIX/VXN), fit on train:")
    for c in sorted(x for x in k if x != "_all"):
        print(f"  {c}: {k[c]:.3f}")
    print(f"  all: {k['_all']:.3f}")

    print("\ncalibration (all rows with a two-sided-ish quote):")
    for lbl, rs in (("train", train), ("test", test)):
        c = calibration(rs, k, spot_map)
        print(f"  {lbl}: n={c['n']} Brier model={c['brier_model']:.4f} market={c['brier_market']:.4f} | "
              f"logloss model={c['logloss_model']:.4f} market={c['logloss_market']:.4f}")

    if spot_map is None:
        print("\nvariance forecast, VIX model vs Kalshi ladder (daily series):")
        implied_vol_diagnostic(train, k, "train")
        implied_vol_diagnostic(test, k, "test")

    print("\ntrain grid (thr x min price):")
    best = None
    for thr in THR_GRID:
        for mp in MIN_PX_GRID:
            tr = signals(train, k, thr, mp, spot_map)
            r_ = roi(tr)
            print(f"  thr={thr:.2f} min_px={mp:.2f} trades={len(tr):5d} ROI={r_:+.3f} +1c={roi(tr, 0.01):+.3f}")
            if len(tr) >= MIN_TRAIN_TRADES and (best is None or r_ > best[0]):
                best = (r_, thr, mp)
    if best is None:
        print("no config with enough train trades")
        return
    _, thr, mp = best
    print(f"\nchosen on train: thr={thr} min_px={mp}")
    tr_tr = signals(train, k, thr, mp, spot_map)
    te = signals(test, k, thr, mp, spot_map)
    print("TRAIN:")
    summarize("all", tr_tr)
    print("TEST (held out, reported once):")
    summarize("all", te)
    summarize("best 5 days removed", drop_best_days(te))
    print(f"  fee stress, 1-contract orders: ROI={roi(te, 0, 1):+.3f}")
    for s in sorted({t["series"] for t in te}):
        summarize(f"series {s}", [t for t in te if t["series"] == s])
    for dt in sorted({t["dt"] for t in te}):
        summarize(f"decision {dt} ET", [t for t in te if t["dt"] == dt])
    for side in ("yes", "no"):
        summarize(f"side {side}", [t for t in te if t["side"] == side])
    for lo_, hi_ in ((0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 0.9), (0.9, 1.0)):
        summarize(f"price {lo_:.1f}-{hi_:.1f}", [t for t in te if lo_ <= t["px"] < hi_])
    # traded-subset calibration
    if te:
        bq = sum((t["q"] - t["win"]) ** 2 for t in te) / len(te)
        bp = sum((t["px"] - t["win"]) ** 2 for t in te) / len(te)
        print(f"  traded rows: mean model prob of side={sum(t['q'] for t in te) / len(te):.3f} "
              f"mean price={sum(t['px'] for t in te) / len(te):.3f} win rate={sum(t['win'] for t in te) / len(te):.3f} "
              f"Brier model={bq:.4f} price={bp:.4f}")
    out = DATA_DIR / f"test_trades_{args.spot}.json"
    out.write_text(json.dumps(te))


if __name__ == "__main__":
    main()
