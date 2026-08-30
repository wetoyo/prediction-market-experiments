"""Runtime configuration for the resolution_alpha live execution loop.

All knobs are environment-overridable so the same code runs in dry-run
(paper) mode by default and can be flipped to live trading explicitly.
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


def _set_env(name: str, default: tuple[str, ...]) -> frozenset:
    val = os.environ.get(name)
    if val is None:
        return frozenset(default)
    return frozenset(s.strip().upper() for s in val.split(",") if s.strip())


# Master safety switch. Must be explicitly set to false to place real orders.
DRY_RUN = _bool_env("RESOLUTION_ALPHA_DRY_RUN", True)

# Which recurring interval series to trade, by Kalshi's `frequency` field.
# Confirmed live on 2026-08-04: BTC/ETH/SOL/etc 15m series report "fifteen_min",
# BTC/ETH/XRP/DOGE/etc hourly above-below series report "hourly". No 30m crypto
# series existed at that time; "thirty_min" is kept here for forward compat.
INTERVAL_FREQUENCIES = ("fifteen_min", "thirty_min", "hourly")

# Entry window: the outer, coarse bound on how early a market is even considered
# a trade candidate (also what ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS is sized
# off, below) -- cheap to check before doing any real work, so kept as a plain
# fixed cutoff. The actual "is *now* a valid moment to trade *this* market"
# decision is tighter and dynamic -- see ENTRY_WINDOW_EXPONENT immediately
# below and runner.py's _dynamic_entry_window_seconds.
ENTRY_WINDOW_SECONDS = _float_env("RESOLUTION_ALPHA_ENTRY_WINDOW_SECONDS", 90)

# Lower end of the interpolation band for the dynamic entry window below --
# deliberately its own knob, not reused from MIN_MARKET_IMPLIED_PROBABILITY
# (0.90) directly, per explicit user request ("lets go with a range of
# 92-100 for the interpolation"). A market priced between 0.90 and 0.92
# still clears the hard MIN_MARKET_IMPLIED_PROBABILITY gate (so it CAN
# trade), but sits at or below this floor too, so it's still only tradeable
# an instant before close (dynamic_window == 0) -- the interpolation band
# doesn't have to span the same range as the hard gate.
ENTRY_WINDOW_PRICE_FLOOR = _float_env("RESOLUTION_ALPHA_ENTRY_WINDOW_PRICE_FLOOR", 0.92)

# Added 2026-08-06 per explicit user request ("look for <60 seconds left,
# dynamically sizing it based on effective prob" / "make the window sized
# based on fill price, maybe 90 * fillprice^2"). The actual allowed entry
# window shrinks from the full ENTRY_WINDOW_SECONDS down toward 0 as the
# MARKET's own top-of-book price (not effective_prob) approaches ENTRY_WINDOW_PRICE_FLOOR:
#
#   normalized = (market_price - ENTRY_WINDOW_PRICE_FLOOR) / (1 - ENTRY_WINDOW_PRICE_FLOOR)
#   dynamic_window_seconds = ENTRY_WINDOW_SECONDS * normalized ** ENTRY_WINDOW_EXPONENT
#
# Deliberately driven by the market's own price, not effective_prob (changed
# 2026-08-06, was effective_prob initially -- see git history/activity_log.md
# for the original version). effective_prob = min(model_prob, market_price +
# MAX_TRUSTED_EDGE_PROB) is still partly model-driven, and the model has
# already proven it can be confidently wrong (the XRP incidents). A real
# trade this session showed effective_prob reading ~1.0 while the position
# still moved heavily against it over the ~90s window -- both the model AND
# the market agreed with extreme confidence and were still wrong, which no
# divergence gate can catch (divergence gates only fire when model and
# market *disagree*). Using the market's own price for how much *timing*
# risk to tolerate is the more grounded signal: it's a real, tradeable
# number reflecting every other participant's belief, not a blend that
# still has the model's own miscalibration baked in. This doesn't touch how
# *edge* is computed or sized (still effective_prob, for that the model's
# extra information beyond the market is exactly the point) -- only how
# early we're willing to act on it.
#
# Squared (not linear) so the window shrinks fast for anything close to
# ENTRY_WINDOW_PRICE_FLOOR -- entering early is reserved for a market that's
# already very confident, not one that's just cleared the hard gate.
ENTRY_WINDOW_EXPONENT = _float_env("RESOLUTION_ALPHA_ENTRY_WINDOW_EXPONENT", 2.0)

# Hard floor under the dynamic window above: no entry at all once fewer than
# this many seconds remain, regardless of how confident the market's own
# price is. Added 2026-08-08 after a real incident: KXBNB15M-26AUG080400-00
# entered with seconds_left=0.5 (dynamic window let it through since the
# market was pricing >99% confidence). Two compounding problems at that
# margin, not one: (1) sigma_used (probability.py) shrinks toward zero as
# remaining time -> 0 by design, so a stale/frozen spot quote --  routine
# for a thinner pair like BNB, POLL_INTERVAL_SECONDS=2 apart -- gets
# amplified into an absurd, falsely-certain z (observed: z=-3940.88) instead
# of being recognized as noise; (2) even a correct call can still lose
# money at this margin -- Kalshi's own order record showed the order
# "filled" 20/20 per fill_count_fp, but reconciling actual account balance
# across every trade that day proved only ~1.43 contracts really got a
# counterparty before the exchange stopped matching at close. There may not
# be time left for even a marketable order to fully execute. A few seconds
# of floor trades away a small amount of the latest, most-confident timing
# information for materially reducing exposure to both failure modes at
# once -- see runner.py's evaluate_and_maybe_trade for the gate itself.
MIN_ENTRY_SECONDS_LEFT = _float_env("RESOLUTION_ALPHA_MIN_ENTRY_SECONDS_LEFT", 3.0)

# Added 2026-08-30. A higher seconds-left floor that applies ONLY to additional
# tranches on a market already held (a first entry still uses the 3.0s floor
# above). Rationale from the 2026-08-29 KXBTC15M-26AUG291945-45 post-mortem:
# the position is already sized, so the only thing a late top-up adds is more
# exposure decided on the least reliable read the model produces -- inside the
# last ~20s the estimate is dominated by settlement-averaging noise
# (probability.py), and that tick is also where the model flipped sides
# entirely. A same-side add that close to close is nearly all downside. 30s
# leaves the first ~60s of the 90s entry window open for stacking and closes
# the noisy tail. Opposite-side flips are handled separately (runner.py's
# side-flip guard) and never reach this check. Set at/below MIN_ENTRY_SECONDS_LEFT
# to disable (every held-market add then uses the same floor as a first entry).
MIN_STACK_ENTRY_SECONDS_LEFT = _float_env("RESOLUTION_ALPHA_MIN_STACK_ENTRY_SECONDS_LEFT", 30.0)

# How far ahead of close_time to start tracking a market's order book over the
# websocket (ws_feed.py), vs. not subscribing at all yet. Needs to be bigger
# than ENTRY_WINDOW_SECONDS so the book snapshot has arrived and settled before
# a market actually enters its trade window. Deliberately NOT "subscribe to
# every open market": only a handful of the ~1,600+ open crypto interval
# markets are ever actually close to closing at a given moment, so tracking
# order-book state for the rest is pure waste -- unrelated to a separate real
# bug (fixed) where ws_feed's subscription-sync loop busy-spun with no
# `await` once steady-state, starving the event loop regardless of how many
# tickers were subscribed.

ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS = _float_env(
    "RESOLUTION_ALPHA_ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS", ENTRY_WINDOW_SECONDS + 60
)

# Minimum model-implied probability on the favored side to consider trading.
MIN_FAVORED_PROBABILITY = _float_env("RESOLUTION_ALPHA_MIN_PROB", 0.97)

# A standalone model-z-score overconfidence cap (EARLY_MAX_TRUSTED_Z, tried
# and iterated on 2026-08-06) was removed the same day per explicit user
# instruction ("remove the prob saturdated thing - too arbitrary. i think
# the dynamic time sizing is enough"): the dynamic entry window above is
# already driven by the MARKET's own price, which gates *when* it's safe to
# act without needing a second, separate opinion on the model's own z-score.
# A real degenerate-sigma incident that motivated part of that cap's design
# (KXBNBD-26AUG0619-T424.99, z=166,565,000,000 from a stale/duplicate spot
# quote) is now caught upstream instead -- see probability.py's
# realized_vol_per_sqrt_second, which refuses to return a literal 0.0
# variance and returns None instead, skipping the market before z is ever
# computed. runner.py still logs the model's raw z on every evaluated trade
# (see evaluate_and_maybe_trade's z_favored) for reference, just without a
# gate hanging off it.

# Minimum edge (model probability - expected fill price) per contract, net of
# fees, in dollars.
MIN_EDGE_DOLLARS = _float_env("RESOLUTION_ALPHA_MIN_EDGE", 0.02)

# --- Probability-model calibration corrections (added 2026-08-29) --------------
# Calibrated against live/logs/samples.db (2.36M evaluated ticks / 249k resolved
# / 2026-08-07..28), replaying the settlement model in probability.py against
# realized outcomes. Findings:
#   * Per-sqrt-second realized vol does NOT systematically expand into the close
#     (bucketed rms/sqrt(s) is flat-to-noisy over the last 90s). But conditional
#     vol is fat-tailed: forward-realized / trailing sigma has median ~0.76 yet
#     p90 ~2.9 -- ~10% of positions hit a 3x+ post-entry vol blow-out the
#     trailing estimator cannot anticipate.
#   * The Gaussian tail is too thin: on rows the live gate would trade
#     (model_prob >= 0.97), the 0.97-0.98 confidence band resolved ~0.90 (not
#     0.976), and model "~1.0" calls resolve against the favored side ~0.4% of
#     the time (partly data-pipeline noise / rare CF-Benchmarks dislocation, but
#     real money either way).
# SIGMA_SAFETY_FACTOR widens sigma_used in probability.py (both regimes) before
# the z-score; 1.25 flattens the 0.97-0.99 band without hurting Brier. It does
# NOT reduce the reversal *count* (z sign is unchanged) -- it stops the model
# reporting false certainty to the Kelly/edge sizer, which is where the damage
# compounded.
SIGMA_SAFETY_FACTOR = _float_env("RESOLUTION_ALPHA_SIGMA_SAFETY_FACTOR", 1.25)

# Hard ceiling on the model's own favored-side probability, applied after the
# normal CDF in probability.py. The data shows the model never actually
# achieves better than ~99.6% realized accuracy, so anything it reports above
# this is noise at best and overconfidence that misleads sizing at worst.
# With MIN_FAVORED_PROBABILITY=0.97 and MIN_EDGE_DOLLARS=0.02 this also caps the
# max entry price near ~0.97 (fills in the overconfident 0.97-0.99 band stop).
# Set to 1.0 to disable.
MODEL_PROB_CAP = _float_env("RESOLUTION_ALPHA_MODEL_PROB_CAP", 0.99)

# Added 2026-08-06 per explicit user request: this strategy has no exit logic
# by design (see runner.py's module docstring -- positions normally ride to
# resolution, since they're only opened in the final seconds before close).
# But the user asked for a defensive backstop: if, after entering, spot moves
# suddenly and adversely by more than this many standard deviations (in the
# probability model's own sigma_used units -- see probability.py's `z` field)
# relative to the z-score *at entry*, attempt a one-shot best-effort exit sell
# ("just incase the sell somehow gets filled" -- acknowledged as unlikely to
# fill in a thin, fast-moving, near-expiry book, but worth trying).
#
# This threshold (and the spot-move floor below) gate only the z-drop trigger.
# A second, independent trigger was added 2026-08-30: if the model swings all
# the way to favoring the OPPOSITE side of a held position with entry-grade
# conviction (opposite side >= MIN_FAVORED_PROBABILITY), _check_exit_conditions
# closes the position immediately, bypassing both gates here -- a full
# inversion is a stronger signal than any bare z-drop. See runner.py's
# side-flip guard (evaluate_and_maybe_trade) and _check_exit_conditions.
#
# Raised 3.0 -> 4.0 on 2026-08-29 after a post-mortem on two live exits that
# day (KXBTC15M-26AUG291400-00, KXBTC15M-26AUG291415-15). The first was a
# genuine regime break -- BTC spiked ~90 pts straight through the strike in
# ~15s, reconstructed z-drop ~10-15 sigma -- and exiting was correct (~$34
# better than holding to a YES resolution that zeroed the NO). The second was
# a false alarm: a ~15-20 pt wiggle that touched the strike and mean-reverted,
# reconstructed z-drop only ~2-4 sigma, and the market never repriced (NO ask
# was still 0.11 when the model panic-sold the YES at ~0.89). It resolved YES;
# the exit turned a ~$6 win into a ~$7 loss. Root cause: sigma_used collapses
# ~tau^1.5 in the last 60s (see probability.py's settlement-window branch), so
# a fixed sigma bar is a shrinking *dollar* bar -- late-expiry noise
# manufactures large z-drops. 4.0 sigma still clears any real move (the first
# incident triggers at 4, 5, even 8) while dropping marginal ones. The harder
# guard is EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC below, an AND condition.
# See runner.py's _z_for_side / _check_exit_conditions for the exact comparison.
EXIT_Z_SCORE_DROP_THRESHOLD = _float_env("RESOLUTION_ALPHA_EXIT_Z_SCORE_DROP_THRESHOLD", 4.0)

# Added 2026-08-29 (same post-mortem as the 3.0 -> 4.0 bump above). A second,
# independent gate the z-drop must clear before an exit actually fires: the
# underlying must have moved, in absolute terms, at least this fraction away
# from where it was when the position was opened. This is the "a 15-pt wiggle
# can't trigger regardless of how small sigma got" backstop -- the z-drop
# threshold alone can't distinguish "spot blew through the strike" from
# "spot drifted 0.02% while tau^1.5 shrank sigma_used", and only the former
# is a real emergency. 0.0005 (5 bp, ~$39 on BTC at $78k) sits above ordinary
# final-minute noise (~1-3 bp) but well under a real move: incident 1 moved
# ~9 bp (fires), incident 2 moved ~2 bp (held). Costs zero latency -- both
# spot and entry_spot are already in hand on the same tick the z-drop is
# computed, so this does NOT slow down a real exit (in incident 1 the move
# had already cleared this floor by the time z dropped). A z-drop that
# clears EXIT_Z_SCORE_DROP_THRESHOLD but not this floor is logged once and
# the position stays eligible -- if the move then develops, the exit still
# fires on a later tick. Set to 0.0 to disable and go back to z-drop only.
EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC = _float_env("RESOLUTION_ALPHA_EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC", 0.0005)

# Hard ceiling on position size per market, in contracts. This is not
# usually the size actually traded -- runner.py's _kelly_contracts sizes
# dynamically off model edge and account balance (see KELLY_FRACTION below);
# this is just the safety cap that dynamic size gets clipped to, so a bad
# probability estimate can't size up an arbitrarily large position. Whether
# it's actually binding (vs. Kelly or book depth) is unmeasured as of
# 2026-08-06 -- Kelly sizing was briefly removed that day in favor of always
# maxing out to this ceiling, then restored at full (1.0) fraction per
# follow-up user instruction, specifically so sizing tracks each trade's
# actual edge instead of a flat number regardless of edge strength. Raise
# this deliberately as account balance grows, not as a way to make Kelly
# sizing bigger.
MAX_CONTRACTS_PER_MARKET = _float_env("RESOLUTION_ALPHA_TARGET_CONTRACTS", 100)

# Max total contracts committed across all markets within a 15-minute wall-clock
# slot. A coarse global cap, not a precise per-market-interval budget -- see
# runner.py's cycle bucketing note. Still enforced as a ceiling alongside Kelly
# sizing, same as MAX_CONTRACTS_PER_MARKET above.
MAX_CYCLE_CONTRACTS = _float_env("RESOLUTION_ALPHA_MAX_CYCLE_CONTRACTS", 100)

# Fraction of full Kelly to use when sizing positions dynamically (see runner.py's
# _kelly_contracts). Kelly-optimal sizing for a binary contract priced at `p` with
# model probability `q` of winning is f* = (q - p) / (1 - p) of bankroll -- full
# Kelly is punishing if `q` is ever wrong, which is why this started deliberately
# small (0.10, then 0.25) pending live validation. Briefly raised to 1.0 (full
# Kelly) on 2026-08-06, then reverted to **0.5** the same day once real numbers
# showed why: at these markets' typical prices (0.90+, given
# MIN_MARKET_IMPLIED_PROBABILITY), the (1 - price) denominator in Kelly's formula
# is tiny, so full Kelly's implied fraction blows up toward the *entire* bankroll
# almost automatically regardless of how modest the raw edge is -- reverse-
# engineering a real trade (eff_prob=0.9997, fill_price=0.976) showed full Kelly
# wanting ~98.8% of bankroll (~96-101 contracts, ~5x the ceiling) off a 2.37-cent
# edge. At that setting the strategy was effectively "always max to the ceiling"
# again, just reached via a much more aggressive formula, not real edge-tracking.
# 0.5 keeps sizing responsive to each trade's actual edge (unlike a flat ceiling)
# while pulling the implied fraction back to something the ceiling isn't
# guaranteed to clip on every single trade.
KELLY_FRACTION = _float_env("RESOLUTION_ALPHA_KELLY_FRACTION", 0.5)

# Which probability the Kelly position sizer (_size_for_edge -> _kelly_contracts
# in runner.py) uses as the win-probability input `q`.
#
# Default (false): effective_probability = min(model_prob, top_of_book +
# MAX_TRUSTED_EDGE_PROB) -- model-driven, clamped toward the market only when
# the model diverges upward.
#
# True: the MARKET's own view -- top_of_book_price + MAX_TRUSTED_EDGE_PROB --
# with the model's raw probability dropped from the min() entirely. The buffer
# is added because a bare top-of-book probability equals the price you'd pay,
# which makes Kelly's (q - p) edge term zero and sizes to nothing; adding the
# same divergence buffer the model-cap already uses gives Kelly a positive
# edge to work with. In the common case for this strategy (model >= market +
# buffer) the two are already equal and this toggle is a no-op; they differ
# only when the model is LESS confident than the market, where this sizes off
# the market's number instead of the lower model one.
#
# Only the Kelly *cap* changes. The MIN_EDGE_DOLLARS trade gate still uses
# effective_probability, so this does not change which trades are taken, only
# how large Kelly lets them get. No effect while MAX_SIZE_MODE is on (that
# path skips _size_for_edge). Off by default.
KELLY_USE_MARKET_PROB = _bool_env("RESOLUTION_ALPHA_KELLY_USE_MARKET_PROB", False)

# When enabled, bypasses Kelly/edge-based sizing (_size_for_edge in runner.py)
# entirely: each trade instead just takes the largest whole-contract size
# supported by whichever binds first -- the book's own available depth for
# the favored side, or what the account's available cash can actually afford
# at each level's price. NOT clipped to MAX_CONTRACTS_PER_MARKET or
# MAX_CYCLE_CONTRACTS (fixed 2026-08-25 in two steps, both live-verified: the
# per-market ceiling exists to backstop Kelly/edge sizing against a bad
# probability estimate, and the cycle-wide one exists to backstop this mode's
# lack of any edge check against overcommitting a single 15-min window --
# neither is relevant to what this mode is actually for, and clipping to
# either one just made fills land at exactly that ceiling whenever the book
# had more, which looked like -- but wasn't -- a real liquidity reading. See
# the max_contracts branch in runner.py's trade-sizing call site for the
# current logic). Still subject to every other entry gate above -- this only
# changes how big the size is, not whether a trade is allowed at all. No
# MIN_EDGE_DOLLARS or KELLY_FRACTION check is applied to the size itself in
# this mode, and now nothing backstops position size except real book depth
# and actual bankroll_dollars -- opt in deliberately (RESOLUTION_ALPHA_MAX_SIZE_MODE=true
# in .env), off by default.
MAX_SIZE_MODE = _bool_env("RESOLUTION_ALPHA_MAX_SIZE_MODE", False)

# --- Model-vs-market sanity gates, added 2026-08-06 after a live incident ---
#
# KXXRP15M-26AUG060245-45: spot was essentially equal to strike with 53s left
# (about as close to a coin flip as this strategy ever sees), the market's own
# top-of-book price implied ~72% confidence in the favored side, but
# probability.py's model said 98.56% -- a 26-point gap versus every other real
# trade that session clustering under 10 points. Root cause: in the last 60s,
# sigma_used is driven by *recent realized* volatility (see probability.py); a
# quiet recent stretch produces a tiny sigma estimate, and a tiny sigma turns
# even a noise-level spot/strike gap into an extreme z-score. That trade most
# likely lost. Two independent gates, both applied to the market's own
# top-of-book price for the favored side (not the model's claim):
#
# 1. Hard floor: refuse to trade at all if the market itself isn't already at
#    least this confident in the favored side. A market pricing the favored
#    side at 72% is telling you it disagrees with a 98.56%-confident model --
#    that disagreement is far more likely to mean the model is wrong than
#    that there's a genuine 26-point mispricing sitting there uncaptured.
#    Raised from 0.80 -> 0.90 on 2026-08-06: with MIN_FAVORED_PROBABILITY=0.97
#    and the old 0.80 floor + 0.12-point cap below, a market sitting at
#    exactly 80% could still get traded at an effective_probability of 92%
#    (80% + 0.12) -- i.e. a market only 80% confident, treated as if it were
#    92% confident, which is the same shape of divergence as the XRP incident
#    these gates exist to prevent. The floor needs to be close enough to the
#    cap's own ceiling that the two gates are actually reinforcing, not
#    independently each allowing half of a bad trade through.
MIN_MARKET_IMPLIED_PROBABILITY = _float_env("RESOLUTION_ALPHA_MIN_MARKET_PROB", 0.90)

# 2. Soft cap: even above that floor, don't fully trust a model probability
#    that diverges from the market's own price by more than this many
#    probability points -- cap the probability used for edge/Kelly sizing at
#    (market_price + this) instead of the raw model claim, so an implausible
#    edge still shrinks the position rather than maxing it out. Does not
#    reject the trade outright (a real large edge can happen); just prevents
#    sizing up on an implausible one. Tightened from 0.12 -> 0.05 alongside
#    the floor increase above (2026-08-06), for the same reason: at the new
#    0.90 floor, a 0.05 cap means effective_probability tops out at 95%,
#    still meaningfully below MIN_FAVORED_PROBABILITY=0.97's raw model claim
#    for the most divergent case, rather than letting the cap alone do all
#    the work at a wide 12-point allowance. Raised 0.05 -> 0.08 later the same
#    day per explicit user instruction, after live observation showed 0.05 was
#    routinely binding on legitimate high-confidence trades (BNB/HYPE/DOGE all
#    hit the cap, not just implausible ones) -- 0.08 keeps effective_probability
#    at or below 98% against the 0.90 floor, still well short of raw model
#    claims, while giving real edges more room.
MAX_TRUSTED_EDGE_PROB = _float_env("RESOLUTION_ALPHA_MAX_TRUSTED_EDGE_PROB", 0.08)

# Bankroll used for Kelly sizing math when running in dry-run without real
# credentials configured (no account balance to query against). Has no effect
# once DRY_RUN=false -- real balance is fetched from the account instead.
DRY_RUN_SIMULATED_BALANCE_DOLLARS = _float_env("RESOLUTION_ALPHA_DRY_RUN_BALANCE", 1000.0)

# How often to re-evaluate active markets and poll spot prices, in seconds.
POLL_INTERVAL_SECONDS = _float_env("RESOLUTION_ALPHA_POLL_INTERVAL_SECONDS", 2.0)

# How often to run market discovery, in seconds.
DISCOVERY_INTERVAL_SECONDS = _float_env("RESOLUTION_ALPHA_DISCOVERY_INTERVAL_SECONDS", 60.0)

# How often to emit the aggregate "N markets evaluated, N skipped by gate X"
# summary line, in seconds. Added 2026-08-06 after the safety-gate rewrite
# (MIN_MARKET_IMPLIED_PROBABILITY etc.) moved most skip reasons onto
# logger.debug calls that logging.basicConfig(level=logging.INFO) silently
# drops -- individual per-market debug lines would be far too noisy
# (~1667 markets/tick), but zero visibility into *why* the entry-window
# candidates aren't trading is worse. See runner.py's _log_stats_summary.
STATS_LOG_INTERVAL_SECONDS = _float_env("RESOLUTION_ALPHA_STATS_LOG_INTERVAL_SECONDS", 60.0)

# Whether to INFO-log the full per-candidate detail line ("<ticker> <side>
# left=..s spot=.. strike=.. model_prob=.. eff_prob=.. z=.. fill_price=..
# edge/contract=..") for candidates that DON'T end up trading. Added
# 2026-08-27: that line was firing at INFO for every candidate that reached
# the sizing stage and then failed the final MIN_EDGE_DOLLARS check, so a
# quiet-but-active window filled the log with dozens of near-miss
# "edge/contract=-0.00xx" lines per minute -- exactly the noise the 60s
# eval summary's low_edge counter already accounts for. Default OFF: the
# detail line still logs at INFO whenever it leads to an actual order (and
# at DEBUG otherwise, so -v / a lower root level can still recover it).
# Turn on to see every near miss inline again.
LOG_NONTRADING_CANDIDATES = _bool_env("RESOLUTION_ALPHA_LOG_NONTRADING_CANDIDATES", False)

# --- Lightweight mode (added 2026-08-16 per explicit user request, revised
# same day into two phases after the first cut sleeping right up until
# LIGHTWEIGHT_TRADING_WINDOW_SECONDS turned out to leave no time to warm up
# volatility history / order-book subscriptions before trading starts) ---
#
# When enabled, run_forever (runner.py) does none of its normal continuous
# work -- discovery, spot/index polling, per-market evaluation, exit-condition
# checks -- for most of each 15-minute wall-clock interval; it sleeps instead.
# The remaining LIGHTWEIGHT_WAKE_WINDOW_SECONDS before the next 15-minute UTC
# boundary (:00/:15/:30/:45) splits into two phases:
#   1. Warm-up (from LIGHTWEIGHT_WAKE_WINDOW_SECONDS down to
#      LIGHTWEIGHT_TRADING_WINDOW_SECONDS before the boundary): discovery,
#      spot/index polling, and ws_feed order-book subscriptions all run at the
#      normal POLL_INTERVAL_SECONDS cadence -- exactly what a market needs
#      warmed up before it can be evaluated (realized_vol_per_sqrt_second needs
#      real history, not a cold start) -- but evaluate_and_maybe_trade and
#      _check_exit_conditions are skipped entirely: no trade evaluation yet.
#   2. Active (the last LIGHTWEIGHT_TRADING_WINDOW_SECONDS before the
#      boundary): runs exactly as with LIGHTWEIGHT_MODE off -- full
#      evaluate_and_maybe_trade gating, same cadence.
# Outside phase 1 entirely, the loop sleeps in one shot straight to the start
# of phase 1 rather than waking every POLL_INTERVAL_SECONDS to no-op.
#
# Also suppresses ALL logging output for as long as this mode is on (see
# runner.py's logging.disable call right after logging.basicConfig) -- the
# point of this mode is minimum footprint (CPU/network/disk) between and
# during active windows, not just reduced-verbosity logging while idle. Two
# exceptions, both routed through runner.py's _log_despite_lightweight_mode:
# the one-time startup banner (so a look at the log can confirm the process
# is actually alive) and every trade-placement confirmation, entry or exit
# (so lightweight mode can never silently place a trade with no record of
# it) -- everything else (per-tick evaluation noise, skip reasons, etc.)
# stays suppressed.
#
# Off by default -- even with the warm-up phase, this still trades away most
# of the loop's visibility and gives volatility history/subscriptions less
# lead time than a fully continuous run would, so it should be an explicit
# opt-in, not the default posture.
LIGHTWEIGHT_MODE = _bool_env("RESOLUTION_ALPHA_LIGHTWEIGHT_MODE", False)

# 180 seconds = 3 minutes, per explicit user request ("up the 90 seconds to 3
# minutes instead, but have the first half just poll for the volatility
# history"). Total span before the boundary during which the loop does
# anything at all (phases 1 + 2 combined) -- must stay >
# LIGHTWEIGHT_TRADING_WINDOW_SECONDS below for phase 1 to have any width.
LIGHTWEIGHT_WAKE_WINDOW_SECONDS = _float_env("RESOLUTION_ALPHA_LIGHTWEIGHT_WAKE_WINDOW_SECONDS", 180.0)

# 90 seconds = 1.5 minutes -- "the first half" above is the other 90s of the
# 180s wake window (LIGHTWEIGHT_WAKE_WINDOW_SECONDS - this). Deliberately its
# own knob, not reused from ENTRY_WINDOW_SECONDS (also 90s by coincidence) --
# the two control different things (how early evaluate_and_maybe_trade even
# considers a market as a trade candidate, vs. how early the loop's phase-2
# full evaluation starts running at all) and shouldn't be forced to move
# together just because they happen to start at the same value.
LIGHTWEIGHT_TRADING_WINDOW_SECONDS = _float_env("RESOLUTION_ALPHA_LIGHTWEIGHT_TRADING_WINDOW_SECONDS", 90.0)

# Rolling window (seconds) of spot samples kept per underlying.
SPOT_HISTORY_SECONDS = 120.0

# Kalshi's settlement TWAP window: these markets resolve on the average of the
# last SETTLEMENT_AVERAGE_SECONDS of CF Benchmarks' Real Time Index before
# close_time (confirmed from a live market's `rules_primary` text on
# 2026-08-04 -- see README.md and live/README.md).
SETTLEMENT_AVERAGE_SECONDS = 60.0

# Logs and the sampling DB live under live/ (the launcher scripts run from
# there and redirect stdout into live/logs/); config.py sits one level up
# now, so join through "live" explicitly rather than dirname(__file__)/logs.
LOG_DIR = os.environ.get(
    "RESOLUTION_ALPHA_LOG_DIR", os.path.join(os.path.dirname(__file__), "live", "logs")
)

# Data-collection sampling (added 2026-08-07 per explicit user request): when
# enabled, every market that enters the coarse entry window gets a row
# appended to a SQLite DB -- ticker/category/spot/strike/model_prob/market
# price/time-to-expiry -- independent of whether it ever clears any trading
# gate. Not used for trading decisions; purely a dataset for later ML work
# (see check_resolutions.py, which fills in each sample's actual outcome
# once its market settles). Defaults OFF -- this adds write load to the same
# process placing real orders, so it shouldn't turn on silently. See
# sampling.py for the schema and runner.py's _maybe_sample_market for the
# call site (wrapped in try/except so a sampling bug can never affect a real
# trade decision).
SAMPLING_ENABLED = _bool_env("RESOLUTION_ALPHA_SAMPLING_ENABLED", False)
SAMPLING_DB_PATH = os.environ.get("RESOLUTION_ALPHA_SAMPLING_DB_PATH", os.path.join(LOG_DIR, "samples.db"))

# Kalshi underlying symbol (from series `tags`) -> Coinbase spot product id.
# This is a free public proxy for Kalshi's actual settlement source (CF
# Benchmarks' Real Time Index, not freely available) -- see the "Spot proxy"
# note in live/README.md for the basis risk this introduces. Symbols with no
# Coinbase listing will simply fail to poll and get skipped (see spot_feed.py).
KALSHI_UNDERLYING_TO_COINBASE_PRODUCT = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "ADA": "ADA-USD",
    "BNB": "BNB-USD",
    "NEAR": "NEAR-USD",
    "ZEC": "ZEC-USD",
    "TON": "TON-USD",
    "HYPE": "HYPE-USD",
}

# Restrict trading to underlyings with a validated settlement reference.
# Added 2026-08-15 after live/logs/samples.db showed all 3 losses across 109
# resolved trades (BTC 22/22, ETH 10/10, SOL/XRP/DOGE/HYPE/ZEC all clean --
# BNB 15/17, NEAR 12/13) fell exclusively on underlyings that fall back to
# spot_feed.py's Coinbase proxy instead of the real CF Benchmarks index
# ws_feed.py streams for BTC/ETH (see KALSHI_UNDERLYING_TO_COINBASE_PRODUCT's
# basis-risk note above). All 3 losses had spot within pennies of strike with
# under 24s left, where probability.py's settlement-window regime treats the
# "realized" portion of the settlement average as zero-variance -- correct
# only when built from the real settlement source, not a proxy that can
# silently diverge from it by more than that margin. That's a missing
# uncertainty term, not a Gaussian-shape problem: BTC/ETH went 32/32 on the
# real index, including entries with sub-parts-per-million spot/strike gaps.
# Until proxy basis risk is actually measured (see backfill_calibration.py),
# only trade underlyings backed by ws_feed's real index feed.
TRUSTED_SETTLEMENT_UNDERLYINGS = _set_env("RESOLUTION_ALPHA_TRUSTED_UNDERLYINGS", ("BTC", "ETH"))

# Master switch for whether the loop touches "unsafe" markets at all -- any
# underlying NOT in TRUSTED_SETTLEMENT_UNDERLYINGS, i.e. one fed by
# spot_feed.py's Coinbase proxy rather than ws_feed's real CF Benchmarks
# settlement index (see TRUSTED_SETTLEMENT_UNDERLYINGS's docstring above: all
# 3 live losses so far landed on proxy-fed underlyings).
#
# OFF (default): discovery.find_active_markets is asked for trusted
# underlyings ONLY, so the ~20 other live crypto series (SOL, XRP, DOGE, BNB,
# HYPE, NEAR, ZEC, ...) are never discovered -- and therefore never
# spot-polled, never order-book-subscribed, and never handed to
# evaluate_and_maybe_trade. Before this flag existed the loop still
# discovered and iterated all ~1,600 markets every tick just to skip the
# untrusted ones one by one (plus a Coinbase REST poll per untrusted
# underlying every POLL_INTERVAL_SECONDS) -- pure wasted compute for markets
# that could never clear the untrusted-underlying gate anyway.
#
# ON: discovery returns every qualifying underlying and
# evaluate_and_maybe_trade's untrusted-underlying gate is lifted, so unsafe
# markets are actually scanned and traded -- subject to every other entry
# gate unchanged (the dynamic entry window in
# _dynamic_entry_window_seconds still shrinks toward zero for non-trusted
# underlyings). Opt in deliberately; this re-exposes the proxy basis risk.
TRADE_UNSAFE_MARKETS = _bool_env("RESOLUTION_ALPHA_TRADE_UNSAFE_MARKETS", False)
