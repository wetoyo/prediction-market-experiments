"""Which settled markets get candles fetched (step 2 of the data pull).

The universe (fetch_universe.py) is every settled binary market since
2025-10-24 in ~2,600 series: ~1M markets, two-thirds of them sports props and
state gas-price ladders. Historical candles are one request per market on a
shared IP, so the candle sample is a stratified draw of ~17k markets:

- eligible: final volume >= MIN_VOLUME contracts and open >= 2h;
- each category gets a quota (QUOTA, DEFAULT_QUOTA otherwise; the
  mechanical stratum shares MECH_QUOTA);
- within a category, series families (first 8 characters of the ticker, so
  the ~50 per-state `KXAAAGAS*` series count once) take turns adding one
  whole random event each (up to PER_EVENT of its markets, drawn at random)
  until the quota is filled, so no single family dominates and the months are
  covered evenly.

The final-volume filter uses information from after entry. It only picks
the universe; the strategy's own liquidity filter (backtest.py) uses
volume traded in the 24h before entry.
"""

import random
from collections import defaultdict

MIN_VOLUME = 500
PER_EVENT = 8
SEED = 7

QUOTA = {"Sports": 5000, "Economics": 1800, "Politics": 1800, "Elections": 1500, "Entertainment": 2000,
         "Mentions": 1800, "Science and Technology": 1000, "Companies": 800, "World": 800, "Health": 400}
DEFAULT_QUOTA = 300
MECH_QUOTA = 1800

MECH_CATEGORIES = {"Crypto", "Financials", "Commodities", "Climate and Weather"}
FAST_FREQ = {"hourly", "fifteen_min"}


def is_mechanical(series_meta: dict) -> bool:
    """Crypto/index/commodity price ranges and weather readings: near close,
    the cheap buckets are mechanically near-certain (resolution_alpha's and
    weather_mos_alpha's territory). Reported as a separate stratum."""
    return series_meta.get("category") in MECH_CATEGORIES or series_meta.get("frequency") in FAST_FREQ


def family(series_ticker: str) -> str:
    return series_ticker[:8]


def stratum(series_meta: dict) -> str:
    return "mech" if is_mechanical(series_meta) else series_meta.get("category", "?")


def pick(universe: dict, series: dict) -> list[dict]:
    rng = random.Random(SEED)
    # stratum -> family -> list of events (each a list of markets)
    tree = defaultdict(lambda: defaultdict(list))
    for s in sorted(universe):
        meta = series.get(s, {})
        by_event = defaultdict(list)
        for m in universe[s]["markets"]:
            if (m["volume"] >= MIN_VOLUME and m["open_ts"] and m["close_ts"]
                    and m["close_ts"] - m["open_ts"] >= 2 * 3600):
                by_event[m["event"]].append(m)
        for e in sorted(by_event):
            ev = by_event[e]
            if len(ev) > PER_EVENT:  # random rungs, not the most liquid: the tails are the point
                ev = rng.sample(sorted(ev, key=lambda m: m["ticker"]), PER_EVENT)
            tree[stratum(meta)][family(s)].append(ev)
    out = []
    for st in sorted(tree):
        quota = MECH_QUOTA if st == "mech" else QUOTA.get(st, DEFAULT_QUOTA)
        fams = {f: rng.sample(evs, len(evs)) for f, evs in sorted(tree[st].items())}
        taken = 0
        while taken < quota and fams:
            for f in sorted(fams):
                if taken >= quota:
                    break
                ev = fams[f].pop()
                out.extend(ev)
                taken += len(ev)
            fams = {f: evs for f, evs in fams.items() if evs}
    return out
