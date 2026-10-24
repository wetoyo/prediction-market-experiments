"""Runtime configuration for the perp_martingale experiment.

Everything here is environment-overridable (`PERP_MARTINGALE_*`). There is no
live runner yet -- these are the backtest's defaults, and the knobs a live
runner would read.
"""

import os


def _float_env(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val is not None else default


def _int_env(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val is not None else default


def _str_env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None else default


# Kalshi's BTC perpetual: 0.0001 BTC per contract, whole contracts only,
# quoted in dollars per contract (~$8 at $80k BTC).
PERP_TICKER = _str_env("PERP_MARTINGALE_PERP_TICKER", "KXBTCPERP")

# One martingale "bet": enter at market, exit at whichever of +/-BRACKET
# (fraction of the entry price) is hit first.
BRACKET = _float_env("PERP_MARTINGALE_BRACKET", 0.005)

# Losses a cycle survives: stakes 1, ~2, ~4, ... for DEPTH + 1 bets. A cycle
# only loses (all DEPTH + 1 bets lost, the "bust") with probability
# (1 - p) ** (DEPTH + 1); 7 -> 1/256 at p = 0.5.
DEPTH = _int_env("PERP_MARTINGALE_DEPTH", 7)

# Direction signal (indicators.py): macd_rsi, macd, rsi, always_long, random,
# or any of them + "_fade" to trade against it.
SIGNAL = _str_env("PERP_MARTINGALE_SIGNAL", "macd_rsi")
SIGNAL_BAR_MINUTES = _int_env("PERP_MARTINGALE_SIGNAL_BAR_MINUTES", 15)
MACD_FAST = _int_env("PERP_MARTINGALE_MACD_FAST", 12)
MACD_SLOW = _int_env("PERP_MARTINGALE_MACD_SLOW", 26)
MACD_SIGNAL = _int_env("PERP_MARTINGALE_MACD_SIGNAL", 9)
RSI_PERIOD = _int_env("PERP_MARTINGALE_RSI_PERIOD", 14)

# Sizing (martingale.py). "kelly": each cycle risks the Kelly fraction of the
# bankroll for the cycle-as-one-bet (win the base profit with probability
# 1 - q^(DEPTH+1), lose the whole ladder otherwise), times KELLY_FRACTION and
# capped at MAX_CYCLE_RISK -- so a bust never takes the bankroll to zero, and a
# cycle with no estimated edge isn't funded. "full": the classic martingale,
# the ladder risks the whole bankroll. "flat": no doubling, every bet is Kelly
# for the single bracket bet (isolates whether the signal has an edge).
SIZING = _str_env("PERP_MARTINGALE_SIZING", "kelly")
KELLY_FRACTION = _float_env("PERP_MARTINGALE_KELLY_FRACTION", 0.5)
MAX_CYCLE_RISK = _float_env("PERP_MARTINGALE_MAX_CYCLE_RISK", 0.5)

# Win probability per bet is estimated walk-forward from the bets resolved so
# far (paper bets included -- unfunded cycles still place size-0 bets so the
# estimate keeps learning), shrunk toward 0.5 with this many pseudo-bets.
P_PRIOR_STRENGTH = _float_env("PERP_MARTINGALE_P_PRIOR_STRENGTH", 200)
# Only the most recent bets count (0 = all of them).
P_WINDOW = _int_env("PERP_MARTINGALE_P_WINDOW", 2000)

# Costs. Taker fee as a fraction of notional, charged on entry and exit
# (Kalshi's filed crypto-perp schedules run 1-12bp by volume tier; the
# account's own rate is at /margin/fee_tiers, auth only). Spread comes from
# the real bid/ask candles. COST_ESTIMATE is the round-trip cost the sizing
# plans with (fees + spread + stop slippage), as a fraction of notional.
FEE_RATE = _float_env("PERP_MARTINGALE_FEE_RATE", 0.0005)
COST_ESTIMATE = _float_env("PERP_MARTINGALE_COST_ESTIMATE", 2 * 0.0005 + 0.0002)

# The perp's leverage estimate (initial margin = notional / this). ~6.4 on
# 2026-10-23; the live value is /margin/markets/KXBTCPERP leverage_estimate.
LEVERAGE = _float_env("PERP_MARTINGALE_LEVERAGE", 6.0)

# Profit skim: this share of each won cycle's profit leaves the working
# bankroll (into a reserve, like ../../shared/profit_skim.py), so sizing never
# compounds on it.
SKIM_FRACTION = _float_env("PERP_MARTINGALE_SKIM_FRACTION", 1.0)

STARTING_BANKROLL_DOLLARS = _float_env("PERP_MARTINGALE_STARTING_BANKROLL_DOLLARS", 1000.0)
