"""weather_mos_alpha backtest: MOS-priced buckets vs Kalshi's executable asks.

    python data.py            # fetch / extend the cache (slow first time)
    python backtest.py        # model selection + threshold selection on train, test once

Method (all choices made on train, test reported once):
 1. Error model. For every city and decision time, pair the MOS forecasts
    usable at that time (runtime + publication delay <= decision time) with
    the NWS CLI daily max, for event dates ERROR_HISTORY_START .. SPLIT_DATE.
    Pick the model variant (center: NBM / mean of NBM+GFS+NAM / NBM+GFS;
    spread: seasonal stdev / NBM's own spread rescaled; normal vs t) by
    integer log-loss on the last 180 training days with the model fit on the
    days before; refit the winner on everything before SPLIT_DATE.
 2. Candidates. For each Kalshi event (city-day) in the backtest window and
    each decision time, price every bucket, read the hourly candle that
    closes at the decision time: YES fills at yes_ask, NO at 1 - yes_bid.
    Fee: ceil(0.07 p (1-p) * 100)/100 per contract (cent rounding, stricter
    than the real $0.0001 rounding). Skip a side with no quote and markets
    that never traded.
 3. Rule. Buy a side when model prob - price - fee > THRESHOLD and the price
    is >= MIN_PRICE (and <= 1 - MIN_PRICE). Grid over threshold, min price,
    side(s) and decision time(s) on train events, pick by train net PnL
    (>= 150 trades), then run that one config on test.
 4. Stats: 1 contract per trade, ROI = net PnL / (price + fee) summed.
    95% CI by bootstrap over event *dates* (all cities' buckets of a date
    resampled together -- weather and flow are correlated within a day),
    slippage +1c/+2c on the same trades, per city, best-5-days removed,
    model vs market calibration, and a fractional-Kelly bankroll replay.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import config
import data
import fees
import model

RUNS_DIR = Path(__file__).resolve().parent / "backtest_runs"


# ------------------------------------------------------------------ loading

def load_city(series: str, need_candles: bool = True) -> dict:
    station, tz = config.CITIES[series]
    months = data._months(config.ERROR_HISTORY_START, config.BACKTEST_END)
    years = sorted({y for y, _ in months})
    idx = {}
    for mdl in config.MOS_MODELS:
        rows = data.load_mos(station, mdl, months, log=lambda *_: None)
        idx[mdl] = model.MosIndex(data.mos_max_forecasts(rows), config.MOS_DELAY_HOURS[mdl])
    obs = data.load_cli(station, years)
    markets = data.backtest_markets(series) if need_candles else []
    candles = data.load_candles(series) if need_candles else {}
    events = defaultdict(list)
    for m in markets:
        if m["ticker"] in candles:
            events[m["event_ticker"]].append(m)
    return {"series": series, "station": station, "tz": tz, "idx": idx, "obs": obs,
            "events": dict(events), "candles": candles}


def error_rows(city: dict, start: str, end: str) -> list[dict]:
    """(features, CLI high) per date in [start, end) and decision time."""
    out = []
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while d < stop:
        day = d.isoformat()
        obs = city["obs"].get(day)
        if obs is not None:
            for label, back, hour in config.DECISION_TIMES:
                ts = model.decision_ts(day, back, hour, city["tz"])
                f = model.features(city["idx"], ts, day)
                if f is not None:
                    out.append({"city": city["series"], "label": label, "day": day, "f": f, "obs": obs})
        d += timedelta(days=1)
    return out


VARIANTS = [dict(center=c, spread=s, dist=dist)
            for c in ("nbs", "mean3", "nbs_gfs") for s in ("seasonal", "nbm") for dist in ("normal", "t")]


def select_error_model(rows: list[dict], split: str, log=print) -> tuple[model.ErrorModel, list]:
    hold_start = (date.fromisoformat(split) - timedelta(days=180)).isoformat()
    fit_rows = [r for r in rows if r["day"] < hold_start]
    hold_rows = [r for r in rows if hold_start <= r["day"] < split]
    table = []
    for v in VARIANTS:
        em = model.ErrorModel(**v).fit(fit_rows)
        ll, n, sq = 0.0, 0, 0.0
        for r in hold_rows:
            pr = em.predict(r["city"], r["label"], r["day"], r["f"])
            if pr is None:
                continue
            ll += model.int_logloss(em, r["obs"], *pr)
            sq += (r["obs"] - pr[0]) ** 2
            n += 1
        table.append((ll / max(n, 1), math.sqrt(sq / max(n, 1)), n, v))
    table.sort(key=lambda t: t[0])
    for ll, rmse, n, v in table:
        log(f"  {v['center']:8s} {v['spread']:9s} {v['dist']:6s}  logloss {ll:.4f}  rmse {rmse:.2f}F  n={n}")
    best = table[0][3]
    em = model.ErrorModel(**best).fit([r for r in rows if r["day"] < split])
    return em, table


# ------------------------------------------------------------------ candidates

def quote_at(candles: list, ts: float, max_stale: float = 2 * 3600):
    """(bid, ask, candle_end) from the last hourly candle closing at or before ts."""
    best = None
    for row in candles:
        if row[0] <= ts:
            best = row
        else:
            break
    if best is None or ts - best[0] > max_stale:
        return None
    bid, ask = best[1], best[2]
    bid = bid if bid is not None and bid > 0 else None
    ask = ask if ask is not None and ask < 1 else None
    return bid, ask


def build_candidates(cities: list[dict], em: model.ErrorModel) -> list[dict]:
    """One row per (event, decision time, market) with model p and quotes."""
    out = []
    for c in cities:
        for ev, ms in c["events"].items():
            day = data.event_date(ev)
            winners = [m for m in ms if m["result"] == "yes"]
            if len(winners) != 1:  # voided / malformed ladder
                continue
            for label, back, hour in config.DECISION_TIMES:
                ts = model.decision_ts(day, back, hour, c["tz"])
                f = model.features(c["idx"], ts, day)
                if f is None:
                    continue
                pr = em.predict(c["series"], label, day, f)
                if pr is None:
                    continue
                mu, sigma = pr
                for m in ms:
                    if m["volume"] <= 0:
                        continue
                    q = quote_at(c["candles"].get(m["ticker"], []), ts)
                    if q is None:
                        continue
                    out.append({
                        "city": c["series"], "event": ev, "day": day, "label": label, "ticker": m["ticker"],
                        "p": model.bucket_prob(em, m, mu, sigma), "mu": mu, "sigma": sigma,
                        "bid": q[0], "ask": q[1], "won": m["result"] == "yes",
                    })
    return out


# ------------------------------------------------------------------ rule + stats

def fee(price: float, rounding: str = "cent") -> float:
    return fees.estimate_fee_dollars_cent(price, 1) if rounding == "cent" else fees.estimate_fee_dollars(price, 1)


def select_trades(cands: list[dict], threshold: float, min_price: float, sides: str, labels: str,
                  slip: float = 0.0, rounding: str = "cent") -> list[dict]:
    """Apply the rule. The decision uses the quoted price; `slip` is added to
    the fill only (same trades, worse fills)."""
    trades = []
    for r in cands:
        if labels != "both" and r["label"] != labels:
            continue
        opts = []
        if sides in ("yes", "both") and r["ask"] is not None:
            opts.append(("yes", r["ask"], r["p"], r["won"]))
        if sides in ("no", "both") and r["bid"] is not None:
            opts.append(("no", 1.0 - r["bid"], 1.0 - r["p"], not r["won"]))
        best = None
        for side, px, prob, win in opts:
            if px < min_price or px > 1 - min_price:
                continue
            edge = prob - px - fee(px, rounding)
            if edge > threshold and (best is None or edge > best[4]):
                best = (side, px, prob, win, edge)
        if best is None:
            continue
        side, px, prob, win, edge = best
        fill = min(px + slip, 0.99)
        cost = fill + fee(fill, rounding)
        trades.append({**r, "side": side, "price": fill, "cost": cost, "prob": prob, "edge": edge,
                       "pnl": (1.0 if win else 0.0) - cost, "win": win})
    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    cost = sum(t["cost"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    return {"trades": n, "days": len({t["day"] for t in trades}), "cost": cost, "pnl": pnl,
            "roi": pnl / cost if cost else 0.0, "win_rate": sum(t["win"] for t in trades) / n if n else 0.0,
            "avg_price": sum(t["price"] for t in trades) / n if n else 0.0,
            "avg_edge": sum(t["edge"] for t in trades) / n if n else 0.0}


def bootstrap_roi(trades: list[dict], key: str = "day", reps: int = 5000, seed: int = 7) -> tuple[float, float]:
    if not trades:
        return (0.0, 0.0)
    groups = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        g = groups[t[key]]
        g[0] += t["pnl"]
        g[1] += t["cost"]
    arr = np.array(list(groups.values()))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(reps, len(arr)))
    pnl = arr[idx, 0].sum(axis=1)
    cost = arr[idx, 1].sum(axis=1)
    roi = pnl / cost
    return float(np.percentile(roi, 2.5)), float(np.percentile(roi, 97.5))


def drop_best_days(trades: list[dict], k: int = 5) -> list[dict]:
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["pnl"]
    best = set(sorted(by_day, key=by_day.get, reverse=True)[:k])
    return [t for t in trades if t["day"] not in best]


def calibration(cands: list[dict]) -> dict:
    """Brier / log-loss per bucket of the model vs the market's normalized
    mid, over ladders (event x decision time) where every bucket has a
    two-sided quote."""
    ladders = defaultdict(list)
    for r in cands:
        ladders[(r["event"], r["label"])].append(r)
    mb = ml = kb = kl = 0.0
    n = 0
    ev_ll_m = ev_ll_k = 0.0
    n_ev = 0
    for rows in ladders.values():
        if any(r["bid"] is None or r["ask"] is None for r in rows):
            continue
        mids = [(r["bid"] + r["ask"]) / 2 for r in rows]
        s = sum(mids)
        if s <= 0:
            continue
        ps = [min(max(r["p"], 1e-4), 1 - 1e-4) for r in rows]
        qs = [min(max(x / s, 1e-4), 1 - 1e-4) for x in mids]
        for r, p, q in zip(rows, ps, qs):
            y = 1.0 if r["won"] else 0.0
            mb += (p - y) ** 2
            kb += (q - y) ** 2
            ml += -(y * math.log(p) + (1 - y) * math.log(1 - p))
            kl += -(y * math.log(q) + (1 - y) * math.log(1 - q))
            n += 1
        if any(r["won"] for r in rows):
            w = [i for i, r in enumerate(rows) if r["won"]][0]
            ev_ll_m += -math.log(ps[w])
            ev_ll_k += -math.log(qs[w])
            n_ev += 1
    if not n:
        return {}
    return {"buckets": n, "ladders": n_ev, "brier_model": mb / n, "brier_market": kb / n,
            "logloss_model": ml / n, "logloss_market": kl / n,
            "winner_logloss_model": ev_ll_m / max(n_ev, 1), "winner_logloss_market": ev_ll_k / max(n_ev, 1)}


def kelly_replay(trades: list[dict], frac: float = 0.25, cap_trade: float = 0.02, cap_event: float = 0.05,
                 cap_day: float = 0.5, bankroll: float = 1000.0) -> dict:
    """Fractional Kelly per trade (f* = (q - c) / (1 - c), q = model prob of
    the side, c = cost/contract), capped per trade, per event and per date
    (cap_day of the morning bankroll in total: a date's ~20 events settle
    together); bankroll marked once per date."""
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t)
    br = peak = bankroll
    mdd = 0.0
    for day in sorted(by_day):
        start = br
        ev_used = defaultdict(float)
        for t in by_day[day]:
            c = t["cost"]
            f = max(0.0, (t["prob"] - c) / (1 - c)) * frac
            stake = min(f, cap_trade) * start
            room = min(cap_event * start - ev_used[t["event"]], cap_day * start - sum(ev_used.values()))
            stake = max(0.0, min(stake, room))
            n = math.floor(stake / c)
            if n <= 0:
                continue
            ev_used[t["event"]] += n * c
            br += n * t["pnl"]
        peak = max(peak, br)
        mdd = max(mdd, (peak - br) / peak)
    return {"start": bankroll, "end": br, "return": br / bankroll - 1, "max_drawdown": mdd}


def _ladders(cands: list[dict]) -> list[list[dict]]:
    lad = defaultdict(list)
    for r in cands:
        lad[(r["event"], r["label"])].append(r)
    out = []
    for rows in lad.values():
        if all(r["bid"] is not None and r["ask"] is not None for r in rows) and any(r["won"] for r in rows):
            out.append(rows)
    return out


def blend_logloss(cands: list[dict], w: float) -> float:
    """Winning-bucket log-loss of p ~ model^w * market^(1-w), renormalized per ladder."""
    tot, n = 0.0, 0
    for rows in _ladders(cands):
        mids = [(r["bid"] + r["ask"]) / 2 for r in rows]
        s = sum(mids)
        q = [max(x / s, 1e-4) for x in mids]
        p = [max(r["p"], 1e-4) for r in rows]
        mix = [pi ** w * qi ** (1 - w) for pi, qi in zip(p, q)]
        z = sum(mix)
        k = [i for i, r in enumerate(rows) if r["won"]][0]
        tot += -math.log(mix[k] / z)
        n += 1
    return tot / max(n, 1)


def blend_weight(cands: list[dict]) -> tuple[float, float]:
    """Weight on the model (0..1) minimizing train log-loss of the pool."""
    best = min(((blend_logloss(cands, w / 20), w / 20) for w in range(21)))
    return best[1], best[0]


GRID = [dict(threshold=th, min_price=mp, sides=sd, labels=lb)
        for th in (0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20)
        for mp in (0.02, 0.05, 0.10)
        for sd in ("yes", "no", "both")
        for lb in ("d-1_12h", "d0_08h", "both")]


def fmt(s: dict) -> str:
    return (f"{s['trades']:5d} trades {s['days']:4d} days  cost ${s['cost']:8.2f}  pnl ${s['pnl']:+8.2f}  "
            f"ROI {s['roi'] * 100:+6.2f}%  win {s['win_rate'] * 100:5.1f}%  avg px {s['avg_price']:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee-rounding", choices=("cent", "bp"), default="cent")
    ap.add_argument("--min-train-trades", type=int, default=150)
    ap.add_argument("--error-model-only", action="store_true", help="stop after step 1 (needs no Kalshi data)")
    args = ap.parse_args()
    t0 = time.time()
    split = config.SPLIT_DATE
    out = {"split": split, "window": [config.BACKTEST_START, config.BACKTEST_END]}

    print("loading ...")
    cities = [load_city(s, need_candles=not args.error_model_only) for s in config.CITIES]

    # --- truth check: CLI high vs the bucket Kalshi settled YES
    agree = total = 0
    for c in cities:
        for ev, ms in c["events"].items():
            obs = c["obs"].get(data.event_date(ev))
            win = [m for m in ms if m["result"] == "yes"]
            if obs is None or len(win) != 1:
                continue
            total += 1
            agree += model.bucket_contains(win[0], int(obs))
    print(f"CLI high lands in Kalshi's winning bucket: {agree}/{total} events")
    out["truth_agreement"] = [agree, total]

    # --- 1. error model on pre-split CLI history
    print(f"\n1. error model (history {config.ERROR_HISTORY_START} .. {split}), holdout = last 180 days before split")
    rows = []
    for c in cities:
        rows += error_rows(c, config.ERROR_HISTORY_START, split)
    em, table = select_error_model(rows, split)
    out["error_model"] = {"chosen": dict(center=em.center, spread=em.spread, dist=em.dist),
                          "table": [(ll, rmse, n, v) for ll, rmse, n, v in table]}
    print(f"  -> {em.center}/{em.spread}/{em.dist}, refit on {sum(1 for r in rows if r['day'] < split)} rows")
    if args.error_model_only:
        return

    # --- 2. candidates
    cands = build_candidates(cities, em)
    train = [r for r in cands if r["day"] < split]
    test = [r for r in cands if r["day"] >= split]
    ev_tr = len({r["event"] for r in train})
    ev_te = len({r["event"] for r in test})
    print(f"\n2. candidates: train {len(train)} bucket-quotes / {ev_tr} events, test {len(test)} / {ev_te} events")
    out["coverage"] = {"train_events": ev_tr, "test_events": ev_te, "train_quotes": len(train),
                       "test_quotes": len(test),
                       "cities": sorted({r["city"] for r in cands}),
                       "first_day": min(r["day"] for r in cands), "last_day": max(r["day"] for r in cands)}

    # --- 3. grid on train
    print("\n3. grid on train (top 10 by net PnL)")
    res = []
    for g in GRID:
        tr = select_trades(train, rounding=args.fee_rounding, **g)
        s = summarize(tr)
        if s["trades"] >= args.min_train_trades:
            res.append((s["pnl"], g, s))
    res.sort(key=lambda x: x[0], reverse=True)
    for pnl, g, s in res[:10]:
        print(f"  {g}  {fmt(s)}")
    if not res or res[0][0] <= 0:
        print("  no config with positive train PnL; testing the best one anyway")
    best = res[0][1] if res else GRID[0]
    out["chosen"] = best
    out["train_top"] = [(g, s) for _p, g, s in res[:10]]

    # --- 4. test once
    print(f"\n4. TEST ({split} .. {config.BACKTEST_END}) with {best}")
    rep = {}
    tr_train = select_trades(train, rounding=args.fee_rounding, **best)
    rep["train"] = summarize(tr_train)
    rep["train"]["ci"] = bootstrap_roi(tr_train)
    tt = select_trades(test, rounding=args.fee_rounding, **best)
    s = summarize(tt)
    s["ci_by_date"] = bootstrap_roi(tt, "day")
    s["ci_by_event"] = bootstrap_roi(tt, "event")
    rep["test"] = s
    print(f"  train : {fmt(rep['train'])}  CI {rep['train']['ci'][0] * 100:+.1f}%..{rep['train']['ci'][1] * 100:+.1f}%")
    print(f"  test  : {fmt(s)}")
    print(f"          95% CI by date {s['ci_by_date'][0] * 100:+.2f}% .. {s['ci_by_date'][1] * 100:+.2f}%, "
          f"by event {s['ci_by_event'][0] * 100:+.2f}% .. {s['ci_by_event'][1] * 100:+.2f}%")
    for slip in (0.01, 0.02):
        ss = summarize(select_trades(test, slip=slip, rounding=args.fee_rounding, **best))
        ss["ci_by_date"] = bootstrap_roi(select_trades(test, slip=slip, rounding=args.fee_rounding, **best))
        rep[f"test_slip{int(slip * 100)}c"] = ss
        print(f"  +{int(slip * 100)}c  : {fmt(ss)}  CI {ss['ci_by_date'][0] * 100:+.1f}%..{ss['ci_by_date'][1] * 100:+.1f}%")
    other = "bp" if args.fee_rounding == "cent" else "cent"
    so = summarize(select_trades(test, rounding=other, **best))
    rep[f"test_fee_{other}"] = so
    print(f"  fee rounding {other}: {fmt(so)}")
    city_pnl = defaultdict(float)
    for t_ in tt:
        city_pnl[t_["city"]] += t_["pnl"]
    top_city = max(city_pnl, key=city_pnl.get) if city_pnl else None
    rep["test_minus_best_city"] = summarize([t_ for t_ in tt if t_["city"] != top_city])
    rep["best_city"] = top_city
    print(f"  -best city ({top_city}): {fmt(rep['test_minus_best_city'])}")
    d5 = drop_best_days(tt)
    rep["test_minus_best5"] = summarize(d5)
    print(f"  -best 5 days: {fmt(rep['test_minus_best5'])}")
    print("  by decision time / side:")
    for lb in ("d-1_12h", "d0_08h"):
        for sd in ("yes", "no"):
            sub = [t for t in tt if t["label"] == lb and t["side"] == sd]
            if sub:
                print(f"    {lb:8s} {sd:3s} {fmt(summarize(sub))}")
                rep[f"test_{lb}_{sd}"] = summarize(sub)
    print("  by city:")
    rep["by_city"] = {}
    for city in sorted({t["city"] for t in tt}):
        sub = [t for t in tt if t["city"] == city]
        rep["by_city"][city] = summarize(sub)
        print(f"    {city:12s} {fmt(rep['by_city'][city])}")
    print("  by price bucket:")
    for lo, hi in ((0, .2), (.2, .5), (.5, .8), (.8, 1.01)):
        sub = [t for t in tt if lo <= t["price"] < hi]
        if sub:
            print(f"    {lo:.1f}-{hi:.1f}: {fmt(summarize(sub))}")

    cal = calibration(test)
    rep["calibration_test"] = cal
    if cal:
        print(f"\n  calibration on test ({cal['buckets']} buckets, {cal['ladders']} ladders):")
        print(f"    Brier    model {cal['brier_model']:.4f}  market {cal['brier_market']:.4f}")
        print(f"    logloss  model {cal['logloss_model']:.4f}  market {cal['logloss_market']:.4f}")
        print(f"    winning-bucket logloss  model {cal['winner_logloss_model']:.4f}  market {cal['winner_logloss_market']:.4f}")
    for lb in ("d-1_12h", "d0_08h"):
        c2 = calibration([r for r in test if r["label"] == lb])
        rep[f"calibration_test_{lb}"] = c2
        if c2:
            print(f"    {lb}: Brier model {c2['brier_model']:.4f} vs market {c2['brier_market']:.4f}; "
                  f"logloss {c2['logloss_model']:.4f} vs {c2['logloss_market']:.4f}")

    # --- diagnostics (reported after the one test above; nothing here is selected on test)
    print("\n  diagnostics:")
    print("    train top-10 configs re-run on test (report only):")
    rep["train_top_on_test"] = []
    for _p, g, s_tr in res[:10]:
        s_te = summarize(select_trades(test, rounding=args.fee_rounding, **g))
        rep["train_top_on_test"].append((g, s_tr["roi"], s_te))
        print(f"      {g['threshold']:.2f} {g['min_price']:.2f} {g['sides']:4s} {g['labels']:8s} "
              f"train ROI {s_tr['roi'] * 100:+6.1f}%  test {fmt(s_te)}")
    w, ll_tr = blend_weight(train)
    ll_te_blend = blend_logloss(test, w)
    ll_te_mkt = blend_logloss(test, 0.0)
    rep["blend"] = {"w_model": w, "train_logloss": ll_tr, "test_logloss_blend": ll_te_blend,
                    "test_logloss_market": ll_te_mkt}
    print(f"    log-linear pool of model and market fit on train: weight on model {w:.2f}; "
          f"test winning-bucket logloss blend {ll_te_blend:.4f} vs market alone {ll_te_mkt:.4f}")
    for lb in ("d-1_12h", "d0_08h"):
        sub = [r for r in cands if r["label"] == lb]
        seen = {}
        for r in sub:
            seen[(r["event"])] = r
        errs = []
        for c in cities:
            for ev, r in seen.items():
                if r["city"] != c["series"]:
                    continue
                obs = c["obs"].get(r["day"])
                if obs is not None:
                    errs.append((r["day"] >= split, obs - r["mu"]))
        for is_test, name in ((False, "train"), (True, "test")):
            e = [x for t_, x in errs if t_ == is_test]
            if e:
                bias = sum(e) / len(e)
                rmse = math.sqrt(sum(x * x for x in e) / len(e))
                rep[f"model_err_{lb}_{name}"] = {"n": len(e), "bias": bias, "rmse": rmse}
                print(f"    model error {lb} {name}: n={len(e)} bias {bias:+.2f}F rmse {rmse:.2f}F")

    kel = kelly_replay(tt)
    rep["kelly_test"] = kel
    print(f"\n  quarter-Kelly replay on test ($1000; caps 2%/trade, 5%/event, 50%/date): "
          f"end ${kel['end']:.0f} ({kel['return'] * 100:+.1f}%), max DD {kel['max_drawdown'] * 100:.1f}%")

    # --- verdict
    t = rep["test"]
    crit = {
        "a_trades>=150": t["trades"] >= 150,
        "b_roi>0_ci_excl_0": t["roi"] > 0 and t["ci_by_date"][0] > 0,
        "c_positive_with_1c_slip": rep["test_slip1c"]["pnl"] > 0,
        "d_positive_without_best5": rep["test_minus_best5"]["pnl"] > 0,
        "d_positive_without_best_city": rep["test_minus_best_city"]["pnl"] > 0,
    }
    rep["criteria"] = crit
    print("\n  pass criteria:", crit, "->", "PASS" if all(crit.values()) else "FAIL")
    out["report"] = rep
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"backtest_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nsaved {path}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
