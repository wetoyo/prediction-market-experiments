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

## Phase 1: prove it doesn't diverge (accepted 2026-09-26)

Result: the restart at 2026-09-22 22:56 EDT ran through 2026-09-26 01:05 EDT (3d 2h). 69 markets
were traded and settled. Across 16,538 checks there were 0 divergences and 0 inconclusive checks.
The ledger tracked every balance move exactly. Three exits triggered, but each filled 0 contracts,
so the pair-redemption timing is **still unverified**. The owner accepted Phase 1 anyway, short of
100 trades. If a filled exit is ever followed by a confirmed divergence of about +pairs, then
another of about -pairs at settlement, move the pair credit into `apply_settlement` (see
`SIM_BANKROLL_HANDOFF.md`).

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

## Phase 2: one runner sizes off its ledger (code done 2026-09-26, flag off)

- The flag is `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL` (default false). When it's on,
  `get_balance_dollars` returns `SimulatedBankroll.sizing_cash(real)`:
  `max(0, min(ledger cash, real balance))`. That's never more than the account actually holds. Before
  the first sync has initialized the ledger, it returns the coming allocation.
  - The `min` means a ledger bug can never size past what the account holds.
  - A fill the ledger missed leaves its cash too high. The divergence check catches that and resyncs
    within two syncs (about 30s).
  - A settlement the ledger hasn't applied yet leaves its cash too low, which only sizes smaller for
    one refresh.
- **Keep** the runner's per-fill `cycle_state["bankroll_dollars"] -= cost`. An earlier draft of this
  plan said to remove it, and that was wrong. The ledger is read only once per 15s refresh, and the
  runner decrements its own copy between refreshes, so nothing is subtracted twice. Without that
  decrement, a burst of fills within one refresh window would size off stale cash.
- Keep the divergence check and resync running as a safety net. Resync keeps the ledger honest if an
  event is missed.
- Roll it out with `allocation_fraction=1.0` first. Sizing should come out the same as today to within
  the tolerance, so you can compare it directly. Then try a fixed `allocation_dollars` below the
  balance.

## Phase 3: multiple models on one account

Each experiment process builds its own `OrderManager` with its own allocation. The allocations must sum
to no more than the account, with some unallocated reserve.

### Built 2026-09-26 (resolution_alpha; everything new is off by default except the order tag)

With a single runner and `SIM_BANKROLL_SHARED_ACCOUNT` off, behaviour is Phase 2's. The only live
change is the order tag in `client_order_id`.

- **Order tagging (on).** Every order's `client_order_id` is now `"<ORDER_TAG>-<y|n>-<28 hex>"`,
  and the tag defaults to `ra` (`RESOLUTION_ALPHA_ORDER_TAG`, no `-` allowed). The middle field is
  the outcome side, so a lost fill can be rebuilt from the order record alone. The ID is at most 36
  characters for tags up to 5 characters, the same length as the bare `uuid4()` it replaced. That's
  the only change in the order path, it's local, and it takes microseconds. The tag must be unique
  for each runner on the account.
- **Shared-account mode (`RESOLUTION_ALPHA_SIM_BANKROLL_SHARED_ACCOUNT`, off).** When it's on:
  - The runner's own check stops comparing against the real balance, since other runners move it
    too. The status reads `shared`.
  - It still books exact costs and its own settlements, and sizing still works as in Phase 2 (capped
    at the real balance).
  - **A restart resumes from this runner's own last status file**: cash, positions with their
    `opened_ts`, pending exact costs, and the allocation epoch. It no longer re-allocates and adopts
    every position on the account, which would include other runners' positions. Settlements that
    land while it's down get applied at the first sync.
  - A **fresh** ledger in shared mode adopts no account positions. So switch a runner into shared
    mode while it's flat.
  - Shared mode needs a fixed `SIM_BANKROLL_ALLOCATION_DOLLARS`, and the runner won't start without
    it. A fraction of the real balance at startup would include other runners' cash.
- **How a restart gets its balance right (shared mode):**
  1. **Until the ledger has resumed, sizing returns $0** (no trades). The real balance includes
     other runners' money, so it's never used as a fallback.
  2. **It resumes from its own status file**: cash, positions with their `opened_ts`, pending exact
     costs, and the allocation epoch. Settlements that landed while it was down are applied at the
     first sync.
  3. **A fill lost in the crash is recovered.** A fill booked in memory but not yet written out
     (a gap of up to 15s) is found in `GET /portfolio/orders` since the status file's `updated_ts`
     minus 120s, recognised by tag, and booked at exact cost. Orders the status file already knows
     are skipped. The resume log line gives the count, at WARNING if it's more than 0.
  4. **Lost state fails closed.** Suppose the status file is missing, unreadable, or belongs to
     another tag, but this tag has filled orders in the last 7 days. The runner then logs
     `NOT TRADING: ... its ledger state is lost` (ERROR, every 15 min) and keeps sizing at $0. To go
     on, either restore the file, or set `RESOLUTION_ALPHA_SIM_BANKROLL_ALLOW_FRESH_ALLOCATION=true`
     for one restart to start over at the allocation, then unset it. A brand-new tag with no
     history starts fresh. The reconciler logs any fresh allocation at WARNING: `<tag> FRESHLY
     ALLOCATED (epoch a -> b)`.
  5. **Two runners on one log dir:** a runner won't overwrite a status file that another live
     runner (a different instance, written within the last 60s) owns, and it logs an ERROR. Each
     runner needs its own `RESOLUTION_ALPHA_LOG_DIR`.
  - If the order records turn out to have no `client_order_id`, steps 3 and 4 can't see this
    runner's history. They log a warning and fall back: nothing is recovered, and a fresh allocation
    is allowed.
- **The status file is richer:** `order_tag`, `instance_id`, `allocation_epoch`, `resumed_from`,
  `fill_seq`, `recent_order_ids` (the last 24h of booked orders), and pending exact costs.
- **`account_reconciler.py`.** A standalone, read-only process that implements item 2 below except
  for correcting ledgers. It:
  - reads every runner's status file and the real balance;
  - checks `sum(Δ ledger cash) == Δ real balance`;
  - needs a gap to show twice, unchanged and with no fill in between, before it counts;
  - re-baselines when the set of ledgers changes or a ledger is freshly re-allocated (a new
    `allocation_epoch`);
  - skips the check when a status file is stale or has an exact cost pending;
  - warns about tickers held by more than one runner (item 3).
  - On a confirmed divergence, `attribute()` lists the evidence: filled orders whose tag matches a
    ledger that never booked them, filled orders with no known tag (a manual or outside trade), and
    the tickers that settled in the window along with who held them.
  - It writes `account_reconciler.json` and `account_reconciler_divergences.jsonl` to `LOG_DIR`.
  - **It reports only.** It never corrects a runner's ledger.

  ```
  # one check on the Pi (the first check only sets the baseline):
  ../../.venv/bin/python account_reconciler.py --count 2 --interval 20
  # several runners:
  ../../.venv/bin/python account_reconciler.py --ledger /path/a/sim_bankroll.json --ledger /path/b/sim_bankroll.json
  ```

  To run it continuously, make it a systemd service. It isn't installed. It needs no restart
  coordination with the runners:

  ```
  [Service]
  WorkingDirectory=/home/wetoyo/prediction-market-experiments/expirments/resolution_alpha
  ExecStart=/home/wetoyo/prediction-market-experiments/.venv/bin/python -u account_reconciler.py --interval 30
  Restart=on-failure
  ```

### Still open

- **Correcting a ledger across processes.** A confirmed account divergence re-baselines and gets
  logged, but the runner at fault keeps its wrong cash. Next step: the reconciler writes a
  correction (`instance_id`, id, delta) into that runner's log dir, and the runner applies it once
  at its next sync. Only do this after `attribute()` has been checked against real payloads (next
  item).
- **Verified 2026-09-26 (`inspect_order_records.py` on the Pi):** all 200 `GET /portfolio/orders`
  records carry `client_order_id`, `ticker`, `order_id`, `fill_count_fp` and all four exact-cost
  fields, and the list honours `min_ts` (1h window: 3 of 200). Restart recovery and attribution can
  rely on them. The reconciler's smoke test against the live ledger came back `ok`, gap $0.0000.
  **Tag acceptance verified 2026-09-26 14:35 EDT:** Kalshi accepts `ra-<32 hex>`. 40 tagged orders
  since the 01:24 restart, 23 filled, the rest IOC 0-fills, 0 HTTP errors; the orders list shows
  them as `ra-<hex>`. The `ra-<y|n>-<28 hex>` format from `2fb9c95` still needs the same check after
  its restart.
- ~~**The other experiments.**~~ **Code done 2026-09-26, not yet run live**: see "Running the other
  two experiments beside resolution_alpha" below.
- Items 3 to 6 below still apply as written.

### Original design notes

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

## Running the other two experiments beside resolution_alpha (code done 2026-09-26, not run live)

### What's built

- **`expirments/shared/`** is the home of the ledger now. `sim_bankroll.py` moved there from
  `resolution_alpha/` (each `order_manager.py` puts `../shared` on `sys.path`, so the import line
  didn't change). Two additions, both no-ops for resolution_alpha:
  - `book_fill(ticker, side, count, cost, order_id, opened_ts)`: books an exact fill, or the newly
    filled part of an order that's already partly booked.
  - **Holds.** `set_hold(order_id, dollars)` records the cash a resting order reserves.
    - The ledger's *available* cash is `cash - sum(holds)`, and `check`, `sizing_cash` and the status
      file's `sim_cash` use it.
    - `ledger_cash` (cash including holds) and `holds` are new status-file keys. `resume` reads
      `ledger_cash` and falls back to `sim_cash` for older files.
- **`shared/tagged_ledger.py`: `TaggedLedger`**, the wiring for runners whose orders can rest.
  btc_implied_prob and golf_field_alpha place GTC orders (the `place_order` default).
  btc_implied_prob also rests take-profit orders. So fills can land long after the POST returns,
  which resolution_alpha's book-from-the-POST wiring would miss. It works like this:
  - `record_order` runs after each POST. It books the immediate fill from the response
    (provisional, 4-decimal averages), tracks the order, and holds the remainder.
  - `sync` runs every `SIM_BANKROLL_SYNC_SECONDS` (15s) in a daemon thread, plus once inline at
    start.
    1. Updates tracked orders: one `GET /portfolio/orders?status=resting` listing, plus
       `GET /portfolio/orders/{id}` for each tracked order that has left it. Books the change in
       `fill_count_fp` and the four exact-cost fields, which also corrects the provisional
       booking. Sets each hold to remaining × price. Drops `executed`/`canceled` orders.
    2. Applies settlements for held tickers.
    3. Fetches the real balance last, then runs the check and writes the status file.
  - The 15s cadence (not the strategy's 60s/300s loop) keeps the status file fresh for
    `account_reconciler.py`, which treats a file older than 90s as stale.
  - Everything else matches resolution_alpha's wiring: the per-runner check with two-check
    confirmation, `sizing_cash`, tags, shared-account resume, fail-closed lost state, the
    two-runners-on-one-log-dir guard, and a status file the reconciler reads unchanged.
  - On a resume it recovers any order placed after the status file's last write. It lists the
    account's orders since `updated_ts - 120`, picks this tag's that the file doesn't know, and books
    them in full at exact cost.
  - A fresh initialize adopts resting orders as well as positions. Alone on the account, that's all
    of them; in shared mode, only this tag's. Their fills so far are already in the balance, so
    only later fills are booked.
  - The fresh-allocation lookback is 14 days rather than 7, because golf baskets are held up to 10.
- **btc_implied_prob** (tag `bip`) and **golf_field_alpha** (tag `gfa`), per experiment:
  - `OrderManager` builds a `TaggedLedger` when live and `<P>SIM_BANKROLL_ENABLED` (on by default,
    so shadow by default). `strategy.py --execute` calls `start_ledger()` before its startup
    reconciliation.
  - Every order carries `<tag>-<y|n>-<28 hex>`.
  - `get_balance_dollars` reads `balance_dollars` (cents `balance` as a fallback). With
    `SIZE_FROM_SIM_BANKROLL` on, it returns the ledger's `sizing_cash`.
  - With `SIZE_FROM_SIM_BANKROLL` on, `buy_favored_side` raises `OrderRefused` (nothing is sent)
    when an order could cost more than the ledger's available cash. The strategies size several
    orders off one balance read, and nothing else stops them spending past the allocation. An order
    that only closes contracts the ledger holds is always allowed (e.g. btc's exits and
    take-profits).
  - Shared mode: `get_positions` returns only this ledger's positions, so the startup
    reconciliation can't adopt another runner's. btc's `get_resting_orders` returns only this tag's,
    so its take-profit logic can't cancel another runner's orders.
  - btc_implied_prob: `_execute` skips a refused entry. `<P>EXCLUDE_SERIES` drops series from
    discovery (see the hazards below).
  - golf_field_alpha: a basket whose new legs don't fit in the ledger's available cash is skipped
    whole, not bought in part.
  - **golf bug fixed along the way:** live runs sized Kelly off `DRY_RUN_SIMULATED_BALANCE_DOLLARS`
    ($1000) whatever the account held, because `scan()` never passed a bankroll to `plan_basket`.
    Only `MAX_EVENT_COST_DOLLARS` ($40) bounded them. Live runs now pass the real (or ledger)
    balance, and skip the tick if it can't be read.
- **Tests.** `python -m pytest shared/tests resolution_alpha/tests` from `expirments/`: 28 new plus
  105 existing. `python test_ledger_wiring.py` in each of `btc_implied_prob/` and
  `golf_field_alpha/`. They run against `shared/tests/fake_kalshi.py`, a fake account that fills
  GTC orders partly, rests the remainder, fills it later, cancels, holds, pairs and settles. One
  test runs two shared-mode runners and `AccountReconciler` over one fake account: `ok` through
  fills, resting fills and settlements, then `suspect` → `diverged` on an outside trade.

### Config (`<P>` = `BTC_IMPLIED_PROB_` or `GOLF_FIELD_ALPHA_`)

| Env var | Default | What |
|---|---|---|
| `<P>SIM_BANKROLL_ENABLED` | true | Build the ledger on a live run (shadow unless the next flag is on) |
| `<P>SIZE_FROM_SIM_BANKROLL` | false | Size off (and refuse orders past) the ledger's available cash |
| `<P>SIM_BANKROLL_SHARED_ACCOUNT` | false | Several runners on the account: needs the next one |
| `<P>SIM_BANKROLL_ALLOCATION_DOLLARS` | 0 (use the fraction) | Fixed allocation |
| `<P>SIM_BANKROLL_ALLOCATION_FRACTION` | 1.0 | Fraction of the balance at a fresh start |
| `<P>ORDER_TAG` | `bip` / `gfa` | 1-5 chars, no `-`, unique on the account |
| `<P>SIM_BANKROLL_SYNC_SECONDS` | 15 | Background sync cadence |
| `<P>SIM_BANKROLL_ALLOW_FRESH_ALLOCATION` | false | One restart only, after a lost status file |
| `<P>LOG_DIR` | `<exp>/live/logs` | Status file + divergence log |
| `BTC_IMPLIED_PROB_EXCLUDE_SERIES` | empty | Comma list of series btc never trades |

### Steps to go live beside resolution_alpha

1. **Pick allocations.** Together they must stay under the balance, with some reserve left over. The
   account held about $5.30 on 2026-09-26, so each slice would be a dollar or two, and at that size
   Kelly rounds most orders to 0 contracts. Fund the account first if that matters.
2. **resolution_alpha into shared mode.** Set `RESOLUTION_ALPHA_SIM_BANKROLL_SHARED_ACCOUNT=true` and
   `RESOLUTION_ALPHA_SIM_BANKROLL_ALLOCATION_DOLLARS=<x>` in `live/.env`. Restart it while it's flat,
   outside a resolution window. A fresh shared ledger adopts no positions.
3. **Each other runner's `live/.env`:** `<P>DRY_RUN=false`, `<P>SIZE_FROM_SIM_BANKROLL=true`,
   `<P>SIM_BANKROLL_SHARED_ACCOUNT=true`, `<P>SIM_BANKROLL_ALLOCATION_DOLLARS=<y>`, and for btc the
   `EXCLUDE_SERIES` choice below. Start it the usual way (`live/start_live.sh`). Expect
   `[sim-bankroll] initialized (SIZING off it, shared account, tag 'bip')` in its log.
4. **Run the reconciler continuously** over all three status files:
   ```
   ../../.venv/bin/python -u account_reconciler.py --interval 30 \
     --ledger live/logs/sim_bankroll.json \
     --ledger ../btc_implied_prob/live/logs/sim_bankroll.json \
     --ledger ../golf_field_alpha/live/logs/sim_bankroll.json
   ```
5. **First checks** (same as resolution_alpha's):
   - Kalshi accepts the `bip-…` / `gfa-…` IDs: `LIVE ORDER` followed by fills, no 400s.
   - `account_reconciler.json` stays `ok`, including while a bip/gfa order rests (the hold
     assumption below).

### Hazards and unverified assumptions

- **Resting-order collateral (unverified).** The ledger assumes a resting buy holds
  remaining × limit out of `balance`, and that an order closing contracts the ledger holds (btc's
  take-profit) holds nothing. No runner has rested an order on this account since the ledger
  exists. If either assumption is wrong, the reconciler shows a gap of about the resting order's
  value for as long as it rests. The fix goes in `TaggedLedger._hold_dollars`.
- **btc_implied_prob overlaps resolution_alpha's series.** btc scans the fifteen_min, thirty_min and
  hourly BTC series, which include KXBTC15M and KXBTCD; resolution_alpha trades those plus KXETH15M
  and KXETHD. Kalshi nets positions per account (Phase 3 item 3), so if the two hold opposite legs
  on one ticker, the account redeems the pair at once. At settlement the account may then hold
  nothing and write no settlement record, and both ledgers would keep a stale position with the $1
  unaccounted for. btc's resting take-profits can also make resolution_alpha's IOC orders cancel
  under `taker_at_cross` self-trade prevention (item 4). `BTC_IMPLIED_PROB_EXCLUDE_SERIES=KXBTC15M,KXBTCD`
  avoids both, at the cost of most of btc's markets. **The owner decides.** The reconciler's
  `overlapping_tickers` shows it when it happens.
- **btc_implied_prob's `_to_api_order` still rounds prices to the cent** (pre-existing). The crypto
  series now tick in 0.001 steps in [0.90, 1.00], where resolution_alpha found that rounding
  under-fills or gets `invalid_price` (its `order_manager.py` docstring). The rest of the band is
  still whole-cent.
- golf_field_alpha's player markets never overlap the crypto series.



`main` now carries everything on this branch. From here on, work on `main`; the
`resolution-alpha-no-kalshi-state` branch stays on `origin` for history.

What was done:

1. `6598821` (the `KalshiStateManager` order-path routing in all three experiments, plus the
   `CAPITAL_FRACTION`/`CAPITAL_CAP_DOLLARS` config) was reverted on `main` (`5facfbf`). Nothing
   else used `kalshi_state.py`, so the submodule dropped it too. The submodule also got the Pi's
   keep-alive `requests.Session` in `fetch_historical.py` as a real commit, so its tree is
   `dd89744` plus that one change (`bb616f4`).
2. The branch was merged (`3f49dad`). Both `runner.py` Kelly hunks took the branch side. A stray
   `market_kelly_caps.setdefault(...)` from main's copy of the freeze fix was dropped, since it
   would NameError after a fill. The three experiments' code on `main` is byte-identical to the
   branch.
3. The dev checkout's stale uncommitted copies of `5670109` were stashed, not deleted (`git stash
   list` in the dev repo). So was the abandoned WAL rework of `kalshi_state.py` inside the
   submodule. Backup ref: `backup/main-pre-ra-sync-20260926`.
4. `reconstruct_trade_history.py` was only ever an untracked file. It's now committed on `main`.

The Pi's code is the same whether it's on the branch tip or `main`: the only differences are docs,
`reconstruct_trade_history.py`, and the submodule's keep-alive, which the Pi already had
uncommitted. Moving its checkout is a disk-only change and needs no restart:

```
cd ~/prediction-market-experiments
git status && git -C prediction_market_scraper status --short   # expect only the keep-alive M
git fetch origin && git -C prediction_market_scraper fetch origin
git diff --stat HEAD origin/main -- expirments                    # expect docs + reconstruct script only
git -C prediction_market_scraper diff bb616f4 -- Clients/Kalshi/fetch_historical.py   # expect empty
git -C prediction_market_scraper checkout -- Clients/Kalshi/fetch_historical.py
git checkout main && git merge --ff-only origin/main && git submodule update --init
```
