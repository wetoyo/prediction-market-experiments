"""Main entrypoint for the resolution_alpha live execution loop.

Cycle (see README.md and live/README.md for the full design):
  1. Discover currently-open recurring crypto interval markets (all series,
     not a hardcoded list -- see discovery.py). By default only underlyings
     in config.TRUSTED_SETTLEMENT_UNDERLYINGS are discovered at all; set
     config.TRADE_UNSAFE_MARKETS to also scan (and trade) proxy-fed
     underlyings -- see config.TRADE_UNSAFE_MARKETS and _underlying_scan_filter.
  2. Every tick, refresh spot/index data for every underlying currently in
     play, so volatility/history is warm *before* a market enters its entry
     window (polling only when inside the window would start the vol
     estimate from zero with ~90 seconds to work with). Prefers Kalshi's own
     websocket feeds (ws_feed.py) over REST polling -- see below.
  3. For each market inside the coarse entry window (last ENTRY_WINDOW_SECONDS
     before close), compute the favored-side probability, walk the order book
     for realistic fill size/price, and buy if the edge clears the threshold
     net of fees -- gated further by a *dynamic* window that only actually
     permits the trade once close enough to close_time for the current
     effective_probability (see config.ENTRY_WINDOW_EXPONENT). A market can be
     bought more than once within its window if it still clears every gate and
     has remaining per-market capacity (see _dynamic_entry_window_seconds and
     market_fills in evaluate_and_maybe_trade) -- but additional tranches on a
     market already held need more runway than a first entry
     (config.MIN_STACK_ENTRY_SECONDS_LEFT), and if the model has swung all the
     way to favoring the *opposite* side of a market we hold, that is treated
     as an exit signal, not a new entry (see the side-flip guard). A first
     entry is also refused outright when spot sits within
     config.MIN_STRIKE_DISTANCE_FRAC of the strike -- a coin flip the model
     still tends to stamp 0.97+ (added 2026-08-30).
  4. Positions normally ride to resolution -- entries only happen in the
     final seconds before close, so there's rarely time for anything to
     develop. As a defensive backstop (added 2026-08-06, see
     config.EXIT_Z_SCORE_DROP_THRESHOLD), each open position is re-checked
     every tick against a fresh probability estimate. A single best-effort
     exit sell attempt fires on either of two triggers: (a) the model has
     flipped to favoring the opposite side with entry-grade conviction while
     we held the position (added 2026-08-30 -- a full inversion; fires
     immediately when more than config.FLIP_EXIT_CONFIRM_SECONDS remain, else
     it must also clear the (b) spot-move floor), or (b) a sudden adverse move
     in the model's own sigma units (relative to the z-score at entry) AND a
     minimum absolute move in the underlying itself (added 2026-08-29, see
     config.EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC) -- the second half of (b) stops
     near-expiry noise, amplified by sigma_used collapsing as tau->0, from
     tripping the first. See _check_exit_conditions.

Data sourcing, in priority order (each falls back to the next if unavailable
-- e.g. no KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH configured, which is the
case in this environment today):
  - Order book: ws_feed.py's reconstructed orderbook_delta state, else a
    fresh REST fetch_orderbook call (the original behavior).
  - Settlement reference price: for BTC/ETH, ws_feed.py's cfbenchmarks_value
    stream -- the actual CF Benchmarks index Kalshi settles on, not a proxy
    (see ws_feed.py's docstring). Every other underlying still uses
    spot_feed.py's Coinbase REST poll, same basis-risk caveat as before.

Order placement is unchanged: order_manager.py's OrderManager wraps
KalshiTradingClient from prediction_market_scraper/Clients/Kalshi/
live_execution.py (via kalshi_gateway.py), the same authenticated REST
trading client as before -- websockets are additive for market data only,
not a replacement for how orders get placed.

Defaults to DRY_RUN (see config.py, order_manager.py): every intended trade
is logged, not placed. The probability model has now been checked for
calibration against settled-market history (see backtest.py and
README.md's Backtest plan) but liquidity/fill economics still have not
been (Kalshi's REST API has no historical order book), so do not flip
RESOLUTION_ALPHA_DRY_RUN=false until real order placement has been verified
with a small test order (see live/README.md's Known gaps).

Optional RESOLUTION_ALPHA_LIGHTWEIGHT_MODE (see config.py): sleeps through
most of each 15-minute interval instead of running continuously, waking only
for the last RESOLUTION_ALPHA_LIGHTWEIGHT_WAKE_WINDOW_SECONDS (default 180s /
3 minutes) before each interval's close -- the first half of that window
(down to RESOLUTION_ALPHA_LIGHTWEIGHT_TRADING_WINDOW_SECONDS, default 90s)
only warms up spot/index history and order-book subscriptions, with actual
evaluate_and_maybe_trade gating starting only in the second half. Suppresses
all logging for as long as it's on, except for the one-time startup banner
and trade-placement confirmations (see _log_despite_lightweight_mode) -- the
point is minimum footprint, not going dark on whether the process is alive
or placing orders. Off by default.
"""

import asyncio
import logging
import math
import sys
import time
from datetime import datetime, timezone

import config
import sampling
from discovery import ActiveMarket, find_active_markets
from fees import DEFAULT_FEE_RATE, estimate_fee_dollars
from order_manager import OrderManager
from orderbook import walk_book
from probability import estimate_probability, realized_vol_per_sqrt_second
from spot_feed import SpotFeed
from ws_feed import KalshiWebsocketFeed

# stream=sys.stdout, not the logging module's stderr default (2026-08-08):
# start_live.ps1 redirects stdout and stderr to two SEPARATE files
# (Start-Process doesn't allow pointing both at the same one). With the
# default stderr stream, every real log line -- including every trade --
# silently landed in the ".stderr" file while the "main" log file stayed
# empty. Real incident: a live loss went completely unnoticed because
# nobody thought to check the .stderr file. This is the one-line fix at the
# source rather than trying to reconcile two files after the fact.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("resolution_alpha.runner")

# See config.LIGHTWEIGHT_MODE's docstring: this mode's whole point is minimum
# footprint, so logging is suppressed globally (not just during the sleeping
# portion of each interval) for as long as it's on -- logging.disable acts on
# every logger process-wide, not just this module's, so this one call covers
# every logger.info/.debug/.warning/.exception call made anywhere below.
if config.LIGHTWEIGHT_MODE:
    logging.disable(logging.CRITICAL)


def _log_despite_lightweight_mode(level: int, msg: str, *args) -> None:
    """logger.log, but guaranteed to be emitted even while the module-level
    logging.disable(CRITICAL) above is suppressing everything else -- used
    only for the two events LIGHTWEIGHT_MODE should never hide: the one-time
    startup banner and trade-placement confirmations (see run_forever and
    evaluate_and_maybe_trade/_check_exit_conditions). logging.disable is a
    single process-wide threshold, not a per-call opt-out, so the only way to
    get one line through it is to briefly lift it, log, then restore it --
    the restore happens even if logger.log itself raises, so a bad format
    string here can't leave lightweight mode's suppression permanently off.
    """
    if not config.LIGHTWEIGHT_MODE:
        logger.log(level, msg, *args)
        return
    logging.disable(logging.NOTSET)
    try:
        logger.log(level, msg, *args)
    finally:
        logging.disable(logging.CRITICAL)


def _underlying_scan_filter() -> frozenset | None:
    """Which underlyings discovery should scan for this run: None (every
    qualifying crypto underlying) when config.TRADE_UNSAFE_MARKETS is on,
    else just config.TRUSTED_SETTLEMENT_UNDERLYINGS. Restricting it here is
    what keeps the loop from spending discovery calls, per-tick Coinbase spot
    polls, ws order-book subscriptions, and evaluate_and_maybe_trade
    iterations on markets the untrusted-underlying gate would only skip
    anyway -- see config.TRADE_UNSAFE_MARKETS's docstring.
    """
    if config.TRADE_UNSAFE_MARKETS:
        return None
    return config.TRUSTED_SETTLEMENT_UNDERLYINGS


def _cycle_key(now: datetime) -> int:
    """Coarse 15-minute wall-clock bucket used to reset the per-cycle
    contract budget. Imprecise for hourly markets (which span four such
    buckets) -- good enough as a blunt global cap for a first pass; a
    per-market-interval budget would be more correct if this matters later.
    """
    return (now.hour * 60 + now.minute) // 15


def _warn_unfunded_shards(active_markets: list, shard_balances: dict | None) -> None:
    """Once per cycle: if any discovered market trades on an exchange shard
    the account holds $0 on, say so loudly. Without collateral on that shard
    every entry there is a silent `404 user_not_found` -- this is the line
    that turns "the bot hasn't traded in days" into an obvious cause. No-op
    if the balance fetch failed (None) or nothing is unfunded.
    """
    if not shard_balances:
        return
    needed = {m.exchange_index for m in active_markets if m.exchange_index is not None}
    unfunded = sorted(s for s in needed if shard_balances.get(s, 0.0) <= 0.0)
    if not unfunded:
        return
    stuck = sum(1 for m in active_markets if m.exchange_index in unfunded)
    logger.warning(
        "exchange shard(s) %s hold $0 collateral but %d/%d discovered markets trade there -- those cannot be "
        "entered until funds are moved (kalshi.com/account/exchange-indexes or the Intra Account Transfer API). "
        "shard balances: %s",
        unfunded, stuck, len(active_markets), shard_balances,
    )


def _kelly_contracts(favored_probability: float, price: float, bankroll_dollars: float, kelly_fraction: float) -> float:
    """Kelly position size, in contracts (not yet floored to an integer or
    clipped to config's hard ceilings -- callers do that).

    Kelly-optimal fraction of bankroll to stake (as total dollar cost) on a
    binary contract priced at `price` with model probability
    `favored_probability` of paying out $1 is (q - p) / (1 - p). See
    config.KELLY_FRACTION's docstring for the current fraction in use.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    edge_prob = favored_probability - price
    if edge_prob <= 0.0:
        return 0.0
    full_kelly_fraction_of_bankroll = edge_prob / (1.0 - price)
    stake_dollars = kelly_fraction * full_kelly_fraction_of_bankroll * bankroll_dollars
    return stake_dollars / price


def _kelly_probability(effective_probability: float, market_price: float) -> float:
    """Win-probability fed to the Kelly size cap in _size_for_edge. Normally
    effective_probability (model, market-clamped); with
    config.KELLY_USE_MARKET_PROB set, the market's own top-of-book price plus
    MAX_TRUSTED_EDGE_PROB instead, with the raw model prob dropped -- see that
    config flag's docstring.
    """
    if config.KELLY_USE_MARKET_PROB:
        return min(1.0, market_price + config.MAX_TRUSTED_EDGE_PROB)
    return effective_probability


def _size_for_edge(
    orderbook_fp: dict,
    side: str,
    edge_probability: float,
    kelly_probability: float,
    bankroll_dollars: float,
    kelly_fraction: float,
    max_contracts: float,
) -> float:
    """Walks the book from best price outward, extending the position by
    whole contracts only while the CUMULATIVE fill so far still (a) clears
    MIN_EDGE_DOLLARS net of fees, (b) stays within the Kelly-implied size at
    that cumulative average price, and (c) fits the account's cash plus fee.
    Kelly's ideal size shrinks as price worsens, since edge shrinks too, so a
    size that was Kelly-justified at the top-of-book price may not be once the
    book has thinned into it.

    `edge_probability` is the win-probability used for the MIN_EDGE_DOLLARS
    check (always effective_probability); `kelly_probability` is the one fed to
    _kelly_contracts for the size cap -- the same value unless
    config.KELLY_USE_MARKET_PROB is set, in which case the caller passes the
    market's own top-of-book-derived probability instead. Check (c) is the
    same fee-aware cash cap _size_for_max_available applies: Kalshi rejects the
    whole order (400) if balance < cost + fee, and a full-Kelly size at a high
    price can exceed the balance on its own.
    Stops at the first contract that fails either check, taking a *partial*
    level if that's where the cutoff falls (binary search within the level,
    not all-or-nothing per level -- a single deep level can easily hold far
    more than Kelly wants, e.g. thousands of contracts at one price, and
    rejecting the whole level in that case would wrongly floor the trade to
    zero instead of taking the Kelly-sized slice of it). Returns the accepted
    contract count (0.0 if not even one contract clears both bars); the
    caller re-walks the book at that final size to get an exact FillEstimate.

    Kelly sizing history (2026-08-06, same day): started at 1/10 fraction,
    raised to 1/4, then removed entirely in favor of maxing out to
    liquidity+ceiling ("suspect even 1x kelly ratio its still not gonna have
    enough liquidity for it anyways"), then restored at **full (1.0)
    fraction** per explicit follow-up user instruction ("i want to js go
    with full kelly ratio") -- rather than a flat ceiling-maxing approach,
    full Kelly scales the position to the *actual computed edge* of each
    trade instead of always requesting the same fixed size regardless of how
    strong the edge is. Whether this ever binds below the account's real book
    depth (as opposed to always hitting MAX_CONTRACTS_PER_MARKET) is exactly
    what's still unmeasured -- see the "why is 20 a ceiling" discussion in
    activity_log.md.

    Sizing off a single top-of-book price and then walking to one fixed
    target either drags a good near-touch trade below the edge threshold by
    averaging in bad deep levels once the book is thin, or leaves real
    profit on the table by capping too early -- this walks level-by-level
    (and within a level, contract-by-contract) instead so a thinning book
    gets exactly the size it can support.

    Assumes ask price is non-decreasing as levels are walked (true --
    orderbook.walk_book consumes best-price-first via _opposite_side_bids)
    and non-decreasing within a level (constant price), so both
    edge_per_contract and the Kelly cap are non-increasing as size grows --
    the first infeasible contract means every one after it is infeasible
    too, which is what makes both the early-break and the binary search
    below valid.
    """
    probe = walk_book(orderbook_fp, side, max_contracts)
    if probe.avg_price is None:
        return 0.0

    spendable = max(0.0, bankroll_dollars - 0.01)  # 1c cushion, see _size_for_max_available

    def feasible(cum_size: float, cum_cost: float, price: float, extra: float) -> bool:
        if extra <= 0:
            return True
        trial_size = cum_size + extra
        trial_avg_price = (cum_cost + price * extra) / trial_size
        trial_fee = estimate_fee_dollars(trial_avg_price, trial_size)
        trial_edge_per_contract = ((edge_probability - trial_avg_price) * trial_size - trial_fee) / trial_size
        kelly_cap_at_price = _kelly_contracts(kelly_probability, trial_avg_price, bankroll_dollars, kelly_fraction)
        affordable = trial_size * trial_avg_price + trial_fee <= spendable
        return (
            trial_edge_per_contract >= config.MIN_EDGE_DOLLARS
            and trial_size <= kelly_cap_at_price
            and affordable
        )

    cum_size = 0.0
    cum_cost = 0.0

    for price, level_size in probe.levels_used:
        level_size_int = int(level_size)
        if not feasible(cum_size, cum_cost, price, level_size_int):
            # Binary search the largest whole-contract slice of *this* level
            # that's still feasible (monotonically non-increasing feasibility
            # within the level -- see docstring).
            lo, hi = 0, level_size_int
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if feasible(cum_size, cum_cost, price, mid):
                    lo = mid
                else:
                    hi = mid - 1
            cum_size += lo
            cum_cost += lo * price
            break  # this level's cutoff binds; every deeper level is worse still

        cum_size += level_size_int
        cum_cost += level_size_int * price

    return cum_size


def _size_for_max_available(
    orderbook_fp: dict,
    side: str,
    bankroll_dollars: float,
    max_contracts: float,
) -> float:
    """config.MAX_SIZE_MODE's sizing: walks the book from best price outward,
    same as _size_for_edge, but with no MIN_EDGE_DOLLARS or Kelly-cap check --
    takes the largest whole-contract size the book/cash actually support,
    period. Stops at whichever binds first: `max_contracts` (already applied
    via the walk_book(..., max_contracts) probe below -- the caller passes
    float("inf") here, NOT MAX_CONTRACTS_PER_MARKET or the cycle-wide
    remaining_budget, since the point of this mode is to measure real depth
    rather than stay clipped to some other ceiling -- see the 2026-08-25 note
    at the call site), a level running out of depth, or bankroll_dollars
    running out of affordable cost.
    """
    probe = walk_book(orderbook_fp, side, max_contracts)
    if probe.avg_price is None:
        return 0.0

    # Kalshi checks balance >= contract cost + trading fee at order time and
    # 400s the WHOLE order otherwise -- seen live 2026-08-28: 221 YES @ 0.902
    # = $199.34 cost + ~$1.37 fee vs a $200.17 balance, rejected outright (not
    # partially filled). `int(budget // price)` spends 100% of cash on
    # contracts and reserves nothing for the fee. Reserve it: per-contract fee
    # ~= DEFAULT_FEE_RATE * price * (1 - price) (the pre-ceil form of
    # fees.estimate_fee_dollars), plus a 1c cushion for its round-up and any
    # exchange minimum-balance.
    spendable = max(0.0, bankroll_dollars - 0.01)

    cum_size = 0.0
    cum_all_in = 0.0  # running (contract cost + reserved fee)
    for price, level_size in probe.levels_used:
        if price <= 0:
            break
        per_contract_all_in = price + DEFAULT_FEE_RATE * price * (1.0 - price)
        level_size_int = int(level_size)
        affordable = int((spendable - cum_all_in) // per_contract_all_in)
        take = min(level_size_int, affordable)
        if take <= 0:
            break
        cum_size += take
        cum_all_in += take * per_contract_all_in
        if take < level_size_int:
            break  # cash (incl. fee reserve) exhausted mid-level; deeper levels only cost more
    return cum_size


def _reconcile_fill(
    order_response: dict, simulated, favored_side: str, dry_run: bool
) -> tuple[float, float, float]:
    """What actually filled, as (contracts, avg_price, fee_dollars) -- for
    position/bankroll/budget accounting after buy_favored_side returns.
    `avg_price` is in FAVORED-SIDE terms (the same convention orderbook.walk_book
    and the rest of evaluate_and_maybe_trade use), NOT Kalshi's YES-denominated
    `average_fill_price`.

    Before this existed, the loop recorded `walk_book`'s *simulated* fill size
    as the position regardless of what Kalshi actually matched. With
    immediate_or_cancel orders (and, before, GTC orders raced near close) a
    partial or zero fill is routine, so trusting the simulation meant phantom
    positions, a bankroll drifting from reality, and a cycle budget consumed
    by contracts never bought.

    dry-run: no real match happened, so the simulated walk stands in.
    live: take the actually-filled *quantity* from `fill_count` (the field that
    varies with a partial fill). Take the price from `average_fill_price`, but
    convert it to favored-side terms first -- it is always quoted in YES
    dollars, so for a NO buy the favored-side price is `1 - average_fill_price`
    -- and fall back to the simulated avg_price if the response omits it or the
    converted value lands implausibly far from the simulation. That guard is
    not paranoia: `cost = avg_price * filled_size`, and under MAX_SIZE_MODE the
    next tick's size is computed off the resulting bankroll, so a mis-scaled
    price (e.g. a NO fill booked at its ~0.03 YES price instead of its ~0.97
    real cost) barely decrements bankroll and the loop immediately fires
    another full-size order. Seen live 2026-08-28.
    """
    if dry_run:
        fee = estimate_fee_dollars(simulated.avg_price, simulated.filled_size)
        return simulated.filled_size, simulated.avg_price, fee

    try:
        filled = float(order_response.get("fill_count") or 0.0)
    except (TypeError, ValueError):
        filled = 0.0
    if filled <= 0.0:
        return 0.0, 0.0, 0.0

    avg_price = simulated.avg_price
    try:
        yes_price = float(order_response["average_fill_price"])  # always YES-denominated
        favored_price = yes_price if favored_side == "yes" else round(1.0 - yes_price, 4)
        if 0.0 < favored_price < 1.0 and abs(favored_price - simulated.avg_price) <= 0.05:
            avg_price = favored_price
        else:
            logger.warning(
                "reconcile: response avg_fill_price %.4f (favored-side %.4f) implausible vs simulated %.4f "
                "-- using simulated",
                yes_price, favored_price, simulated.avg_price,
            )
    except (KeyError, TypeError, ValueError):
        pass  # response omitted the price -- simulated.avg_price already assigned

    try:
        resp_fee = float(order_response.get("average_fee_paid") or 0.0) * filled  # avg is per-contract
        fee = resp_fee if 0.0 <= resp_fee <= filled else estimate_fee_dollars(avg_price, filled)
    except (TypeError, ValueError):
        fee = estimate_fee_dollars(avg_price, filled)
    return filled, avg_price, fee


_STAT_KEYS = (
    "in_window", "traded",
    "skip_untrusted_underlying",
    "skip_no_spot", "skip_no_vol", "skip_low_model_prob",
    "skip_blacklisted", "skip_budget_exhausted", "skip_orderbook_failed",
    "skip_no_liquidity", "skip_low_market_prob", "skip_dynamic_window", "skip_min_seconds_left",
    "skip_ceiling_lt1", "skip_no_edge_size", "skip_no_fillable_depth", "skip_low_edge", "skip_no_bankroll",
    "skip_no_shard_collateral", "skip_side_flip_hold", "skip_near_strike",
)


def _new_stats() -> dict:
    return {k: 0 for k in _STAT_KEYS}


def _log_stats_summary(stats: dict, window_seconds: float) -> None:
    """Periodic low-volume substitute for the DEBUG-level skip lines that
    logging.basicConfig(level=logging.INFO) normally drops entirely -- see
    README.md's Known gaps ("log volume dropped after the safety-gate
    rewrite") for why this exists: individual per-market DEBUG lines would be
    ~1667-markets-per-tick noisy, but *some* visibility into why markets in
    the entry window aren't trading (vs. genuinely zero activity) is worth a
    few lines every 60s.
    """
    if stats["in_window"] == 0:
        return
    logger.info(
        "eval summary (last %.0fs): in_window=%d traded=%d | untrusted_underlying=%d no_spot=%d no_vol=%d low_model_prob=%d "
        "blacklisted=%d budget=%d orderbook_failed=%d no_liquidity=%d "
        "low_market_prob=%d dynamic_window=%d min_seconds_left=%d ceiling=%d no_edge_size=%d no_fillable_depth=%d low_edge=%d "
        "no_bankroll=%d no_shard_collateral=%d side_flip_hold=%d near_strike=%d",
        window_seconds, stats["in_window"], stats["traded"],
        stats["skip_untrusted_underlying"],
        stats["skip_no_spot"], stats["skip_no_vol"], stats["skip_low_model_prob"],
        stats["skip_blacklisted"], stats["skip_budget_exhausted"],
        stats["skip_orderbook_failed"], stats["skip_no_liquidity"], stats["skip_low_market_prob"],
        stats["skip_dynamic_window"], stats["skip_min_seconds_left"], stats["skip_ceiling_lt1"], stats["skip_no_edge_size"],
        stats["skip_no_fillable_depth"], stats["skip_low_edge"],
        stats["skip_no_bankroll"], stats["skip_no_shard_collateral"], stats["skip_side_flip_hold"],
        stats["skip_near_strike"],
    )


_WALL_CLOCK_INTERVAL_SECONDS = 15 * 60


def _seconds_to_next_wall_clock_boundary(now_ts: float) -> float:
    """Seconds until the next 15-minute UTC wall-clock boundary (:00/:15/:30/
    :45), which is what fifteen_min markets' close_time already aligns to.
    `now_ts % 900` works directly here (no datetime math needed) because the
    Unix epoch (1970-01-01T00:00:00 UTC) itself falls exactly on one of these
    boundaries, so every multiple of 900 seconds since epoch does too.
    """
    seconds_into_interval = now_ts % _WALL_CLOCK_INTERVAL_SECONDS
    return _WALL_CLOCK_INTERVAL_SECONDS - seconds_into_interval


def _seconds_until_lightweight_wake(now_ts: float) -> float:
    """How long run_forever should sleep before config.LIGHTWEIGHT_MODE's
    warm-up phase opens -- 0.0 if `now_ts` is already inside phase 1 or 2 (see
    config.LIGHTWEIGHT_MODE's docstring for the two phases).
    """
    seconds_to_boundary = _seconds_to_next_wall_clock_boundary(now_ts)
    return max(0.0, seconds_to_boundary - config.LIGHTWEIGHT_WAKE_WINDOW_SECONDS)


def _lightweight_trading_phase(now_ts: float) -> bool:
    """True once `now_ts` is inside phase 2 (full evaluate_and_maybe_trade /
    _check_exit_conditions) -- False means phase 1 (warm-up only: discovery +
    spot/index polling, no evaluation). Only meaningful once
    _seconds_until_lightweight_wake has already returned 0.0 for this tick.
    """
    seconds_to_boundary = _seconds_to_next_wall_clock_boundary(now_ts)
    return seconds_to_boundary <= config.LIGHTWEIGHT_TRADING_WINDOW_SECONDS


def _dynamic_entry_window_seconds(underlying: str, market_probability: float) -> float:
    """How close to close_time a market must be to trade *right now*, given
    the MARKET's own current top-of-book price for the favored side (not
    effective_prob -- see config.ENTRY_WINDOW_EXPONENT's docstring for why:
    effective_prob is still partly model-driven, and the model has already
    proven it can be confidently wrong even when the market agrees).
    Shrinks from the full config.ENTRY_WINDOW_SECONDS (at market_probability
    == 1.0) down to 0 (at market_probability <= config.ENTRY_WINDOW_PRICE_FLOOR --
    a separate, tighter knob than the hard MIN_MARKET_IMPLIED_PROBABILITY gate
    the caller already enforced; see config.ENTRY_WINDOW_PRICE_FLOOR's docstring).

    Bypassed entirely for underlyings in config.TRUSTED_SETTLEMENT_UNDERLYINGS
    (added 2026-08-25 per explicit user request): the shrink-toward-zero logic
    exists to protect against acting too early on a market whose own price
    isn't yet confident, but every underlying that clears
    TRUSTED_SETTLEMENT_UNDERLYINGS's upstream gate (see
    evaluate_and_maybe_trade -- currently just BTC/ETH) is backed by
    ws_feed's real CF Benchmarks settlement index rather than a Coinbase
    proxy, which is exactly the distinction that gate exists to draw (see its
    own docstring: all 3 live losses landed on proxy-fed underlyings, BTC/ETH
    went 32/32). For those, always use the full window instead -- no need to
    also earn extra entry time via price confidence on top of an
    already-trusted feed.
    """
    if underlying in config.TRUSTED_SETTLEMENT_UNDERLYINGS:
        return config.ENTRY_WINDOW_SECONDS

    floor = config.ENTRY_WINDOW_PRICE_FLOOR
    if market_probability <= floor:
        return 0.0
    normalized = min((market_probability - floor) / (1.0 - floor), 1.0)
    return config.ENTRY_WINDOW_SECONDS * (normalized ** config.ENTRY_WINDOW_EXPONENT)


def _spot_source(market: ActiveMarket, spot_feed: SpotFeed, ws_feed: KalshiWebsocketFeed):
    """Returns (now_epoch, spot, history) from whichever source has data for
    this underlying, preferring ws_feed's direct CF Benchmarks index feed
    (BTC/ETH only, see ws_feed.py) over spot_feed's Coinbase REST proxy.
    """
    if ws_feed.connected() and ws_feed.index_id_for(market.underlying):
        latest = ws_feed.latest_index_value(market.underlying)
        if latest is not None:
            return latest[0], latest[1], ws_feed.index_history(market.underlying)

    latest = spot_feed.latest(market.underlying)
    if latest is None:
        return None
    return latest[0], latest[1], spot_feed.history(market.underlying)


def _maybe_sample_market(
    market: ActiveMarket,
    estimate,
    spot: float,
    seconds_left: float,
    ws_feed: KalshiWebsocketFeed,
) -> int | None:
    """Records one dataset row for this market/tick if sampling is enabled
    (config.SAMPLING_ENABLED) -- see sampling.py's module docstring. Called
    right after the model estimate is computed and BEFORE any trading gate
    (MIN_FAVORED_PROBABILITY etc.), deliberately, so the dataset reflects the
    model's full raw output distribution, not just candidates that would
    have been traded. Returns the new row's id (or None if sampling is
    disabled) so the caller can fill in traded/skip_reason once this tick's
    gate evaluation finishes -- see _finalize_sample.

    market_price is best-effort only: reads whatever order book the
    websocket has already cached (ws_feed.get_orderbook_fp), never triggers
    a fresh REST fetch purely for sampling -- a market this close to its
    entry window almost always already has its book synced via
    config.ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS's wider lookahead, so this
    is normally free; when it isn't cached yet, market_price is just left
    NULL (the schema supports this) rather than spending an extra API call
    on every candidate for a purely observational feature.

    Deliberately does not touch cycle_state, stats, or any trading gate --
    this function's only side effect is a DB write, and callers wrap it in
    try/except so a sampling bug can never affect a real trade decision.
    """
    if not config.SAMPLING_ENABLED:
        return None

    market_price = None
    orderbook_fp = ws_feed.get_orderbook_fp(market.ticker)
    if orderbook_fp is not None:
        top_of_book = walk_book(orderbook_fp, estimate.favored_side, 1.0)
        market_price = top_of_book.avg_price

    return sampling.record_sample(
        config.SAMPLING_DB_PATH,
        ticker=market.ticker,
        series_ticker=market.series_ticker,
        category="crypto",  # only category discovery.py scans as of 2026-08-07
        underlying=market.underlying,
        direction=market.direction,
        strike=market.strike,
        close_time=market.close_time,
        seconds_to_expiry=seconds_left,
        spot=spot,
        favored_side=estimate.favored_side,
        model_prob=estimate.favored_probability,
        market_price=market_price,
        z=estimate.z,
    )


def _finalize_sample(sample_id: int | None, *, traded: bool, skip_reason: str | None) -> None:
    """Fills in the traded/skip_reason outcome on a sample row recorded
    earlier this tick by _maybe_sample_market. sample_id is None whenever
    sampling was disabled for that call (or the initial insert itself
    failed, which is caught at the call site) -- a no-op in that case.
    Wrapped in its own try/except, same rationale as _maybe_sample_market:
    a sampling bug must never affect a real trade decision.
    """
    if sample_id is None:
        return
    try:
        sampling.record_trade_outcome(
            config.SAMPLING_DB_PATH, sample_id=sample_id, traded=traded, skip_reason=skip_reason,
        )
    except Exception:
        logger.exception("failed to record trade outcome for sample_id=%s (non-fatal)", sample_id)


def _diagnostic_skip_reasons(
    market: ActiveMarket,
    estimate,
    spot: float,
    cycle_state: dict,
    ws_feed: KalshiWebsocketFeed,
    seconds_left: float,
) -> list[str]:
    """Independently re-checks every trading gate for sample-logging purposes
    only, so a skipped row's skip_reason can list EVERY applicable reason
    (2026-08-07, user: "does it check liquidity first? ... might be better to
    add every reason applicable") instead of just whichever single gate the
    real evaluate_and_maybe_trade happened to hit first before returning --
    its gates short-circuit in a fixed order (see its own comments) purely
    for efficiency/correctness of the real trade decision, not because later
    gates are less "true" when an earlier one already failed.

    Deliberately cache-only: reads whatever order book ws_feed already has
    (never a fresh REST fetch, same rationale as _maybe_sample_market) so
    this diagnostic pass never adds REST load to the live trading loop. If
    the book isn't cached yet, every gate past the blacklist/budget checks is
    simply left unevaluated (omitted, not guessed at) rather than reported as
    a false positive or forcing a fetch purely for logging.

    MAINTENANCE WARNING: this mirrors evaluate_and_maybe_trade's gate logic
    rather than sharing it (the real function can't be made non-short-
    -circuiting without either adding REST calls for candidates that would
    never trade anyway, or leaving later gates unevaluated when an earlier
    one blocks -- both defeat the point of "every reason"). If a gate's
    condition or ordering changes there, mirror the change here too, or this
    function's output will silently drift out of sync with reality. Purely
    diagnostic and read-only either way -- can never affect a real trade.
    """
    reasons: list[str] = []

    if estimate.favored_probability < config.MIN_FAVORED_PROBABILITY:
        reasons.append("low_model_prob")

    if spot and abs(spot - market.strike) / spot < config.MIN_STRIKE_DISTANCE_FRAC:
        reasons.append("near_strike")

    market_blacklist = cycle_state.get("market_blacklist", set())
    if market.ticker in market_blacklist:
        reasons.append("blacklisted")

    already_committed = cycle_state.get("contracts_committed", 0.0)
    remaining_budget = config.MAX_CYCLE_CONTRACTS - already_committed
    # Mirrors the real gate's MAX_SIZE_MODE override (2026-08-25) -- otherwise
    # this diagnostic would report budget_exhausted for trades the real path
    # no longer blocks on it for.
    if remaining_budget <= 0 and not config.MAX_SIZE_MODE:
        reasons.append("budget_exhausted")

    orderbook_fp = ws_feed.get_orderbook_fp(market.ticker)
    if orderbook_fp is None:
        return reasons  # nothing book-dependent is knowable without a fetch

    top_of_book = walk_book(orderbook_fp, estimate.favored_side, 1.0)
    if top_of_book.avg_price is None:
        reasons.append("no_liquidity")
        return reasons  # nothing below is computable without a price

    if top_of_book.avg_price < config.MIN_MARKET_IMPLIED_PROBABILITY:
        reasons.append("low_market_prob")

    effective_probability = min(estimate.favored_probability, top_of_book.avg_price + config.MAX_TRUSTED_EDGE_PROB)

    dynamic_window = _dynamic_entry_window_seconds(market.underlying, top_of_book.avg_price)
    if seconds_left > dynamic_window:
        reasons.append("dynamic_window")

    market_fills = cycle_state.get("market_fills", {})
    already_filled_this_market = market_fills.get(market.ticker, 0.0)
    # cycle_state.get(...) alone isn't enough here: the key is present with
    # value None whenever the real bankroll query failed this cycle (see the
    # main loop), and .get's default only kicks in when the key is *absent*.
    # This diagnostic path is non-fatal either way (see the try/except around
    # its caller) but 0.0 keeps its skip-reason accounting meaningful instead
    # of throwing.
    bankroll = cycle_state.get("bankroll_dollars") or 0.0
    # Mirrors the real gate's per-market Kelly cap (2026-09-04, fixed to only
    # freeze once a fill exists -- see evaluate_and_maybe_trade's own comment)
    # -- read-only: never writes cycle_state["market_kelly_caps"], since
    # whether this diagnostic pass even runs depends on unrelated sampling
    # config, and it must never be able to change what the real path computes
    # (see this function's docstring). Falls back to computing its own
    # estimate when the real path hasn't cached one yet this cycle.
    market_kelly_caps = cycle_state.get("market_kelly_caps", {})
    if already_filled_this_market > 0 and market.ticker in market_kelly_caps:
        kelly_cap = market_kelly_caps[market.ticker]
    else:
        kelly_cap = _kelly_contracts(
            _kelly_probability(effective_probability, top_of_book.avg_price),
            top_of_book.avg_price, bankroll, config.KELLY_FRACTION,
        )
    max_contracts = min(
        config.MAX_CONTRACTS_PER_MARKET - already_filled_this_market,
        kelly_cap - already_filled_this_market,
        remaining_budget,
    )
    if max_contracts < 1:
        reasons.append("ceiling_lt1")
        return reasons

    raw_size = _size_for_edge(
        orderbook_fp, estimate.favored_side, effective_probability,
        _kelly_probability(effective_probability, top_of_book.avg_price),
        bankroll, config.KELLY_FRACTION, max_contracts,
    )
    target_size = math.floor(raw_size)
    if target_size < 1:
        reasons.append("no_edge_size")
        return reasons

    fill = walk_book(orderbook_fp, estimate.favored_side, target_size)
    if fill.avg_price is None or fill.filled_size <= 0:
        reasons.append("no_fillable_depth")
        return reasons

    fee = estimate_fee_dollars(fill.avg_price, fill.filled_size)
    edge_total = (effective_probability - fill.avg_price) * fill.filled_size - fee
    edge_per_contract = edge_total / fill.filled_size
    if edge_per_contract < config.MIN_EDGE_DOLLARS:
        reasons.append("low_edge")

    return reasons


def _z_for_side(z: float, direction: str, side: str) -> float:
    """Orients probability.py's raw z (positive always favors "above"/yes,
    per its docstring) so that LARGER always means "better for whoever holds
    `side`" on a market of the given `direction` -- lets exit-monitoring
    compare entry vs. current z on a single consistent scale regardless of
    which side/direction the position actually is.
    """
    z_favoring_yes = z if direction == "above" else -z
    return z_favoring_yes if side == "yes" else -z_favoring_yes


async def _check_exit_conditions(
    open_positions: dict,
    spot_feed: SpotFeed,
    ws_feed: KalshiWebsocketFeed,
    order_manager: OrderManager,
) -> None:
    """Defensive backstop, not a real exit strategy (see config.py's
    EXIT_Z_SCORE_DROP_THRESHOLD docstring for why this exists at all): for
    each still-open position, recompute a fresh probability estimate and
    compare its z-score (oriented to our held side, see _z_for_side) against
    the z-score recorded at entry. One best-effort sell attempt per position
    fires on either trigger: (a) position["exit_on_flip"], set by the entry
    path when the model swung to favoring the opposite side with entry-grade
    conviction (fires immediately, no other gate), or (b) a z-score drop of
    EXIT_Z_SCORE_DROP_THRESHOLD+ AND an underlying move past
    EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC. Never more than one attempt per position,
    since a resting order that doesn't fill immediately and
    gets *re-submitted* on the next tick would stack multiple sell orders
    against the same position and risk selling past what we actually hold
    (economically opening a fresh position in the opposite direction instead
    of just closing this one). Closed/settled markets are dropped from
    open_positions here too, so this dict doesn't grow unboundedly.
    """
    for ticker, position in list(open_positions.items()):
        market = position["market"]
        now = datetime.now(timezone.utc)
        seconds_left = (market.close_time - now).total_seconds()
        if seconds_left <= 0:
            del open_positions[ticker]
            continue

        if position["exit_attempted"]:
            continue

        source = _spot_source(market, spot_feed, ws_feed)
        if source is None:
            continue
        now_epoch, spot, history = source
        sigma_log = realized_vol_per_sqrt_second(history)
        if sigma_log is None:
            continue

        estimate = estimate_probability(
            direction=market.direction, strike=market.strike, spot=spot,
            seconds_to_close=seconds_left, sigma_log_per_sqrt_second=sigma_log,
            spot_history=history, now_epoch=now_epoch,
        )
        current_z = _z_for_side(estimate.z, market.direction, position["side"])
        z_drop = position["entry_z"] - current_z

        entry_spot = position.get("entry_spot")
        adverse_move_frac = abs(spot - entry_spot) / entry_spot if entry_spot else float("inf")

        # Trigger (a): the entry path flagged a full model side-flip while we
        # held this position (see evaluate_and_maybe_trade's side-flip guard) --
        # the opposite side cleared the 0.97 entry bar, i.e. the model now puts
        # our held side below ~0.03. That is a strictly stronger signal than a
        # bare z-drop, so it fires immediately, bypassing the z-drop threshold --
        # AND, when more than config.FLIP_EXIT_CONFIRM_SECONDS remain, the
        # spot-move floor too. Inside that window the flip is being produced by
        # the same collapsing-sigma settlement blend the spot-move floor exists
        # to filter (2026-08-30, KXBTC15M-26AUG291415-15: model flipped on a
        # ~2 bp wiggle at 18s left, exit dumped a winner at 0.11), so near expiry
        # a flip has to clear the same move floor as a z-drop.
        flip_triggered = bool(position.get("exit_on_flip"))
        flip_needs_confirm = flip_triggered and seconds_left < config.FLIP_EXIT_CONFIRM_SECONDS

        # Trigger (b): a sudden adverse z-drop for our held side, AND a real
        # move in the underlying. The z-drop alone can't tell a regime break
        # from ordinary noise amplified by sigma_used collapsing ~tau^1.5 near
        # expiry, so require the underlying to have actually moved, in absolute
        # terms, away from the entry spot (config.EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC).
        # Zero added latency -- entry_spot and spot are both in hand right here.
        # If the z-drop clears but the move floor doesn't, log once and keep the
        # position eligible: a move that's really developing clears the floor on
        # a later tick and the exit fires then.
        if not flip_triggered and z_drop < config.EXIT_Z_SCORE_DROP_THRESHOLD:
            continue
        if (not flip_triggered or flip_needs_confirm) and (
            adverse_move_frac < config.EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC
        ):
            if not position.get("exit_deferred_logged"):
                _log_despite_lightweight_mode(
                    logging.WARNING,
                    "%s EXIT (%s) signalled but underlying only moved %.3f%% from entry "
                    "(floor %.3f%%) -- market has not confirmed; holding, still monitoring "
                    "(z-drop %.2f, %.0fs left)",
                    ticker, "side-flip" if flip_triggered else "z-drop",
                    adverse_move_frac * 100.0, config.EXIT_MIN_ADVERSE_SPOT_MOVE_FRAC * 100.0,
                    z_drop, seconds_left,
                )
                position["exit_deferred_logged"] = True
            continue

        _log_despite_lightweight_mode(
            logging.WARNING,
            "%s EXIT TRIGGER (%s): held %s x%.2f, entry_z=%.2f current_z=%.2f (dropped %.2f sigma, "
            "threshold=%.2f), underlying moved %.3f%% from entry, settlement_est=%.2f vs strike=%.2f, "
            "sigma_used=%.4f -- attempting one-shot best-effort sell, %.0fs left",
            ticker, "side-flip" if flip_triggered else "z-drop",
            position["side"], position["contracts"], position["entry_z"], current_z,
            z_drop, config.EXIT_Z_SCORE_DROP_THRESHOLD, adverse_move_frac * 100.0,
            estimate.settlement_estimate, market.strike, estimate.sigma_used, seconds_left,
        )
        position["exit_attempted"] = True  # set before attempting: see docstring, never retry

        orderbook_fp = ws_feed.get_orderbook_fp(ticker)
        if orderbook_fp is None:
            from kalshi_gateway import fetch_orderbook
            try:
                orderbook_fp = await asyncio.to_thread(fetch_orderbook, ticker)
            except Exception:
                logger.exception("%s exit attempt: failed to fetch orderbook, giving up on this exit", ticker)
                continue

        exit_side = "no" if position["side"] == "yes" else "yes"
        fill = walk_book(orderbook_fp, exit_side, position["contracts"])
        if fill.avg_price is None or fill.filled_size <= 0:
            logger.warning("%s exit attempt: no liquidity on the exit side, giving up (position rides to resolution)", ticker)
            continue

        try:
            # reduce_only: a best-effort exit must never fill past what we
            # actually hold and flip into an opposite position. exchange_index:
            # route to the market's own shard (same as entries). to_thread:
            # don't block the event loop on the HTTP round-trip.
            exit_response = await asyncio.to_thread(
                order_manager.buy_favored_side,
                ticker, exit_side, fill.filled_size, fill.levels_used[-1][0],
                market.exchange_index, "immediate_or_cancel", True,
            )
            exit_filled, _, _ = _reconcile_fill(exit_response, fill, exit_side, order_manager.dry_run)
            _log_despite_lightweight_mode(
                logging.WARNING, "%s exit sell: filled %.2f of %.2f held @ boundary %.4f",
                ticker, exit_filled, position["contracts"], fill.levels_used[-1][0],
            )
        except Exception:
            logger.exception("%s exit attempt: order placement failed", ticker)


async def evaluate_and_maybe_trade(
    market: ActiveMarket,
    spot_feed: SpotFeed,
    ws_feed: KalshiWebsocketFeed,
    order_manager: OrderManager,
    cycle_state: dict,
    stats: dict,
    open_positions: dict,
) -> None:
    now = datetime.now(timezone.utc)
    seconds_left = (market.close_time - now).total_seconds()
    if seconds_left <= 0 or seconds_left > config.ENTRY_WINDOW_SECONDS:
        return
    stats["in_window"] += 1

    if market.underlying not in config.TRUSTED_SETTLEMENT_UNDERLYINGS and not config.TRADE_UNSAFE_MARKETS:
        # See config.TRUSTED_SETTLEMENT_UNDERLYINGS's docstring: all 3 losses
        # in live/logs/samples.db landed on Coinbase-proxy-fed underlyings
        # (BNB, NEAR), never on BTC/ETH's real CF Benchmarks index feed.
        # Checked before any spot/vol work -- cheapest possible skip. With
        # config.TRADE_UNSAFE_MARKETS off, discovery already filters these
        # out (see _underlying_scan_filter), so this is a belt-and-braces
        # backstop for a stray untrusted market rather than the primary
        # filter; with it on, unsafe markets are meant to trade, so the gate
        # lifts entirely.
        logger.debug(
            "%s (%s) not in TRUSTED_SETTLEMENT_UNDERLYINGS, skipping",
            market.ticker, market.underlying,
        )
        stats["skip_untrusted_underlying"] += 1
        return

    source = _spot_source(market, spot_feed, ws_feed)
    if source is None:
        logger.debug("no spot price yet for %s (%s), skipping", market.ticker, market.underlying)
        stats["skip_no_spot"] += 1
        return
    now_epoch, spot, history = source

    sigma_log = realized_vol_per_sqrt_second(history)
    if sigma_log is None:
        logger.debug("not enough spot history yet for %s, skipping", market.underlying)
        stats["skip_no_vol"] += 1
        return

    estimate = estimate_probability(
        direction=market.direction,
        strike=market.strike,
        spot=spot,
        seconds_to_close=seconds_left,
        sigma_log_per_sqrt_second=sigma_log,
        spot_history=history,
        now_epoch=now_epoch,
    )

    sample_id = None
    diagnostic_reasons: list[str] = []
    try:
        sample_id = _maybe_sample_market(market, estimate, spot, seconds_left, ws_feed)
        if sample_id is not None:
            diagnostic_reasons = _diagnostic_skip_reasons(market, estimate, spot, cycle_state, ws_feed, seconds_left)
    except Exception:
        logger.exception("sampling failed for %s (non-fatal, continuing to trade evaluation)", market.ticker)

    def _record_skip_sample(reason: str) -> None:
        # Unions the real flow's actual trigger into the cache-only
        # diagnostic list -- covers it being incomplete (book not cached
        # when this reason needed one) or reflecting a reason (e.g.
        # orderbook_failed, order_placement_failed) the diagnostic pass
        # deliberately never evaluates itself (see its docstring).
        if reason not in diagnostic_reasons:
            diagnostic_reasons.append(reason)
        _finalize_sample(sample_id, traded=False, skip_reason=",".join(diagnostic_reasons))

    if estimate.favored_probability < config.MIN_FAVORED_PROBABILITY:
        stats["skip_low_model_prob"] += 1
        _record_skip_sample("low_model_prob")
        return

    # Side-flip guard (added 2026-08-30). If we already hold this market and the
    # model has now swung all the way to favoring the OTHER side with entry-grade
    # conviction (favored_probability >= MIN_FAVORED_PROBABILITY, checked just
    # above -- so prob on our held side has collapsed below ~0.03), that is not a
    # fresh opportunity: our open position is now the losing side of the strike.
    # Opening a fresh opposing taker position here is what happened live on
    # 2026-08-29 (KXBTC15M-26AUG291945-45: held YES 136 @ 0.934, then bought
    # NO 64 @ 0.959 with 19s left -- a near-certain loss at a punishing price,
    # plus a tangled two-sided position the exit accounting then mis-tracked).
    # Route the signal to the exit backstop instead: flag the position so
    # _check_exit_conditions closes it (reduce_only, one-shot) on this same
    # cycle, bypassing the spot-move floor -- a full inversion that clears the
    # entry bar on the far side is a strictly stronger signal than the bare
    # z-drop that floor was built to filter.
    held = open_positions.get(market.ticker)
    if held is not None and held["side"] != estimate.favored_side:
        if not held.get("exit_on_flip"):
            _log_despite_lightweight_mode(
                logging.WARNING,
                "%s model favored side flipped %s -> %s at prob %.4f while holding %s x%.2f "
                "(%.0fs left) -- not entering the flip; handing to exit backstop to close",
                market.ticker, held["side"], estimate.favored_side, estimate.favored_probability,
                held["side"], held["contracts"], seconds_left,
            )
        held["exit_on_flip"] = True
        stats["skip_side_flip_hold"] += 1
        _record_skip_sample("side_flip_hold")
        return

    # Strike-distance gate (added 2026-08-30). Even a 0.97+ model call is only
    # ~90-93% reliable when spot is parked within ~1-4 bp of the strike -- every
    # historical loser on a gated row, and all 5 of the 2026-08-29/30 live
    # losers, sat in that band (they settled 0.2-3.3 bp from the strike). The
    # settlement-window sigma floor (probability.py) makes the model's number
    # less overconfident there but doesn't make the outcome any less of a coin
    # flip. Refuse the entry outright when spot is that close. Placed after the
    # side-flip guard so a held position that drifts into this band still gets
    # routed to the exit backstop rather than silently ignored. See
    # config.MIN_STRIKE_DISTANCE_FRAC.
    strike_distance_frac = abs(spot - market.strike) / spot if spot else 0.0
    if strike_distance_frac < config.MIN_STRIKE_DISTANCE_FRAC:
        logger.debug(
            "%s spot %.6f only %.5f%% from strike %.6f (< MIN_STRIKE_DISTANCE_FRAC=%.4f%%), skipping "
            "regardless of model_prob=%.4f -- coin flip on the strike",
            market.ticker, spot, strike_distance_frac * 100.0, market.strike,
            config.MIN_STRIKE_DISTANCE_FRAC * 100.0, estimate.favored_probability,
        )
        stats["skip_near_strike"] += 1
        _record_skip_sample("near_strike")
        return

    # z_favored (not gated on its own anymore -- removed 2026-08-06, user:
    # "remove the prob saturdated thing - too arbitrary. i think the dynamic
    # time sizing is enough". The dynamic entry window below is driven by the
    # MARKET's own price, not the model's z, so it already gates *when* it's
    # safe to act without needing a second, separate opinion on the model's
    # own confidence). Still computed here -- used for the log line and as
    # entry_z for exit-monitoring (_check_exit_conditions) once a trade goes
    # through.
    z_favored = _z_for_side(estimate.z, market.direction, estimate.favored_side)

    # A market only lands on this blacklist if a *placement attempt* failed
    # (see the try/except around buy_favored_side below) -- successful buys
    # don't block further evaluation, since the whole point of market_fills
    # (below) is to keep buying the same market again as long as it still
    # clears every gate and there's remaining per-market capacity. A market
    # is blacklisted, not just skipped-this-tick, because a placement failure
    # (bad price, network error, anything) means we don't know why it failed,
    # and retrying blindly is exactly what caused a real repeated-failed-order
    # incident live on 2026-08-06 (see order_manager.py's module docstring).
    market_blacklist = cycle_state.setdefault("market_blacklist", set())
    if market.ticker in market_blacklist:
        stats["skip_blacklisted"] += 1
        _record_skip_sample("blacklisted")
        return

    already_committed = cycle_state.setdefault("contracts_committed", 0.0)
    remaining_budget = config.MAX_CYCLE_CONTRACTS - already_committed
    # MAX_SIZE_MODE overrides MAX_CYCLE_CONTRACTS entirely (2026-08-25, see
    # the max_contracts branch below) -- so once a cycle's nominal budget is
    # "used up" this must not skip either, or every trade after the first in
    # a cycle would still be silently blocked by the very knob this mode is
    # supposed to ignore. contracts_committed is still tracked (below, and at
    # the fill-recording site) purely for stats/logging in this mode.
    if remaining_budget <= 0 and not config.MAX_SIZE_MODE:
        logger.debug("cycle contract budget exhausted, skipping %s", market.ticker)
        stats["skip_budget_exhausted"] += 1
        _record_skip_sample("budget_exhausted")
        return

    orderbook_fp = ws_feed.get_orderbook_fp(market.ticker)
    orderbook_source = "ws"
    if orderbook_fp is None:
        from kalshi_gateway import fetch_orderbook
        try:
            orderbook_fp = await asyncio.to_thread(fetch_orderbook, market.ticker)
            orderbook_source = "rest"
        except Exception:
            logger.exception("failed to fetch orderbook for %s", market.ticker)
            stats["skip_orderbook_failed"] += 1
            _record_skip_sample("orderbook_failed")
            return

    # Model-vs-market sanity gates (added 2026-08-06 -- see config.py's
    # MIN_MARKET_IMPLIED_PROBABILITY / MAX_TRUSTED_EDGE_PROB docstrings for the
    # live incident that prompted these). Both compare against the market's own
    # top-of-book price for the favored side, not the model's claim.
    top_of_book = walk_book(orderbook_fp, estimate.favored_side, 1.0)
    if top_of_book.avg_price is None:
        logger.debug("no liquidity at all for %s %s (source=%s)", market.ticker, estimate.favored_side, orderbook_source)
        stats["skip_no_liquidity"] += 1
        _record_skip_sample("no_liquidity")
        return

    if top_of_book.avg_price < config.MIN_MARKET_IMPLIED_PROBABILITY:
        logger.debug(
            "%s market itself only implies %.4f confidence in %s (< MIN_MARKET_IMPLIED_PROBABILITY=%.2f), skipping "
            "regardless of model_prob=%.4f",
            market.ticker, top_of_book.avg_price, estimate.favored_side,
            config.MIN_MARKET_IMPLIED_PROBABILITY, estimate.favored_probability,
        )
        stats["skip_low_market_prob"] += 1
        _record_skip_sample("low_market_prob")
        return

    effective_probability = min(estimate.favored_probability, top_of_book.avg_price + config.MAX_TRUSTED_EDGE_PROB)
    if effective_probability < estimate.favored_probability - 1e-9:
        logger.warning(
            "%s model/market divergence capped: model_prob=%.4f market_price=%.4f (top of book) -> using "
            "eff_prob=%.4f for sizing/edge (MAX_TRUSTED_EDGE_PROB=%.2f) -- treating this as likely model "
            "overconfidence, not an uncaptured real edge",
            market.ticker, estimate.favored_probability, top_of_book.avg_price,
            effective_probability, config.MAX_TRUSTED_EDGE_PROB,
        )

    # Dynamic entry window (added 2026-08-06, see config.ENTRY_WINDOW_EXPONENT's
    # docstring): even though the market passed the coarse ENTRY_WINDOW_SECONDS
    # candidate check up top, whether *this instant* is actually early enough to
    # trade depends on how confident the MARKET's own price already is -- not
    # effective_probability, which is still partly model-driven and the model
    # has already proven it can be confidently wrong even when the market
    # agrees. A market sitting right at the MIN_MARKET_IMPLIED_PROBABILITY
    # floor only qualifies an instant before close; a market already pricing
    # the favored side near-certain can trade as early as the full window
    # allows.
    dynamic_window = _dynamic_entry_window_seconds(market.underlying, top_of_book.avg_price)
    if seconds_left > dynamic_window:
        logger.debug(
            "%s market_price=%.4f only justifies entry within %.0fs of close, %.0fs still left, skipping for now",
            market.ticker, top_of_book.avg_price, dynamic_window, seconds_left,
        )
        stats["skip_dynamic_window"] += 1
        _record_skip_sample("dynamic_window")
        return

    # Hard floor under the dynamic window above -- see config.MIN_ENTRY_SECONDS_LEFT's
    # docstring for the real incident (KXBNB15M-26AUG080400-00) this guards
    # against: a market pricing near-certainty can otherwise pass the dynamic
    # window with a fraction of a second left, where a stale spot quote can
    # produce a falsely-certain z, and where there may not be enough time
    # left for the order to actually finish matching before the exchange
    # stops accepting fills.
    if seconds_left < config.MIN_ENTRY_SECONDS_LEFT:
        logger.debug(
            "%s only %.2fs left, below MIN_ENTRY_SECONDS_LEFT=%.1f, skipping regardless of price/confidence",
            market.ticker, seconds_left, config.MIN_ENTRY_SECONDS_LEFT,
        )
        stats["skip_min_seconds_left"] += 1
        _record_skip_sample("min_seconds_left")
        return

    # Stacking floor (added 2026-08-30). A *first* entry can go in as late as
    # MIN_ENTRY_SECONDS_LEFT (3s) when the market itself prices near-certainty.
    # An *additional* tranche on a market we already hold is different: the
    # position is already sized, the marginal timing information in the last
    # few seconds is dominated by settlement-averaging noise (probability.py),
    # and a late add at a worse price is exactly what compounded the
    # 2026-08-29 KXBTC15M-26AUG291945-45 mess. Any opposite-side flip was
    # already handled above, so a position still open here is same-side; just
    # require more runway before topping it up.
    if (
        open_positions.get(market.ticker) is not None
        and seconds_left < config.MIN_STACK_ENTRY_SECONDS_LEFT
    ):
        logger.debug(
            "%s already held, only %.1fs left (< MIN_STACK_ENTRY_SECONDS_LEFT=%.1f) -- not stacking",
            market.ticker, seconds_left, config.MIN_STACK_ENTRY_SECONDS_LEFT,
        )
        stats["skip_min_seconds_left"] += 1
        _record_skip_sample("min_seconds_left_stack")
        return

    # Per-market fill tracking (replaces the old one-shot-per-market dedup,
    # 2026-08-06): a market can be bought more than once within its entry
    # window now, as long as there's remaining capacity under its per-market
    # ceiling (see market_kelly_caps below) and it still clears every gate at
    # the fresh price -- per explicit user request ("continuing to buy if
    # there is available liquidity ... is good"). This does NOT reopen the
    # old repeated-buy-attempt bug (buying 9 times in under a minute as the
    # book thinned, 2026-08-05): that bug was re-buying at *deteriorating*
    # edge: here, every additional buy still has to clear MIN_EDGE_DOLLARS
    # and _size_for_edge's own per-tranche Kelly cap at its own price, same
    # as the first one, so a thinning book naturally stops further buys on
    # its own once the edge is gone.
    bankroll = cycle_state["bankroll_dollars"]
    if bankroll is None:
        # Real balance query failed this cycle (see the main loop) -- skip
        # trading rather than size against a fabricated bankroll.
        logger.debug("no bankroll available this cycle (balance fetch failed), skipping %s", market.ticker)
        stats["skip_no_bankroll"] += 1
        _record_skip_sample("no_bankroll")
        return

    # Exchange-shard collateral guard (Kalshi Exchange Sharding,
    # docs.kalshi.com/getting_started/exchange_sharding). Collateral is local
    # to a shard; an order routed to a shard the account holds no balance on
    # is rejected `404 {"error":{"code":"user_not_found"}}`, and the except
    # around buy_favored_side below would then blacklist the ticker for the
    # whole cycle. Every crypto interval series migrated onto a nonzero shard
    # in Aug 2026 -- if the account's collateral hasn't been moved there too
    # (kalshi.com/account/exchange-indexes, or the Intra Account Transfer
    # API), nothing here can trade. Skip cleanly with a dedicated stat rather
    # than attempt-and-blacklist. shard_balances is fetched once per cycle
    # (see run_forever); falsy (fetch failed, or an unexpected empty
    # breakdown) -> don't guard, let the real order attempt be the judge
    # rather than silently skipping every market.
    shard_balances = cycle_state.get("shard_balances")
    if (
        not order_manager.dry_run
        and market.exchange_index is not None
        and shard_balances
        and shard_balances.get(market.exchange_index, 0.0) <= 0.0
    ):
        logger.debug(
            "%s trades on exchange shard %d, which holds $0 collateral -- skipping "
            "(move funds: kalshi.com/account/exchange-indexes)",
            market.ticker, market.exchange_index,
        )
        stats["skip_no_shard_collateral"] += 1
        _record_skip_sample("no_shard_collateral")
        return

    market_fills = cycle_state.setdefault("market_fills", {})
    already_filled_this_market = market_fills.get(market.ticker, 0.0)

    # Per-market Kelly cap (2026-09-04, replaces the flat MAX_CONTRACTS_PER_MARKET
    # ceiling below as the primary per-market limit). _size_for_edge already
    # applies its own fresh Kelly cap to every individual tranche, but that cap
    # is computed against whatever bankroll happens to be left *at that
    # moment* -- correct for sizing one tranche in isolation, but repeated
    # full-Kelly tranches against a shrinking-but-still-large bankroll
    # compound past what a single Kelly calculation on the whole opportunity
    # would allow, since they're the same directional bet, not independent
    # ones. 2026-09-04 KXETH15M-26SEP041100-00: four tranches (58+25+12+4=99
    # contracts), each individually Kelly-justified against the bankroll left
    # at its own moment, stacked into a position that then couldn't be
    # exited when the model flipped at 8s left.
    #
    # Only freeze the cap once a tranche has actually FILLED (already_filled >
    # 0), not on the first look at the market -- same "only once already held"
    # gating as the MIN_STACK_ENTRY_SECONDS_LEFT floor below. Freezing on first
    # look regardless of fill status was a real live bug (caught 2026-09-04,
    # same evening): a market's first tick in its entry window can catch a
    # thin edge (price just barely past MIN_MARKET_IMPLIED_PROBABILITY), which
    # locks in a tiny/zero kelly_cap -- and since already_filled stays 0 until
    # a real fill happens, every later tick (even as price/edge improve toward
    # close, or after an IOC order that zero-filled) kept re-reading that same
    # stale near-zero cap and skipping via ceiling_lt1, permanently blocking
    # the market for the rest of its window despite a real, growing edge.
    # Recomputing fresh here on every no-fill-yet tick costs nothing extra
    # (_kelly_contracts is cheap) and matches pre-fix behavior for a first
    # entry; the cap only locks once there's an actual position to protect
    # from compounding, at the exact number that justified that first fill
    # (recorded below, alongside market_fills, once filled_size is known).
    market_kelly_caps = cycle_state.setdefault("market_kelly_caps", {})
    if already_filled_this_market > 0:
        kelly_cap = market_kelly_caps[market.ticker]
    else:
        kelly_cap = _kelly_contracts(
            _kelly_probability(effective_probability, top_of_book.avg_price),
            top_of_book.avg_price, bankroll, config.KELLY_FRACTION,
        )

    # MAX_SIZE_MODE's whole point is to discover how much the book/cash
    # actually support, so it isn't clipped to MAX_CONTRACTS_PER_MARKET/the
    # Kelly cap above (those exist to backstop Kelly/edge sizing against a
    # bad probability estimate -- irrelevant here, since this mode already
    # sizes off real depth/cash with no edge check) OR to MAX_CYCLE_CONTRACTS
    # (2026-08-25, confirmed live: with the per-market ceiling fixed, fills
    # just started landing on the cycle-wide ceiling instead -- same
    # round-number symptom, different knob. Verified by temporarily raising
    # MAX_CYCLE_CONTRACTS to 100 against a <100 account balance and watching
    # a fill land at the real cash-bound size instead of 100). The only
    # backstops left in this mode are real book depth and actual
    # bankroll_dollars, both enforced inside _size_for_max_available itself.
    # See config.MAX_SIZE_MODE's docstring.
    if config.MAX_SIZE_MODE:
        max_contracts = float("inf")
    else:
        max_contracts = min(
            config.MAX_CONTRACTS_PER_MARKET - already_filled_this_market,
            kelly_cap - already_filled_this_market,
            remaining_budget,
        )
    if max_contracts < 1:
        logger.debug(
            "cycle/market ceiling leaves room for <1 contract for %s (already_filled=%.2f), skipping",
            market.ticker, already_filled_this_market,
        )
        stats["skip_ceiling_lt1"] += 1
        _record_skip_sample("ceiling_lt1")
        return

    # Walk the book level by level, capped at max_contracts, keeping only the
    # prefix that's still Kelly/edge-justified at its own cumulative average
    # price -- see _size_for_edge's docstring for why a fixed target size
    # sized off a single top-of-book price isn't correct once the book thins.
    if config.MAX_SIZE_MODE:
        raw_size = _size_for_max_available(orderbook_fp, estimate.favored_side, bankroll, max_contracts)
    else:
        raw_size = _size_for_edge(
            orderbook_fp, estimate.favored_side, effective_probability,
            _kelly_probability(effective_probability, top_of_book.avg_price),
            bankroll, config.KELLY_FRACTION, max_contracts,
        )
    target_size = math.floor(raw_size)
    if target_size < 1:
        logger.debug(
            "%s no size clears edge+kelly within available depth (bankroll=%.2f, max_contracts=%.2f), skipping",
            market.ticker, bankroll, max_contracts,
        )
        stats["skip_no_edge_size"] += 1
        _record_skip_sample("no_edge_size")
        return

    # Re-walk at the final (integer, floored) size for an exact fill -- since price
    # is non-decreasing with size, this can only match or improve on the average
    # price implied by raw_size, so the edge/fee numbers below are the real ones
    # this order will actually see, not an estimate.
    fill = walk_book(orderbook_fp, estimate.favored_side, target_size)
    if fill.avg_price is None or fill.filled_size <= 0:
        logger.debug("no fillable depth for %s %s (source=%s)", market.ticker, estimate.favored_side, orderbook_source)
        stats["skip_no_fillable_depth"] += 1
        _record_skip_sample("no_fillable_depth")
        return

    fee = estimate_fee_dollars(fill.avg_price, fill.filled_size)
    edge_total = (effective_probability - fill.avg_price) * fill.filled_size - fee
    edge_per_contract = edge_total / fill.filled_size

    # spot/strike logged at 6 decimals, not 2 -- a 2-decimal log line reads as
    # "spot=1.05 strike=1.05, identical" for an asset like XRP that actually
    # moves in fractional cents, hiding exactly the information needed to tell
    # a genuine near-the-money situation from a real gap. Matters far less for
    # BTC/ETH-scale prices but costs nothing to apply uniformly.
    # z_favored still logged (not gated on anymore -- see the removal note
    # above evaluate_and_maybe_trade's z_favored computation) so it's
    # available for reference/debugging without a dedicated gate to hang it on.
    #
    # The final edge gate. Computed before the detail line below only so that
    # line can pick its level: a candidate that clears MIN_EDGE_DOLLARS is
    # about to place an order (nothing between here and buy_favored_side can
    # skip it), so it's worth an INFO line; one that doesn't is a near miss
    # the 60s eval summary's low_edge counter already accounts for, and at
    # INFO it floods a quiet-but-active window with dozens of
    # "edge/contract=-0.00xx" lines a minute. Flooring target_size down from
    # raw_size can land right on the threshold boundary, so this genuinely
    # fires in normal operation, not just rare cases.
    will_trade = edge_per_contract >= config.MIN_EDGE_DOLLARS

    _candidate_log = (
        logger.info if (will_trade or config.LOG_NONTRADING_CANDIDATES) else logger.debug
    )
    _candidate_log(
        "%s %s left=%.0fs spot=%.6f strike=%.6f model_prob=%.4f eff_prob=%.4f z=%.2f "
        "fill_price=%.4f filled=%.2f/%.2f (raw_kelly_size=%.2f) edge/contract=%.4f already_filled=%.2f "
        "dyn_window=%.0fs book=%s",
        market.ticker, estimate.favored_side, seconds_left, spot, market.strike,
        estimate.favored_probability, effective_probability, z_favored,
        fill.avg_price, fill.filled_size, target_size,
        raw_size, edge_per_contract, already_filled_this_market, dynamic_window, orderbook_source,
    )

    if not will_trade:
        stats["skip_low_edge"] += 1
        _record_skip_sample("low_edge")
        return

    # Submit the boundary (worst-acceptable) price of the walked fill, not
    # fill.avg_price -- Kalshi rejects non-tick-aligned prices, and an average
    # across multiple levels lands on fractional cents as soon as a fill spans
    # more than one level (see order_manager.py's module docstring). The
    # boundary price is always one of the book's own already-cent-aligned
    # levels; Kalshi's matching engine walks the book itself from there.
    #
    # On failure: blacklist this ticker for the rest of the cycle rather than
    # letting it retry on the next tick -- see market_blacklist's docstring
    # above for the 2026-08-06 incident this guards against. On success:
    # record the fill in market_fills so this market can still be bought
    # again later (up to its remaining ceiling) rather than being blocked
    # outright, per the new per-market-fill-tracking design above.
    #
    # buy_favored_side runs in a worker thread (await asyncio.to_thread): in
    # live mode it does a blocking `requests` POST, and calling it inline here
    # would stall the ws_feed receive coroutine for the whole HTTP round-trip
    # -- every market evaluated later in the same tick would then size off an
    # order book that stopped applying deltas when this order fired.
    try:
        order_response = await asyncio.to_thread(
            order_manager.buy_favored_side,
            market.ticker,
            estimate.favored_side,
            fill.filled_size,
            fill.levels_used[-1][0],
            market.exchange_index,
        )
    except Exception:
        market_blacklist.add(market.ticker)
        _record_skip_sample("order_placement_failed")
        raise

    # What actually matched, not what walk_book simulated -- the order is IOC,
    # so a partial or zero fill (beaten to the book near close, or no depth at
    # the boundary at submit time) is routine and must not be booked as a full
    # position. See _reconcile_fill.
    filled_size, filled_avg_price, fee = _reconcile_fill(
        order_response, fill, estimate.favored_side, order_manager.dry_run
    )
    if filled_size <= 0.0:
        logger.info(
            "%s order placed but 0 filled (requested %.2f %s @ %.4f) -- no marketable depth at submit time; "
            "not blacklisting, re-evaluates next tick",
            market.ticker, fill.filled_size, estimate.favored_side, fill.levels_used[-1][0],
        )
        _finalize_sample(sample_id, traded=False, skip_reason="zero_fill")
        return
    if filled_size + 1e-9 < fill.filled_size:
        logger.warning(
            "%s partial fill: %.2f of %.2f requested %s @ ~%.4f (IOC remainder dropped)",
            market.ticker, filled_size, fill.filled_size, estimate.favored_side, filled_avg_price,
        )

    edge_per_contract = ((effective_probability - filled_avg_price) * filled_size - fee) / filled_size

    # Diagnostic only, MAX_SIZE_MODE fills -- re-walks the same orderbook_fp
    # with an effectively unlimited probe, purely to report how much book
    # depth actually existed beyond what we took. Added 2026-08-25 after
    # fills kept landing at whatever the current ceiling happened to be
    # (MAX_CONTRACTS_PER_MARKET=20, then MAX_CYCLE_CONTRACTS=30) -- both since
    # fixed, so max_contracts is now float("inf") in this mode (see the call
    # site above) and the only real backstops left are book depth and
    # bankroll_dollars. This line is what actually proves that instead of
    # asking for trust. One extra walk_book call, only on an actual trade
    # (not every candidate evaluation), so it never touches the hot loop above
    # and never affects sizing/order placement, which already completed.
    depth_note = ""
    if config.MAX_SIZE_MODE:
        true_depth = walk_book(orderbook_fp, estimate.favored_side, 1_000_000.0)
        if true_depth.filled_size > filled_size + 1e-9:
            depth_note = " [book had %.2f available -- cash-bound, not liquidity-bound]" % true_depth.filled_size
        else:
            depth_note = " [book depth itself was the true bind]"

    _log_despite_lightweight_mode(
        logging.INFO, "%s entry filled: %s x%.2f of %.2f req @ ~%.4f (boundary %.4f, edge/contract=%.4f)%s",
        market.ticker, estimate.favored_side, filled_size, fill.filled_size, filled_avg_price,
        fill.levels_used[-1][0], edge_per_contract, depth_note,
    )

    stats["traded"] += 1
    _finalize_sample(sample_id, traded=True, skip_reason=None)
    market_fills[market.ticker] = already_filled_this_market + filled_size
    # Lock the Kelly cap in now that a real fill exists -- see the cap's own
    # comment above for why this can't happen before a fill is confirmed.
    market_kelly_caps.setdefault(market.ticker, kelly_cap)
    cost = filled_avg_price * filled_size + fee
    cycle_state["bankroll_dollars"] = bankroll - cost
    cycle_state["contracts_committed"] = already_committed + filled_size

    # Register/update the open position for exit-monitoring (see
    # _check_exit_conditions). Stacking (buying the same market more than
    # once) accumulates contracts here but keeps the *original* entry_z and
    # entry_spot -- later tranches were themselves gated by the same
    # probability checks, so the first entry's values are still a reasonable
    # "what did we believe / where was spot when we started building this
    # position" reference point, and recomputing blended ones buys precision
    # this backstop doesn't need.
    entry_z = _z_for_side(estimate.z, market.direction, estimate.favored_side)
    existing = open_positions.get(market.ticker)
    if existing is None:
        open_positions[market.ticker] = {
            "market": market, "side": estimate.favored_side,
            "contracts": filled_size, "entry_z": entry_z, "entry_spot": spot,
            "exit_attempted": False,
        }
    else:
        existing["contracts"] += filled_size


async def run_forever() -> None:
    spot_feed = SpotFeed()
    ws_feed = KalshiWebsocketFeed()
    order_manager = OrderManager()
    _log_despite_lightweight_mode(
        logging.INFO, "resolution_alpha live loop starting (dry_run=%s, lightweight_mode=%s)",
        order_manager.dry_run, config.LIGHTWEIGHT_MODE,
    )

    if config.SAMPLING_ENABLED:
        sampling.init_db(config.SAMPLING_DB_PATH)
        logger.info("sampling enabled, writing to %s", config.SAMPLING_DB_PATH)

    # Per-shard collateral at startup (Kalshi Exchange Sharding). Order
    # collateral is local to a shard; the crypto interval series this strategy
    # trades sit on nonzero shards, so a shard breakdown that's $0 everywhere
    # but shard 0 means no live entry can succeed until funds are moved
    # (kalshi.com/account/exchange-indexes). Logged loudly here so it's the
    # first thing visible in the log rather than a silent stream of 404s.
    if not order_manager.dry_run:
        try:
            logger.info("per-shard collateral at startup: %s", order_manager.get_shard_balances())
        except Exception:
            logger.exception("could not fetch per-shard balances at startup")

    ws_task = asyncio.create_task(ws_feed.run())

    last_discovery = 0.0
    active_markets: list[ActiveMarket] = []
    cycle_state: dict = {}
    last_cycle_key = None
    stats = _new_stats()
    last_stats_log = time.time()
    # Open-position tracking for exit-monitoring (see _check_exit_conditions) --
    # deliberately run-scoped, not cycle_state, since a position bought right
    # before a cycle boundary must still be watchable after cycle_state resets.
    open_positions: dict = {}

    try:
        while True:
            now_ts = time.time()

            lightweight_trading_phase = True  # meaningless unless LIGHTWEIGHT_MODE is on
            if config.LIGHTWEIGHT_MODE:
                sleep_for = _seconds_until_lightweight_wake(now_ts)
                if sleep_for > 0.0:
                    # Outside both lightweight phases -- do none of the loop's
                    # normal work this tick (see config.LIGHTWEIGHT_MODE's
                    # docstring). Sleeps in one shot straight to phase 1's start
                    # rather than busy-checking every POLL_INTERVAL_SECONDS;
                    # `continue` re-reads now_ts fresh afterward, so the
                    # discovery check right below always sees a stale-enough
                    # last_discovery and fires immediately on the first tick of
                    # phase 1.
                    await asyncio.sleep(sleep_for)
                    continue
                lightweight_trading_phase = _lightweight_trading_phase(now_ts)

            if now_ts - last_discovery >= config.DISCOVERY_INTERVAL_SECONDS:
                try:
                    active_markets = await asyncio.to_thread(find_active_markets, _underlying_scan_filter())
                    logger.info("discovered %d active markets", len(active_markets))
                except Exception:
                    logger.exception("discovery failed, keeping previous market list")
                last_discovery = now_ts

            now = datetime.now(timezone.utc)
            key = _cycle_key(now)
            if key != last_cycle_key:
                cycle_state = {}
                last_cycle_key = key
                # Bankroll snapshot for this cycle's Kelly sizing (see
                # evaluate_and_maybe_trade / _kelly_contracts) -- refreshed once per
                # 15-minute bucket rather than queried on every trade decision, and
                # decremented locally after each fill within the bucket. A real
                # balance query needs credentials; dry-run without them (or in
                # general) uses the configured simulated bankroll instead.
                #
                # None means "couldn't determine a real bankroll this cycle" --
                # evaluate_and_maybe_trade must skip sizing/trading entirely rather
                # than guess (2026-08-13: this used to fall back to
                # DRY_RUN_SIMULATED_BALANCE_DOLLARS on a failed live query, which
                # meant Kelly sizing could size real orders against a fabricated
                # bankroll instead of halting -- same bug caught and fixed in
                # ../btc_implied_prob/strategy.py's _bankroll_dollars).
                if not order_manager.dry_run:
                    try:
                        cycle_state["bankroll_dollars"] = await asyncio.to_thread(order_manager.get_balance_dollars)
                    except Exception:
                        logger.exception("failed to fetch account balance -- skipping sizing/trading this cycle")
                        cycle_state["bankroll_dollars"] = None
                    # Per-shard balances for this cycle's exchange-shard collateral
                    # guard (see evaluate_and_maybe_trade). None -> guard falls
                    # through and lets the real order attempt be the judge.
                    try:
                        cycle_state["shard_balances"] = await asyncio.to_thread(order_manager.get_shard_balances)
                    except Exception:
                        logger.exception("failed to fetch per-shard balances this cycle")
                        cycle_state["shard_balances"] = None
                    _warn_unfunded_shards(active_markets, cycle_state.get("shard_balances"))
                else:
                    cycle_state["bankroll_dollars"] = config.DRY_RUN_SIMULATED_BALANCE_DOLLARS

            # Only ask the websocket to track order books for markets actually
            # approaching close, not every open market -- see
            # config.ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS's docstring for why
            # subscribing to all ~1,600+ open markets at once stalled the loop.
            near_close_tickers = {
                m.ticker for m in active_markets
                if 0 < (m.close_time - now).total_seconds() <= config.ORDERBOOK_SUBSCRIBE_LOOKAHEAD_SECONDS
            }
            ws_feed.set_desired_tickers(near_close_tickers)

            underlyings = {m.underlying for m in active_markets}
            for underlying in underlyings:
                if ws_feed.connected() and ws_feed.index_id_for(underlying):
                    continue  # fed directly by cfbenchmarks_value, no REST poll needed
                await asyncio.to_thread(spot_feed.poll, underlying)

            # In lightweight mode's phase 1 (warm-up), everything above this
            # point still runs -- discovery, ws order-book subscriptions, and
            # spot/index polling -- so realized_vol_per_sqrt_second has real
            # history to work with once phase 2 starts, instead of starting
            # cold with only LIGHTWEIGHT_TRADING_WINDOW_SECONDS to build it up.
            # Evaluation/exit-checking themselves are phase-2-only.
            if lightweight_trading_phase:
                for market in active_markets:
                    try:
                        await evaluate_and_maybe_trade(market, spot_feed, ws_feed, order_manager, cycle_state, stats, open_positions)
                    except Exception:
                        logger.exception("error evaluating %s", market.ticker)

                if open_positions:
                    try:
                        await _check_exit_conditions(open_positions, spot_feed, ws_feed, order_manager)
                    except Exception:
                        logger.exception("error checking exit conditions")

            if now_ts - last_stats_log >= config.STATS_LOG_INTERVAL_SECONDS:
                _log_stats_summary(stats, now_ts - last_stats_log)
                stats = _new_stats()
                last_stats_log = now_ts

            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        ws_task.cancel()


if __name__ == "__main__":
    asyncio.run(run_forever())
