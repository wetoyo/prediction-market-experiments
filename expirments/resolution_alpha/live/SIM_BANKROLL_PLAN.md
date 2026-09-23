# Per-runner order managers with their own bankroll: plan

Written 2026-09-22 on `resolution-alpha-no-kalshi-state`, the branch that is live on the Pi.

## Why

`main` sends every experiment's Kalshi traffic through one shared `KalshiStateManager`: a global
throttle, a balance cache, and an `order_log` in SQLite, with `CAPITAL_FRACTION`/`CAPITAL_CAP_DOLLARS`
taking a slice of the *real* balance. On 2026-09-04 that throttle's SD-card fsyncs sat in front of
every order POST, and IOC orders arrived after the book had moved (5/5 zero fills; see the handoff in
`README.md`). This branch removed it, and that fixed the fills.

A fraction of the real balance also isn't a real allocation. Once two models share an account, model
A's open positions and losses shrink the real balance, so model B's "25%" shrinks with them. Each model
needs its own cash that moves only with its own fills and settlements.

The target design: every runner builds its own `OrderManager` with an allocation (a dollar amount or a
fraction of the balance at startup). The manager keeps that runner's ledger (cash, positions, fills,
settlements), and the runner sizes off that ledger. Nothing shared sits in the order path.

## Phase 0: shadow ledger (done, this commit)

- `sim_bankroll.py`: `SimulatedBankroll` is a pure ledger with no network calls.
  - Every fill costs `price * count + fee`.
  - A matched yes+no pair on one ticker redeems for $1.
  - Settlement pays $1 per contract this ledger holds on the winning side. It deliberately does not
    use the settlement record's account-wide `revenue`.
- `OrderManager(allocation_dollars=..., allocation_fraction=...)`
  (`RESOLUTION_ALPHA_SIM_BANKROLL_ALLOCATION_DOLLARS` / `_FRACTION`, default fraction 1.0):
  - `buy_favored_side` books each live fill from the POST response right after the order returns.
    That's in-memory work only, it's wrapped so it can never raise, and the POST itself is unchanged.
  - The POST response only has 4-decimal per-contract averages, so the booked cost is provisional.
    The next sync replaces it with the exact `taker/maker_fill_cost_dollars + fees` from
    `GET /portfolio/orders/{id}`.
  - `get_balance_dollars` returns the same real balance as before. It also stashes that balance and
    the ledger's fill counter.
  - `sync_sim_bankroll` runs after every successful bankroll refresh (every 15s) as a fire-and-forget
    background thread. It never delays the trading loop. It:
    1. fetches exact order costs,
    2. applies settlements for tickers it holds (`/portfolio/settlements?min_ts=`),
    3. checks the ledger against the real balance.
- **Divergence check.** It compares *changes* since the last sync: `cash` should equal
  `real_balance + offset`, where `offset = cash - real` at the last sync. This is correct for any
  allocation as long as this is the only runner on the account.
  - Tolerance is $0.01 (`..._TOLERANCE_DOLLARS`).
  - A gap must show on **two consecutive checks** before it counts. A settlement that lands between
    the balance fetch and the settlements fetch causes a legitimate one-poll gap.
  - A check is skipped (`inconclusive`) when a fill was booked after the balance snapshot, or when a
    fill's exact cost hasn't been fetched yet.
  - A confirmed divergence: `divergence_count += 1`, a WARNING log line (also printed in lightweight
    mode), one line appended to `live/logs/sim_bankroll_divergences.jsonl`, and a resync to the real
    balance.
- `live/logs/sim_bankroll.json` holds the current ledger, the last check, and the counts. It's
  rewritten on every sync.
- On startup the ledger adopts positions already open on the account (`GET /portfolio/positions`), so
  settling them doesn't read as divergence after a restart.
- **Nothing sizes off the ledger.** The Kelly bankroll is still the real balance.
- `RESOLUTION_ALPHA_SIM_BANKROLL_ENABLED=false` removes it completely. It's inactive in dry-run.

It takes effect on the next `resolution-alpha.service` restart.

## Phase 1: prove it doesn't diverge

This phase only works while this runner is the only thing trading the account.

1. Run for at least 3 days and at least 100 settled trades, including at least one exit. An exit tests
   the pair-redemption timing assumption: the ledger credits $1 per matched pair as soon as the pair
   exists. If Kalshi only credits it at settlement, you'll see a confirmed divergence of +pairs right
   after the exit, then the opposite one at settlement.
2. Pass criterion: `divergence_count == 0` in `sim_bankroll.json` for every run, and
   `sim_bankroll_divergences.jsonl` is empty. The only allowed exceptions are events explained by
   something outside the runner (a deposit or withdrawal, or a manual trade on the website), and each
   one needs a written explanation.
3. Watch `inconclusive_checks / checks`. It should be small, around the fill rate. If it's high, exact
   order-cost fetches are failing.
4. Any unexplained divergence: read the jsonl event (gap, open positions), fix the ledger rule, and
   restart the clock.

Handy checks on the Pi:

```
cat expirments/resolution_alpha/live/logs/sim_bankroll.json
journalctl -u resolution-alpha.service | grep sim-bankroll
```

## Phase 2: one runner sizes off its ledger

- Add a flag, `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL` (default false). When it's on,
  `get_balance_dollars` returns `min(ledger cash, real balance)`. Never more than the account actually
  holds.
- The runner's per-fill `cycle_state["bankroll_dollars"] -= cost` becomes redundant, because the
  ledger already decrements on every fill. Remove it in the same change so the cost isn't subtracted
  twice.
- Keep the divergence check and resync running as a safety net. Resync keeps the ledger honest if an
  event is missed.
- Roll it out with `allocation_fraction=1.0` first. Sizing should come out the same as today to within
  the tolerance, so you can compare it directly. Then try a fixed `allocation_dollars` below the
  balance.

## Phase 3: multiple models on one account

Each experiment process builds its own `OrderManager` with its own allocation. The allocations must sum
to no more than the account, with some unallocated reserve. What has to change:

1. **Attributing fills without shared state.** Pass
   `client_order_id=f"{EXPERIMENT_NAME}-{uuid4()}"` (`place_order` already accepts it). Any process can
   then assign every account fill to a model from `/portfolio/fills` alone. There's no shared database
   and nothing is added to the order path.
2. **An account-level reconciler replaces the per-runner check.** The Phase 0 check assumes one runner
   ("every real balance change is mine"), and that stops being true. Instead:
   - Each runner publishes its ledger (the `sim_bankroll.json` it already writes, one file per
     experiment).
   - A reconciler checks `sum(Δ ledger_i) == Δ real balance` for the account.
   - It then attributes any gap by rebuilding each ledger from `/portfolio/fills` (grouped by
     `client_order_id` prefix) and `/portfolio/settlements` (paid out on each ledger's own contracts).
     The rebuild tells you *which* ledger is wrong, so the resync fixes only that one.
   - Run it as a small standalone timer service so it's never in a trading process's loop.
   - Log per-model divergence counts, same as Phase 0.
   - Deposits and withdrawals show up as an unattributable gap. Handle them explicitly: split them by
     allocation fraction, or put them in the reserve.
3. **Netting between models on the same ticker.** Kalshi nets positions at the account level. If model
   A holds YES and model B buys NO on the same ticker, the account redeems the pair right away, but
   each ledger still thinks it holds its own leg. The account total still agrees at settlement, but in
   the meantime the reconciler has to expect this. There's also a real trading hazard: A's
   `reduce_only` exit can fail or only partly fill because the *account's* net position isn't A's
   position. Either keep models to separate tickers or series, or treat cross-model netting as a known
   reconciler case and don't use `reduce_only` for exits there.
4. **Self-trade prevention.** Orders use `self_trade_prevention_type="taker_at_cross"`. If one model
   ever rests orders, another model's taker order crossing them gets cancelled. Every model is IOC
   today, so this doesn't apply yet. Recheck it before adding a maker strategy.
5. **Rate limits.** No shared throttle in the order path (see "Why"). Each process can pace its own
   calls, and the reconciler's reads are off-path. If account-level 429s appear, give each process a
   fixed share of the rate limit rather than a shared lock.
6. **Shards.** Collateral is per `exchange_index`. Allocations are in dollars across the whole account,
   so the per-shard guard (`get_shard_balances`) still has to run against the real account.

## Syncing this branch with `main`

State as of 2026-09-22:

- `main` has `6598821`, which routes all three experiments through `KalshiStateManager`. This branch
  deliberately doesn't have it.
- `main` also has two README doc commits (`c6bb2f1`, `22fd3a7`).
- `6a01b71` on main and `5d5d33c` on this branch are the same Kelly-cap-freeze fix.
- This branch adds the live Pi batch (`5670109`) and this shadow ledger.

A trial merge of this branch into `main` conflicts only in `runner.py`, in two hunks, both where main's
per-ticker `market_kelly_caps` meets this branch's group-shared `kelly_group_caps`. Take this branch's
side for both, since it's the newer fix.

Steps:

1. On `main`, revert `6598821`'s order-path routing: `order_manager.py` in all three experiments, and
   the `CAPITAL_FRACTION`/`CAPITAL_CAP_DOLLARS` config. The per-runner allocation above replaces it.
   Keep `kalshi_state.py` in the submodule only if something off the order path still uses it.
2. Merge this branch into `main` and resolve the `runner.py` hunks to this branch's side.
3. The dev working tree on `main` has uncommitted copies of most of `5670109`. Discard them after the
   merge. They're a subset of the commit, which also has the bankroll-refresh fix they lack.
4. The submodule: the Pi has an uncommitted keep-alive `requests.Session` change in
   `prediction_market_scraper/Clients/Kalshi/fetch_historical.py`. Commit it in the submodule repo,
   then bump the pointer here. main's pointer (`5388705`) and this branch's (`dd89744`) differ only by
   the `kalshi_state` work.
5. Point the Pi at `main` only after step 1 is merged. Until then it stays on this branch.
