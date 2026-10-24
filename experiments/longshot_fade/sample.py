"""Which settled markets get candles fetched (step 2 of the data pull).

The universe (fetch_universe.py) is every settled binary market since
2025-10-24 in ~2,600 series. Fetching hourly candles for all of it is too
much for one shared IP (historical candles are one request per market), so:

- keep markets with final volume >= MIN_VOLUME contracts and open >= 2h;
- per event, keep at most PER_EVENT markets, highest volume first (a ladder's
  tails are where the longshots are, so this is generous);
- per series, add whole events in random (seeded) order until PER_SERIES
  markets (PER_SERIES_MECH for the mechanical stratum), so the months are
  covered evenly and a ladder stays together.

The final-volume filter uses information from after entry. It only picks
the universe; the strategy's own liquidity filter (backtest.py) uses
volume traded before entry.
"""

import random
from collections import defaultdict

MIN_VOLUME = 500
PER_EVENT = 20
PER_SERIES = 300
PER_SERIES_MECH = 80
SEED = 7

MECH_CATEGORIES = {"Crypto", "Financials", "Commodities", "Climate and Weather"}
FAST_FREQ = {"hourly", "fifteen_min"}


def is_mechanical(series_meta: dict) -> bool:
    """Crypto/index/commodity price ranges and weather readings: near close,
    the cheap buckets are mechanically near-certain (resolution_alpha's and
    weather_mos_alpha's territory). Reported as a separate stratum."""
    return series_meta.get("category") in MECH_CATEGORIES or series_meta.get("frequency") in FAST_FREQ


def pick(universe: dict, series: dict) -> list[dict]:
    rng = random.Random(SEED)
    out = []
    for s in sorted(universe):
        ms = [m for m in universe[s]["markets"]
              if m["volume"] >= MIN_VOLUME and m["open_ts"] and m["close_ts"]
              and m["close_ts"] - m["open_ts"] >= 2 * 3600]
        by_event = defaultdict(list)
        for m in ms:
            by_event[m["event"]].append(m)
        cap = PER_SERIES_MECH if is_mechanical(series.get(s, {})) else PER_SERIES
        events = sorted(by_event)
        rng.shuffle(events)  # whole events, so a ladder stays together
        kept = []
        for e in events:
            if len(kept) >= cap:
                break
            ev = sorted(by_event[e], key=lambda m: -m["volume"])
            kept.extend(ev[:PER_EVENT])
        out.extend(kept)
    return out
