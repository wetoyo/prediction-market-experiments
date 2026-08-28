"""golf_field_alpha strategy: buys YES on a basket of players in a Kalshi
golf outright-winner event, selected by de-vigging the field's own prices
(see selection.py's two methods), sized to a per-event dollar cap, held to
the tournament's resolution.

    python strategy.py            # scan once, print baskets, place nothing
    python strategy.py --execute  # also place YES orders for the chosen basket
                                   # (still simulated unless GOLF_FIELD_ALPHA_DRY_RUN=false
                                   # AND Kalshi creds are set)
    python strategy.py --loop 300 # rescan every 300s until interrupted

Both selection methods are printed on every scan; --execute trades whichever
one config.SELECTION_METHOD names. There is no exit logic -- a golf basket
rides to settlement ($1 back if a bought player wins, $0 if not). See
./README.md for the thesis and known limitations (thin books / partial
fills not modelled, de-vig circularity, roster churn, in-play staleness).

open_positions is keyed by player-market ticker, persisted to
config.POSITIONS_STATE_PATH every --execute tick, reloaded on startup, and
reconciled against the real Kalshi account once at the start of an
--execute run -- see positions_store.py. A leg already held is never
re-bought; a later scan CAN add newly-qualifying legs to the same event's
basket (prices move over a multi-day tournament).
"""

import argparse
import contextlib
import logging
import sys
import time
from datetime import datetime, timezone

# Golf fields are full of accented names (Åberg, Bezuidenhout, ...). The
# Windows console defaults to cp1252 and would raise UnicodeEncodeError
# printing them -- force UTF-8 with replacement so a scan never dies on a
# name it can't encode.
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import positions_store
from discovery import GolfEvent, find_open_golf_events
from order_manager import OrderManager
from selection import BasketPlan, FieldQuote, plan_basket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("golf_field_alpha.strategy")


def _quotes_for_event(event: GolfEvent) -> list[FieldQuote]:
    return [
        FieldQuote(ticker=p.ticker, name=p.name, buy_price=p.buy_price, implied=p.implied)
        for p in event.players
    ]


def _within_trade_window(event: GolfEvent, now: datetime) -> bool:
    secs = event.seconds_to_close(now)
    return config.MIN_SECONDS_TO_CLOSE <= secs <= config.MAX_SECONDS_TO_CLOSE


def _print_event_header(event: GolfEvent, plan: BasketPlan, now: datetime) -> None:
    hrs = event.seconds_to_close(now) / 3600.0
    print(
        f"{event.event_ticker}  ({event.series_ticker}, closes in {hrs:.1f}h)  "
        f"field={plan.field_size} overround={plan.overround:.3f} devig={plan.devig_method}"
    )
    print(f"  {event.title}")


def _print_plan(event: GolfEvent, plan: BasketPlan, now: datetime) -> None:
    if plan.n_legs == 0:
        print(f"  [{plan.method}] no basket -- {plan.reason or 'nothing qualified'}")
        return

    print(
        f"  [{plan.method}] {plan.n_legs} legs, cost ${plan.total_cost:.2f}, "
        f"P(basket wins)~{plan.basket_prob:.3f}, skipped tail~{plan.skipped_prob:.3f}, "
        f"mkt-consistent E[value]~{plan.expected_value:+.3f}"
        + (f", unit={plan.unit_contracts:.0f}" if plan.method == "favorites_basket" else "")
    )
    for leg in sorted(plan.legs, key=lambda x: -x.fair_prob):
        print(
            f"    {leg.name:26s} px={leg.buy_price:.3f} fair={leg.fair_prob:.3f} "
            f"x{leg.contracts:.0f} edge/ct={leg.edge_per_contract:+.4f} cost=${leg.cost:.2f}"
        )


def scan(events: list[GolfEvent], now: datetime) -> dict[str, BasketPlan]:
    """Builds the configured-method basket for every in-window event and
    prints both methods. Returns {event_ticker: plan_for_config_method}.
    """
    plans: dict[str, BasketPlan] = {}
    in_window = [e for e in events if _within_trade_window(e, now)]
    print(
        f"{len(events)} open golf winner event(s), {len(in_window)} inside the "
        f"[{config.MIN_SECONDS_TO_CLOSE / 3600:.0f}h, {config.MAX_SECONDS_TO_CLOSE / 86400:.0f}d] trade window\n"
    )
    for event in in_window:
        quotes = _quotes_for_event(event)
        method_plans = {m: plan_basket(quotes, method=m) for m in ("devig_edge", "favorites_basket")}
        _print_event_header(event, next(iter(method_plans.values())), now)
        for method, plan in method_plans.items():
            _print_plan(event, plan, now)
            if method == config.SELECTION_METHOD:
                plans[event.event_ticker] = plan
        print()
    return plans


def execute(
    events: list[GolfEvent],
    plans: dict[str, BasketPlan],
    open_positions: dict,
    manager: OrderManager,
) -> None:
    events_by_ticker = {e.event_ticker: e for e in events}
    for event_ticker, plan in plans.items():
        if plan.n_legs == 0:
            continue
        event = events_by_ticker.get(event_ticker)
        if event is None:
            continue
        for leg in plan.legs:
            if leg.ticker in open_positions:
                held = open_positions[leg.ticker]["contracts"]
                logger.info("%s (%s): already holding %.0f, skipping", leg.ticker, leg.name, held)
                continue
            try:
                manager.buy_favored_side(
                    ticker=leg.ticker, side="yes", contracts=leg.contracts, limit_price=leg.buy_price,
                )
            except Exception:
                logger.exception("%s (%s): order placement failed, will retry next tick", leg.ticker, leg.name)
                continue
            open_positions[leg.ticker] = {
                "event_ticker": event_ticker,
                "series_ticker": event.series_ticker,
                "name": leg.name,
                "side": "yes",
                "contracts": leg.contracts,
                "entry_price": leg.buy_price,
                "entry_edge": leg.edge_per_contract,
                "entry_fair": leg.fair_prob,
                "method": plan.method,
                "close_time": event.close_time.isoformat(),
                "reconciled": False,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="place YES orders for the chosen basket")
    parser.add_argument("--loop", type=float, default=None, metavar="SECONDS", help="rescan on an interval")
    args = parser.parse_args()

    manager = OrderManager()
    open_positions: dict = positions_store.load(config.POSITIONS_STATE_PATH)
    reconciled = False
    logger.info(
        "golf_field_alpha starting (dry_run=%s, method=%s, edge_threshold=%.3f)",
        manager.dry_run, config.SELECTION_METHOD, config.EDGE_THRESHOLD,
    )

    while True:
        now = datetime.now(timezone.utc)
        try:
            events = find_open_golf_events()
        except Exception:
            logger.exception("discovery failed this tick")
            events = []

        positions_store.prune_closed(open_positions, now)

        if args.execute and not reconciled:
            positions_store.reconcile_with_kalshi(open_positions, manager, events)
            reconciled = True

        plans = scan(events, now)

        if args.execute:
            execute(events, plans, open_positions, manager)
            positions_store.save(config.POSITIONS_STATE_PATH, open_positions)

        if args.loop is None:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
