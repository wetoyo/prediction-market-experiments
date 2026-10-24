"""Step 1 of the data pull: series metadata + the settled universe of the
sampled series (cached under data/; rerun resumes).

Series sample: every series with >= 1M contracts traded all-time, plus every
series with >= 100k in the smaller categories (so World, Health, Companies,
Science, Economics, Financials, ... aren't crowded out by sports). Hourly
and 15-minute series (crypto/index/commodity ranges, hourly temperature) are
capped at 2 pages per endpoint: they are the "mechanically near-certain"
longshots resolution_alpha already covers, kept only as a separate stratum.
"""
from datetime import datetime, timezone

import data

START = datetime(2025, 10, 24, tzinfo=timezone.utc).timestamp()
BIG_CATEGORIES = {"Sports", "Elections", "Entertainment", "Politics", "Mentions"}
FAST_FREQ = {"hourly", "fifteen_min"}


def pick_series(series: dict) -> list[str]:
    out = []
    for t, s in series.items():
        v = s["volume"]
        if v >= 1e6 or (v >= 1e5 and s["category"] not in BIG_CATEGORIES):
            out.append(t)
    return sorted(out)


if __name__ == "__main__":
    s = data.load_series()
    picked = pick_series(s)
    print(f"series: {len(s)}, picked {len(picked)}")
    caps = {t: 2 for t in picked if s[t]["frequency"] in FAST_FREQ}
    u = data.load_universe(picked, START, caps)
    n = sum(len(v["markets"]) for v in u.values())
    print(f"universe: {n} settled binary markets in {len(u)} series since {data.iso(START)}; "
          f"{sum(v['truncated'] for v in u.values())} series truncated")
