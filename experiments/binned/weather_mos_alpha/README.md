# weather_mos_alpha (binned 2026-10-24)

**Question:** Kalshi lists a daily high-temperature ladder for each of 23 US
cities (`KXHIGHNY`, `KXHIGHCHI`, `KXHIGHT*`, ...). Each event is one
city-day with about six mutually exclusive buckets ("63° to 64°", "65° or
above", ...). It settles on the NWS climate report's daily max for one
station. Can a calibrated error distribution around the free NWS MOS
forecasts (NBM, GFS, NAM) price these buckets better than the market, well
enough to beat the fee and the spread? Trade a bucket when the model's
probability beats the executable price by a threshold, and hold to
settlement.

**Backtest answer (2026-10-24): no.** The market is better calibrated than
the MOS model at both decision times, on train as well as test. The model
is overconfident: its average claimed edge on train trades was 13c, and the
realized edge was 1c. On the held-out test window, the strategy picked on
train lost 11.4% of capital risked (95% CI -15.5% to -7.3%). It failed 4
of the 5 pass checks (fixed before any trade was priced). There is no live
runner, and none is worth building on these results.

## The strategy

- **Forecast** (`model.py`): at a decision time, use the newest MOS run of
  each model whose runtime plus a publication delay is before that time.
  The delay is 2h for NBM (`NBS`) and 4h for GFS and NAM MOS.
  `MosIndex.latest` is the no-lookahead gate. The max-temp row (`n_x`, or
  `txn` for NBM) that verifies at 00Z is assigned to the local day it
  covers.
- **Error model:** mu = a point forecast + a seasonal bias for the city;
  sigma = a seasonal residual stdev for the city, or NBM's own spread
  (`xnd`) rescaled. The error is normal or unit-variance Student-t (5 df).
  Both are fitted per city and decision time, as kernel means over ±30
  days of the year across all training years. Bucket probability is
  P(lo - 0.5 ≤ T < hi + 0.5), which accounts for the integer report.
- **Rule:** buy YES at `yes_ask` or NO at `1 - yes_bid` when
  `prob - price - fee > THRESHOLD` and the price is in `[MIN_PRICE,
  1 - MIN_PRICE]`. Take at most one side per bucket per decision time, 1
  contract, held to settlement.
- **Fee:** `ceil(0.07 · p · (1-p) · 100) / 100` per contract, the
  cent-rounded quadratic fee from the brief. The weather series report
  `fee_type: quadratic`, `fee_multiplier: 1`. The real rounding is to
  $0.0001 (`fees.estimate_fee_dollars`), and a sensitivity row reruns with
  it.

## Data

| source | what | coverage |
|---|---|---|
| Kalshi `/markets`, `/historical/markets` | every settled market of the 23 series, with `result` and `expiration_value` | full history, cached |
| Kalshi candlesticks: `/historical/markets/{t}/candlesticks` (per market), `/series/{s}/events/{e}/candlesticks` (live, per event) | hourly `yes_bid` / `yes_ask` closes | event dates 2026-06-20 .. 2026-10-22: 2,680 events, ~16k markets |
| IEM MOS archive (`mos.py`) | NBS, GFS, NAM runs, 23 stations | 2023-01 .. 2026-10 (3,174 station-model-months) |
| IEM `json/cli.py` | NWS CLI daily max per station | 2023 .. 2026 |

- **Settlement station:** read off each series' `rules_primary`, e.g.
  Central Park (KNYC), Midway (KMDW), Hobby (KHOU), DFW, MSY and DCA (see
  `config.CITIES`). Kalshi switched the stated source from the NWS CLI
  product to "The Weather Company" on 2026-08-15, but the station and the
  number stayed the same. The IEM CLI high lands in Kalshi's winning bucket
  for 2,678 of 2,679 events.
- **Why only 4 months of prices:** the historical candle endpoint takes one
  market per request, and Kalshi allows about 2.5 to 3 requests/s per IP.
  The plan was 2026-01-15 onwards (26k requests, about 2.5h). Partway
  through, another fetch from the same IP (the `shock_reversion` agent) cut
  our rate to about 0.2 req/s. The fetch ran newest first, so it was
  stopped at a contiguous block, 2026-06-20 .. 2026-10-22. The split was
  then set at 60% of those days, before anything was priced.
- Older markets (2021+ for NY and Chicago) exist. `data.py` extends the
  cache if `config.BACKTEST_START` is moved earlier.

## Method (no lookahead, everything chosen on train)

1. **Error model.** Pair the usable MOS with the CLI high for every city,
   date and decision time from 2023-01-01 to the split (61,493 rows). Score
   12 variants by integer log-loss on the last 180 pre-split days, fitted
   on the days before. Variants: center NBM / mean of NBM+GFS+NAM /
   NBM+GFS average; sigma seasonal / NBM-scaled; normal / t. The winner
   was **NBM+GFS average, NBM-scaled spread, t**: holdout RMSE 2.63°F,
   log-loss 2.244 vs 2.342 for the plainest variant. It was refit on all
   pre-split rows.
2. **Decision times** (local clock): `d-1_12h` (noon the day before) and
   `d0_08h` (8am on the day). Quotes come from the hourly candle that
   closes at that time. A side is skipped if it has no quote, if the
   candle is more than 2h stale, or if the market never traded.
3. **Grid on train** (event dates 2026-06-20 .. 09-01: 74 days, 1,508
   events). The grid is threshold {2, 4, 6, 8, 10, 15, 20c} × min price
   {2, 5, 10c} × side {yes, no, both} × decision time {d-1, d0, both}.
   The winner is the config with the highest net PnL among those with ≥150
   trades.
4. **Test once** (09-02 .. 10-22: 51 days, 1,172 events, all 23 cities).
   The 95% CI comes from 5,000 bootstrap resamples of event dates: every
   city's buckets for a date move together. Resampling by event gives the
   same CI.

```bash
python data.py          # fill the cache (slow: Kalshi's historical candles are 1 request per market)
python backtest.py      # steps 1-4 plus diagnostics; JSON in backtest_runs/
python backtest.py --error-model-only
python -m pytest tests  # offline: bucket math, no-lookahead gate, fills, fees, bootstrap
```

## Results

**Chosen on train:** threshold 6c, min price 2c, both sides, `d-1_12h`.
Train: 2,611 trades, ROI +2.4% (CI -2.1% to +6.7%), which is already not
significant.

**Test (held out), 1 contract per trade:**

| run | trades | cost | net PnL | ROI | 95% CI (by date) |
|---|---|---|---|---|---|
| test | 2,700 | $1,034.67 | -$117.67 | **-11.4%** | -15.5% .. -7.3% |
| +1c slippage | 2,700 | $1,061.77 | -$144.77 | -13.6% | -17.7% .. -9.6% |
| +2c slippage | 2,700 | $1,088.91 | -$171.91 | -15.8% | -19.8% .. -11.9% |
| $0.0001 fee rounding | 2,858 | $1,073.44 | -$105.44 | -9.8% | |
| without best 5 days | 2,485 | $946.98 | -$131.98 | -13.9% | |
| without best city (PHX) | 2,551 | $980.63 | -$120.63 | -12.3% | |

**Pass criteria** (fixed in advance): (a) ≥150 trades: yes, 2,700.
(b) ROI > 0 with a CI excluding 0: **no**, the CI excludes 0 on the
*negative* side. (c) positive at +1c: **no**. (d) not driven by one city
or a few days: **no**. 19 of 23 cities lost, and the 4 winners (AUS, MIA,
PHIL, PHX: +2% to +6% on 106-149 trades each) are noise.

**By side and price** (test). YES longshots (1,355 trades, average price
11c) lost 31%. NO (1,345, average 62c) lost 7.5%. By price: below 20c,
-38%; 20-50c, -34%; 50-80c, -3.4%; above 80c, -5%. The model's tails are
too thin where it says a bucket is underpriced: it is overconfident.

**Calibration** (test ladders with a two-sided quote on every bucket; the
market price is the mid, normalized over the ladder):

| | buckets | Brier model | Brier market | log-loss model | log-loss market |
|---|---|---|---|---|---|
| all | 5,570 | 0.1260 | **0.1130** | 0.4016 | **0.3580** |
| d-1 noon | | 0.1255 | **0.1106** | 0.4000 | **0.3483** |
| d0 8am | | 0.1281 | **0.1232** | 0.4087 | **0.3983** |

The market's probability on the winning bucket has log-loss 1.320,
against the model's 1.534. On train too, the market was better (Brier
0.1151 vs 0.1208). The market is sharper and still calibrated.

**Does MOS add anything to the market?** A log-linear pool of model and
market was fitted on train. It puts weight 0.25 on the model. On test, its
log-loss is 1.3182 vs 1.3203 for the market alone, a 0.2% improvement that
is nowhere near the fee plus the spread.

**Other report-only numbers (none selected on test).**
- All 10 of the top train configs lost on test, from -9.7% to -31.2%.
- The same rule at `d0_08h` lost 3.1% on train and 9.9% on test. With
  both decision times, it lost 0.4% on train and 10.7% on test.
- The first run used `d-1_10h`, which was a bug: markets open at 10:00 ET
  the day before, so no Eastern city had a quote at 10:00 local. It chose
  15c/2c/both/d-1 on train (+5.7%) and lost 12.5% on test (CI -21.3% to
  -3.8%, 629 trades).
- Model error grew from train to test: RMSE 2.26 → 2.51°F at d-1 noon and
  2.01 → 2.26°F at d0 8am, with the bias within 0.25°F. This reflects the
  harder fall transition, but the market handled the same transition
  better.
- Quarter-Kelly replay ($1,000; caps 2% per trade, 5% per event, 50% per
  date): **-99.9%** on test. It also lost 19% on train, where flat stakes
  made +2%. Kelly leans into the model's largest claimed edges, which are
  its most overconfident ones.

## Why it failed

The thesis assumed retail-heavy markets and a strong free model. The
markets turned out to be priced by someone with a better model than MOS:
probably the ensembles (ECMWF/GEFS), HRRR and RAP, NBM percentiles, and,
by the morning of the day, the overnight observations. Some cities' books
are deep (KXHIGHNY has traded about 150M contracts since 2021). At d-1 noon, the market
already beats a seasonal MOS error model by 12% in Brier. By 8am on the
day, the gap narrows but remains. A 2°F bucket is narrow compared with a
2.3-2.5°F forecast error. Most of the model's disagreements with the
market are therefore tail-width disagreements, and the model loses those
(YES longshots -31%).

## What might revive it

- **A better model than the market's,** not MOS: probabilistic NBM
  percentiles (`NBP` text product), ECMWF ensemble spread, or HRRR runs
  later on the day. The pooled-blend diagnostic shows how to test any new
  input: if a pool fitted on train doesn't beat the market's log-loss on
  test by more than about 1-2%, the edge can't survive the fee.
- **Intraday on the event day,** using observed temperatures so far (METAR
  / ASOS 5-minute data) after the morning. The max is often locked in by
  mid-afternoon, and quotes may lag. This is a different trade (a nowcast,
  not a forecast) and needs minute-level candles.
- **Longer history.** Only June-October 2026 was priced. A winter test,
  when forecast errors are larger and retail flow may differ, needs the
  2025-2026 candles. Fetch them when no other job shares the IP (about
  2.5h for 2026-01-15 onwards).
