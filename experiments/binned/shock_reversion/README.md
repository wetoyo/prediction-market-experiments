# shock_reversion (binned 2026-10-24)

**Question:** in thin Kalshi books, do large, fast mid moves on markets
with no continuously quoted underlying partly revert, enough to fade them
at the executable price after fees? Or do they keep going (momentum), enough
to follow them?

**Backtest answer (2026-10-24): no, in either direction.** Across 3,520
markets in 1,247 events and 12 categories over 12 months, the mid moves
about 0c on average after a shock (±0.2c at 24h, under ±1c in any
category). A round trip costs about 6c per contract, made up of the spread
on both legs plus the taker fee. Every one of the 576 configs searched lost
money on train. The config chosen on train lost 14% on the held-out test
(108 trades, 57 events). It failed all four pass criteria. There is no live
runner, and none is worth building on these results.

## The strategy

- **Shock:** on hourly candles, the yes mid moves by at least X within k
  hours (`|mid_t - mid_{t-k}| >= X`). The spread must be at most
  `max_spread` at both ends of the move, so a spread blowing out doesn't
  count as a move.
- **Entry:** at the **next** candle's close, an hour after the shock
  candle closed. The price is executable: buy YES at `yes_ask`, buy NO at
  `1 - yes_bid`. `fade` buys the side that just fell. `momentum` buys the
  side that just rose.
- **Exit:** H hours later at the other side of the book (sell YES at
  `yes_bid`, sell NO at `1 - yes_ask`). If the market closes first, the
  trade takes the settlement value instead. One open position per market at
  a time.
- **Costs:** Kalshi's taker fee on entry, times the series
  `fee_multiplier`, and again on exit unless the trade settled. `fees.py`
  is a copy of `resolution_alpha/fees.py`: `0.07·n·p·(1-p)` rounded up to
  $0.0001, sized at 10 contracts per order. Slippage sensitivity adds +1c
  and +2c per side (entry, and a pre-settlement exit).
- **Filters searched** (`backtest.GRID`, 576 configs):
  - window k = 1 or 3h;
  - X = 5, 10 or 15c;
  - horizon H = 6, 24 or 72h;
  - direction fade or momentum;
  - low-volume moves only (contracts traded during the move below the
    train median);
  - pre-move mid in [15c, 85c];
  - at least 48h from entry to close or expected expiration;
  - max spread 2c or 5c.

## Data

`data.py`, Kalshi public REST, cached in `data/` (gitignored, ~12MB). The
first fetch took 72 minutes for 12.6k requests on a shared IP, 4.7k of them
throttled (429) and retried with backoff. It is capped at 3.5 req/s across
threads.

1. `/series`: 15,390 series. Kept the KX* (2024+) series whose category's
   underlying is not a continuously quoted price, which drops Crypto,
   Financials and Commodities. Dropped hourly, 15-minute and daily series,
   and series whose title names a price level (gas, oil, index, yield,
   mortgage, temperature, ...). 12.2k series were eligible.
2. A seeded, stratified random sample of 1,434 of those series, up to
   250 per category (`SERIES_PER_CATEGORY`). The first attempt paged every
   settled event since the window start. That came to about 10k pages,
   swamped by sports games: too slow on a rate-limited, shared IP.
3. Settled markets per series from `/markets` (the live set) and
   `/historical/markets` (archived before `/historical/cutoff`,
   2026-08-25), closing after 2025-10-24. 729 series had any.
4. Kept markets with volume ≥ 2,000 contracts and ≥ 3 days of life in the
   window. Capped at 12 events per series and the 4 highest-volume markets
   per event: 4,863 markets.
5. Hourly yes bid/ask candles from `/series/{s}/markets/{t}/candlesticks`
   or `/historical/markets/{t}/candlesticks`. 3,520 markets had at least 24
   hours of candles; 2,646 of them were archived. Hours with no candle
   carry the last quote forward. A bid of 0 or an ask of 1 counts as an
   empty side.

**Final sample:**

| category | markets | events |
|---|---|---|
| Sports | 854 | 331 |
| Mentions | 522 | 135 |
| Climate and Weather (monthly rain etc.) | 514 | 153 |
| Economics | 459 | 157 |
| Science and Technology | 399 | 134 |
| Elections | 277 | 99 |
| Entertainment | 239 | 98 |
| Politics | 161 | 79 |
| other 5 | 95 | 61 |

That is 538 series in all, with candles from 2025-10-24 to 2026-10-24.
**Split:** at 2026-07-16, where 65% of the base config's (k=3h, X=10c)
shocks fall before it. It was set from shock counts only, before any P&L
was computed. Trades are assigned to train or test by entry time.

```bash
python data.py                 # fetch / refresh the cache (~70 min cold)
python backtest.py             # full report: baselines, train tables, search, test, criteria
python backtest.py --offline   # same, cache only
python -m pytest tests         # mechanics: no-lookahead entry, book sides, fees, slippage, settlement
```

The full output is written to `backtest_runs/full.txt` when it's redirected
there.

## Results

Each table row reports four things:

- **ROI:** P&L / capital risked (entry price + entry fee).
- **CI:** a 95% bootstrap over events.
- **drift:** the mid's move from entry to exit in the traded side's favour,
  before spread and fees. This is the raw signal.
- **cost:** drift minus net P&L, i.e. the spread plus fees per contract.

### Baselines, no filters (spread ≤ 5c)

| config | split | trades | events | ROI | 95% CI | +1c | drift | cost |
|---|---|---|---|---|---|---|---|---|
| fade k=1h X=5c H=6h | train | 3,182 | 504 | -12.2% | [-13.5, -11.1] | -16.1% | +0.52c | 6.1c |
| | test | 1,391 | 240 | -12.9% | [-15.2, -10.6] | -16.8% | +0.74c | 6.6c |
| momentum k=1h X=5c H=6h | train | 3,140 | 497 | -11.1% | [-12.0, -10.3] | -14.2% | -0.52c | 6.1c |
| | test | 1,373 | 240 | -12.1% | [-13.8, -10.2] | -15.1% | -0.74c | 6.7c |
| fade k=3h X=10c H=24h | train | 1,487 | 420 | -14.8% | [-17.4, -12.3] | -18.7% | -0.15c | 6.2c |
| | test | 645 | 188 | -11.7% | [-16.7, -6.3] | -15.6% | +0.87c | 6.2c |
| momentum k=3h X=10c H=24h | train | 1,440 | 410 | -9.4% | [-11.2, -7.6] | -12.3% | +0.15c | 6.2c |
| | test | 630 | 186 | -11.2% | [-14.6, -7.4] | -13.9% | -0.87c | 6.2c |
| fade k=3h X=15c H=24h | train | 715 | 311 | -17.3% | [-21.5, -13.4] | -21.4% | -0.42c | 6.4c |
| | test | 365 | 139 | -8.0% | [-15.3, +0.3] | -12.0% | +2.85c | 6.3c |

The signal is close to zero and changes sign between train and test. Fade
and momentum trade the same shocks, so their drifts mirror each other.
Both lose about the cost. Measured from the shock candle's own mid
(entering instantly, which the hourly data can't execute), momentum's drift
is about 1c worse: roughly 1c of a move reverts within the first hour.
That is real but tiny next to a 6c round trip.

### Conditioning (train, fade, k=3h X ≥ 5c H=24h)

None of the conditioning variables produces a positive bucket:

| bucket | trades | ROI | drift |
|---|---|---|---|
| move 5-10c | 3,279 | -11.8% | +0.06c |
| move 10-15c | 505 | -14.5% | -0.51c |
| move 15-25c | 274 | -14.0% | +0.03c |
| move 25c+ | 140 | -9.7% | **+3.56c** |
| low-volume move | 659 | -14.5% | +0.54c |
| high-volume move | 3,539 | -11.8% | +0.01c |
| < 1 day to close | 381 | -9.0% | -0.22c |
| > 14 days to close | 2,184 | -12.3% | +0.28c |
| pre-move mid < 15c | 693 | -7.8% | +0.32c |
| pre-move mid > 85c | 298 | -6.4% | +1.48c |

Two results run against the thesis:

- **Low-volume moves don't revert more.** They revert 0.5c, and their spread
  is wider (7.1c cost vs 5.8c), so they lose more.
- **Only moves of 25c or more revert noticeably,** by 3-4c. That is still
  below the cost, and on 140 trades.

By category (full period, fade k=3h X=10c H=24h), every category loses,
from -10.9% (Economics) to -18.6% (Science and Technology). The drift lies
between -1.9c and +1.3c and the cost between 5.0c and 7.0c. Momentum is
the mirror image: -5% (Elections) to -13% (Economics).

### Search and held-out test

All 576 configs were run on train. 252 had at least 250 train trades, and
**0 of the 252 had train ROI > 0**. The configs were ranked by the
event-clustered t of ROI, and the least bad one was taken to test:

| | trades | events | ROI | 95% CI | +1c | +2c |
|---|---|---|---|---|---|---|
| train: fade k=3h X=10c H=72h, mid band, ≥48h to close, spread ≤ 2c | 319 | 167 | -7.7% | [-14.1, -1.2] | -12.4% | -16.9% |
| **test**, same config | **108** | **57** | **-14.3%** | **[-28.9, +2.4]** | **-18.8%** | **-23.0%** |
| test, best 5 events removed | 96 | 52 | -27.0% | [-39.3, -15.6] | | |
| test, best momentum config on train (k=1h X=15c H=72h) | 168 | 88 | -13.3% | [-23.7, -2.1] | -15.5% | -17.6% |

### Pass criteria (fixed before the run)

| criterion | result |
|---|---|
| (a) ≥ 150 test trades over ≥ 40 events | **FAIL**: 108 trades, 57 events |
| (b) net ROI > 0, event-bootstrap CI excluding 0 | **FAIL**: -14.3% [-28.9, +2.4] |
| (c) net positive at +1c slippage per side | **FAIL**: -18.8% |
| (d) not one category / a handful of events | **FAIL**: -27.0% with the best 5 events removed. The only category with ≥ 20 trades (Climate and Weather) was at 0.0% |
| (e) capacity | median 26 contracts traded in the entry hour (58 for the k=3h X=10c baseline), and about 2,100 in the 24h before the shock |

## Why it failed

1. **There is no signal to fade.** After a 5-15c move in one to three
   hours, the next 6-72 hours' mid change averages within ±1c of zero in
   every category, horizon and volume bucket. Kalshi's non-price markets
   absorb these moves about as information. About 1c reverts inside the
   first hour, and an hourly strategy can't catch it.
2. **The round trip costs about 6c.** Most of these books quote 2-5c wide
   even under the spread filter. Crossing that twice plus the quadratic fee
   (about 1.7c per leg near 50c) needs a drift of 6c or more to break even.
   Only moves of 25c or more come close (3-4c), and there are too few of
   them.
3. **Holding to settlement doesn't help.** It saves the exit spread and
   fee, but the signal is still zero, and settlement adds binary variance.

## Not modeled

- **Intra-hour quotes.** Candles carry hourly closes only, and the entry
  is an hour after the shock. A minute-level version could catch the
  first-hour reversion (about 1c on momentum's side). That is still well
  under the cost.
- **Order book depth.** Capacity is approximated by contracts traded in the
  entry hour.
- **Maker entries** (resting a limit order at the bid). This would save the
  spread and, on most series, the fee, but fills would be adverse-selected,
  and candles can't show queue position.
- **News flags.** "No hard news driver" couldn't be identified from the
  data. The move-volume split was the stand-in, and it went the wrong way.

## If you want to keep going

The pipeline is reusable: a stratified sample of non-price series, both
historical endpoints, throttled and cached fetching, and a no-lookahead
book-side replay with an event bootstrap. What would revive the idea:

- **A maker version.** Rest the fade at the touch after a 25c+ move and only
  take fills. This needs forward logging of order books
  (`../../loggers`), because candles can't model fills.
- **Minute candles on the 25c+ moves only.** Check whether the 3-4c
  reversion is bigger when measured from the shock minute. There are few
  enough such moves to fetch at 1-minute resolution.
- **Lower costs.** A fee holiday or rebate series (`fee_multiplier` 0 or
  0.5: 32 series) would cut the cost by about a third, which is still not
  enough at a drift of about 0c.
