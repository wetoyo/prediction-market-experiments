# index_range_vix (binned 2026-10-24)

**Question:** can Kalshi's S&P 500 and Nasdaq-100 markets be beaten by
pricing each strike with a lognormal digital that takes its vol from VIX
(S&P) or VXN (Nasdaq)? This is the `../../btc_implied_prob/` model moved to
equities. The markets tested:

- `KXINX` / `KXNASDAQ100`: daily close range buckets and tails;
- `KXINXU` / `KXNASDAQ100U`: hourly above/below thresholds.

**Backtest answer (2026-10-24): no.** On the held-out test period the
strategy lost 12% of the capital it risked (95% CI -31% to +10%). With the
best 5 days removed it lost 30%. Where Kalshi quotes are tight, the Kalshi
mid is better calibrated than the model in every series. The ladder's own
implied vol forecast the realized move better than VIX x k did, even though
k was fit on the train data. There is no live runner.

## The strategy

At decision time t, for each market:

- **Spot S:** the `^GSPC` / `^NDX` close of the latest Yahoo 60-minute bar
  that ended at or before t. Bars end at :30 and decisions are at :30, so S
  is the index at the decision minute.
- **Vol V:** the `^VIX` / `^VXN` value at t, taken the same way. VIX bars
  end on the hour, so V can be up to 30 minutes old.
- **Clock T:** (close - t) / 6.5h / 252, in trading years.
- **sigma:** k x V / 100. k is fit on train days only, separately for each
  (index, decision time, close hour). This one number covers both the
  intraday vol shape and the gap between VIX and realized vol:
  k² = mean(ln(X/S)² / ((V/100)² T)), where X is Kalshi's settlement value.
- **p:** the Black-76 digital P(lo <= X <= hi) (`black_scholes.py`). Spot
  stands in for the forward, since over a few hours the difference is far
  below a basis point.
- **Trade:**
  - buy YES at the minute's `yes_ask` if p - ask - fee >= thr;
  - buy NO at 1 - `yes_bid` if (1 - p) - (1 - bid) - fee >= thr;
  - one contract per signal, held to settlement.
  - Fee: ceil(0.07 n p(1-p) x 100) / 100 per 10-contract order. Kalshi's
    `/series` reports `fee_type` quadratic with `fee_multiplier` 1 for all
    four series.
- **Fit on train:** thr and a minimum price, from {0.02 … 0.15} x {1c, 5c,
  10c}, with at least 300 train trades. The test period was scored once.

## Data

`build_dataset.py` fetches and caches the data under `data/`. `kalshi_data.py`
spaces requests 0.3 s apart, backs off exponentially on 429/5xx and caches
every response. `market_data.py` reads Yahoo.

- **Kalshi:** settled events and their 1-minute `yes_bid`/`yes_ask` candles.
  - Events before the 2026-08-25 cutoff come from `/historical/markets` and
    `/historical/markets/{t}/candlesticks`, one market per call. These
    candles are only emitted when the quote changes, so the quote is carried
    forward from the market's open.
  - Later events come from `/markets` and the batch `/markets/candlesticks`.
- **Window:** 2025-10-27 to 2026-10-23, 248 trading days, 27,640
  market-minute rows.
  - `KXINX`: 247 events. `KXNASDAQ100`: 240.
  - `KXINXU`: 243 events. `KXNASDAQ100U`: 236. Only the 15:00 ET events
    were used, at strikes within ±1% of the prior close, thinned to
    multiples of 25 (S&P) / 100 (NDX).
  - Daily buckets were kept if they come within ±2% of the prior close.
  - Every strike filter uses only the prior day's close.
- **Decision times:** 10:30, 12:30, 14:30 and 15:30 ET, keeping only those
  at least 30 minutes before the market's close.
- **Why only a year:** the shared IP got about 1 to 1.5 requests/s through
  the 429s. Yahoo's 60-minute history would allow two years (from
  2024-10-24).
- **Underlying and vol:** Yahoo's v8 chart API (`^GSPC`, `^NDX`, `^VIX`,
  `^VXN`, 60-minute bars, last 730 days). CBOE's CDN CSV redirects (307).
  FRED has daily closes only.
- **Settlement check:** Kalshi's `expiration_value` matches Yahoo's close to
  the cent (7623.68 on 2026-10-23).
- **Split:** train 2025-10-27 to 2026-06-22 (161 days), test 2026-06-23 to
  2026-10-23 (87 days).

```bash
python build_dataset.py          # ~1.5 h cold on the shared IP; seconds from cache
python backtest.py               # all four series, Yahoo spot
python backtest.py --spot ladder # daily series, spot = median of the Kalshi bucket ladder
python -m pytest tests           # offline
```

## Results

**k fit on train:** 0.45 to 0.56 across combos (all combos together:
0.49). Over this year, intraday realized vol ran at about half of VIX/VXN.

**Train grid** (all series, ROI on capital risked):

| thr | trades | ROI | +1c |
|---|---|---|---|
| 0.02 | 1,948 | -3.7% | -5.8% |
| 0.04 | 1,099 | -4.8% | -7.0% |
| 0.06 | 708 | -2.5% | -4.8% |
| **0.08** (chosen) | 467 | +2.8% | +0.1% |
| 0.10 | 317 | +1.7% | -0.9% |
| 0.15 | 115 | -4.7% | -7.4% |

All rows use a 5c minimum price. At 1c or 10c the results are within a
point of these. No setting is clearly positive even on the data it was fit
on.

**Test (held out), thr 0.08, min price 5c:**

| slice | trades | days | ROI | 95% CI (by day) | +1c | +2c |
|---|---|---|---|---|---|---|
| **all** | 321 | 62 | **-12.2%** | [-31.1%, +9.6%] | -14.4% | -16.5% |
| best 5 days removed | 277 | 57 | -29.8% | [-43.5%, -14.4%] | -31.5% | -33.2% |
| KXINX | 158 | 55 | -1.0% | [-26%, +26%] | -3.4% | -5.7% |
| KXNASDAQ100 | 106 | 48 | -22.1% | [-46%, +4%] | -23.9% | -25.6% |
| KXINXU | 30 | 26 | -22.1% | [-64%, +61%] | -25.1% | -27.9% |
| KXNASDAQ100U | 27 | 20 | -33.0% | [-75%, +32%] | -35.0% | -36.9% |
| 10:30 ET | 50 | 39 | +0.8% | [-23%, +23%] | -0.8% | -2.3% |
| 12:30 ET | 66 | 41 | -35.0% | [-59%, -10%] | -36.2% | -37.4% |
| 14:30 ET | 145 | 45 | -12.0% | [-38%, +21%] | -14.8% | -17.5% |
| 15:30 ET | 60 | 36 | +7.6% | [-34%, +68%] | +3.9% | +0.5% |
| buy YES | 79 | 32 | -34.7% | | | |
| buy NO | 242 | 62 | -8.8% | | | |

With 1-contract orders (the 1c fee floor) the loss is -13.0%. The
ladder-spot variant does about the same: -14.7% on test, CI [-33%, +4%].

**Pass criteria** (fixed before the run):

| criterion | result |
|---|---|
| ≥150 trades over ≥30 days | ✔ 321 trades / 62 days |
| net ROI > 0 with CI excluding 0 | ✘ -12.2%, CI [-31%, +10%] |
| positive at +1c | ✘ -14.4% |
| not driven by a few days | ✘ -29.8% without the best 5 days; negative in every series |

**Calibration, test period.** These are the rows where the quote is a real
forecast (spread ≤ 10c). The table is Brier score, model vs Kalshi mid:

| series | n | model | market |
|---|---|---|---|
| KXINX | 4,404 | 0.0460 | **0.0447** |
| KXNASDAQ100 | 3,752 | 0.0495 | **0.0476** |
| KXINXU | 573 | 0.0321 | **0.0304** |
| KXNASDAQ100U | 456 | 0.0322 | **0.0293** |

Over all rows the model scores better (0.0536 vs 0.0619). That is only
because the thinned U-series strikes often have very wide quotes (median
spread 32-40c), whose mids aren't forecasts and can't be traded.

On the traded rows:

| | value |
|---|---|
| model probability for the side bought | 0.527 |
| price paid | 0.384 |
| win rate | 0.349 |

Where the model disagreed with the market, the market was right.

**Variance forecast** (`implied_vol_diagnostic`). At each decision minute,
the vol implied by the daily ladder was fit, as a multiple of VIX. Both
forecasts were then scored on the realized log return to settlement:

| period | ladder-implied k / VIX-model k (median) | log-lik per obs: VIX model | log-lik per obs: ladder |
|---|---|---|---|
| train | 0.89 | 4.210 | **4.219** |
| test | 0.79 | 4.427 | **4.557** |

The ladder's vol forecast beat VIX x k even on the data k was fit on.

## Why it failed

1. **VIX is a 30-day, all-hours vol, and the ratio to intraday vol moves
   around.**
   - VIX includes overnight and weekend variance, event days and the
     variance risk premium.
   - The fitted ratio k was about 0.5, but the ladder-implied k moved from
     0.6x to 1.4x of it day to day (p10 to p90).
   - In the test window, realized vol fell further below VIX (ladder k /
     model k: 0.89 on train, 0.79 on test). A k fixed on train therefore
     priced the distribution too wide.
   - So the model's trades were mostly NO bets on central buckets, where
     the market's YES price had a median of 56c, plus YES bets on outer
     buckets at a median of 16c. Those are exactly the bets a
     too-wide distribution makes, and they lost.
2. **The Kalshi index ladders are well arbed.**
   - Spreads are 1-3c near the money.
   - The ladder's center tracks the index to within a bucket: the ladder-spot
     variant does no better than Yahoo spot.
   - Its implied width already folds in the intraday information that a
     30-day index can't carry.
   - Unlike `btc_implied_prob`, there is no deep, matched-tenor option
     market here to free-ride on.
3. **Fees:**
   - 0.07 p(1-p) is about 1.75c at 50c, the price range where the model's
     disagreements cluster.
   - The few configs that were positive on train cleared fees by less than
     a cent.

## What might revive it

- **A matched-tenor vol source:**
  - SPX 0DTE option implied vol, or the Cboe 1-day VIX (`VIX1D`), instead of
    the 30-day VIX.
  - The model's whole disadvantage is a variance forecast worse than the
    ladder's. A same-day vol that beats the ladder's (score it with
    `implied_vol_diagnostic`) is the precondition.
- **Event days:** CPI, FOMC and earnings for the NDX heavyweights. The flat
  k ignores them. A k by day type might help, but needs more history than
  one year.
- **The reverse trade:** treat the Kalshi ladder as the forecast and look
  for stale quotes right after fast index moves. That needs sub-minute
  index data and a real order book, which the candles don't provide.
- **More history:** two years of Yahoo 60-minute data are available, and
  Kalshi has had these series since 2022. Rerun with
  `build_dataset.py --since 2024-10-28` when the IP isn't shared.

## Not modeled

- Queue position and size at the ask: the fill is assumed at the candle's
  closing quote for any size. This is optimistic, but the result is already
  negative.
- Yahoo's 60-minute index close as the spot. The official Kalshi source
  agency may differ by a few hundredths.
- Fees: per-order cent rounding at 10 contracts; the 1-contract stress is
  shown above.
