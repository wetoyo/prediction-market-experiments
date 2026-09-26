# Handoff: per-runner bankroll (resolution_alpha), Phases 1–3

**Last updated:** 2026-09-26 01:40 EDT, by a Claude Code session, for whoever picks this up next.
**Owner:** wetoyo. **Rollout plan and design:** `live/SIM_BANKROLL_PLAN.md`. Read it for the *why*;
this file covers *where things stand and what to do next*.

---

## TL;DR: what's running right now

| | |
|---|---|
| Pi service | `resolution-alpha.service`, PID **41866**, restarted by the owner **2026-09-26 01:24:39 EDT** |
| Code the process runs | **`f16bb50`** (Phase 3 groundwork). The disk has `2fb9c95`, which isn't running (see below). |
| Branch | `resolution-alpha-no-kalshi-state` (**not `main`**), pushed to origin |
| `live/.env` | `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL=true`. Shared-account mode is **not** set (off). |
| Effect | **Phase 2 is live: the runner sizes off its ledger** (`initialized (SIZING off it)` at 01:24:44). Allocation fraction 1.0, so sizing should equal the real balance. Orders carry `client_order_id = "ra-<32 hex>"`. |
| Ledger at handoff | sim $3.3313 == real $3.3313. 0 divergences. No trades since the restart yet (as of 01:35). |
| Dev worktree | `D:/Files/Code/pme-ra-branch` has the branch checked out, with the submodule initialized. The main checkout at `D:/Files/Code/prediction-market-experiments` is on `main`, with unrelated uncommitted work. **Don't commit this branch's work there.** |

### Commits this session (all on the branch, all pushed)

| Commit | What | Running? |
|---|---|---|
| `d846160` | Phase 2: `SIZE_FROM_SIM_BANKROLL` (sizing = `max(0, min(ledger cash, real))`) + this handoff | yes (flag on) |
| `f16bb50` | Phase 3 groundwork: order tagging, `SIM_BANKROLL_SHARED_ACCOUNT` (off), `account_reconciler.py` | yes (tag only) |
| `2fb9c95` | Shared-mode restart safety (resume recovers lost fills, fails closed on lost state, and more) | **no: needs a restart** |
| (next) | `inspect_order_records.py` + this rewrite | n/a, docs/tooling |

`2fb9c95` changes the order ID format to `"ra-<y|n>-<28 hex>"`. Otherwise it only touches shared
mode, which is off. Restarting onto it is safe once the caveat below is checked, and the owner
decides when.

---

## ⚠ Open caveat: unverified Kalshi payload fields (do this first)

Several Phase 3 features read **`GET /portfolio/orders`** list records, and nobody has looked at a
real one on this account yet. The earlier session's attempt was blocked by the auto-mode classifier.
The owner said they'd run the next session with `--dangerously-skip-permissions` so these can be read.

**What depends on what:**

| Assumption | Needed by | If it's false |
|---|---|---|
| List records carry `client_order_id` | Shared-mode restart recovery of a crash-lost fill (`OrderManager._own_filled_orders`), the fail-closed lost-state guard (`_fresh_allocation_allowed`), and the reconciler's attribution (`account_reconciler.attribute`) | Recovery and the guard log a warning and **fall back**: nothing is recovered, and a fresh allocation is allowed. Attribution files every fill as "untagged". The summed reconciler check still works. |
| List records carry `ticker`, `order_id`, `fill_count_fp`, and the exact-cost fields | Same as above (`_book_order_record`) | Recovery raises a KeyError on `ticker`, the sync logs an exception, and the ledger stays uninitialized. **Sizing stays $0 in shared mode.** Fails closed. |
| `min_ts` is honoured on the orders list | Recovery window and the 7-day lookback | Results are deduped by order id, so it's still correct, but capped at 10 pages × 200. If `min_ts` is ignored, the 7-day lookback only sees the newest 2,000 orders. |
| Kalshi **accepts** a tagged `client_order_id` (`ra-<hex>`, and from `2fb9c95` `ra-<y|n>-<hex>`) | Every live order since the 01:24 restart | Orders are rejected, probably with HTTP 400. The ticker gets blacklisted for the cycle, so **no entries fill**. |

**How to check (read-only, safe while trading):**

```
ssh wetoyo@100.109.148.56
cd ~/prediction-market-experiments/expirments/resolution_alpha
# 1. Did orders since the 01:24 restart go through? Look for LIVE ORDER followed by "entry filled", and no 400s.
journalctl -u resolution-alpha.service --since '2026-09-26 01:24' | grep -E 'LIVE ORDER|entry filled|HTTPError|400|ERROR'
# 2. Field check: prints field names, counts and client_order_id shapes only (no balances or credentials)
../../.venv/bin/python inspect_order_records.py
# 3. Reconciler smoke test: the first check sets the baseline, the second is a real check. Expect "ok".
../../.venv/bin/python account_reconciler.py --count 2 --interval 20
```

What to do with the results:

- `client_order_id` **present** and the `ra-...` shapes show up among recent orders: the recovery,
  the guard and attribution are all good to go. Remove the "Unverified" bullet in the plan's Phase 3
  "Still open" list.
- `client_order_id` **missing**: the tag can't be read back from the list. Options: look for it on
  `GET /portfolio/fills` instead; or have the ledger persist every order id immediately (writing a
  small append-only file off the order path, **not** in `buy_favored_side`'s critical section).
  Update `_own_filled_orders` and `attribute` to match, and adjust the tests' `_Account` fake to
  the real record shape.
- `ticker` or cost fields **missing** from the list: fetch each candidate order with
  `GET /portfolio/orders/{id}`, which is known to have the cost fields, before booking it.
- **Orders rejected** because of the tag: urgent, since the live runner can't fill. Tell the owner.
  The fastest fix is to revert to a bare uuid by passing no `client_order_id`, which makes
  `live_execution.place_order` fall back to `uuid4()`. Tagging, recovery and attribution then need
  another way to identify orders (see the previous point).

The other open caveat, carried over from Phase 1: **pair-redemption timing is still unverified.**
Three exits triggered in Phase 1, and all filled 0 contracts. The first exit that *fills* is the
test; see "Known open question" below. If the assumption is wrong, the ledger briefly shows about
+$1 per pair *more* cash than the account right after the exit. Sizing is capped at the real balance,
so it never sizes past what the account holds. The divergence check resyncs it within about 30s,
and the mirror-image gap at settlement briefly under-sizes.

---

## Next steps, in order

1. **Clear the caveat above.** Then record the outcome in the results log at the bottom of this
   file.
2. **Restart onto `2fb9c95`** when the owner OKs it (`sudo systemctl restart
   resolution-alpha.service`). Afterwards, check that the new-format order IDs are accepted, using
   the same journal grep.
3. **Watch Phase 2** for a few days: `divergence_count` stays 0, and sizing looks the same as
   before. Then try a fixed `RESOLUTION_ALPHA_SIM_BANKROLL_ALLOCATION_DOLLARS` below the balance
   (the plan's Phase 2 last step).
4. **Optionally, run the reconciler continuously.** The systemd unit sketch is in the plan's
   Phase 3 section. It isn't installed. With one runner it duplicates the runner's own check.
5. **Sync the branch into `main`** (plan: "Syncing this branch with main"). This is a prerequisite
   for getting the other experiments onto per-runner ledgers.
6. **Remaining Phase 3 work** (plan's "Still open"):
   - cross-process ledger correction from the reconciler (after attribution is verified);
   - porting `sim_bankroll` + wiring to `btc_implied_prob` / `golf_field_alpha`, which are stale
     on this branch.

### Before turning shared-account mode on (only when a second runner exists)

- Set `RESOLUTION_ALPHA_SIM_BANKROLL_ALLOCATION_DOLLARS` (it's required; the runner refuses to start
  without it).
- Give each runner its own `RESOLUTION_ALPHA_ORDER_TAG` (no `-`) **and** its own
  `RESOLUTION_ALPHA_LOG_DIR`.
- Switch while the runner is **flat**. A fresh shared-mode ledger adopts no account positions.
- On restart it resumes from its own `sim_bankroll.json` and recovers crash-lost fills. If that
  file is lost while the tag has recent fills, it logs `NOT TRADING: ... its ledger state is lost`
  and sizes $0. To go on, restore the file, or set
  `RESOLUTION_ALPHA_SIM_BANKROLL_ALLOW_FRESH_ALLOCATION=true` for **one** restart, then unset it.

---

## Where everything is

| What | Where (under `expirments/resolution_alpha/`) |
|---|---|
| Ledger logic (pure, no network) | `sim_bankroll.py`: `SimulatedBankroll` (`record_fill`, `apply_exact_cost`, `apply_settlement`, `check`, `sizing_cash`, `resume`, `_book_order_record`, `snapshot`) |
| Wiring | `order_manager.py`: `buy_favored_side` (tagging + `record_fill`), `get_balance_dollars` (sizing), `sync_sim_bankroll` / `_initialize_sim` (resume, recovery, fail-closed guard), `_status_file_taken` |
| Runner hook | `runner.py`, bankroll-refresh block: starts the sync in the background after each good balance poll. The per-fill `bankroll -= cost` is **intentionally kept** (see the plan, Phase 2). |
| Account reconciler | `account_reconciler.py`: pure `AccountReconciler.check` + `attribute`; CLI loop |
| Payload check | `inspect_order_records.py` (read-only) |
| Config | `config.py`: `SIM_BANKROLL_*`, `SIZE_FROM_SIM_BANKROLL`, `ORDER_TAG`, `SIM_BANKROLL_SHARED_ACCOUNT`, `SIM_BANKROLL_ALLOW_FRESH_ALLOCATION`. Every env var is prefixed `RESOLUTION_ALPHA_`. |
| Tests | `tests/test_sim_bankroll.py`, `tests/test_account_reconciler.py`. **105 tests** in the full suite; all pass locally and on the Pi. Run `python -m pytest tests -q` from `expirments/resolution_alpha`. |
| Pi | `wetoyo@100.109.148.56`, repo at `~/prediction-market-experiments`, venv at `.venv` |

## Phase 1 (done): what it tested

Phase 1 ran the ledger **in shadow** from 2026-09-22 22:56 to 2026-09-26 01:24. It was tracked and
checked, but nothing traded off it, to prove it stays in lockstep with the real balance while
resolution_alpha is the only thing trading the account. Result: 69 settled trades, 16,560 checks,
**0 divergences**, 0 inconclusive checks. The owner accepted it without waiting for the 100-trade
minimum or a filled exit.

The sections below still apply to reading the live ledger under Phase 2.

## How to read the results

All results are on the Pi, under `~/prediction-market-experiments/expirments/resolution_alpha/live/logs/`.

```
# Current state + running tally (rewritten every ~15s)
cat live/logs/sim_bankroll.json

# One JSON line per confirmed divergence (the file doesn't exist if there have been none)
cat live/logs/sim_bankroll_divergences.jsonl

# Log lines: init, "suspect" gaps, confirmed divergences, and a ~15-min summary
journalctl -u resolution-alpha.service --since "2026-09-22" | grep sim-bankroll
```

Fields in `sim_bankroll.json`:

- `divergence_count`: **the headline number.** It counts confirmed divergences in this process run,
  and it resets on restart. For a lifetime count, count the lines in the `.jsonl`.
- `last_check.status` is one of:
  - `ok`: the ledger matches the real balance to within tolerance.
  - `suspect`: a gap was seen once and is waiting for confirmation on the next poll. This is normal
    occasionally, since a settlement can land between the balance fetch and the settlements fetch.
  - `inconclusive`: the check was skipped. Either a fill happened after the balance snapshot, or the
    exact cost for a fill hasn't been fetched yet. This is normal right after fills.
  - `diverged`: confirmed, counted, and resynced.
- `last_check.gap`: ledger minus expected. Positive means the ledger thinks it has more cash than the
  account really does.
- `checks` / `inconclusive_checks`: the inconclusive share should be near zero. The exact-cost fetch
  runs earlier in the same sync, so a check is only inconclusive when a fill races the balance snapshot.
  If the share is high, the exact-cost fetches (`GET /portfolio/orders/{id}`) are failing. Grep the
  journal for `could not fetch exact cost`.
- `open_positions`, `pending_exact_orders`: what the ledger currently thinks it holds.
- `last_divergence`: a snapshot of the most recent confirmed divergence.

The startup log line should look like this:
`[sim-bankroll] initialized (shadow only): sim $X of real $X, adopted N open position(s)`.
If it's missing, the ledger never started. Check that the service isn't in dry-run and that
`SIM_BANKROLL_ENABLED` isn't false.

## Pass / fail (Phase 1, historical)

**Pass (move on to Phase 2 in the plan):**

- At least 3 days and at least 100 settled trades since the start.
- `divergence_count == 0` and the `.jsonl` is empty, apart from events with a documented outside
  cause (see below).
- At least one **exit** (a "EXIT TRIGGER" / "exit sell" log line) in the window, with no divergence
  around it. This is the one rule that hasn't been verified yet (see "Known open question").

**Fail:** any confirmed divergence with no outside explanation. Work out the cause (next section),
fix the ledger rule, add a test, redeploy, and **restart the clock**.

## When a divergence happens

1. Look at the event in the `.jsonl`: `gap`, `sim_cash`, `real_balance`, `open_positions`, and `ts`.
2. Match `ts` against the journal, and against Kalshi fills and settlements around that time. The
   read-only script `expirments/resolution_alpha/reconstruct_trade_history.py` pulls both.
3. Common explanations:
   - **Outside cause:** a deposit, a withdrawal, or a manual trade on kalshi.com. The gap equals that
     amount. This isn't a ledger bug; write it down in the results log below.
   - **Gap of about +/- a whole number of dollars, right after an exit:** Kalshi credits matched
     YES+NO pairs at a different time than the ledger assumes. The mirror-image gap at settlement
     confirms it. See the next section.
   - **A small gap, cents or less, that builds up over time:** an exact-cost fetch is being skipped,
     or the fee rounding is being handled wrong.
   - **A gap equal to a settlement payout:** a settlement was missed or applied twice. Check
     `min_ts` and the tickers.
4. **Restarts:** the ledger adopts positions that are already open (from `GET /portfolio/positions`),
   so a restart mid-window shouldn't cause a divergence. If one appears right after a restart, that's
   a bug worth noting.

## Known open question: pair redemption timing

The exit path buys the *opposite* side with `reduce_only`. The ledger assumes Kalshi pays $1 per
matched YES+NO pair **immediately** at the time of that fill. If Kalshi actually pays at
**settlement**, the first exit will produce two confirmed divergences:

- about +pairs × $1 right after the exit;
- the mirror image at settlement.

If that happens, move the pair credit in `SimulatedBankroll._add_contracts` into `apply_settlement`.

## Background: the rounding in order responses

The order POST response only has 4-decimal per-contract averages (`average_fill_price`,
`average_fee_paid`). Multiplying those back out can be off by about $0.0001 × contracts.

For example, one real 4-contract order returned a fee average of `0.0016`. That multiplies out to
$0.0064; Kalshi's fee formula (in `fees.py`) gives $0.0066.

So the ledger works in two steps:

1. It records the rounded cost right away.
2. At the next sync (~15s later), it replaces that with the exact `taker/maker_fill_cost_dollars +
   fees` from `GET /portfolio/orders/{id}`.

Checks stay `inconclusive` until that swap happens. The fetch is kept out of the order path on
purpose: added latency before an order caused the 2026-09-04 zero-fill incident (see `README.md`).

## Turning it off / rolling back

- **Stop sizing off the ledger (back to the real balance), keeping it as a shadow:** remove
  `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL=true` from `live/.env`, then restart.
- **Disable the ledger entirely:** add `RESOLUTION_ALPHA_SIM_BANKROLL_ENABLED=false` to `live/.env`,
  then restart the service.
- **Full code rollback:** back up the files, then
  `git checkout 5670109 -- expirments/resolution_alpha` on the Pi. File backups from the deploy are at
  `expirments/resolution_alpha/*.bak.20260922_224920_simbankroll`.
- With `SIZE_FROM_SIM_BANKROLL` on, the ledger **does** drive sizing, capped at the real balance.
  Every other path only observes.

## Pi etiquette (read before touching the box)

- It's **live real-money trading**. Back up (`cp f f.bak.$(date +%Y%m%d_%H%M%S)`) before editing
  anything.
- Never restart or stop `resolution-alpha.service` without the owner's OK.
- Never hit the `/kalshi` or `/mc` toggle endpoints on the wake server; they flip live services.
- The Pi has **no git identity**. Commit on a dev machine, push, then on the Pi run `git fetch` and
  `git merge --ff-only`. Run `git status` / `git diff` on the Pi first.
- `live/.env` isn't shell-sourceable (`set -a; . .env` breaks). Parse it in Python the way
  `reconstruct_trade_history.py` does. Don't print it; it holds credentials.
- There's still one uncommitted change inside the `prediction_market_scraper` submodule on the Pi
  (the keep-alive `requests.Session` in `fetch_historical.py`). Leave it alone unless you're
  committing it properly.

## Results log (fill this in)

| Date checked | Uptime / trades settled | divergence_count | .jsonl lines | inconclusive / checks | Exits seen | Notes |
|---|---|---|---|---|---|---|
| 2026-09-22 23:07 EDT | 11 min / 1 settled | 0 | 0 | 0 / 43 | 0 | Restarted 22:56:21 onto `c50cb90` (PID 27128). Initialized at $4.3086. First trade: 1 YES KXBTCD-26SEP2223-T86499.99 @0.94 + $0.004 fee, settled YES. Ledger $4.3646 == real $4.3646, gap $0.0000. |
| 2026-09-26 01:05 EDT | 3d 2h (same PID 27128) / 69 markets entered, all settled (70 entry fills) | 0 | 0 (file absent) | 0 / 16538 | 3 triggered, **0 filled** | Ledger $3.3313 == real $3.3313. No `suspect` lines, no `could not fetch exact cost`, no sync exceptions. Balance swings tracked exactly, including the -$2.69 loss on KXBTC15M-26SEP252000-00 (Sep 25 ~20:00). The 3 exit triggers (Sep 24 15:14 KXETH15M, Sep 25 13:59 KXETHD, Sep 25 19:59 KXBTC15M) each filled 0 contracts, so pair-redemption timing is **still unverified**. 0 inconclusive is expected: the exact-cost fetch runs in the same sync, before the check, so a check is only inconclusive when a fill races the balance snapshot. **Not a pass yet:** needs 100+ settled trades (~1.5 more days at ~22/day) and one exit that actually fills. |
| 2026-09-26 01:35 EDT | Phase 2 restart at 01:24:39 (PID 41866, `f16bb50`, sizing ON) | 0 | 0 | 0 / few | 0 | `initialized (SIZING off it): sim $3.3313 of real $3.3313, adopted 0 of 0`. No orders yet since the restart, so tagged-ID acceptance is still unconfirmed (see the caveat). |
| | | | | | | |
