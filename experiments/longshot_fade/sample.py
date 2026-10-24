"""Which settled markets get candles fetched (step 2 of the data pull).

The universe (fetch_universe.py) is every settled binary market since
2025-10-24 in ~2,600 series: ~1M markets, two-thirds of them sports props and
state gas-price ladders. Historical candles are one request per market on a
shared IP, so the candle sample is a stratified draw of ~30k markets:

- eligible: final volume >= MIN_VOLUME contracts and open >= 2h;
- each category gets a quota (QUOTA, DEFAULT_QUOTA otherwise; the
  mechanical stratum shares MECH_QUOTA);
- within a category, series families (first 8 characters of the ticker, so
  the ~50 per-state `KXAAAGAS*` series count once) take turns adding one
  whole random event each (up to PER_EVENT markets, highest volume first)
  until the quota is filled, so no single family dominates, the months are
  covered evenly, and a ladder stays together.

The final-volume filter uses information from after entry. It only picks
the universe; the strategy's own liquidity filter (backtest.py) uses
volume traded in the 24h before entry.
"""

import random
from collections import defaultdict

MIN_VOLUME = 500
PER_EVENT = 20
SEED = 7

QUOTA = {"Sports": 9000, "Economics": 3000, "Politics": 3000, "Elections": 2500, "Entertainment": 3500,
         "Mentions": 3000, "Science and Technology": 1500, "Companies": 1200, "World": 1200, "Health": 600}
DEFAULT_QUOTA = 500
MECH_QUOTA = 3000

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
            ev = sorted(by_event[e], key=lambda m: -m["volume"])[:PER_EVENT]
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
