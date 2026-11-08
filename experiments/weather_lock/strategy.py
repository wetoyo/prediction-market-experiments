"""weather_lock live runner: buys the side of a Kalshi daily temperature
market that the NWS observations have already decided.

    python strategy.py              # scan once, print locked markets, place nothing
    python strategy.py --execute    # also buy them (simulated unless
                                    # WEATHER_LOCK_DRY_RUN=false and Kalshi creds are set)
    python strategy.py --loop 60    # repeat every 60s

Each tick: discover open KXHIGH*/KXLOW* markets, pull recent reports for
every station with a market on today (or an earlier, unsettled day), work
out the bounds the reports prove (nws_obs.day_bounds), and buy any locked
side priced below 1 - MIN_EDGE after fees (lock.py). Positions ride to
settlement; there's no exit logic. See README.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import positions_store
from discovery import TempMarket, find_open_markets, find_temperature_series
from lock import locked_side, plan_entry
from nws_obs import DayBounds, day_bounds, fetch_readings
from order_manager import OrderManager, OrderRefused

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("weather_lock.strategy")


def _log_signal(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(config.SIGNALS_LOG_PATH), exist_ok=True)
        with open(config.SIGNALS_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.exception("could not append to %s", config.SIGNALS_LOG_PATH)


def compute_bounds(markets: list[TempMarket], now: datetime) -> dict[tuple[str, object], DayBounds]:
    """Bounds for every (station, day) that has an open market on a day
    that has started. One NWS request per station."""
    wanted: dict[str, set] = defaultdict(set)
    stations = {}
    for m in markets:
        if m.day <= m.station.climate_date(now):
            wanted[m.station.icao].add(m.day)
            stations[m.station.icao] = m.station
    out = {}
    for icao, days in wanted.items():
        try:
            readings = fetch_readings(stations[icao], user_agent=config.NWS_USER_AGENT)
        except Exception:
            logger.exception("%s: NWS observations failed this tick", icao)
            continue
        for d in days:
            out[(icao, d)] = day_bounds(stations[icao], d, readings, config.MIN_CONFIRMING_OBS, now=now)
    return out


def _caps_left(m: TempMarket, side: str, positions: dict, bankroll: float) -> tuple[float, float, str]:
    """(contracts, dollars) this market may still take, or a reason it can't."""
    held = positions.get(m.ticker)
    if held and held["side"] != side:
        return 0, 0, f"already hold the {held['side']} side"
    held_n = held["contracts"] if held else 0.0
    held_cost = held["cost"] if held else 0.0
    contracts = config.MAX_CONTRACTS_PER_MARKET - held_n
    dollars = min(
        config.MAX_COST_PER_MARKET_DOLLARS - held_cost,
        config.MAX_COST_PER_EVENT_DOLLARS - positions_store.event_cost(positions, m.event_ticker),
        config.MAX_TOTAL_OPEN_COST_DOLLARS - positions_store.total_cost(positions),
        bankroll * config.MAX_BANKROLL_FRACTION_PER_ORDER,
    )
    if contracts < 1 or dollars <= 0:
        return 0, 0, "caps reached"
    return contracts, dollars, ""


def tick(markets: list[TempMarket], bounds: dict, positions: dict, manager: OrderManager | None,
         bankroll: float, now: datetime) -> int:
    """Evaluates every market; buys locked sides when `manager` is given.
    Returns the number of locked markets seen."""
    n_locked = 0
    for m in sorted(markets, key=lambda x: x.ticker):
        b = bounds.get((m.station.icao, m.day))
        if b is None:
            continue
        side = locked_side(m, b, config.LOCK_MARGIN_F)
        if side is None:
            continue
        n_locked += 1
        ask, ask_size = m.ask(side)
        contracts, dollars, why = _caps_left(m, side, positions, bankroll)
        entry = None
        if not why:
            entry, why = plan_entry(m, side, config.MIN_EDGE, contracts, dollars)
        bound = b.high_at_least if m.kind == "high" else b.low_at_most
        signal = {
            "ts": now.isoformat(timespec="seconds"), "ticker": m.ticker, "kind": m.kind, "day": m.day.isoformat(),
            "station": m.station.icao, "strike_type": m.strike_type, "floor": m.floor, "cap": m.cap,
            "bound_f": bound, "side": side, "ask": ask, "ask_size": ask_size,
            "last_report": b.last_report, "day_complete": b.complete,
        }
        action = "skip: " + why if entry is None else (
            f"BUY {entry.contracts:.0f} {side.upper()} <= {entry.limit_price:.2f} (ask {entry.best_ask:.2f}, "
            f"edge {entry.edge_at_limit:.3f})")
        print(f"  {m.ticker:28s} {m.kind:4s} obs {'>=' if m.kind == 'high' else '<='}{bound}F  "
              f"{m.subtitle:16s} {side.upper():3s} locked  ask={ask if ask is not None else '-'}  {action}")

        filled = 0.0
        if entry is not None and manager is not None:
            try:
                filled = manager.buy(m.ticker, side, entry.contracts, entry.limit_price, m.exchange_index)
            except OrderRefused:
                filled = 0.0
            except Exception:
                logger.exception("%s: order failed, will retry next tick", m.ticker)
                filled = 0.0
            if filled > 0:
                frac = filled / entry.contracts
                positions_store.add_fill(positions, m.ticker, side, filled, entry.worst_cost * frac,
                                         m.event_ticker, m.settle_time)
        signal.update({"action": action, "filled": filled})
        _log_signal(signal)
    return n_locked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="place orders (dry-run unless configured live)")
    parser.add_argument("--loop", type=float, default=None, metavar="SECONDS", help="rescan on an interval")
    args = parser.parse_args()

    manager = OrderManager() if args.execute else None
    live = manager is not None and not manager.dry_run
    if live:
        manager.start_ledger()
    # Dry-run fills are pretend; keep them out of the live state file.
    state_path = config.POSITIONS_STATE_PATH if live else config.POSITIONS_STATE_PATH + ".dryrun"
    positions = positions_store.load(state_path)
    if manager is not None:
        positions_store.reconcile(positions, manager)
    logger.info("weather_lock starting (execute=%s, dry_run=%s, margin=%.1fF, min_edge=%.3f)",
                args.execute, manager.dry_run if manager else True, config.LOCK_MARGIN_F, config.MIN_EDGE)

    series: list[str] = []
    series_at = 0.0
    while True:
        now = datetime.now(timezone.utc)
        if not series or time.time() - series_at > config.SERIES_REFRESH_SECONDS:
            try:
                series = find_temperature_series(config.SERIES_ALLOWLIST, config.SERIES_BLOCKLIST)
                series_at = time.time()
                logger.info("watching %d temperature series", len(series))
            except Exception:
                logger.exception("series discovery failed")

        markets = find_open_markets(series)
        # Drop series with no market we can parse (unknown station), so later ticks skip them.
        parsed = {m.series_ticker for m in markets}
        series = [s for s in series if s in parsed] or series

        positions_store.prune_settled(positions, now)
        bounds = compute_bounds(markets, now)

        bankroll = config.DRY_RUN_SIMULATED_BALANCE_DOLLARS
        if live:
            try:
                bankroll = manager.bankroll_dollars()
            except Exception:
                logger.exception("could not read the balance -- not trading this tick")
                bankroll = 0.0

        print(f"\n{now:%Y-%m-%d %H:%M:%S}Z  {len(markets)} open markets, {len(bounds)} station-days observed, "
              f"open cost ${positions_store.total_cost(positions):.2f}, bankroll ${bankroll:.2f}")
        n = tick(markets, bounds, positions, manager, bankroll, now)
        print(f"  {n} locked market(s)")
        sys.stdout.flush()

        if manager is not None:
            try:
                positions_store.save(state_path, positions)
            except OSError:
                logger.exception("could not save positions")

        if args.loop is None:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
