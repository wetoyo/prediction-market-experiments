# Handoff: running all three experiments on one Kalshi account

**Written:** 2026-09-26 ~15:00 EDT (Pi clock), by a Claude Code session, right after commit `1b9145e`.
**Owner:** wetoyo. **For:** the next agent picking up the multi-runner work.

Two docs sit alongside this one:
- `resolution_alpha/live/SIM_BANKROLL_PLAN.md` is the design and rollout plan. Its section **"Running
  the other two experiments beside resolution_alpha"** is the reference for this work: what's built,
  every env var, the go-live steps, and the hazards.
- `resolution_alpha/live/SIM_BANKROLL_HANDOFF.md` is resolution_alpha's own live status and results
  log.

This file covers where the multi-runner work stands and what to do next.

---

## The goal

Three strategies share one Kalshi account: `resolution_alpha` (`ra`, live on the Pi),
`btc_implied_prob` (`bip`) and `golf_field_alpha` (`gfa`). They will run at the same time, each on
its own dollar allocation.

- Each runner keeps its own ledger: cash, positions, fills and settlements.
- Each runner tags its orders with `client_order_id = <tag>-<y|n>-<28 hex>`.
- `resolution_alpha/account_reconciler.py` checks that the ledgers sum to the real balance.
- Nothing shared sits in any order path. A shared SQLite throttle once caused a zero-fill incident;
  the plan's "Why" section explains it.

## State right now

| | |
|---|---|
| resolution_alpha on the Pi | `resolution-alpha.service`, PID **45269**. The owner restarted it at **2026-09-26 14:37:48 EDT** onto `24e28af`. Phase 2 is live: it sizes off its own ledger, allocation fraction 1.0, **not** shared mode. Balance about **$5.30**. |
| New ID format `ra-<y|n>-<28 hex>` | **Unconfirmed.** No resolution_alpha order had been placed since the restart as of 14:58. The older `ra-<32 hex>` was accepted: 40 orders and 23 fills with 0 HTTP errors, 01:24-14:37. |
| btc_implied_prob / golf_field_alpha | **Code done, nothing running** anywhere. They're shadow by default. |
| Git | Everything is on `origin/main` (`1b9145e`). The Pi's checkout is still `24e28af`: it has **not pulled** `1b9145e`. |
| Tests | `python -m pytest shared/tests resolution_alpha/tests` from `expirments/`: 133 pass. `python test_ledger_wiring.py` in `btc_implied_prob/` and in `golf_field_alpha/`: all scenarios pass. |

### What `1b9145e` did

- Moved `sim_bankroll.py` from `resolution_alpha/` to **`expirments/shared/`**.
  - Each `order_manager.py` appends `../shared` to `sys.path`, so the imports read the same as
    before.
  - It adds resting-order **holds**: the available cash is `cash - sum(holds)`, and the status file's
    `sim_cash` is that available figure. It also adds `book_fill`.
  - Both additions are no-ops for resolution_alpha, which sends IOC orders only.
- Added **`shared/tagged_ledger.py` (`TaggedLedger`)**, the ledger wiring for runners with GTC orders
  that can rest and fill later.
  - It tracks each order until it's executed or canceled.
  - It books the change in the order record's fill count and cost since the last sync, at exact cost.
  - It applies settlements, runs the per-runner check, and supports shared-mode resume and
    fail-closed behaviour.
  - It syncs in a 15s daemon thread.
- Wired `TaggedLedger` into btc_implied_prob and golf_field_alpha:
  - both: `order_manager.py`, `config.py` (the "Per-runner bankroll" block) and `strategy.py`;
  - golf: `live/.env.example`.
- Fixed a golf bug: live runs sized off the $1000 dry-run balance.
- Added `BTC_IMPLIED_PROB_EXCLUDE_SERIES`.

## Start here

1. **Check the new resolution_alpha ID format.** This is read-only and safe while trading:
   ```
   ssh wetoyo@100.109.148.56
   journalctl -u resolution-alpha.service --since '2026-09-26 14:37' --no-pager | grep -v 'eval summary' | grep -E 'LIVE ORDER|entry filled|0 filled|HTTPError|invalid|400|ERROR|Traceback'
   cd ~/prediction-market-experiments/expirments/resolution_alpha && ../../.venv/bin/python inspect_order_records.py
   ```
   Fills, no 400s, and the `ra-<y|n>-<hex>` shape all showing means it's accepted: add a row to
   `SIM_BANKROLL_HANDOFF.md`'s results log. A 400 or `invalid` means it's rejected: **tell the owner
   right away**, because resolution_alpha then can't enter any trade. The fix is described in that
   handoff's caveat section, under "Orders rejected".
2. **Get the Pi onto `1b9145e`** (disk only, no restart). This matters before resolution_alpha's
   *next* restart: its `order_manager.py` on `main` imports from `expirments/shared/`, and a restart
   onto a partial checkout would fail at import. Moving the Pi to `main` is a plain fast-forward:
   ```
   cd ~/prediction-market-experiments
   git status && git -C prediction_market_scraper status --short   # expect only the *.bak files untracked
   git fetch origin && git diff --stat HEAD origin/main              # expect the 1b9145e files
   git merge --ff-only origin/main && git submodule update
   # import smoke test (loads what a restart would load; places nothing):
   cd expirments/resolution_alpha && ../../.venv/bin/python -c "import order_manager; print('import ok')"
   ```
   The running process is unaffected by a disk change. Ask the owner before any restart.
3. **Get the owner's decisions** (below) before anything else goes live.

## Decisions the owner hasn't made yet

1. **Allocations.** They must total less than the balance, with some reserve. At about $5.30, each
   slice is a dollar or two, and Kelly rounds most orders to 0 contracts. Funding the account first
   may be the real prerequisite.
2. **btc_implied_prob and resolution_alpha overlap.** btc scans KXBTC15M and KXBTCD, which
   resolution_alpha trades too.
   - Kalshi nets positions per account, so opposite legs held by two runners redeem at once.
   - The account may then get no settlement record, and both ledgers keep a phantom position.
   - btc's resting take-profits can also make resolution_alpha's IOC orders cancel under
     `taker_at_cross` self-trade prevention.

   `BTC_IMPLIED_PROB_EXCLUDE_SERIES=KXBTC15M,KXBTCD` avoids all of this, at the cost of most of btc's
   markets. The other way out is to build a settlement fallback (next section, item 3).
3. **When to put resolution_alpha into shared mode.** Set
   `RESOLUTION_ALPHA_SIM_BANKROLL_SHARED_ACCOUNT=true` plus `..._ALLOCATION_DOLLARS`, then restart
   while it's flat, outside a resolution window. It must be in shared mode before a second runner goes
   live: its per-runner check assumes it's alone on the account and would resync to the other runners'
   moves.

The go-live order, from the plan: ra into shared mode → set each other runner's `live/.env`
(`<P>DRY_RUN=false`, `SIZE_FROM_SIM_BANKROLL=true`, `SIM_BANKROLL_SHARED_ACCOUNT=true`,
`SIM_BANKROLL_ALLOCATION_DOLLARS=<x>`) → start it with `live/start_live.sh` → run `account_reconciler.py
--interval 30` with one `--ledger` per runner.

## Open work, most important first

1. **Check the resting-order collateral assumption. It's untested.** `TaggedLedger._hold_dollars`
   assumes two things:
   - a resting buy holds remaining × limit out of `balance`;
   - an order that closes contracts this ledger holds, like btc's take-profit (a sell of the held
     side), holds nothing.

   Nothing on this account has rested since the ledger existed. When a bip or gfa order first rests,
   watch the reconciler or the per-runner check. A gap of about the order's value while it rests means
   an assumption is wrong. Fees reserved on resting orders are not modelled either.
   - A cheaper test: rest one 1-contract buy priced never to fill, compare the balance before and
     after, then cancel it. That puts a real order on the live account and changes resolution_alpha's
     balance, so **only with the owner's OK**.
2. **btc_implied_prob's `_to_api_order` still rounds to the cent.** The crypto series tick in 0.001
   steps in [0.90, 1.00]. resolution_alpha found that rounding there under-fills or gets
   `invalid_price` (see its `order_manager.py` docstring). The fix is to port resolution_alpha's
   `round(_, 4)` with its `(0, 1)` guard, and check btc's callers pass real book levels (they pass
   top-of-book `yes_ask` / `1 - yes_bid`).
3. **A settlement fallback for netted tickers.** It's only needed if btc keeps resolution_alpha's
   series. If a ledger holds a ticker past its close and no `/portfolio/settlements` record arrives,
   read the market's result from the public `GET /markets/{ticker}` and apply that.
4. **The reconciler can't correct a ledger across processes.** It still only reports. The plan's
   Phase 3 "Still open" section has the design.
5. **Neither new experiment has a systemd unit.** They start with `live/start_live.sh` (nohup, a PID
   file). Before running them on the Pi, give each one a unit like `resolution-alpha.service`, a
   `live/.env`, and the reconciler as a service. The plan has a unit sketch.

## Where things are

| What | Where |
|---|---|
| Pure ledger (cash, positions, pairs, settlements, holds, check, snapshot/resume) | `shared/sim_bankroll.py` |
| GTC ledger wiring (`TaggedLedger`) | `shared/tagged_ledger.py`. Its module docstring explains the booking model. |
| resolution_alpha's own wiring (books from the POST response, IOC) | `resolution_alpha/order_manager.py` |
| Account-level check | `resolution_alpha/account_reconciler.py` |
| Fake Kalshi account for tests | `shared/tests/fake_kalshi.py`. It follows the same hold assumption, so it tests wiring, not Kalshi's rules. |
| Tests | `shared/tests/test_tagged_ledger.py`, `resolution_alpha/tests/`, `<exp>/test_ledger_wiring.py` |
| Per-experiment config | the "Per-runner bankroll" block at the end of `btc_implied_prob/config.py` and `golf_field_alpha/config.py` |
| Status file per runner | `<exp>/live/logs/sim_bankroll.json`, set by `<P>LOG_DIR`, which must differ per runner |

The Kalshi payload facts checked on the Pi on 2026-09-26 (read-only):
- `GET /portfolio/orders` records carry `client_order_id`, `outcome_side` (`yes`/`no`, the economic
  side), `status` (`resting`/`canceled`/`executed`), `fill_count_fp`, `remaining_count_fp`,
  `yes_price_dollars`/`no_price_dollars` and the four exact-cost fields.
- `status=resting` and `min_ts` filters work.
- `GET /portfolio/balance` has both `balance_dollars` and the cents `balance`.

## Working on this (read before touching the Pi)

- The Pi (`wetoyo@100.109.148.56`) is **live real-money trading**.
  - Never restart or stop `resolution-alpha.service` without the owner's OK.
  - Back up files before editing them on the Pi.
  - Never hit the wake server's toggle endpoints.
- The Pi has no git identity. Commit on the dev machine, push, then run `git merge --ff-only` on the
  Pi.
- `live/.env` files aren't shell-sourceable, and they hold credentials. Parse them in Python (see
  `inspect_order_records.py`'s `_load_env`) and never print them.
- **Harness tip (Windows dev box):** in the Bash tool, a quoted heredoc (`<<'EOF'`) whose body
  contains an apostrophe can fail with "unexpected EOF". For multi-line Python edits, write the script
  to the scratchpad and run it.
- Dev Python is 3.10 and the Pi's is 3.13. Keep code 3.10-compatible; for example,
  `datetime.fromisoformat` doesn't take a trailing `Z` on 3.10.
