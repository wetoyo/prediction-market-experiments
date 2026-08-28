"""Historical backtest for golf_field_alpha.

For every settled Kalshi golf outright-winner event in the lookback window,
this replays selection.py's basket construction at one or more decision
points before the tournament closed, then settles the basket against the
real winner (`result == "yes"`). It imports and runs the exact live
selection/de-vig/fee code -- a result here is "the live basket logic, on
real historical prices, would have done X", not "some similar rule would".

What it uses for prices: Kalshi's `/markets/trades` endpoint -- real
executed fills, which DO have history (the order book itself has no
historical endpoint, same limitation as the other two experiments). At
each decision point, each player's price is its last real trade at or
before that timestamp. That single price stands in for BOTH the de-vig
`implied` input AND the `buy_price` you'd pay -- there is no historical
bid/ask spread to reconstruct, so this is optimistic versus actually
crossing a spread on ~20-40 legs. Flagged again in the output.

What it does NOT model:
  - Order-book depth / partial fills. A basket leg is assumed to fill in
    full at the last trade price. Golf player books are thin; real
    slippage on a multi-leg basket is unmeasured.
  - Roster churn between the decision point and close (withdrawals resolve
    a player NO early). A player with no trade at/before the decision point
    is treated as untradeable and excluded -- including, sometimes, the
    eventual winner (reported separately as "winner not yet tradeable").
  - Re-entry over the tournament. Live, a later scan can add newly-
    qualifying legs; here each decision point is a single independent
    snapshot. Run multiple --decision-hours to see the time profile.

Sizing is deliberately fixed at 1 contract per leg here (both methods), so
the headline P&L / ROI is independent of the Kelly / unit-sizing config --
that measures the raw alpha of the *selection*, which is the question.

Usage:
    python backtest.py [--days 240] [--decision-hours 72,24]
                       [--method both] [--devig-method proportional]
                       [--edge-threshold 0.03] [--series KXPGATOUR,KXKFTOUR]
"""

import argparse
import bisect
import contextlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from discovery import _tournament_title  # noqa: E402
from fees import estimate_fee_dollars  # noqa: E402
from kalshi_gateway import BASE_URL, _get_with_retry, fetch_series, fetch_trades  # noqa: E402
from selection import FieldQuote, plan_basket  # noqa: E402

CHECKPOINT_DIR = Path(__file__).resolve().parent / "backtest_runs"


# --------------------------------------------------------------------------
# Discovery of settled events
# --------------------------------------------------------------------------

@dataclass
class SettledPlayer:
    ticker: str
    name: str


@dataclass
class SettledEvent:
    event_ticker: str
    series_ticker: str
    title: str
    open_time: datetime
    close_time: datetime
    winner_ticker: str
    winner_name: str
    players: list[SettledPlayer] = field(default_factory=list)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_trade_time(value: str) -> datetime:
    """Kalshi trade timestamps carry variable-precision fractional seconds;
    pad/truncate to exactly 6 before fromisoformat. Copied from
    ../btc_implied_prob/backtest.py.
    """
    value = value[:-1] + "+00:00" if value.endswith("Z") else value
    if "." in value:
        base, rest = value.split(".", 1)
        tz_start = next((i for i, c in enumerate(rest) if c in "+-"), len(rest))
        frac, tz = rest[:tz_start], rest[tz_start:]
        value = f"{base}.{frac.ljust(6, '0')[:6]}{tz}"
    return datetime.fromisoformat(value)


def _player_name(market: dict) -> str:
    name = market.get("yes_sub_title")
    if name:
        return name
    strike = market.get("custom_strike") or {}
    for v in strike.values():
        if v:
            return str(v)
    return market.get("ticker", "?")


def _fetch_settled_markets_since(series_ticker: str, cutoff: datetime) -> list[dict]:
    """Paginates status='settled' markets for one series (newest first),
    stopping once an entire page closed before `cutoff`. Same shape as
    ../btc_implied_prob/backtest.py's helper.
    """
    markets: list[dict] = []
    cursor = None
    for page in range(2000):
        params = {"series_ticker": series_ticker, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = _get_with_retry(f"{BASE_URL}/markets", params).json()
        except requests.exceptions.HTTPError as exc:
            print(f"    settled fetch failed for {series_ticker} page {page}: {exc}")
            break
        page_markets = payload.get("markets", [])
        if not page_markets:
            break
        markets.extend(page_markets)
        try:
            oldest_close = min(_parse_time(m["close_time"]) for m in page_markets if "close_time" in m)
        except ValueError:
            oldest_close = None
        cursor = payload.get("cursor")
        if not cursor or (oldest_close is not None and oldest_close < cutoff):
            break
        time.sleep(0.1)
    return markets


def find_settled_winner_events(lookback_days: float, series_filter: set[str] | None) -> list[SettledEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    series_list = fetch_series(category="Sports")
    want = {s.upper() for s in (series_filter or config.WINNER_SERIES)}
    qualifying = [s for s in series_list if s.get("ticker", "").upper() in want]
    print(f"scanning {len(qualifying)} winner series for settled events back to {cutoff.date()}...")

    events: dict[str, SettledEvent] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    series_of: dict[str, str] = {}

    for series in qualifying:
        st = series["ticker"]
        markets = _fetch_settled_markets_since(st, cutoff)
        print(f"  {st}: {len(markets)} settled markets fetched")
        time.sleep(0.3)
        for m in markets:
            ev = m.get("event_ticker")
            if not ev:
                continue
            grouped[ev].append(m)
            series_of[ev] = st

    for ev, mkts in grouped.items():
        winners = [m for m in mkts if m.get("result") == "yes"]
        if len(winners) != 1:
            continue  # not a clean single-winner event (voided / miscreated)
        try:
            close_time = max(_parse_time(m["close_time"]) for m in mkts if "close_time" in m)
            open_time = min(_parse_time(m["open_time"]) for m in mkts if "open_time" in m)
        except (KeyError, ValueError):
            continue
        if close_time < cutoff:
            continue
        w = winners[0]
        events[ev] = SettledEvent(
            event_ticker=ev,
            series_ticker=series_of[ev],
            title=_tournament_title(w.get("title", ""), ev),
            open_time=open_time,
            close_time=close_time,
            winner_ticker=w["ticker"],
            winner_name=_player_name(w),
            players=[SettledPlayer(ticker=m["ticker"], name=_player_name(m)) for m in mkts],
        )

    ordered = sorted(events.values(), key=lambda e: e.close_time)
    print(f"\n{len(ordered)} clean single-winner events in the last {lookback_days:g} days\n")
    return ordered


# --------------------------------------------------------------------------
# Trade history (checkpointed per event)
# --------------------------------------------------------------------------

def _load_or_fetch_trades(event: SettledEvent) -> dict[str, list[tuple[float, float]]]:
    """Returns {player_ticker: [(epoch, yes_price), ...] ascending}. Cached
    to backtest_runs/<event_ticker>.json so re-running a sweep with
    different --decision-hours / --edge-threshold doesn't re-fetch tens of
    thousands of trades.
    """
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{event.event_ticker}.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {k: [(float(ts), float(px)) for ts, px in v] for k, v in raw["trades"].items()}
        except (json.JSONDecodeError, KeyError, OSError, ValueError):
            print(f"  {event.event_ticker}: bad checkpoint, refetching")

    min_ts = int(event.open_time.timestamp()) - 5
    max_ts = int(event.close_time.timestamp()) + 5
    trades: dict[str, list[tuple[float, float]]] = {}
    for i, pl in enumerate(event.players):
        raw = fetch_trades(pl.ticker, min_ts=min_ts, max_ts=max_ts, max_pages=30)
        series: list[tuple[float, float]] = []
        for t in raw:
            try:
                ts = _parse_trade_time(t["created_time"]).timestamp()
                px = float(t["yes_price_dollars"])
            except (KeyError, ValueError):
                continue
            if 0.0 < px < 1.0:
                series.append((ts, px))
        series.sort()
        trades[pl.ticker] = series
        time.sleep(0.03)
        if (i + 1) % 50 == 0:
            print(f"    {event.event_ticker}: fetched trades for {i + 1}/{len(event.players)} players")

    path.write_text(json.dumps({
        "meta": {
            "event_ticker": event.event_ticker, "series_ticker": event.series_ticker,
            "open_time": event.open_time.isoformat(), "close_time": event.close_time.isoformat(),
            "winner_ticker": event.winner_ticker, "winner_name": event.winner_name,
        },
        "trades": {k: [[ts, px] for ts, px in v] for k, v in trades.items()},
    }))
    return trades


def _price_at_or_before(series: list[tuple[float, float]], epoch: float) -> float | None:
    ts_only = [ts for ts, _ in series]
    i = bisect.bisect_right(ts_only, epoch) - 1
    return series[i][1] if i >= 0 else None


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

@dataclass
class BasketResult:
    event_ticker: str
    series_ticker: str
    method: str
    decision_hours: float
    n_legs: int
    total_cost_per_unit: float       # sum(price_i + fee_i(price_i, 1)) over legs
    predicted_win_prob: float        # plan.basket_prob (de-vigged)
    won: bool                        # winner_ticker among the legs
    winner_tradeable: bool           # winner had a trade at/before the decision point
    pnl_per_unit: float              # (1 if won else 0) - total_cost_per_unit
    reason: str = ""                 # if n_legs == 0


def evaluate_event(
    event: SettledEvent,
    trades: dict[str, list[tuple[float, float]]],
    decision_hours: float,
    method: str,
    devig_method: str,
    edge_threshold: float,
    max_overround: float,
    min_field_size: float,
) -> BasketResult | None:
    close_ts = event.close_time.timestamp()
    decision_ts = close_ts - decision_hours * 3600.0
    if decision_ts < event.open_time.timestamp():
        return None  # tournament wasn't open yet this far out

    quotes: list[FieldQuote] = []
    for pl in event.players:
        px = _price_at_or_before(trades.get(pl.ticker, []), decision_ts)
        if px is None:
            continue
        quotes.append(FieldQuote(ticker=pl.ticker, name=pl.name, buy_price=px, implied=px))

    winner_tradeable = _price_at_or_before(trades.get(event.winner_ticker, []), decision_ts) is not None

    plan = plan_basket(
        quotes, method=method, edge_threshold=edge_threshold, devig_method=devig_method,
        max_overround=max_overround, min_field_size=min_field_size,
        # 1 contract per leg -> sizing-agnostic headline. Give Kelly a big
        # bankroll and a per-player cap of 1 so devig_edge picks legs by
        # edge sign, not by size, and every chosen leg is exactly 1 contract.
        bankroll=1e9, kelly_fraction=1.0, max_contracts_per_player=1, max_event_cost=1e9,
    )

    if plan.n_legs == 0:
        return BasketResult(
            event_ticker=event.event_ticker, series_ticker=event.series_ticker, method=method,
            decision_hours=decision_hours, n_legs=0, total_cost_per_unit=0.0,
            predicted_win_prob=0.0, won=False, winner_tradeable=winner_tradeable,
            pnl_per_unit=0.0, reason=plan.reason,
        )

    leg_tickers = {leg.ticker for leg in plan.legs}
    cost_per_unit = sum(leg.buy_price + estimate_fee_dollars(leg.buy_price, 1.0) for leg in plan.legs)
    won = event.winner_ticker in leg_tickers
    return BasketResult(
        event_ticker=event.event_ticker, series_ticker=event.series_ticker, method=method,
        decision_hours=decision_hours, n_legs=plan.n_legs, total_cost_per_unit=cost_per_unit,
        predicted_win_prob=plan.basket_prob, won=won, winner_tradeable=winner_tradeable,
        pnl_per_unit=(1.0 if won else 0.0) - cost_per_unit,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _report(results: list[BasketResult], decision_hours_list: list[float], methods: list[str]) -> None:
    for method in methods:
        print(f"\n{'=' * 78}\n=== method: {method} ===")
        for h in decision_hours_list:
            rs = [r for r in results if r.method == method and r.decision_hours == h]
            traded = [r for r in rs if r.n_legs > 0]
            print(f"\n--- decision point: T-{h:g}h before close  "
                  f"({len(rs)} events in range, {len(traded)} produced a basket) ---")
            if not traded:
                reasons = defaultdict(int)
                for r in rs:
                    reasons[r.reason or "?"] += 1
                for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
                    print(f"    no basket: {reason} ({n})")
                continue

            wins = sum(r.won for r in traded)
            n = len(traded)
            total_cost = sum(r.total_cost_per_unit for r in traded)
            total_pnl = sum(r.pnl_per_unit for r in traded)
            avg_legs = sum(r.n_legs for r in traded) / n
            avg_cost = total_cost / n
            avg_pred = sum(r.predicted_win_prob for r in traded) / n
            hit_rate = wins / n
            roi = total_pnl / total_cost if total_cost else 0.0
            winner_untradeable = sum(1 for r in traded if not r.winner_tradeable)
            losses_from_untradeable_winner = sum(
                1 for r in traded if not r.won and not r.winner_tradeable
            )

            print(f"    baskets            : {n}")
            print(f"    avg legs / basket  : {avg_legs:.1f}")
            print(f"    avg cost / unit    : ${avg_cost:.4f}   (1 contract per leg)")
            print(f"    basket hit rate    : {hit_rate:.1%}  ({wins}/{n})")
            print(f"    predicted win prob : {avg_pred:.1%}   (de-vigged; gap {avg_pred - hit_rate:+.1%})")
            print(f"    avg P&L / unit     : {total_pnl / n:+.4f}")
            print(f"    total P&L (1u each): {total_pnl:+.3f}  over ${total_cost:.2f} deployed")
            print(f"    ROI                : {roi:+.1%}   "
                  f"(last-trade price stands in for the ask -> optimistic vs crossing a spread)")
            print(f"    winner untradeable at decision point: {winner_untradeable}/{n} baskets "
                  f"({losses_from_untradeable_winner} of them lost purely for that reason)")

        # per-series breakdown at the tightest decision point
        tightest = min(decision_hours_list)
        by_series: dict[str, list[BasketResult]] = defaultdict(list)
        for r in results:
            if r.method == method and r.decision_hours == tightest and r.n_legs > 0:
                by_series[r.series_ticker].append(r)
        if by_series:
            print(f"\n    per-series @ T-{tightest:g}h:")
            for st, rs in sorted(by_series.items()):
                tc = sum(r.total_cost_per_unit for r in rs)
                tp = sum(r.pnl_per_unit for r in rs)
                w = sum(r.won for r in rs)
                print(f"      {st:16s} n={len(rs):3d}  hit={w / len(rs):5.1%}  "
                      f"P&L={tp:+.3f}  ROI={(tp / tc if tc else 0):+.1%}")


def run(args) -> None:
    series_filter = {s.strip().upper() for s in args.series.split(",")} if args.series else None
    events = find_settled_winner_events(args.days, series_filter)
    if not events:
        print("nothing to backtest -- widen --days")
        return

    decision_hours_list = [float(x) for x in args.decision_hours.split(",")]
    methods = ["devig_edge", "favorites_basket"] if args.method == "both" else [args.method]

    results: list[BasketResult] = []
    for i, event in enumerate(events):
        print(f"[{i + 1}/{len(events)}] {event.event_ticker} ({event.series_ticker}) "
              f"{len(event.players)} players, winner = {event.winner_name}")
        trades = _load_or_fetch_trades(event)
        for h in decision_hours_list:
            for method in methods:
                r = evaluate_event(
                    event, trades, h, method, args.devig_method, args.edge_threshold,
                    args.max_overround, args.min_field_size,
                )
                if r is not None:
                    results.append(r)

    _report(results, decision_hours_list, methods)
    print("\nnote: no order-book depth / partial-fill / spread modelling, and a player with no "
          "trade before the decision point (sometimes the eventual winner) is excluded. See module docstring.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=float, default=240.0, help="lookback window of settled events")
    parser.add_argument("--decision-hours", type=str, default="72,24",
                        help="comma-separated hours-before-close decision points")
    parser.add_argument("--method", choices=["devig_edge", "favorites_basket", "both"], default="both")
    parser.add_argument("--devig-method", choices=["proportional", "power"], default=config.DEVIG_METHOD)
    parser.add_argument("--edge-threshold", type=float, default=config.EDGE_THRESHOLD)
    parser.add_argument("--max-overround", type=float, default=config.MAX_OVERROUND,
                        help="skip an event whose field's implied probs sum to more than this")
    parser.add_argument("--min-field-size", type=float, default=config.MIN_FIELD_SIZE)
    parser.add_argument("--series", type=str, default=None,
                        help="comma-separated subset of WINNER_SERIES to test (default: all)")
    run(parser.parse_args())
