# Handoff: simulated-bankroll shadow experiment (resolution_alpha)

**Started:** 2026-09-22, evening (EDT), when `resolution-alpha.service` was restarted onto `c50cb90`.
To get the exact start time on the Pi:

```
systemctl show resolution-alpha.service -p ActiveEnterTimestamp
```

**Owner:** wetoyo. Written by a Claude Code session, for whoever picks this up next.

---

## Status (2026-09-26): Phase 1 accepted, Phase 2 code ready with the flag off

- The owner accepted Phase 1 on 2026-09-26: 0 divergences over 3 days and 69 trades. They waived
  the 100-trade minimum and the filled-exit requirement, so **pair-redemption timing is still
  unverified**. Keep an eye on the first exit that actually fills.
- Phase 2 (size off the ledger) is committed on the branch behind
  `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL` (default false). See `SIM_BANKROLL_PLAN.md` Phase 2.
  - **To turn it on:** add `RESOLUTION_ALPHA_SIZE_FROM_SIM_BANKROLL=true` to `live/.env`, pull the
    code, and restart. The restart is the owner's call.
  - The startup line then reads `initialized (SIZING off it)`.
  - With the fraction at 1.0, sizing should match today's to within $0.01, since the ledger has
    tracked the real balance exactly.
  - Keep checking `divergence_count`. It's now a live safety net, not just a measurement.
- Phase 3 pieces are also on the branch (see the plan, Phase 3, "Built 2026-09-26").
  - Orders are tagged `ra-<uuid>`. This is live after the next restart and is the only behaviour
    change.
  - Shared-account mode is behind `RESOLUTION_ALPHA_SIM_BANKROLL_SHARED_ACCOUNT` (off).
  - `account_reconciler.py` is read-only. Running it now, with one runner, duplicates the runner's
    own check, which is a good way to validate it before a second model exists.

---

## What this experiment is testing

Eventually each model (runner) will get its own `OrderManager`, started with its own slice of the Kalshi
account (a dollar amount or a fraction). Each model will size its bets off that slice, not off the
whole real balance. That only works if each manager's own cash ledger never drifts from reality.

This experiment tests exactly that. It runs **in shadow**: the ledger is tracked and checked, but
**nothing trades off it**. The runner still sizes off the real account balance, exactly as before.
resolution_alpha is currently the only thing trading the account. So every change in the real
balance should be explained by this runner's own fills and settlements, and the ledger should match
to within $0.01.

**The question:** does `divergence_count` stay at 0?

## Where everything is

| What | Where |
|---|---|
| Branch (live on the Pi) | `resolution-alpha-no-kalshi-state`. **Not `main`**. |
| Commits | `5670109` is the Pi's existing hotfix batch, now committed. `c50cb90` is this experiment. |
| Ledger logic (pure, no network) | `expirments/resolution_alpha/sim_bankroll.py` |
| Wiring | `order_manager.py`: `record_fill` in `buy_favored_side`, `get_balance_dollars`, `sync_sim_bankroll` |
| Runner hook | `runner.py`, the bankroll-refresh block (`else:` branch). It starts the sync in the background after each good balance poll. |
| Config | `config.py` `SIM_BANKROLL_*`. Env vars are `RESOLUTION_ALPHA_SIM_BANKROLL_{ENABLED,ALLOCATION_DOLLARS,ALLOCATION_FRACTION,TOLERANCE_DOLLARS}`. |
| Tests | `tests/test_sim_bankroll.py` (24 tests; the full suite has 67) |
| Full rollout plan, and how to sync with main | `live/SIM_BANKROLL_PLAN.md` (on the branch) |
| Pi | `wetoyo@100.109.148.56`, repo at `~/prediction-market-experiments` |

Settings the Pi is running with: allocation fraction **1.0** (whole account), tolerance **$0.01**,
enabled.

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

## Pass / fail

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

- **Disable just the ledger:** add `RESOLUTION_ALPHA_SIM_BANKROLL_ENABLED=false` to `live/.env`, then
  restart the service.
- **Full code rollback:** back up the files, then
  `git checkout 5670109 -- expirments/resolution_alpha` on the Pi. File backups from the deploy are at
  `expirments/resolution_alpha/*.bak.20260922_224920_simbankroll`.
- Trading behaviour is the same either way. The ledger only observes.

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
| | | | | | | |
