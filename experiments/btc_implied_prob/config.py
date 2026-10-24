"""Runtime configuration for the btc_implied_prob strategy.

Everything here is environment-overridable. Defaults to dry-run: real order
placement requires both BTC_IMPLIED_PROB_DRY_RUN=false *and* valid Kalshi
trading credentials (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).
"""

import os


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val is not None else default


def _str_env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _tuple_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    val = os.environ.get(name)
    if val is None:
        return default
    return tuple(s.strip().upper() for s in val.split(",") if s.strip())


# Master safety switch. Must be explicitly set to false to place real orders.
DRY_RUN = _bool_env("BTC_IMPLIED_PROB_DRY_RUN", True)

# Which Kalshi BTC series (by `frequency`) to scan. Confirmed live 2026-08-12:
# KXBTCD reports frequency="hourly" (60s settlement-average window), KXBTC15M
# reports "fifteen_min". Range-type markets (strike_type == "between") are out
# of scope -- kalshi_btc_markets._extract_strike returns None for them.
INTERVAL_FREQUENCIES = ("fifteen_min", "thirty_min", "hourly")

# Skip markets closing sooner than this. Once inside the settlement-average
# window (see each market's settlement_timer_seconds, typically 60s), part of
# the settlement value is already realized and a plain "time left" Black-76
# probability overstates remaining variance -- this strategy doesn't model
# that blend (see README "Limitations"), so it just stays out instead.
MIN_SECONDS_TO_CLOSE = _float_env("BTC_IMPLIED_PROB_MIN_SECONDS_TO_CLOSE", 90)

# Skip markets closing further out than this. Deribit's shortest-dated listed
# expiry is usually same-day/next-day; anything closer to it than the surface
# actually covers gets extrapolated from the nearest single expiry's smile
# (see deribit_iv.py), which degrades gracefully but shouldn't be trusted at
# arbitrary distance.
MAX_SECONDS_TO_CLOSE = _float_env("BTC_IMPLIED_PROB_MAX_SECONDS_TO_CLOSE", 3 * 86400)

# Minimum model-vs-market probability edge (after estimated fees, in price
# terms) required to signal a trade.
EDGE_THRESHOLD = _float_env("BTC_IMPLIED_PROB_EDGE_THRESHOLD", 0.03)

# Skip markets with no resting quote on the side we'd need to trade -- an
# empty book means no real fill price to compare the model against.
MAX_SPREAD = _float_env("BTC_IMPLIED_PROB_MAX_SPREAD", 0.15)

# Fraction of full Kelly to use when sizing positions (see strategy.py's
# _kelly_contracts). Kelly-optimal fraction of bankroll to stake on a binary
# contract priced at `price` with model probability `p` of paying out $1 is
# (p - price) / (1 - price). Defaults conservative (quarter-Kelly) pending
# live validation here -- see ../resolution_alpha/live/config.py's
# KELLY_FRACTION docstring for why full Kelly overshoots badly at the
# near-1.0 prices these interval markets often clear at (that experiment
# found full Kelly implying ~99% of bankroll off a 2-cent edge, and settled
# on 0.5 after live trades; this strategy starts a notch more conservative
# since it hasn't been live-validated at all yet).
KELLY_FRACTION = _float_env("BTC_IMPLIED_PROB_KELLY_FRACTION", 0.1)

# Hard ceiling on position size per signal, in contracts. Kelly sizing is
# clipped to this regardless of the computed edge, so a bad probability
# estimate (e.g. a still-extrapolated smile, see TradeSignal.extrapolated)
# can't size up an arbitrarily large position.
MAX_CONTRACTS_PER_TRADE = _float_env("BTC_IMPLIED_PROB_MAX_CONTRACTS_PER_TRADE", 10.0)

# Bankroll used for Kelly sizing when running in dry-run without real Kalshi
# credentials configured (no account balance to query). Has no effect once
# DRY_RUN=false -- real balance is fetched from the account instead (see
# order_manager.OrderManager.get_balance_dollars).
DRY_RUN_SIMULATED_BALANCE_DOLLARS = _float_env("BTC_IMPLIED_PROB_DRY_RUN_BALANCE", 1000.0)

DERIBIT_CURRENCY = "BTC"

# --- Perp delta hedge (added 2026-10-21, replaces the take-profit/trailing-
# stop exits) ---
#
# Every open binary position rides to settlement; its BTC exposure is
# offset with Kalshi's BTC perpetual instead of being stopped out. Each tick
# hedger.py sums the positions' deltas (dollars per $1 BTC move, from the same
# Deribit sigma/forward the entry was priced off -- see deribit_iv.
# estimate_delta_yes) and trades the perp toward the opposite exposure. The
# perp trades on Kalshi's margin exchange instance, which has its own cash:
# hedger.py moves this runner's own ledger cash over and back as the hedge
# needs it. Dry-run simulates the hedge position and skips the transfers.
ENABLE_PERP_HEDGE = _bool_env("BTC_IMPLIED_PROB_ENABLE_PERP_HEDGE", True)

# KXBTCPERP is 0.0001 BTC per contract, whole contracts only (2026-10-21).
# Settles off CF Benchmarks' BRTI, the same index the binaries settle on.
PERP_HEDGE_TICKER = _str_env("BTC_IMPLIED_PROB_PERP_HEDGE_TICKER", "KXBTCPERP")

# Only rebalance when the target differs from the held perp position by at
# least this many contracts, so the hedge isn't churned (and charged fees) on
# every small move in delta. Going flat (target 0) always trades.
HEDGE_REBALANCE_CONTRACTS = _float_env("BTC_IMPLIED_PROB_HEDGE_REBALANCE_CONTRACTS", 2.0)

# Hard cap on the perp position either way, in contracts. A binary's delta
# grows without bound near the strike as close approaches (n(d2)/(F*sigma*
# sqrt(T))) -- 10 at-the-money hourly contracts already want ~90 perp
# contracts (~$750 notional) -- so this bounds the margin the hedge can use.
HEDGE_MAX_CONTRACTS = _float_env("BTC_IMPLIED_PROB_HEDGE_MAX_CONTRACTS", 100.0)

# Positions closing sooner than this drop out of the hedge (their delta
# counts as 0, so the hedge unwinds them). Inside the last stretch delta
# swings too hard to chase once a tick, and inside the 60s settlement-
# averaging window part of the payout is already fixed.
HEDGE_STOP_SECONDS_BEFORE_CLOSE = _float_env("BTC_IMPLIED_PROB_HEDGE_STOP_SECONDS_BEFORE_CLOSE", 120.0)

# Hedge orders are IOC limits this fraction through the top of book
# (0.0005 = 5bp), so a fast market can't fill them at an arbitrary price.
HEDGE_MAX_SLIPPAGE = _float_env("BTC_IMPLIED_PROB_HEDGE_MAX_SLIPPAGE", 0.0005)

# Expected hedge cost, taken off every signal's edge (hedger.
# hedge_cost_per_contract): the perp's taker fee plus half its spread, paid on
# HEDGE_TURNOVER times the position's delta notional (opening the hedge, unwinding
# it, plus about as much again rebalancing), plus funding at the current
# estimated rate for as long as it'd be hedged. Live, the taker fee is this
# account's own rate from /margin/fee_tiers; HEDGE_TAKER_FEE_RATE is the
# fallback and the dry-run value (an assumption -- Kalshi's fee tiers need auth).
HEDGE_TURNOVER = _float_env("BTC_IMPLIED_PROB_HEDGE_TURNOVER", 3.0)
HEDGE_TAKER_FEE_RATE = _float_env("BTC_IMPLIED_PROB_HEDGE_TAKER_FEE_RATE", 0.001)

# Margin funding (hedger.PerpHedger._fund), all in multiples of the initial
# margin the hedge needs (notional / the perp's leverage estimate). Below
# TOPUP_RATIO, transfer from this runner's ledger cash up to TARGET_RATIO;
# above SWEEP_RATIO, transfer the excess back down to TARGET_RATIO. Each new
# entry also sets aside TARGET_RATIO x its own hedge's margin from the cash it
# sizes against, so entries can't spend the money the hedge will need.
HEDGE_MARGIN_TOPUP_RATIO = _float_env("BTC_IMPLIED_PROB_HEDGE_MARGIN_TOPUP_RATIO", 1.5)
HEDGE_MARGIN_TARGET_RATIO = _float_env("BTC_IMPLIED_PROB_HEDGE_MARGIN_TARGET_RATIO", 2.0)
HEDGE_MARGIN_SWEEP_RATIO = _float_env("BTC_IMPLIED_PROB_HEDGE_MARGIN_SWEEP_RATIO", 3.0)
# Smaller top-ups/sweeps aren't worth a transfer.
HEDGE_MIN_TRANSFER_DOLLARS = _float_env("BTC_IMPLIED_PROB_HEDGE_MIN_TRANSFER_DOLLARS", 1.0)
# Transfers are asynchronous; stop waiting for one to show on the margin side
# after this long (the ledger's balance check flags it if it never happened).
HEDGE_TRANSFER_TIMEOUT_SECONDS = _float_env("BTC_IMPLIED_PROB_HEDGE_TRANSFER_TIMEOUT_SECONDS", 300.0)

# Where strategy.py's open_positions dict is persisted between ticks (and
# reloaded across restarts). Added 2026-08-15 after a live incident: with no
# dedup check on the entry side, a running process re-bought the same
# handful of tickers on every single loop tick for over an hour (open
# positions were tracked only in memory, and only when the since-removed
# exit toggle was on) before anyone noticed -- ~750 contracts accumulated across 7
# strikes in one event, $4.31 in avoidable fees. open_positions is now always
# tracked (regardless of the exit toggle) and always persisted here so a
# restart doesn't forget what's already held. Relative path resolves against
# whatever directory the process is run from (see live/start_live.ps1, which
# runs from live/, alongside .runner.pid and logs/).
POSITIONS_STATE_PATH = os.environ.get("BTC_IMPLIED_PROB_POSITIONS_STATE_PATH", "positions_state.json")

# Series never traded, even when discovery finds them (e.g. "KXBTC15M,KXBTCD").
# Added 2026-09-26 for sharing the Kalshi account with resolution_alpha, which
# trades KXBTC15M/KXBTCD/KXETH15M/KXETHD: Kalshi nets positions per account, so
# two runners on one ticker can redeem each other's legs early, and this
# strategy's resting take-profit orders can make resolution_alpha's IOC orders
# cancel under self-trade prevention (those take-profits were removed
# 2026-10-21) (../resolution_alpha/live/
# SIM_BANKROLL_PLAN.md, Phase 3 items 3-4). Empty by default.
EXCLUDE_SERIES = _tuple_env("BTC_IMPLIED_PROB_EXCLUDE_SERIES", ())

# --- Per-runner bankroll (added 2026-09-26) ---
#
# Same ledger as resolution_alpha (../shared/sim_bankroll.py), fed by
# ../shared/tagged_ledger.py because this strategy's orders are GTC and can
# fill after the POST returns. With SIM_BANKROLL_ENABLED (the default) and a
# live run, the ledger tracks this runner's own cash and positions and checks
# them against the real balance every SIM_BANKROLL_SYNC_SECONDS in a
# background thread; shadow only until SIZE_FROM_SIM_BANKROLL is on, which
# sizes off (and refuses orders beyond) the ledger's available cash. To trade
# beside resolution_alpha on one account: SIZE_FROM_SIM_BANKROLL=true,
# SIM_BANKROLL_SHARED_ACCOUNT=true and a fixed SIM_BANKROLL_ALLOCATION_DOLLARS,
# the same way resolution_alpha is set up. See
# ../resolution_alpha/live/SIM_BANKROLL_PLAN.md ("Running the other two
# experiments beside resolution_alpha").
SIM_BANKROLL_ENABLED = _bool_env("BTC_IMPLIED_PROB_SIM_BANKROLL_ENABLED", True)
SIM_BANKROLL_ALLOCATION_DOLLARS = _float_env("BTC_IMPLIED_PROB_SIM_BANKROLL_ALLOCATION_DOLLARS", 0.0)
SIM_BANKROLL_ALLOCATION_FRACTION = _float_env("BTC_IMPLIED_PROB_SIM_BANKROLL_ALLOCATION_FRACTION", 1.0)
SIM_BANKROLL_TOLERANCE_DOLLARS = _float_env("BTC_IMPLIED_PROB_SIM_BANKROLL_TOLERANCE_DOLLARS", 0.01)
SIM_BANKROLL_SYNC_SECONDS = _float_env("BTC_IMPLIED_PROB_SIM_BANKROLL_SYNC_SECONDS", 15.0)
SIZE_FROM_SIM_BANKROLL = _bool_env("BTC_IMPLIED_PROB_SIZE_FROM_SIM_BANKROLL", False)
# Prefixes every order's client_order_id ("bip-<y|n>-<hex>"). Must be unique
# per runner on the account (resolution_alpha is "ra"), 1-5 chars, no "-".
ORDER_TAG = _str_env("BTC_IMPLIED_PROB_ORDER_TAG", "bip")
SIM_BANKROLL_SHARED_ACCOUNT = _bool_env("BTC_IMPLIED_PROB_SIM_BANKROLL_SHARED_ACCOUNT", False)
# Shared mode only: set for ONE restart to start a fresh allocation after this
# runner's status file was lost (it otherwise refuses to trade). Unset after.
SIM_BANKROLL_ALLOW_FRESH_ALLOCATION = _bool_env("BTC_IMPLIED_PROB_SIM_BANKROLL_ALLOW_FRESH_ALLOCATION", False)

# The ledger's status file (sim_bankroll.json, read by
# ../resolution_alpha/account_reconciler.py --ledger) and divergence log.
# Absolute, unlike POSITIONS_STATE_PATH: every runner needs its own.
LOG_DIR = os.environ.get("BTC_IMPLIED_PROB_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "live", "logs"))
SIM_BANKROLL_STATUS_PATH = os.path.join(LOG_DIR, "sim_bankroll.json")
SIM_BANKROLL_DIVERGENCE_LOG_PATH = os.path.join(LOG_DIR, "sim_bankroll_divergences.jsonl")
