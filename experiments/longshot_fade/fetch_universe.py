"""Step 1 of the data pull: series metadata + the settled universe of the
sampled series (cached under data/; rerun resumes).

Series sample: candidates are every series with >= 1M contracts traded
all-time, plus every series with >= 100k in the smaller categories (so
World, Health, Companies, Science, Economics, ... aren't crowded out by
sports). That is ~2,600 series; the shared IP's rate limit (heavy 429s with
other agents running) made paging all of them impractical, so per stratum
(category, or "mech") the TOP_FAMILIES series families (sample.family) with
the most volume are kept, and within a family its SERIES_PER_FAMILY biggest
series. Each series is paged newest-first, at most MAX_PAGES x 1000 markets
from each of the live and historical sets, so the largest series (sports
props, table tennis) are covered only for their most recent markets in each
set. The mechanical stratum (crypto/index/commodity ranges, weather) is kept
only as a separate stratum.
"""
from collections import defaultdict

from datetime import datetime, timezone

import data

START = datetime(2025, 10, 24, tzinfo=timezone.utc).timestamp()
TOP_FAMILIES = {"Sports": 80, "mech": 30}
DEFAULT_TOP_FAMILIES = 40
SERIES_PER_FAMILY = 2
MAX_PAGES = 3
BIG_CATEGORIES = {"Sports", "Elections", "Entertainment", "Politics", "Mentions"}
FAST_FREQ = {"hourly", "fifteen_min"}


def candidate_series(series: dict) -> list[str]:
    out = []
    for t, s in series.items():
        v = s["volume"]
        if v >= 1e6 or (v >= 1e5 and s["category"] not in BIG_CATEGORIES):
            out.append(t)
    return sorted(out)


def pick_series(series: dict) -> list[str]:
    import sample
    fams = defaultdict(lambda: defaultdict(list))
    for t in candidate_series(series):
        fams[sample.stratum(series[t])][sample.family(t)].append(t)
    out = []
    for st, f in fams.items():
        k = TOP_FAMILIES.get(st, DEFAULT_TOP_FAMILIES)
        ranked = sorted(f.items(), key=lambda kv: -sum(series[t]["volume"] for t in kv[1]))[:k]
        for _, ts in ranked:
            out.extend(sorted(ts, key=lambda t: -series[t]["volume"])[:SERIES_PER_FAMILY])
    return sorted(out)


if __name__ == "__main__":
    s = data.load_series()
    picked = pick_series(s)
    print(f"series: {len(s)}, picked {len(picked)}")
    u = data.load_universe(picked, START, MAX_PAGES)
    n = sum(len(v["markets"]) for v in u.values())
    print(f"universe: {n} settled binary markets in {len(u)} series since {data.iso(START)}; "
          f"{sum(v['truncated'] for v in u.values())} series truncated")
