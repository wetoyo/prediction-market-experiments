# Live system

Runtime component of [golf_field_alpha](../README.md). `../strategy.py`
discovers every open Kalshi golf outright-winner event, builds a YES
basket for each in-window event by de-vigging the field's own prices (see
`../selection.py`'s two methods), and — with `--execute` — places the
legs and tracks them to the tournament's resolution. **Defaults to
dry-run** (logs intended orders, places nothing). See Status.

Plain synchronous REST loop (`strategy.py --loop`), like
`../../btc_implied_prob`, not the asyncio/websocket design of
`../../resolution_alpha` — golf events move over days, a 5-minute rescan
is plenty.

## Running it

```
cd expirments/golf_field_alpha/live
pip install -r ../requirements.txt
cp .env.example .env        # edit, then start_live loads it into the process
./start_live.sh             # or double-click start_live.bat on Windows
./stop_live.sh              # or stop_live.bat
```

`start_live.*` launches `../strategy.py --loop $GOLF_FIELD_ALPHA_LOOP_SECONDS
--execute` as a **detached** process (Start-Process on Windows, not bash
nohup/disown — see `../../btc_implied_prob/live/start_live.ps1`'s header
for the console-job-object incident that motivated the switch), archives
the previous run's logs into `logs/old/`, and records the PID in
`.runner.pid`.

Without `GOLF_FIELD_ALPHA_DRY_RUN=false` **and** both `KALSHI_API_KEY_ID` /
`KALSHI_PRIVATE_KEY_PATH`, every "order" is just a `[DRY RUN] would BUY …`
log line. Discovery and scanning need no credentials at all.

## Pre-flight checklist (read before any real-money run)

1. **Check nothing is already running.** `./stop_live.sh` first, or check
   `.runner.pid` / Task Manager. A stray second instance trades the same
   account twice.
2. **Check `.env`.** `GOLF_FIELD_ALPHA_DRY_RUN` — a *missing* var is safe
   (defaults to dry-run), a stale `false` from a previous session is not.
   `KALSHI_PRIVATE_KEY_PATH` — forward slashes even on Windows (`source
   .env` mangles backslashes).
3. **Sanity-check the risk cap against real balance.**
   `MAX_EVENT_COST_DOLLARS` is the worst-case loss *per event's basket*
   (you get $0 back if no bought player wins). With several tournaments
   open at once (PGA + Champions + Korn Ferry + DP World + LPGA can all
   overlap), simultaneous exposure is roughly `MAX_EVENT_COST_DOLLARS ×
   number of in-window events`. Make sure that number is one you're fine
   losing in full — the backtest has *not* shown this strategy is
   profitable.
4. **Prefer starting in dry-run after any code change** and watch it sit
   through a real tournament week — the per-leg re-buy guard, the
   re-entry-as-prices-move behaviour, and basket sizing under a real
   thin book are all things only a live dry-run surfaces (both crypto
   experiments caught real bugs this exact way).
5. **First real run: place one tiny order first.** Set
   `MAX_EVENT_COST_DOLLARS` to a dollar or two, let it buy one small
   basket, verify the fills and fees on Kalshi match what
   `order_manager` logged, then scale up.
6. **Tail the log for the first few cycles** rather than walking away —
   the fastest signal something's wrong is a `LIVE ORDER:` line you
   didn't expect.

## What's actually implemented

- `discovery.find_open_golf_events()` — `fetch_series(category="Sports")`,
  keep `config.WINNER_SERIES`, pull every open market, group by
  `event_ticker` into `GolfEvent`s. Per-player quote from
  `yes_bid_dollars`/`yes_ask_dollars` (`last_price_dollars` fallback for
  the de-vig only). Confirmed live 2026-08-27: 4 open events (PGA Tour
  Championship, DP World, LPGA, Champions), fields of 29–156, overrounds
  1.03–2.13.
- `selection.plan_basket()` — both methods, all fee/Kelly/cap logic. Zero
  legs + a `reason` string when the field is too thin, the overround too
  wide, or nothing clears the edge.
- `order_manager.OrderManager` — dry-run by default. Only ever buys YES
  (`side="bid"`). **No order-book depth walk** — a leg is a marketable
  limit at the displayed ask and a partial fill is left partial (same
  simplification as `../../btc_implied_prob`; the risk is real, golf
  books are thin).
- `positions_store` — `open_positions` keyed by player-market ticker,
  persisted to `../positions_state.json` every `--execute` tick, reloaded
  on startup, reconciled against the real account once at start. A leg
  already held is never re-bought; a later scan **can** add new legs to
  the same event as prices move.
- No exit logic. A golf basket rides to settlement.

## Known gaps / things to fix before trusting this with money

- **Nothing is validated.** No profitable backtest exists yet (n is tiny
  — see `../README.md`). This is a research scaffold, not a proven edge.
- **Fill economics unmodelled.** Displayed-ask fills, no depth walk, no
  partial-fill handling, no slippage. A 40-leg basket into thin books is
  the least realistic part of both the backtest and the live path.
- **`devig_edge` rarely fires live and can't be backtested** — see
  `../README.md`. If you're testing that method, you're testing it live
  in dry-run only.
- **Re-entry over a tournament week is untested.** The loop will add legs
  to an event's basket as prices move; whether that compounds sensibly or
  just chases a moving field has never been watched end-to-end.
- **`MAX_EVENT_COST_DOLLARS` is a per-event cap, not a portfolio cap.**
  Overlapping tournaments stack exposure with no global ceiling.
- **No resolution / P&L logging.** The loop logs decisions and orders but
  doesn't record tournament outcomes or reconcile realized P&L — needed
  to turn live runs into backtest-quality data.
- **Reconciliation needs the player market in the current scan.** A held
  leg whose event has aged out of discovery can't be reconciled (no
  close_time to anchor it); logged and left untracked.

## Emergency stop

Kill the process (`./stop_live.sh` / `stop_live.bat`, or `kill` the PID in
`.runner.pid`). That stops new orders. It does **not** cancel anything
already resting or sell anything already filled — but `order_manager`
places marketable limits, so legs generally fill immediately and there's
usually nothing resting to cancel. There's no unwind logic: an unwanted
basket rides to the tournament resolution unless you manually sell the
legs on Kalshi.

## Status

Built 2026-08-27, **dry-run only, never run against real money.** Runs
end-to-end against the live API (discovery, scan, dry-run order path) and
the settled API (backtest). Order placement itself has **not** been
exercised against a real account from this experiment — the endpoint and
the `yes`→`bid` translation are copied from `../../resolution_alpha` /
`../../btc_implied_prob`, which verified them live, but re-verify with one
tiny real order before trusting it here. Do not flip
`GOLF_FIELD_ALPHA_DRY_RUN=false` until there's a real backtest result and
a live dry-run has been watched through a full tournament.
