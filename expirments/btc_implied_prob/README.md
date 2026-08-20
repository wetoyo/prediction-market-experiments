# btc_implied_prob

Prices Kalshi's BTC "above"/"below" interval markets (`KXBTCD` hourly,
`KXBTC15M` 15-minute, etc.) against a probability implied by Deribit's *live
options market*, and signals a trade wherever the two disagree by more than
a fee-adjusted threshold.

This is the same shape of strategy as `../resolution_alpha` (price a Kalshi
BTC interval market's strike against a probability model, trade the
disagreement), but the probability model is different: resolution_alpha
estimates volatility from *realized* spot price history; this one reads
volatility directly off Deribit's *option* market, i.e. what other traders
are currently paying to be exposed to BTC moving, priced forward rather than
backward-looking.

## Methodology

**1. Kalshi side** (`kalshi_btc_markets.py`): scans every open BTC series on
a recurring interval cadence and extracts each market's strike, direction
(`above`/`below`, from `strike_type`), close time, and current yes bid/ask.

**2. Deribit side** (`deribit_iv.py`): fetches Deribit's live BTC option
chain (`get_instruments` + `get_book_summary_by_currency`) and builds a
per-expiry implied-vol smile from `mark_iv`, using each strike's
out-of-the-money side (calls above the forward, puts below) since that's the
more liquidly quoted side. Deribit quotes options against a per-expiry
forward (`underlying_price`), not spot -- convenient, since it means no
separate risk-free-rate/basis assumption is needed (see `black_scholes.py`).

**3. Interpolation**: Kalshi's strikes and close times essentially never
match Deribit's actual listed strikes and expiries, so getting a
(strike, time) pair the Kalshi market actually asks about takes two
interpolation steps:
  - *Across strikes*, within a single Deribit expiry: linear in
    log-moneyness `ln(K/F)`, flat-extrapolated past the smile's own quoted
    strike range.
  - *Across time*, between the two Deribit expiries bracketing the target
    close time: linear in **total variance** (`sigma^2 * T`), the standard
    "flat forward variance" term-structure interpolation -- linearly
    interpolating volatility itself instead would understate variance at the
    target date whenever the term structure isn't flat, which crypto vol
    term structures usually aren't.

**4. Probability** (`black_scholes.py`): the risk-neutral probability that
the interpolated forward finishes above (or below) the Kalshi strike is the
textbook Black-76 digital option formula, `N(d2)` -- this literally *is* the
market-implied probability a liquid digital option on that forward would
trade at, which is exactly what Kalshi's above/below markets are.

**5. Signal** (`strategy.py`): compares the model probability to Kalshi's
current yes mid-price. If they disagree by more than `EDGE_THRESHOLD`
(config.py) after Kalshi's quadratic trading fee (`fees.py`, same formula
validated live in resolution_alpha), it's a candidate trade on whichever
side (yes/no) the model favors, at whichever price (ask, since these are
marketable-limit buys) would actually fill.

**6. Sizing** (`strategy.py`'s `_kelly_contracts`): position size is
fractional Kelly -- `KELLY_FRACTION * (favored_probability - price) / (1 -
price)` of account bankroll (real balance when live, else
`DRY_RUN_SIMULATED_BALANCE_DOLLARS`), floored to a whole contract count and
clipped to `MAX_CONTRACTS_PER_TRADE`. Same formula as
`../resolution_alpha/live/runner.py`'s `_kelly_contracts`, which found full
Kelly overshoots badly at the near-1.0 prices these interval markets often
clear at (the `(1 - price)` denominator shrinks toward zero, so full Kelly's
implied fraction blows toward the entire bankroll off a modest edge) --
`KELLY_FRACTION` defaults conservative here pending the same kind of live
validation.

**7. Exit** (`strategy.py`'s `_check_exit_conditions`): off by default
(`ENABLE_TRAILING_EXIT`) -- until 2026-08-13 this strategy was buy-only, no
exit logic at all, every position rode to settlement. When enabled, each
open position is re-checked every tick against a fresh Deribit-implied
probability and the market's current quote, and exits (buys the opposite
side to flatten) once either the edge that justified entering has closed to
`EXIT_EDGE_THRESHOLD` or below (take-profit) or price has given back
`TRAILING_STOP_DROP` from its best point since entry (trailing stop, also a
plain stop-loss on a position that never improves) -- trailing-stop has its
own toggle, `ENABLE_TRAILING_STOP`, off by default, independent of
`ENABLE_TRAILING_EXIT`: with it off, take-profit is the only exit path, and a
position that drifts against the entry edge without ever clearing
`EXIT_EDGE_THRESHOLD` just rides to settlement. **Exiting costs a second
fee** on top of the entry fee -- Kalshi's quadratic fee applies per trade,
not per position, so a completed round trip roughly doubles fee drag versus
holding to settlement; `EXIT_EDGE_THRESHOLD` defaults positive (not 0.0)
specifically to bank real profit net of that.

Take-profit itself is handled differently live vs. dry-run, added
2026-08-15: dry-run has no real order book, so it keeps a simple tick-driven
simulated check. Live instead places an actual resting limit order on the
exit side at the take-profit target the moment a position is entered, and
repriced (canceled and replaced) each tick as the target drifts with time
decay and the IV surface moving -- Kalshi's own matching engine then fills it
the instant price actually crosses, rather than waiting up to a full loop
tick (60s by default) for this process to notice and place a marketable
order. Trailing-stop stays tick-driven either way, since its trigger price
moves with the position's own peak and would need the same per-tick
repricing regardless. This also surfaced (and fixed) a real pricing bug: the
tick-driven decision logic had been reading the wrong side of the spread
(`yes_ask`/`1-yes_bid` instead of `yes_bid`/`1-yes_ask`) for what a held
position could actually be sold for -- harmless for the old dry-run-only
comparison, but would have meant the resting order's target price didn't
match what the trigger logic thought it was targeting.

**8. Position tracking** (`positions_store.py`): `open_positions` is tracked
unconditionally -- not just when `ENABLE_TRAILING_EXIT` is on -- persisted to
`POSITIONS_STATE_PATH` every tick, reloaded on startup, and reconciled
against Kalshi's real account state once at the start of a `--execute` run.
Added 2026-08-15 after a live incident: with tracking gated on the exit
toggle, a running process (toggle off, the default) had no record of tickers
it already held, so `_execute` re-bought the same qualifying tickers on every
single loop tick -- ~750 contracts accumulated across 7 strikes in one event
over ~80 minutes before anyone noticed. A ticker already in `open_positions`
is now skipped outright rather than re-bought. Reconciliation is best-effort
for a position it finds with no local record (e.g. one opened before this
file existed): it can recover contracts/side/an average entry price from
Kalshi's own data, but not the original model edge that justified the trade,
so that field is left `None` and flagged `"reconciled": True`.

## Usage

```bash
pip install -r requirements.txt

python strategy.py            # scan once, print signals, no orders placed
python strategy.py --execute  # also place orders for qualifying signals
                               # (still simulated unless BTC_IMPLIED_PROB_DRY_RUN=false
                               # AND KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH are set)
python strategy.py --loop 30  # rescan every 30s until interrupted
```

`config.py`'s knobs are all environment-overridable (`BTC_IMPLIED_PROB_*` --
see that file for the full list: `EDGE_THRESHOLD`, `MIN_SECONDS_TO_CLOSE`,
`MAX_SECONDS_TO_CLOSE`, `MAX_SPREAD`, `KELLY_FRACTION`,
`MAX_CONTRACTS_PER_TRADE`, `DRY_RUN_SIMULATED_BALANCE_DOLLARS`, `DRY_RUN`,
`ENABLE_TRAILING_EXIT`, `EXIT_EDGE_THRESHOLD`, `ENABLE_TRAILING_STOP`,
`TRAILING_STOP_DROP`, `POSITIONS_STATE_PATH`).

Each module also has a `__main__` smoke test:
`python deribit_iv.py` prints the built vol surface and a sample probability;
`python kalshi_btc_markets.py` lists currently open BTC markets.

## Backtesting

```bash
python backtest.py --hours 24 --decision-seconds 90,60,30 --min-prob 0.97
```

Deribit's public API has no historical implied-vol/option-chain endpoint --
only current-state snapshots, plus `get_historical_volatility` (BTC's own
*realized*-vol index, hourly, ~16 days back). `backtest.py` is therefore a
calibration check, not a replay of the live strategy: same Black-76 `N(d2)`
formula from `black_scholes.py`, fed that historical realized-vol series
(as a sigma proxy) and Coinbase 1-minute candles (as a spot/forward proxy)
against real settled Kalshi BTC markets. A pass means the probability math
and time-decay handling calibrate against history -- it does not validate
the live IV-surface edge itself, which only `strategy.py`'s own live
predictions (see `live/logs/`) can do.

## Limitations

- **Settlement-averaging window not modeled.** Kalshi settles these markets
  on a trailing `settlement_timer_seconds`-second (typically 60s) average of
  CF Benchmarks' BRTI ending at close, not an instantaneous last tick. Once
  inside that window, part of the settlement value is already realized and a
  plain "time left" Black-76 probability overstates remaining variance.
  Rather than model that blend (resolution_alpha's `probability.py` attempts
  a version of this for its own realized-vol model), this strategy just
  stays out via `MIN_SECONDS_TO_CLOSE`.

- **Extrapolation beyond Deribit's listed expiries.** Deribit's shortest
  listed BTC option expiry is usually the next scheduled cycle (often
  08:00 UTC), which is frequently *later* than a 15-minute Kalshi market's
  close time. When the target close time falls outside the range of expiries
  Deribit actually has quotes for, the surface flat-extrapolates from the
  nearest single expiry's smile instead of interpolating -- flagged as
  `extrapolated=True` on the estimate, marked with `*` in `strategy.py`'s
  output, and skipped by `--execute` regardless of the computed edge. In
  practice this means the shortest-dated Kalshi markets (15-minute series,
  and hourly markets closing same-cycle) are priced but never auto-traded.

- **Index basis risk.** The model prices against Deribit's own BTC index/
  forward; Kalshi settles against CF Benchmarks' BRTI. These normally track
  closely but aren't identical, and no basis adjustment is applied here.

- **No correlation-aware sizing across simultaneously open BTC markets.**
  Position sizing is fractional-Kelly per signal (see "Sizing" above), but
  each signal is sized independently against the full bankroll -- it doesn't
  account for other BTC positions already open at the same time, which are
  highly correlated with each other since they're all driven by the same
  underlying moving the same way.

- **Thin near-dated smiles.** Very short-dated Deribit expiries sometimes
  have only a handful of live-quoted strikes; the smile interpolation
  degrades gracefully (flat extrapolation past the quoted range) but a
  sparse smile is a weaker basis for a strike far from the money.
