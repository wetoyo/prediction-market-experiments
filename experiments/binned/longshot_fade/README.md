# longshot_fade (binned 2026-10-24)

**Question:** does the favorite-longshot bias pay on Kalshi? Cheap contracts
are supposed to resolve YES less often than their price says (Bürgi, Deng
and Whelan 2025, "Makers and Takers", on Kalshi data). The trade: some fixed
time before close (24h, 6h or 1h), buy NO on every contract whose YES trades
in a cheap band, across all categories, and hold to settlement. The mirror
was tested too: buy YES when the NO side is the cheap longshot (YES at
85-99c). Either way you buy the favorite at 85-99c to win 1-15c.

**Backtest answer (2026-10-24): no.** The bias shows up in the calibration
tables, but most of it sits inside the bid/ask spread. Buying the favorite
means paying 1 - yes_bid, not 1 - mid. The config picked on train (NO on YES
1-5c, 6h before close, volume >= 100 in the prior 24h) made +1.22% on train
and **+0.05% on test**, with a 95% event-bootstrap CI of [-1.45%, +1.22%].
It turns negative with 1c of slippage, and it fails four of the five pass
criteria. There is no live runner. One un-optimised variant (YES <= 10c at
6h) was positive on test. It is an unconfirmed lead, not a pass (see
"What might revive it").

## The trade (`backtest.py`)

- **Clock:** entry is H hours before the **event's** last `close_time`,
  for H = 1, 6, 24. Single-market events are dropped. Why: most Kalshi
  markets "close early if the event occurs", so a market's own close time
  can leak its outcome. Counting back from it would know a YES was coming
  within H hours. In a multi-market event, the last close is shared (a game
  ending, a data release, a ladder expiring) and doesn't say which market
  won. A rung that closed before entry is skipped, because a live trader
  would see it closed. `--ref market --single` runs the naive per-market
  clock as a check. It gives the same answer (below).
- **Quote:** the latest hourly candle that *ended* at or before entry, using
  its closing `yes_bid` / `yes_ask`. A market without a two-sided quote then
  (bid 0 or ask 1) is skipped. There is no lookahead.
- **Fill:** side `no` (the YES is the longshot, yes mid in the band) buys
  NO at 1 - yes_bid. Side `yes` (the NO is the longshot) buys YES at
  yes_ask. Side `any` takes whichever is the favorite. One contract per
  market, held to settlement. ROI = sum(pnl) / sum(fill + fee).
- **Fees:** `fees.py` is a verbatim copy of `../../resolution_alpha/fees.py`:
  `ceil(rate · n · p · (1-p) · 1e4) / 1e4`, rounding confirmed against real
  fills. It is charged per contract on a 10-lot, at the series' own rate
  (0.07 × `fee_multiplier`: 0.5 and 0 on a few series). There are two
  sensitivities. One is +1c / +2c slippage. The other is the published
  cent-rounded fee on a 1-lot (`ceil(0.07·p·(1-p)·100)/100`, i.e. a 1c floor
  per contract), the worst case.
- **Strata:** "mechanical" = Crypto, Financials, Commodities, Climate and
  Weather, plus any hourly or 15-minute series (`sample.is_mechanical`).
  Near close, the cheap buckets there are mechanically near-certain, which
  is `resolution_alpha`'s and `weather_mos_alpha`'s territory. They are
  reported separately and excluded from "all".
- **Split:** chronological by settlement time. The first 65% of rows
  (before 2026-08-18 22:10 UTC) are train and the rest are test. The grid
  covers side (3) × band (8: 1-5, 5-10, 10-15, 1-10, 1-15, 3-10, 3-15,
  5-15c) × H (3) × universe ("all" or one category) × volume traded in the
  24h before entry (>= 0 / 100 / 1000) × max spread (4c / 10c). That is
  4,752 configs. The pick was fixed in advance and uses train only: among
  configs with >= 300 train trades over >= 60 events, the highest
  event-clustered 95% lower bound on ROI. That config is run once on test.
- **CIs:** a bootstrap that resamples whole events (2,000 draws), so a
  ladder or a game's props count as one draw.

## Data

The data comes from Kalshi's public REST API (`data.py`), cached in `data/`.
Throttling is <= 3.5 requests/s with exponential backoff on 429/5xx. The IP
is shared with other agents, and 429s were heavy, which drove the
sampling below. Markets settled after the historical cutoff (2026-08-25)
use the live endpoints: `/markets`, and the batch `/markets/candlesticks`
at 100 markets per call. Older ones use `/historical/markets` and the
per-market `/historical/markets/{t}/candlesticks`.

1. **Series** (`fetch_universe.py`). The candidates are all series with
   >= 1M contracts traded, plus those with >= 100k in the smaller
   categories (2,604 series). Paging all of them wasn't practical under the
   rate limit, so each category keeps its 40 highest-volume series families
   (80 for Sports, 30 for mechanical), and each family keeps its 2 biggest
   series. That leaves 503 series. Each one is paged newest-first, up to
   3,000 markets from each of the live and historical sets. This gives
   488,403 settled binary markets from 2025-10-24 to 2026-10-24. 64 big
   series (sports props, table tennis) hit the page cap and are covered
   only for their most recent markets in each set.
2. **Candle sample** (`sample.py`). The sample keeps markets with final
   volume >= 500 that were open >= 2h. Each category gets a quota, and
   within a category the series families take turns adding one random
   whole event (up to 8 random rungs). That gives 16,794 markets, 16,776 of
   which have candles. Hourly candles cover the 30h before close.
3. **Backtest table:** 22,755 market × horizon rows with a two-sided quote
   at entry. They cover 10,500 markets, 2,438 events and 318 series:
   14,784 rows / 1,617 events in train and 7,971 / 821 in test. By
   markets (events): Sports 4,355 (1,124), mechanical 1,550 (275), Mentions
   1,453 (196), Politics 1,109 (201), Economics 862 (226), Entertainment 664
   (217), Science and Technology 353 (117), Elections 110 (70), World 30,
   Companies 13, Health 1.

```bash
python fetch_universe.py   # ~30-60 min under shared-IP 429s (cached after)
python fetch_candles.py    # ~90 min (11k historical markets are one call each)
python backtest.py         # event clock (default); ~40s
python backtest.py --ref market --single   # naive per-market clock
python -m pytest tests
```

## Results

**Calibration (train, non-mechanical, H = 6h: yes mid → realised YES
rate).** The bias is real in the tails: 0-3c contracts resolved YES 0.0% of
the time (n=360), 3-5c resolved 1.0% (n=204), 5-10c 5.3% vs a 7.1% mid
(n=376), and 10-15c 11.3% vs 12.3%. The favorite side mirrors it: 90-95c
resolved 88.8% (n=143), *below* its price. In the mechanical stratum the
cheap buckets are also near-zero, but n is small and the 5-15c buckets are
noisy.

**Where the edge goes** (all periods, non-mechanical, spread <= 10c; buy
NO at 1 - yes_bid):

| YES band | H | n | yes mid | yes bid | realised YES | fee/ct | ROI |
|---|---|---|---|---|---|---|---|
| 1-5c | 1h | 941 | 2.7c | 1.9c | 1.6% | 0.12c | +0.14% |
| 1-5c | 6h | 930 | 2.7c | 1.9c | 1.1% | 0.13c | +0.70% |
| 1-5c | 24h | 1,055 | 2.8c | 2.0c | 1.9% | 0.14c | -0.05% |
| 5-10c | 1h | 576 | 7.2c | 5.7c | 5.9% | 0.36c | -0.54% |
| 5-10c | 6h | 608 | 7.2c | 6.0c | 4.3% | 0.39c | +1.43% |
| 5-10c | 24h | 627 | 7.3c | 5.9c | 4.6% | 0.39c | +0.96% |
| 10-15c | 1h | 356 | 12.3c | 11.0c | 13.2% | 0.65c | -3.23% |
| 10-15c | 6h | 416 | 12.3c | 11.2c | 13.5% | 0.67c | -3.25% |
| 10-15c | 24h | 452 | 12.3c | 11.0c | 12.4% | 0.67c | -2.31% |

Longshots resolve below their *mid*, but the NO buyer pays 1 - *bid*. The
bid sits 1-1.5c under the mid, and at 96-98c the fee is another 0.1-0.4c.
What's left is +1% at best on 97c of capital, and the sign flips between
horizons and periods.

**Fixed configs, not optimised (all non-mechanical, spread <= 10c):**

| trade | H | train ROI (n / events) | test ROI (n / events) | test 95% CI |
|---|---|---|---|---|
| NO on YES 1-10c | 1h | -0.25% (829 / 497) | -0.01% (646 / 280) | [-1.57, +1.39] |
| NO on YES 1-10c | 6h | +0.77% (891 / 490) | **+1.49%** (608 / 252) | **[+0.32, +2.58]** |
| NO on YES 1-10c | 24h | +0.36% (998 / 519) | +0.23% (642 / 263) | [-1.16, +1.43] |
| NO on YES 1-15c | 1h / 6h / 24h | -0.78 / +0.22 / -0.45% | -0.72 / +0.03 / +0.02% | all span 0 |
| YES on YES 90-99c | 1h / 6h / 24h | -1.58 / -2.89 / -1.88% | +1.28 / +1.68 / +1.12% | all span 0 |
| YES on YES 85-99c | 1h / 6h / 24h | -2.06 / -3.23 / -2.45% | +0.39 / +1.67 / +0.13% | all span 0 |
| mechanical, NO on YES 1-10c | 1h / 6h / 24h | -2.40 / -1.55 / +1.91% | -1.56 / -4.70 / +0.57% | all span 0 |

The favorite-YES mirror changes sign between train and test at every
horizon. The mechanical stratum is small (40-60 test events) and goes
both ways.

**Train grid:** 376 of 4,752 configs met the size floor, and only 80 of
those had positive train ROI. The top 15 by clustered lower bound are all
NO on YES 1-5c or 1-10c at 6h (or `any` 1-5c at 1h), at +0.7% to +1.3%.
Only two single categories had any config with enough train trades. In
Economics the best was +0.28% (se 1.24). In Sports it was NO on 1-10c, 6h,
vol >= 100, spread <= 4c: +1.15% (se 0.99, n=305), which is not
significant. Every other category was too thin on its own.

**The chosen config on test** (NO on YES 1-5c, H = 6h, all
non-mechanical, >= 100 contracts traded in the prior 24h, spread <= 10c):

| | n | events | ROI | 95% CI (by event) |
|---|---|---|---|---|
| train | 491 | 312 | +1.22% | (clustered se 0.36%) |
| **test** | 354 | 165 | **+0.05%** | **[-1.45%, +1.22%]** |
| test, +1c slippage | 354 | 165 | -0.89% | |
| test, +2c slippage | 354 | 165 | -1.37% | |
| test, cent-rounded fee on a 1-lot | 354 | 165 | -0.83% | |
| test without the best 5 events | | | -0.12% | |

Per category on test: Politics 206 trades / 69 events at -0.22%, Sports
69 / 42 at -0.79%, and Economics, Entertainment and Science +1-2% on 21-28
trades each. Capacity was not the problem: the median market traded 5,722
contracts in the 24h before entry (p25 1,543), and the median final volume
was 22,524.

**Pass criteria (fixed before the test run):**

| criterion | result |
|---|---|
| (a) >= 150 test trades over >= 40 events | pass: 354 / 165 |
| (b) ROI > 0 with a 95% event-bootstrap CI excluding 0 | **fail**: +0.05%, CI [-1.45, +1.22] |
| (c) net positive with +1c slippage | **fail**: -0.89% |
| (d) not driven by one category/series or a handful of events | **fail**: -0.12% without the best 5 events, -0.05% without the top series (`KXNFLTD`) |
| (e) capacity | median 5,722 contracts traded in the 24h before entry |

The naive clock (`--ref market --single`, 24,036 rows) picks almost the
same config (spread <= 4c). It gets +0.06% on test, CI [-1.36, +1.27], and
-0.87% with +1c. So the early-close lookahead isn't what sinks it.

## Why it failed

1. **The spread eats the bias.** Longshots are overpriced relative to the
   mid by 1-3c. The favorite buyer crosses to 1 - bid, which hands back
   1-1.5c. At 96-99c the remaining edge is about 1c on about 97c of capital.
2. **What's left is too small to survive selection.** The best train
   configs are all +1% with standard errors of 0.4-0.7%. The pick (1-5c,
   the tightest-looking) regressed to zero on test.
3. **Asymmetric, unstable mirror.** Buying YES favorites was -2% to -3% on
   train and +1% to +2% on test. It is not one stable bias you can harvest
   from both sides.
4. **Slippage and fee rounding.** 1c of slippage is a full point of ROI on a
   97c contract. The published cent-rounded fee costs about as much again.

## What might revive it

- **The 6h, YES <= 10c lead.** This is the literal idea, un-optimised: NO on
  YES 1-10c, 6h before the event's close, all non-mechanical, spread
  <= 10c. On test it made +1.49% (CI [+0.32, +2.58], 608 trades / 252
  events) and +0.52% with +1c. It stayed at +1.28% without the best 5
  events, and Sports carried half its pnl. It would have met all five
  criteria. But it was one of 12 pre-specified non-mechanical configs
  shown on test, and the train-only rule didn't pick it (train +0.77%,
  se 0.59). The 1h and 24h versions were about zero. Treat it as a
  hypothesis now fixed in advance: log it forward on fresh markets (paper,
  `../../loggers` style) for 2-3 months, then re-test with these same
  criteria. Don't mine this window further.
- **Maker fills.** Resting a NO bid at mid instead of lifting 1 - bid would
  recover most of the spread, which is the whole edge here. That needs
  queue/fill modelling the candles can't support. Forward-log it.
- **Sports at 6h** was the one category where the tilt kept its sign
  (train +1.15%, se 0.99; test +2.25% on 142 trades / 72 events in the
  diagnostic), though neither half is significant. It's the natural scope
  for the forward log.
- **Lower fees.** The edge is at most about 1c at 96-98c. A fee waiver or
  maker rebate changes the arithmetic more than any filter does.
