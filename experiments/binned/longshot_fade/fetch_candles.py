"""Step 2 of the data pull: hourly yes_bid/yes_ask candles for the sample
(sample.py) of the universe fetch_universe.py cached. Resumable."""

import data
import sample
from fetch_universe import MAX_PAGES, START, pick_series


def _universe(series):
    return data.load_universe(pick_series(series), START, MAX_PAGES)


def load_universe_markets() -> list[dict]:
    return [m for v in _universe(data.load_series()).values() for m in v["markets"]]


def load(fetch: bool = False):
    series = data.load_series()
    universe = _universe(series)
    markets = sample.pick(universe, series)
    if fetch:
        candles = data.fetch_candles(markets, f"{int(START)}")
    else:
        path = data.DATA_DIR / f"candles_{int(START)}.json.gz"
        candles = data._read(path) if path.exists() else {}
    return markets, candles, series


if __name__ == "__main__":
    markets, candles, series = load(fetch=True)
    have = sum(1 for m in markets if candles.get(m["ticker"]))
    print(f"sample: {len(markets)} markets, {have} with candles")
