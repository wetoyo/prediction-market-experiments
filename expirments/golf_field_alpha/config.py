"""Runtime configuration for the golf_field_alpha strategy.

Everything here is environment-overridable (prefix `GOLF_FIELD_ALPHA_`).
Defaults to dry-run: real order placement requires both
GOLF_FIELD_ALPHA_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).

See ./README.md for the strategy and what each knob is trading off.
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
DRY_RUN = _bool_env("GOLF_FIELD_ALPHA_DRY_RUN", True)

# --- Which Kalshi series count as "outright tournament winner" fields ---
#
# Each of these is a series whose events are a single golf tournament, with
# one binary YES/NO sub-market per entrant and EXACTLY ONE resolving YES
# (confirmed against settled history 2026-08-27: KXPGATOUR 9/11 events had
# exactly 1 yes, the other 2 were voided/uncreated; KXCHAMPTOUR 7/7,
# KXKFTOUR 7/7, KXDPWORLDTOUR 4/4, KXLPGATOUR 6/7, KXLIVTOUR 3/3). This is
# the golf analogue of the crypto experiments' `frequency` filter -- a
# hardcoded-but-overridable whitelist, because there's no single API field
# that cleanly separates "outright winner" from the ~40 other golf series
# (top-N finisher, make-cut, head-to-head, round leader, ...), which have a
# different YES-count structure the basket math here doesn't model.
WINNER_SERIES = _tuple_env(
    "GOLF_FIELD_ALPHA_WINNER_SERIES",
    ("KXPGATOUR", "KXCHAMPTOUR", "KXKFTOUR", "KXLPGATOUR", "KXDPWORLDTOUR", "KXLIVTOUR", "KXGOLFTOURN"),
)

# --- Selection method ---
#
# "devig_edge": de-vig the field's own prices to a fair probability per
#   player (see devig.py), then buy YES on every player whose fair prob
#   exceeds its ask by at least EDGE_THRESHOLD per contract net of fees.
#   Sized per-leg by fractional Kelly. A basket of whichever names the
#   market has left underpriced relative to its own de-vigged consensus.
#
# "favorites_basket": sort players by price descending, buy YES from the
#   favorites down, accumulating players until the running basket cost
#   (price + fee, per unit) reaches 1 - EDGE_THRESHOLD, then stop -- the
#   skipped tail is the cheapest longshots. Every included leg gets the
#   same contract count, so exactly one of them paying $1 returns
#   `unit_contracts` dollars for a basket that cost <= 1 - EDGE_THRESHOLD
#   per unit. Loses only if a skipped longshot wins.
#
# Both are computed and printed by strategy.py regardless; this picks which
# one --execute actually trades.
SELECTION_METHOD = _str_env("GOLF_FIELD_ALPHA_SELECTION_METHOD", "devig_edge")

# De-vig method, see devig.py. "proportional" (fair_i = implied_i / sum) or
# "power" (fair_i = implied_i ** k, k solved so sum == 1 -- pulls down
# longshots harder, closer to observed favorite-longshot bias).
DEVIG_METHOD = _str_env("GOLF_FIELD_ALPHA_DEVIG_METHOD", "proportional")

# The "fixed edge%". For devig_edge: minimum (fair - price - fee) per
# contract, in dollars, for a leg to be included. For favorites_basket:
# the margin below $1 the per-unit basket cost must stay under. 0.03 = buy
# a leg only if it looks 3c underpriced / build a basket costing <= 97c per
# unit. Named a "%" loosely -- it's in absolute contract-dollar terms
# (payoff is $1), so 0.03 IS 3% of max payoff.
EDGE_THRESHOLD = _float_env("GOLF_FIELD_ALPHA_EDGE_THRESHOLD", 0.03)

# Only consider players whose YES buy price is in [MIN_PRICE, MAX_PRICE].
# MIN_PRICE excludes 0/penny noise (a 1c ask with no real depth isn't a
# tradeable edge); MAX_PRICE excludes the heavy favorite (paying 96c for a
# 1c edge is mostly fee/spread risk, and de-vig error is largest on the
# short-priced end). The de-vig normalization itself still runs over the
# WHOLE field, not just this price band -- this only gates what gets bought.
MIN_PRICE = _float_env("GOLF_FIELD_ALPHA_MIN_PRICE", 0.02)
MAX_PRICE = _float_env("GOLF_FIELD_ALPHA_MAX_PRICE", 0.90)

# Skip an event whose field has fewer than this many players with a usable
# quote -- de-vig on a handful of names is meaningless, and a nearly-empty
# book is a sign the tournament isn't really trading yet.
MIN_FIELD_SIZE = _float_env("GOLF_FIELD_ALPHA_MIN_FIELD_SIZE", 12)

# Skip an event whose raw implied probabilities sum to more than this. Some
# overround (sum > 1) is expected and is where the edge supposedly lives,
# but a sum of 1.5+ means the book is stale/wide on both sides and the
# de-vig is dividing by garbage. Observed ~1.27 on one fully-settled event.
MAX_OVERROUND = _float_env("GOLF_FIELD_ALPHA_MAX_OVERROUND", 1.40)

# Don't trade inside the final MIN_SECONDS_TO_CLOSE before an event closes:
# live play is in progress, prices are moving fast on every shot, and the
# de-vig snapshot goes stale between the scan and the fill. Default 6h.
MIN_SECONDS_TO_CLOSE = _float_env("GOLF_FIELD_ALPHA_MIN_SECONDS_TO_CLOSE", 6 * 3600)

# Don't trade an event more than this far out -- books are thin and the
# field roster still churns (withdrawals, Monday qualifiers). Default 10d.
MAX_SECONDS_TO_CLOSE = _float_env("GOLF_FIELD_ALPHA_MAX_SECONDS_TO_CLOSE", 10 * 86400)

# --- Sizing ---
#
# devig_edge: fraction of full Kelly per leg. Kelly-optimal stake on a
# binary priced `p` with fair prob `q` is (q - p) / (1 - p) of bankroll.
# The legs of one basket are mutually exclusive (only one player wins), so
# they're strongly negatively correlated and per-leg Kelly OVER-states the
# combined risk -- 0.10 is deliberately conservative on top of that,
# pending any live validation. See ../resolution_alpha/live/config.py's
# KELLY_FRACTION docstring for why full Kelly misbehaves near p=1 (not the
# regime here, but the same denominator).
KELLY_FRACTION = _float_env("GOLF_FIELD_ALPHA_KELLY_FRACTION", 0.10)

# Hard ceiling on contracts bought per player, both methods. Backstops a
# bad de-vig (fair prob way off) from sizing one leg huge, and caps
# favorites_basket's unit count.
MAX_CONTRACTS_PER_PLAYER = _float_env("GOLF_FIELD_ALPHA_MAX_CONTRACTS_PER_PLAYER", 20)

# Hard ceiling on total dollars deployed into one event's basket (sum of
# leg cost + fees). If per-leg sizing wants more than this, all legs scale
# down proportionally and re-floor. This is the real risk cap -- worst case
# per event is roughly this amount (you get $0 back only if no bought
# player wins).
MAX_EVENT_COST_DOLLARS = _float_env("GOLF_FIELD_ALPHA_MAX_EVENT_COST_DOLLARS", 40.0)

# Bankroll used for Kelly sizing when running dry-run without real Kalshi
# credentials. No effect once DRY_RUN=false -- real balance is fetched.
DRY_RUN_SIMULATED_BALANCE_DOLLARS = _float_env("GOLF_FIELD_ALPHA_DRY_RUN_BALANCE", 1000.0)

# Where strategy.py persists its open_positions dict between ticks / across
# restarts. Same rationale as ../btc_implied_prob/config.py's
# POSITIONS_STATE_PATH -- without it a looping --execute process re-buys the
# same legs every tick. Relative path resolves against the process's run
# dir (live/start_live.* runs from live/).
POSITIONS_STATE_PATH = os.environ.get("GOLF_FIELD_ALPHA_POSITIONS_STATE_PATH", "positions_state.json")
