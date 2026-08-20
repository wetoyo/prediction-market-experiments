"""btc_implied_prob strategy: prices Kalshi's BTC above/below interval
markets against a Deribit-options-implied probability (see deribit_iv.py's
Black-76 model) and signals a trade wherever the two disagree by more than
EDGE_THRESHOLD after estimated fees.

    python strategy.py            # scan once, print signals, dry-run only
    python strategy.py --execute  # also place orders for qualifying signals
                                   # (still simulated unless config.DRY_RUN is
                                   # explicitly false AND Kalshi creds are set)
    python strategy.py --loop 30  # rescan every 30s until interrupted

Model probability comes from Deribit's *current* option chain, i.e. the
market's forward-looking view of BTC volatility over the relevant window --
not a backward-looking realized-vol estimate. See ./README.md for the full
methodology and known limitations (settlement-averaging window, Deribit/CF
Benchmarks index basis risk, thin near-dated smiles).

Exit is a config toggle (config.ENABLE_TRAILING_EXIT, added 2026-08-13,
off by default): every position used to just ride to settlement ($1 or $0),
buy-only, no exit logic at all. When enabled, _check_exit_conditions
re-checks each open position every tick against a fresh Deribit-implied
probability and the market's current quote, and exits (buys the opposite
side to flatten) once either the edge that justified entering has closed to
config.EXIT_EDGE_THRESHOLD or below (take-profit) or price has given back
config.TRAILING_STOP_DROP from its best point since entry (trailing stop,
also a plain stop-loss on a position that never improves) -- trailing-stop
has its own separate toggle, config.ENABLE_TRAILING_STOP (added 2026-08-15,
also off by default), so take-profit can run without it. Exiting costs a
second fee on top of the entry fee -- see config.ENABLE_TRAILING_EXIT's
docstring.

open_positions is tracked unconditionally (not just when ENABLE_TRAILING_EXIT
is on) and persisted to config.POSITIONS_STATE_PATH every tick, reloaded on
startup, and reconciled against Kalshi's real account state once at the start
of a --execute run -- see positions_store.py. Added 2026-08-15 after a live
incident: with tracking gated on the exit toggle, a running process had no
record of tickers it already held and re-bought the same handful on every
single loop tick for ~80 minutes before anyone noticed.
"""

import argparse
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import deribit_iv
import fees
import positions_store
from kalshi_btc_markets import ActiveMarket, find_active_btc_markets
from order_manager import OrderManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("btc_implied_prob.strategy")


@dataclass
class TradeSignal:
    market: ActiveMarket
    seconds_to_close: float
    model_prob_yes: float
    yes_mid: float
    favored_side: str  # "yes" or "no"
    favored_probability: float  # model probability that favored_side wins
    trade_price: float  # dollars, cost to buy one contract of favored_side
    edge_after_fee: float
    extrapolated: bool


def _evaluate(market: ActiveMarket, surface: deribit_iv.Surface, now: datetime) -> TradeSignal | None:
    seconds_to_close = (market.close_time - now).total_seconds()
    if not (config.MIN_SECONDS_TO_CLOSE <= seconds_to_close <= config.MAX_SECONDS_TO_CLOSE):
        return None

    spread = market.yes_ask - market.yes_bid
    if market.yes_ask <= 0.0 or market.yes_ask >= 1.0 or spread > config.MAX_SPREAD or spread < 0:
        return None  # no usable two-sided quote

    estimate = deribit_iv.estimate_probability(
        surface, direction=market.direction, strike=market.strike, seconds_to_expiry=seconds_to_close,
    )
    yes_mid = (market.yes_bid + market.yes_ask) / 2.0

    if estimate.prob_yes > yes_mid:
        favored_side = "yes"
        favored_probability = estimate.prob_yes
        trade_price = market.yes_ask
    else:
        favored_side = "no"
        favored_probability = 1.0 - estimate.prob_yes
        trade_price = 1.0 - market.yes_bid
    edge_raw = favored_probability - trade_price

    fee_per_contract = fees.estimate_fee_dollars(trade_price, 1.0)
    edge_after_fee = edge_raw - fee_per_contract

    return TradeSignal(
        market=market,
        seconds_to_close=seconds_to_close,
        model_prob_yes=estimate.prob_yes,
        yes_mid=yes_mid,
        favored_side=favored_side,
        favored_probability=favored_probability,
        trade_price=trade_price,
        edge_after_fee=edge_after_fee,
        extrapolated=estimate.extrapolated,
    )


def scan(markets: list[ActiveMarket], surface: deribit_iv.Surface) -> list[TradeSignal]:
    """Evaluates every given market against the Deribit-implied model,
    returning signals sorted best-edge-first. Takes markets/surface as
    params (fetched once per tick by main(), or ad hoc by a caller) rather
    than fetching them internally, so the same (comparatively expensive)
    Deribit surface build + BTC market discovery pass can also feed
    _check_exit_conditions in the same tick without duplicating those calls.
    """
    now = datetime.now(timezone.utc)
    signals = []
    for market in markets:
        signal = _evaluate(market, surface, now)
        if signal is not None:
            signals.append(signal)

    signals.sort(key=lambda s: s.edge_after_fee, reverse=True)
    return signals


def _print_signals(signals: list[TradeSignal]) -> None:
    candidates = [s for s in signals if s.edge_after_fee >= config.EDGE_THRESHOLD]
    print(f"{len(signals)} markets passed liquidity/time filters, {len(candidates)} clear EDGE_THRESHOLD")
    if not candidates:
        return
    print(f"{'ticker':28s} {'side':4s} {'model':>7s} {'mkt_mid':>7s} {'price':>7s} {'edge':>7s} closes_in")
    for s in candidates:
        flag = "*" if s.extrapolated else " "
        print(
            f"{s.market.ticker:28s} {s.favored_side:4s} {s.model_prob_yes:7.4f} {s.yes_mid:7.4f} "
            f"{s.trade_price:7.4f} {s.edge_after_fee:+7.4f}{flag} {s.seconds_to_close:.0f}s"
        )
    if any(s.extrapolated for s in candidates):
        print("* target close time falls outside Deribit's listed expiry range (extrapolated smile) -- "
              "these are skipped by --execute, see README Limitations")


def _kelly_contracts(favored_probability: float, price: float, bankroll_dollars: float) -> float:
    """Kelly position size, in contracts (not yet floored to an integer or
    clipped to config.MAX_CONTRACTS_PER_TRADE -- the caller does that).

    Kelly-optimal fraction of bankroll to stake (as total dollar cost) on a
    binary contract priced at `price` with model probability
    `favored_probability` of paying out $1 is (p - price) / (1 - price),
    scaled by config.KELLY_FRACTION. See ../resolution_alpha/live/runner.py's
    _kelly_contracts, which this mirrors, for the same formula validated
    against real trades.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    edge_prob = favored_probability - price
    if edge_prob <= 0.0:
        return 0.0
    full_kelly_fraction_of_bankroll = edge_prob / (1.0 - price)
    stake_dollars = config.KELLY_FRACTION * full_kelly_fraction_of_bankroll * bankroll_dollars
    return stake_dollars / price


def _bankroll_dollars(manager: OrderManager) -> float | None:
    """None means "couldn't determine a real bankroll" -- callers must skip
    sizing/trading entirely rather than guess, since guessing wrong (e.g.
    falling back to a value far above the real balance) would oversize
    positions against real money. Only dry-run gets a stand-in number,
    because dry-run never places a real order regardless of size.
    """
    if manager.dry_run:
        return config.DRY_RUN_SIMULATED_BALANCE_DOLLARS
    try:
        return manager.get_balance_dollars()
    except Exception:
        logger.exception("failed to fetch account balance -- skipping sizing/execution this scan")
        return None


def _place_resting_exit_order(
    ticker: str, position: dict, target_price: float, manager: OrderManager,
) -> None:
    """Places a limit order on the exit side priced at `target_price`
    (converted from held-side terms) away from the current market, so it
    rests on Kalshi's book rather than filling immediately -- the exchange's
    own matching engine then executes it the instant price actually reaches
    that level, instead of waiting for the next loop tick (config-default
    60s) to notice and place a marketable order. Mutates
    position["tp_order_price"] to the newly-placed target on success (left
    untouched on failure, so the caller's next-tick comparison correctly
    still sees the old/no target and retries).
    """
    exit_side = "no" if position["side"] == "yes" else "yes"
    exit_price = round(1.0 - target_price, 2)
    try:
        manager.buy_favored_side(
            ticker=ticker, side=exit_side, contracts=position["contracts"], limit_price=exit_price,
        )
    except Exception:
        logger.exception("%s: failed to place resting take-profit order, will retry next tick", ticker)
        return
    position["tp_order_price"] = target_price
    logger.info(
        "%s: resting take-profit order (re)placed -- %s x%.2f @ %.4f (target held-side price %.4f)",
        ticker, exit_side, position["contracts"], exit_price, target_price,
    )


def _cancel_resting_orders(ticker: str, manager: OrderManager) -> None:
    """Best-effort: cancels every currently-resting order on this ticker.
    In practice this strategy only ever rests at most one (the take-profit
    order) -- same "assume it's ours, entries always fully fill" posture as
    the rest of this module. A cancel racing an order that just filled fails
    harmlessly (Kalshi rejects canceling an already-filled order); caught and
    logged rather than raised, since a stale/failed cancel here shouldn't
    abort the rest of that position's exit-check this tick.
    """
    for order in manager.get_resting_orders(ticker):
        try:
            manager.cancel_order(order["order_id"])
        except Exception:
            logger.exception("%s: failed to cancel resting order %s", ticker, order.get("order_id"))


def _maintain_take_profit_order(
    ticker: str, open_positions: dict, model_prob_held: float, manager: OrderManager,
) -> None:
    """Live-only (see _check_exit_conditions -- dry-run has no real resting
    orders to track, and keeps the simpler tick-driven simulated take-profit
    check instead). Ensures a resting exit-side order sits at the current
    take-profit target, repricing it (cancel + replace) if the target has
    drifted since it was last placed -- the target itself isn't static, since
    model_prob_held moves with time decay and Deribit's IV surface itself
    moving between ticks. Detects a fill (the previously-placed order is no
    longer in Kalshi's resting list, and this call didn't just cancel it
    itself to reprice) by deleting the position from open_positions outright
    -- Kalshi's matching engine, not this tick, is what actually executed the
    exit.

    Known residual race: if the trailing-stop check (same tick, right after
    this returns) decides to force a marketable exit, it cancels this resting
    order first (see _check_exit_conditions), but a fill landing in the
    narrow window between that cancel call and Kalshi processing it could
    still both execute -- Kalshi's API has no atomic bracket/OCO order type
    to close this gap entirely. Not addressed here; would need per-position
    order-id tracking plus a post-trade position-size reconciliation to catch.
    """
    position = open_positions[ticker]
    resting = manager.get_resting_orders(ticker)

    target = model_prob_held - config.EXIT_EDGE_THRESHOLD
    target_price = round(target, 2) if 0.0 < target < 1.0 else None

    if not resting:
        if position["tp_order_price"] is not None:
            logger.warning(
                "%s: resting take-profit order (target %.4f) no longer resting -- treating as filled, "
                "closing position", ticker, position["tp_order_price"],
            )
            del open_positions[ticker]
            return
        if target_price is None:
            return  # nothing resting yet, and no valid target to place one at this tick
        _place_resting_exit_order(ticker, position, target_price, manager)
        return

    if target_price is not None and position["tp_order_price"] != target_price:
        _cancel_resting_orders(ticker, manager)
        _place_resting_exit_order(ticker, position, target_price, manager)
    # else: something's resting and, as far as we know, still at the right price -- leave it.


def _execute(signals: list[TradeSignal], open_positions: dict, manager: OrderManager) -> None:
    """Places orders for qualifying signals and registers each into
    open_positions -- unconditionally, not just when config.ENABLE_TRAILING_EXIT
    is on. That gating used to live here (removed 2026-08-15): with it off,
    this function had no record of tickers it already held, so a signal that
    kept clearing EDGE_THRESHOLD tick after tick got bought again every
    single time, with no cap besides the account running out of free cash --
    confirmed live: ~750 contracts accumulated across 7 strikes in one event
    over ~80 minutes of unattended looping. A ticker already in
    open_positions is now skipped outright rather than stacked into, which
    also means entry_edge/peak_price no longer need an update-in-place branch
    -- a ticker only ever gets registered once, the first time it's bought.
    """
    bankroll_dollars = _bankroll_dollars(manager)
    if bankroll_dollars is None:
        return
    for s in signals:
        if s.edge_after_fee < config.EDGE_THRESHOLD:
            continue
        if s.extrapolated:
            logger.info("skipping %s: target time falls outside Deribit's listed expiry range", s.market.ticker)
            continue
        if s.market.ticker in open_positions:
            logger.info(
                "%s: already holding %.2f contracts, skipping re-entry",
                s.market.ticker, open_positions[s.market.ticker]["contracts"],
            )
            continue

        raw_contracts = _kelly_contracts(s.favored_probability, s.trade_price, bankroll_dollars)
        contracts = math.floor(min(raw_contracts, config.MAX_CONTRACTS_PER_TRADE))
        if contracts < 1:
            logger.info(
                "%s kelly size rounds to 0 contracts (raw=%.3f, bankroll=%.2f), skipping",
                s.market.ticker, raw_contracts, bankroll_dollars,
            )
            continue

        manager.buy_favored_side(
            ticker=s.market.ticker,
            side=s.favored_side,
            contracts=contracts,
            limit_price=s.trade_price,
        )

        position = {
            "market": s.market, "side": s.favored_side,
            "contracts": float(contracts), "entry_edge": s.edge_after_fee,
            "entry_price": s.trade_price,  # immutable, for the exit-fee log line
            "peak_price": s.trade_price,  # trailing-stop reference, see config.TRAILING_STOP_DROP -- mutated
            "reconciled": False,
            "tp_order_price": None,  # set by _place_resting_exit_order once a resting order is live
        }
        open_positions[s.market.ticker] = position

        # Live only -- dry-run has no real order book to rest an order on, and keeps the
        # simpler tick-driven simulated take-profit in _check_exit_conditions instead. Placed
        # here (using the model probability this signal was already computed against) rather
        # than waiting for the first _check_exit_conditions tick, to minimize the window where
        # a fast favorable move right after entry has no resting order protecting it yet.
        if config.ENABLE_TRAILING_EXIT and not manager.dry_run:
            entry_target = s.favored_probability - config.EXIT_EDGE_THRESHOLD
            if 0.0 < entry_target < 1.0:
                _place_resting_exit_order(s.market.ticker, position, round(entry_target, 2), manager)


def _check_exit_conditions(
    open_positions: dict,
    markets_by_ticker: dict[str, ActiveMarket],
    surface: deribit_iv.Surface,
    manager: OrderManager,
) -> None:
    """Always prunes settled positions out of open_positions (regardless of
    config.ENABLE_TRAILING_EXIT), since a position past its close_time is
    just dead weight in the tracked dict -- pointless to keep persisting or
    reconciling against once it's resolved. The actual take-profit/trailing-
    stop *exit* logic below that, however, is a no-op unless
    ENABLE_TRAILING_EXIT is set -- see that config's docstring for why it's
    off by default (unvalidated, and exiting costs a second fee on top of the
    entry fee). When enabled, for each still-open position: recompute the
    Deribit-implied probability for the side actually held at the market's
    *current* seconds-to-expiry, and that side's current market price, from
    this tick's already-fetched market list (markets_by_ticker) -- no extra
    network call. Exit (buy the opposite side to flatten) once either:
      - current edge (model probability for the held side minus its current
        price) has closed to config.EXIT_EDGE_THRESHOLD or below
        (take-profit), or
      - current price has given back config.TRAILING_STOP_DROP from its best
        (highest) point since entry (position["peak_price"], trailing stop) --
        gated by its own config.ENABLE_TRAILING_STOP toggle (added 2026-08-15,
        off by default, independent of ENABLE_TRAILING_EXIT itself): with it
        off, take-profit is the only exit path and a position that just
        drifts against the entry edge without ever clearing EXIT_EDGE_THRESHOLD
        rides to settlement instead of being stopped out.

    Unlike resolution_alpha's equivalent, this doesn't track partial fills:
    buy_favored_side here (no order-book depth walk, just top-of-book
    yes_bid/yes_ask) has no fill-size signal to key a retry off of, so a
    successful order placement is treated as a full fill and the position is
    closed outright -- matching this module's existing entry-side buys,
    which make the same assumption. A failed placement is logged and retried
    next tick, still bounded to whatever contracts remain.

    Take-profit itself splits on manager.dry_run: dry-run has no real order
    book to rest an order on, so it keeps the original tick-driven simulated
    check (current_edge <= EXIT_EDGE_THRESHOLD triggers a simulated
    marketable exit immediately, same as before 2026-08-15). Live instead
    delegates to _maintain_take_profit_order, which places/reprices an actual
    resting order and lets Kalshi's matching engine execute it -- see that
    function's docstring for why (up to a full loop tick, default 60s via
    --loop, of latency between price crossing the trigger and this process
    noticing it, versus the exchange reacting immediately). Trailing-stop stays
    tick-driven either way -- its trigger price itself moves with
    position["peak_price"], so a resting order would need the same
    every-tick repricing this loop already does, with no latency benefit
    over just placing the marketable exit directly.
    """
    now = datetime.now(timezone.utc)
    for ticker, position in list(open_positions.items()):
        market = position["market"]
        seconds_left = (market.close_time - now).total_seconds()
        if seconds_left <= 0:
            del open_positions[ticker]
            continue

        if not config.ENABLE_TRAILING_EXIT:
            continue  # tracked (for dedup/persistence/reconciliation) but not exit-monitored

        current_market = markets_by_ticker.get(ticker)
        if current_market is None:
            continue  # not in this tick's discovery pass (rare -- delisted/settled between ticks)
        # Both sides checked, not just whichever this position's side derives
        # its price from: yes_bid defaults to 0.0 when Kalshi's response
        # omits it (see kalshi_btc_markets.ActiveMarket), and a "no"-side
        # current_price is 1.0-yes_bid -- an unchecked missing yes_bid would
        # silently read as a too-good-to-be-true current_price of 1.0 instead
        # of being skipped, same failure mode _evaluate() already guards
        # against on the entry side.
        if (
            current_market.yes_ask <= 0.0 or current_market.yes_ask >= 1.0
            or current_market.yes_bid <= 0.0 or current_market.yes_bid >= 1.0
        ):
            continue  # no usable two-sided quote right now

        try:
            estimate = deribit_iv.estimate_probability(
                surface, direction=market.direction, strike=market.strike, seconds_to_expiry=seconds_left,
            )
        except ValueError:
            continue  # empty surface this tick (see scan()'s caller) -- try again next tick

        model_prob_held = estimate.prob_yes if position["side"] == "yes" else 1.0 - estimate.prob_yes
        # What this position could actually be sold for right now, not what it costs to buy
        # more of: a held "yes" position is worth yes_bid (what a buyer's currently offering),
        # not yes_ask (what a seller's currently asking) -- and symmetrically for "no". Fixed
        # 2026-08-15 (previously used yes_ask/1-yes_bid, i.e. the wrong side of the spread, an
        # overly optimistic current_price that also mismatched the exit_price actually used
        # below when a marketable exit fires -- found while deriving the resting take-profit
        # order's target price, which has to match true realizable value to mean anything).
        current_price = (
            current_market.yes_bid if position["side"] == "yes" else 1.0 - current_market.yes_ask
        )
        position["peak_price"] = max(position["peak_price"], current_price)

        current_edge = model_prob_held - current_price
        trailing_drop = position["peak_price"] - current_price

        if manager.dry_run:
            if current_edge <= config.EXIT_EDGE_THRESHOLD:
                reason = "take_profit"
            elif config.ENABLE_TRAILING_STOP and trailing_drop >= config.TRAILING_STOP_DROP:
                reason = "trailing_stop"
            else:
                continue  # neither condition met (or trailing-stop is off), keep holding
        else:
            _maintain_take_profit_order(ticker, open_positions, model_prob_held, manager)
            if ticker not in open_positions:
                continue  # the resting take-profit order filled -- _maintain_take_profit_order closed it
            if config.ENABLE_TRAILING_STOP and trailing_drop >= config.TRAILING_STOP_DROP:
                reason = "trailing_stop"
                _cancel_resting_orders(ticker, manager)  # avoid a double-exit race, see docstring
            else:
                continue  # take-profit is the resting order's job now; nothing left to check here

        exit_side = "no" if position["side"] == "yes" else "yes"
        exit_price = current_market.yes_ask if exit_side == "yes" else 1.0 - current_market.yes_bid
        contracts = position["contracts"]

        entry_fee = fees.estimate_fee_dollars(position["entry_price"], contracts)
        exit_fee = fees.estimate_fee_dollars(exit_price, contracts)
        # entry_edge is None for a reconciled position (positions_store.reconcile_with_kalshi --
        # no local record of what edge originally justified a position discovered on the real
        # account) rather than one this process actually entered itself.
        entry_edge_str = f"{position['entry_edge']:.4f}" if position["entry_edge"] is not None else "unknown(reconciled)"
        logger.warning(
            "%s EXIT TRIGGER (%s): held %s x%.2f, entry_edge=%s current_edge=%.4f peak_price=%.4f "
            "current_price=%.4f (trailing_drop=%.4f, entry_fee=%.4f exit_fee=%.4f total_fee=%.4f) "
            "-- attempting sell, %.0fs left",
            ticker, reason, position["side"], contracts, entry_edge_str, current_edge,
            position["peak_price"], current_price, trailing_drop, entry_fee, exit_fee,
            entry_fee + exit_fee, seconds_left,
        )

        try:
            manager.buy_favored_side(
                ticker=ticker, side=exit_side, contracts=contracts, limit_price=exit_price,
            )
        except Exception:
            logger.exception("%s exit attempt: order placement failed, will retry next tick", ticker)
            continue

        logger.warning("%s exit sell filled: %.2f contracts @ %.4f", ticker, contracts, exit_price)
        del open_positions[ticker]  # assumed full fill -- see docstring


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="place orders for qualifying signals")
    parser.add_argument("--loop", type=float, default=None, metavar="SECONDS", help="rescan on an interval")
    args = parser.parse_args()

    manager = OrderManager()
    # Loaded regardless of --execute (harmless if empty/absent in dry-run) so a restart
    # doesn't forget positions a previous --execute run already registered. See
    # positions_store's module docstring for the incident this is a response to.
    open_positions: dict = positions_store.load(config.POSITIONS_STATE_PATH)
    reconciled = False

    while True:
        surface = deribit_iv.build_surface(config.DERIBIT_CURRENCY)
        if not surface.expiries:
            logger.warning("Deribit surface has no usable expiries -- no live option quotes to price against")
            markets: list = []
        else:
            markets = find_active_btc_markets()
        markets_by_ticker = {m.ticker: m for m in markets}

        if args.execute and not reconciled:
            # Once, on startup, before the first _execute -- picks up any real position
            # Kalshi shows that this process has no local record of (see positions_store.
            # reconcile_with_kalshi's docstring).
            positions_store.reconcile_with_kalshi(open_positions, manager, markets_by_ticker)
            reconciled = True

        signals = scan(markets, surface) if markets else []
        _print_signals(signals)
        qualifying = [s for s in signals if s.edge_after_fee >= config.EDGE_THRESHOLD]
        if args.execute and qualifying:
            _execute(qualifying, open_positions, manager)

        if args.execute and open_positions:
            _check_exit_conditions(open_positions, markets_by_ticker, surface, manager)

        if args.execute:
            positions_store.save(config.POSITIONS_STATE_PATH, open_positions)

        if args.loop is None:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
