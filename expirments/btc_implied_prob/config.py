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

# Take-profit + trailing-stop exit toggle, off by default. Added 2026-08-13
# per explicit user request -- this strategy was buy-only until now (every
# position rode to settlement, no exit logic at all; see strategy.py's
# _check_exit_conditions for what turning this on adds). Off by default
# because it's brand new and unvalidated, same conservative-until-proven
# posture as KELLY_FRACTION starting low above.
#
# Important cost this doesn't happen for free: exiting early means TWO trades
# instead of one (enter, then exit by buying the opposite side to flatten),
# and Kalshi's 7% quadratic fee (see fees.py) applies to every trade, not
# just entries -- so a completed round trip pays roughly double the fee drag
# of just holding to settlement. EXIT_EDGE_THRESHOLD below defaults positive
# (not 0.0) specifically to bank real profit net of that doubled cost, not
# exit at a price that only just covers it.
ENABLE_TRAILING_EXIT = _bool_env("BTC_IMPLIED_PROB_ENABLE_TRAILING_EXIT", True)

# Take-profit threshold, in the same probability-minus-price "edge" units as
# EDGE_THRESHOLD above. Once the current edge for the side actually held
# (fresh Deribit-implied probability minus current market price) closes down
# to this value or below, _check_exit_conditions attempts to sell. Not 0.0 --
# see ENABLE_TRAILING_EXIT's docstring: the exit leg pays its own fee on top
# of the entry fee already paid, so exiting at literal edge-breakeven would
# still net a loss after that second fee. At this strategy's typical entry
# prices (~0.85-0.97, given EDGE_THRESHOLD=0.03 and Deribit-implied
# confidence), fee-per-contract runs roughly 0.004-0.009 one-way (0.07 *
# price * (1-price)), so ~0.02 covers a round trip with room to spare; tune
# via env var if live fills show otherwise. Only takes effect when
# ENABLE_TRAILING_EXIT is true.
EXIT_EDGE_THRESHOLD = _float_env("BTC_IMPLIED_PROB_EXIT_EDGE_THRESHOLD", 0.02)

# Trailing-stop toggle, off by default, independent of ENABLE_TRAILING_EXIT.
# Added 2026-08-15: ENABLE_TRAILING_EXIT alone still turns on take-profit
# (dry-run's simulated check, or live's resting order -- see strategy.py's
# _check_exit_conditions/_maintain_take_profit_order); this additionally
# gates whether the trailing-stop condition below can ever fire. Off by
# default for the same conservative-until-proven posture as
# ENABLE_TRAILING_EXIT itself -- take-profit alone (a resting order live) is
# the better-understood, lower-latency piece; trailing-stop is still
# tick-driven (see TRAILING_STOP_DROP's docstring for why a resting order
# doesn't help there) and hasn't been live-validated at all yet.
ENABLE_TRAILING_STOP = _bool_env("BTC_IMPLIED_PROB_ENABLE_TRAILING_STOP", False)

# Trailing stop-loss, same units as EXIT_EDGE_THRESHOLD. Tracks the best
# (highest) market price seen for the held side since entry and exits if
# price has given back this many dollars from that peak. Because the peak
# starts at the entry price itself, this doubles as a plain stop-loss on a
# position that never improves (an immediate adverse move of this size right
# after entry exits right away) as well as a trailing lock-in on a position
# that moved favorably and then reversed before the edge fully closed.
# Independent of EXIT_EDGE_THRESHOLD -- either condition triggers an exit on
# its own. Only takes effect when both ENABLE_TRAILING_EXIT and
# ENABLE_TRAILING_STOP are true.
TRAILING_STOP_DROP = _float_env("BTC_IMPLIED_PROB_TRAILING_STOP_DROP", 0.05)

# Where strategy.py's open_positions dict is persisted between ticks (and
# reloaded across restarts). Added 2026-08-15 after a live incident: with no
# dedup check on the entry side, a running process re-bought the same
# handful of tickers on every single loop tick for over an hour (open
# positions were tracked only in memory, and only when ENABLE_TRAILING_EXIT
# was on) before anyone noticed -- ~750 contracts accumulated across 7
# strikes in one event, $4.31 in avoidable fees. open_positions is now always
# tracked (regardless of the exit toggle) and always persisted here so a
# restart doesn't forget what's already held. Relative path resolves against
# whatever directory the process is run from (see live/start_live.ps1, which
# runs from live/, alongside .runner.pid and logs/).
POSITIONS_STATE_PATH = os.environ.get("BTC_IMPLIED_PROB_POSITIONS_STATE_PATH", "positions_state.json")
