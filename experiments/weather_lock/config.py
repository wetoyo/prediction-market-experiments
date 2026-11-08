"""Runtime configuration for weather_lock.

Every knob is environment-overridable (prefix `WEATHER_LOCK_`). Defaults to
dry-run: real orders need WEATHER_LOCK_DRY_RUN=false *and* Kalshi trading
credentials (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).
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
DRY_RUN = _bool_env("WEATHER_LOCK_DRY_RUN", True)

# --- Which markets ---
#
# Empty = every open daily KXHIGH*/KXLOW* series whose settlement station is
# in stations.STATIONS (discovered from each market's rules_primary). Set a
# comma list to restrict, e.g. "KXHIGHNY,KXHIGHCHI".
SERIES_ALLOWLIST = _tuple_env("WEATHER_LOCK_SERIES", ())
SERIES_BLOCKLIST = _tuple_env("WEATHER_LOCK_EXCLUDE_SERIES", ())
# Re-discover the series list this often (new series appear rarely).
SERIES_REFRESH_SECONDS = _float_env("WEATHER_LOCK_SERIES_REFRESH_SECONDS", 6 * 3600)

# --- The lock ---
#
# Degrees F subtracted from the observed bound before calling a market
# locked. Covers NWS-vs-Weather-Company differences and C->F rounding the
# precision model misses. 1.0 = an observed 63F floor only locks brackets
# that need the high to be <= 61F.
LOCK_MARGIN_F = _float_env("WEATHER_LOCK_MARGIN_F", 1.0)

# A bound must be reached by at least this many separate observations
# (counting the 6-hour max/min group as one). 2 ignores a single bad reading
# at the cost of reacting one report later.
MIN_CONFIRMING_OBS = int(_float_env("WEATHER_LOCK_MIN_CONFIRMING_OBS", 2))

# How many recent observations to pull per station (NWS returns newest
# first; ~30 hourly + specials covers a full climate day plus yesterday).
OBS_LIMIT = int(_float_env("WEATHER_LOCK_OBS_LIMIT", 72))

# --- Entry ---
#
# Minimum edge per contract, after fees: 1 - price - fee_per_contract.
# 0.02 = only buy a locked side at 97-98c or cheaper.
MIN_EDGE = _float_env("WEATHER_LOCK_MIN_EDGE", 0.02)

# Sizing caps. Locked trades are near-certain, so the caps are about tail
# risk (a data error or a rules surprise), not Kelly.
MAX_CONTRACTS_PER_MARKET = _float_env("WEATHER_LOCK_MAX_CONTRACTS_PER_MARKET", 25)
MAX_COST_PER_MARKET_DOLLARS = _float_env("WEATHER_LOCK_MAX_COST_PER_MARKET", 20.0)
MAX_COST_PER_EVENT_DOLLARS = _float_env("WEATHER_LOCK_MAX_COST_PER_EVENT", 40.0)
# Total open cost across every unsettled position this runner holds.
MAX_TOTAL_OPEN_COST_DOLLARS = _float_env("WEATHER_LOCK_MAX_TOTAL_OPEN_COST", 150.0)
# Never spend more than this fraction of the bankroll on one order.
MAX_BANKROLL_FRACTION_PER_ORDER = _float_env("WEATHER_LOCK_MAX_BANKROLL_FRACTION", 0.25)

DRY_RUN_SIMULATED_BALANCE_DOLLARS = _float_env("WEATHER_LOCK_DRY_RUN_BALANCE", 1000.0)

# Rescan cadence (start_live.* and the systemd unit read this).
LOOP_SECONDS = _float_env("WEATHER_LOCK_LOOP_SECONDS", 60)

# NWS asks for a User-Agent naming the client. Generic by default.
NWS_USER_AGENT = _str_env("WEATHER_LOCK_NWS_USER_AGENT", os.environ.get("NWS_USER_AGENT", "weather-lock-runner/0.1"))

_HERE = os.path.dirname(os.path.abspath(__file__))
POSITIONS_STATE_PATH = os.environ.get("WEATHER_LOCK_POSITIONS_STATE_PATH", os.path.join(_HERE, "positions_state.json"))
# Ledger status and the signal log. Not live/logs/: start_live.* archives
# everything in there on each start, which would lose the ledger's status
# file (and with it a shared-mode resume). Must differ per runner.
STATE_DIR = os.environ.get("WEATHER_LOCK_STATE_DIR", os.path.join(_HERE, "live", "state"))
# One JSON line per locked market seen per tick (traded or not): the raw
# material for a later backtest of how long locks stay mispriced.
SIGNALS_LOG_PATH = os.path.join(STATE_DIR, "signals.jsonl")

# --- Per-runner bankroll (same ledger as the other experiments) ---
# See ../golf_field_alpha/config.py "Per-runner bankroll" and
# ../MULTI_RUNNER_HANDOFF.md. Shadow by default.
SIM_BANKROLL_ENABLED = _bool_env("WEATHER_LOCK_SIM_BANKROLL_ENABLED", True)
SIM_BANKROLL_ALLOCATION_DOLLARS = _float_env("WEATHER_LOCK_SIM_BANKROLL_ALLOCATION_DOLLARS", 0.0)
SIM_BANKROLL_ALLOCATION_FRACTION = _float_env("WEATHER_LOCK_SIM_BANKROLL_ALLOCATION_FRACTION", 1.0)
SIM_BANKROLL_TOLERANCE_DOLLARS = _float_env("WEATHER_LOCK_SIM_BANKROLL_TOLERANCE_DOLLARS", 0.01)
SIM_BANKROLL_SYNC_SECONDS = _float_env("WEATHER_LOCK_SIM_BANKROLL_SYNC_SECONDS", 15.0)
SIZE_FROM_SIM_BANKROLL = _bool_env("WEATHER_LOCK_SIZE_FROM_SIM_BANKROLL", False)
ORDER_TAG = _str_env("WEATHER_LOCK_ORDER_TAG", "wl")
SIM_BANKROLL_SHARED_ACCOUNT = _bool_env("WEATHER_LOCK_SIM_BANKROLL_SHARED_ACCOUNT", False)
SIM_BANKROLL_ALLOW_FRESH_ALLOCATION = _bool_env("WEATHER_LOCK_SIM_BANKROLL_ALLOW_FRESH_ALLOCATION", False)
SIM_BANKROLL_STATUS_PATH = os.path.join(STATE_DIR, "sim_bankroll.json")
SIM_BANKROLL_DIVERGENCE_LOG_PATH = os.path.join(STATE_DIR, "sim_bankroll_divergences.jsonl")
