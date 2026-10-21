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

Every position rides to settlement ($1 or $0) -- no take-profit or stop-loss
(both removed 2026-10-21). Instead, with config.ENABLE_PERP_HEDGE on, each
tick hedger.PerpHedger offsets the open positions' combined BTC delta with
Kalshi's BTC perpetual -- see hedger.py.

open_positions is tracked unconditionally and persisted to config.POSITIONS_STATE_PATH every tick, reloaded on
startup, and reconciled against Kalshi's real account state once at the start
of a --execute run -- see positions_store.py. Added 2026-08-15 after a live
incident: with tracking gated on a since-removed exit toggle, a process had no
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
from hedger import PerpHedger
from kalshi_btc_markets import ActiveMarket, find_active_btc_markets
from order_manager import OrderManager, OrderRefused

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
    Deribit surface build + BTC market discovery pass can also feed the
    perp hedge in the same tick without duplicating those calls.
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


def _execute(signals: list[TradeSignal], open_positions: dict, manager: OrderManager) -> None:
    """Places orders for qualifying signals and registers each into
    open_positions -- unconditionally. Registration used to be gated on an
    exit toggle (removed 2026-08-15): with it off, this function had no
    record of tickers it already held, so a signal that kept clearing
    EDGE_THRESHOLD tick after tick got bought again every single time, with no cap besides the account running out of free cash --
    confirmed live: ~750 contracts accumulated across 7 strikes in one event
    over ~80 minutes of unattended looping. A ticker already in
    open_positions is now skipped outright rather than stacked into -- a
    ticker only ever gets registered once, the first time it's bought.
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

        try:
            manager.buy_favored_side(
                ticker=s.market.ticker,
                side=s.favored_side,
                contracts=contracts,
                limit_price=s.trade_price,
            )
        except OrderRefused:
            continue  # over this runner's ledger cash; logged by order_manager, nothing placed

        open_positions[s.market.ticker] = {
            "market": s.market, "side": s.favored_side,
            "contracts": float(contracts), "entry_edge": s.edge_after_fee,
            "entry_price": s.trade_price, "reconciled": False,
        }


def _prune_settled(open_positions: dict) -> None:
    """Drops positions past their close_time -- settled (or settling), so
    there's nothing left to track, persist, reconcile or hedge.
    """
    now = datetime.now(timezone.utc)
    for ticker, position in list(open_positions.items()):
        if (position["market"].close_time - now).total_seconds() <= 0:
            del open_positions[ticker]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="place orders for qualifying signals")
    parser.add_argument("--loop", type=float, default=None, metavar="SECONDS", help="rescan on an interval")
    args = parser.parse_args()

    manager = OrderManager()
    if args.execute and not manager.dry_run:
        # Per-runner ledger (config.py, "Per-runner bankroll"): first sync now,
        # before the reconciliation and the first order, then in the background.
        manager.start_ledger()
    # Loaded regardless of --execute (harmless if empty/absent in dry-run) so a restart
    # doesn't forget positions a previous --execute run already registered. See
    # positions_store's module docstring for the incident this is a response to.
    open_positions: dict = positions_store.load(config.POSITIONS_STATE_PATH)
    reconciled = False
    hedger = PerpHedger(manager) if config.ENABLE_PERP_HEDGE else None

    while True:
        surface = deribit_iv.build_surface(config.DERIBIT_CURRENCY)
        if not surface.expiries:
            logger.warning("Deribit surface has no usable expiries -- no live option quotes to price against")
            markets: list = []
        else:
            markets = find_active_btc_markets()
        if config.EXCLUDE_SERIES:
            markets = [m for m in markets if m.series_ticker.upper() not in config.EXCLUDE_SERIES]
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

        if args.execute:
            _prune_settled(open_positions)
            # Every tick, even with nothing open: a hedge left over from settled
            # positions (or a restart) gets unwound to flat. Skipped without a
            # surface -- no deltas to hedge against, so don't trade blind.
            if hedger is not None and surface.expiries:
                hedger.rebalance(open_positions, surface)
            positions_store.save(config.POSITIONS_STATE_PATH, open_positions)

        if args.loop is None:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
