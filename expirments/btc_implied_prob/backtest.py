"""Historical calibration backtest for btc_implied_prob.

Deribit's public REST API does not expose historical implied-vol surfaces
or option order books -- every function in derbit/fetch_historical.py
(get_instruments, get_order_book, get_book_summary_by_currency, ...) is a
*current-state* snapshot, and this project hasn't been continuously
archiving them long enough to have built a local history either
(Data/derbit.db holds one test row). The one genuinely historical vol series
Deribit's public API exposes is get_historical_volatility -- BTC's own
*realized*-volatility index, hourly buckets, ~16 days back at time of
writing -- which is NOT the live strategy's input (a live per-expiry
*implied*-vol smile read off the current option chain). Realized and
implied vol are correlated but implied usually carries a variance risk
premium above realized, so this backtest tests a related but distinct
model: the identical Black-76 N(d2) formula from black_scholes.py (the same
function strategy.py calls), fed a realized-vol reading instead of a live
options-market-implied one, with the forward approximated by spot (no
historical futures/basis curve is available either -- reasonable for BTC's
usually-small carry, not exact).

Unlike Deribit, Kalshi's own price history IS fetchable -- the current
order book has no historical endpoint, but individual executed fills do
(public GET /markets/trades). So this replays strategy.py's actual entry
condition, not just a bare probability-calibration check: for each settled
market, walk forward tick-by-tick (--tick-seconds apart) from its own
open_time through close_time - MIN_SECONDS_TO_CLOSE (config.py's live entry
gates), pricing the model against the last real trade at-or-before each
tick, and take the first tick where edge_after_fee clears EDGE_THRESHOLD --
same "buy once, first qualifying tick, no re-entry" behavior as
strategy.py's _execute. The one thing this can't reconstruct is a real
historical bid/ask spread (trade prints are a single executed price, not a
two-sided book), so the last trade price stands in for strategy.py's
yes_mid AND yes_ask/yes_bid both -- optimistic versus a real spread cost,
flagged in the output.

A pass here means "the probability math, time-decay handling, and the
buy-mispriced-contracts signal calibrate against history," not "the live
IV-surface edge is real" -- that second claim can only be validated by
letting strategy.py run and log its own predictions going forward (see
live/logs/).

Spot price is Coinbase's free historical 1-minute candles -- same source
and caveats (1-minute granularity, 300-candle/request paging) as
../resolution_alpha/backtest.py.

Usage:
    python backtest.py [--hours 24] [--tick-seconds 30] [--edge-threshold 0.03]
"""

import argparse
import bisect
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from derbit.fetch_historical import get_historical_volatility  # noqa: E402

import config  # noqa: E402
import fees  # noqa: E402
from black_scholes import prob_forward_above_strike, prob_forward_below_strike  # noqa: E402
from kalshi_btc_markets import _extract_strike, _parse_time  # noqa: E402
from kalshi_gateway import BASE_URL, fetch_series, fetch_trades  # noqa: E402
from fetch_historical import _get_with_retry as _kalshi_get_with_retry  # noqa: E402  (Kalshi client, path injected by kalshi_gateway above)

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLE_GRANULARITY_SECONDS = 60
MAX_CANDLES_PER_REQUEST = 300  # Coinbase's per-request cap at 60s granularity = 5h
SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass
class SettledMarket:
    ticker: str
    direction: str
    strike: float
    open_time: datetime
    close_time: datetime
    result: str  # "yes" or "no"


def _fetch_settled_markets_since(series_ticker: str, cutoff: datetime) -> list[dict]:
    """Paginates status="settled" markets for one series, newest-first
    (confirmed against the live API), stopping as soon as an entire page is
    older than `cutoff`. A fixed max_pages (fetch_markets' usual approach)
    either silently truncates a long lookback -- KXBTCD alone settles on the
    order of ~200 markets/hour, so its default max_pages=10 (2000 markets)
    covers barely 10 hours, nowhere near a 30-day backtest -- or, if raised
    naively high, wastes hundreds of pages paging all the way back to the
    series' inception with no cutoff awareness at all. This bounds total
    requests to roughly what the requested window actually needs.
    """
    markets: list[dict] = []
    cursor = None
    for page in range(5000):  # hard ceiling, not expected to bind -- see docstring
        params = {"series_ticker": series_ticker, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = _kalshi_get_with_retry(f"{BASE_URL}/markets", params).json()
        except requests.exceptions.HTTPError as exc:
            print(f"    settled-market fetch failed for {series_ticker} page {page}: {exc}")
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
        time.sleep(0.1)  # be polite between pages on what can be a many-hundred-page scan

    return markets


def find_settled_btc_markets(lookback_hours: float) -> list[SettledMarket]:
    """Mirrors kalshi_btc_markets.find_active_btc_markets's series filter and
    strike parsing exactly, but against status="settled" markets.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    settled: list[SettledMarket] = []

    series_list = fetch_series(category="Crypto")
    qualifying = [
        s for s in series_list
        if s.get("frequency") in config.INTERVAL_FREQUENCIES and "BTC" in [t.upper() for t in (s.get("tags") or [])]
    ]
    print(f"scanning {len(qualifying)} qualifying BTC interval series for settled markets...")

    for series in qualifying:
        markets = _fetch_settled_markets_since(series["ticker"], cutoff)
        print(f"  {series['ticker']}: fetched {len(markets)} settled markets back to cutoff")
        time.sleep(0.5)  # be polite to the shared Kalshi rate limit budget

        kept = 0
        for market in markets:
            result = market.get("result")
            if result not in ("yes", "no"):
                continue
            parsed = _extract_strike(market)
            if parsed is None:
                continue
            direction, strike = parsed
            try:
                open_time = _parse_time(market["open_time"])
                close_time = _parse_time(market["close_time"])
            except (KeyError, ValueError):
                continue
            if close_time < cutoff:
                continue
            settled.append(SettledMarket(
                ticker=market["ticker"], direction=direction, strike=strike,
                open_time=open_time, close_time=close_time, result=result,
            ))
            kept += 1
        if kept:
            print(f"  {series['ticker']}: {kept} settled markets in lookback window")

    return settled


def fetch_candles(start: datetime, end: datetime) -> list[tuple[float, float]]:
    """Returns [(epoch_seconds, close_price), ...] ascending by time, at
    1-minute granularity, paging Coinbase's public historical-candles
    endpoint backwards in <=300-candle (5h) chunks.
    """
    candles: list[tuple[float, float]] = []
    page_end = end
    while page_end > start:
        page_start = max(start, page_end - timedelta(seconds=MAX_CANDLES_PER_REQUEST * CANDLE_GRANULARITY_SECONDS))
        try:
            response = requests.get(
                COINBASE_CANDLES_URL,
                params={
                    "granularity": CANDLE_GRANULARITY_SECONDS,
                    "start": page_start.isoformat(),
                    "end": page_end.isoformat(),
                },
                headers={"User-Agent": "btc_implied_prob-backtest"},
                timeout=20,
            )
            response.raise_for_status()
            for row in response.json():
                ts, _low, _high, _open, close, _volume = row
                # Label by bucket end (ts + granularity), not bucket start --
                # the close price isn't knowable until the bucket ends, so a
                # later "candles with timestamp <= decision_epoch" filter
                # can't leak up to 60s of future price into a decision point.
                candles.append((float(ts) + CANDLE_GRANULARITY_SECONDS, float(close)))
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"    candle fetch failed [{page_start}..{page_end}]: {exc}")
        page_end = page_start
        time.sleep(0.1)  # be polite to Coinbase's public endpoint

    candles.sort(key=lambda c: c[0])
    return candles


def _parse_trade_time(value: str) -> datetime:
    """Kalshi trade timestamps carry variable-precision fractional seconds
    (e.g. "...53.34723Z", 5 digits, not always 6) -- pad/truncate to exactly
    6 before handing to fromisoformat rather than relying on a specific
    Python version's leniency about fraction length.
    """
    value = value[:-1] + "+00:00" if value.endswith("Z") else value
    if "." in value:
        base, rest = value.split(".", 1)
        tz_start = next(i for i, c in enumerate(rest) if c in "+-")
        frac, tz = rest[:tz_start], rest[tz_start:]
        value = f"{base}.{frac.ljust(6, '0')[:6]}{tz}"
    return datetime.fromisoformat(value)


def fetch_trade_prices(ticker: str, open_time: datetime, close_time: datetime) -> list[tuple[float, float]]:
    """Returns [(epoch_seconds, yes_price), ...] ascending by time from
    Kalshi's real trade history for one settled market -- the actual price
    the market traded at, not a proxy. A single executed price stands in for
    both sides of a historical bid/ask spread (untrackable -- the order book
    has no historical endpoint), so this is optimistic versus what a real
    marketable order would have paid across the spread.
    """
    raw = fetch_trades(
        ticker, min_ts=int(open_time.timestamp()) - 5, max_ts=int(close_time.timestamp()) + 5,
    )
    prices: list[tuple[float, float]] = []
    for trade in raw:
        try:
            ts = _parse_trade_time(trade["created_time"]).timestamp()
            yes_price = float(trade["yes_price_dollars"])
        except (KeyError, ValueError):
            continue
        prices.append((ts, yes_price))
    prices.sort(key=lambda p: p[0])
    return prices


def _value_at_or_before(series: list[tuple[float, float]], epoch_seconds: float) -> float | None:
    """Last (timestamp, value) in an ascending-by-timestamp series with
    timestamp <= epoch_seconds, or None if the series doesn't reach back
    that far.
    """
    timestamps = [t for t, _ in series]
    i = bisect.bisect_right(timestamps, epoch_seconds) - 1
    return series[i][1] if i >= 0 else None


@dataclass
class Trial:
    market: SettledMarket
    entry_epoch: float
    seconds_to_close: float
    favored_side: str
    favored_probability: float
    trade_price: float
    edge_after_fee: float
    fee_dollars: float
    correct: bool  # favored_side matched the actual result
    pnl_per_contract: float  # (1.0 if correct else 0.0) - trade_price - fee, i.e. buy-and-hold-to-settlement P&L


def find_first_signal(
    market: SettledMarket,
    candles: list[tuple[float, float]],
    hv_series: list[tuple[float, float]],
    trade_prices: list[tuple[float, float]],
    tick_seconds: float,
    edge_threshold: float,
) -> Trial | None:
    """Walks forward tick-by-tick through this market's own real entry
    window -- [open_time, close_time - MIN_SECONDS_TO_CLOSE], further capped
    to MAX_SECONDS_TO_CLOSE, exactly strategy.py's _evaluate gate -- and
    returns the first tick where the model-vs-market edge clears
    edge_threshold, same "buy once on the first qualifying tick, no
    re-entry" behavior as strategy.py's _execute. None if no tick in the
    window ever qualifies (including if the window is empty, or the market
    has no usable trade price at any tick).
    """
    close_epoch = market.close_time.timestamp()
    window_start = max(market.open_time.timestamp(), close_epoch - config.MAX_SECONDS_TO_CLOSE)
    window_end = close_epoch - config.MIN_SECONDS_TO_CLOSE

    tick = window_start
    while tick <= window_end:
        spot = _value_at_or_before(candles, tick)
        sigma_pct = _value_at_or_before(hv_series, tick)
        market_yes_price = _value_at_or_before(trade_prices, tick)

        if spot is None or sigma_pct is None or sigma_pct <= 0:
            tick += tick_seconds
            continue
        if market_yes_price is None or not (0.0 < market_yes_price < 1.0):
            tick += tick_seconds
            continue  # no real trade yet at this point, or a degenerate print -- can't price a fill against it

        sigma = sigma_pct / 100.0
        seconds_to_close = close_epoch - tick
        years_to_expiry = max(seconds_to_close, 0.0) / SECONDS_PER_YEAR
        if market.direction == "above":
            model_prob_yes = prob_forward_above_strike(spot, market.strike, sigma, years_to_expiry)
        else:
            model_prob_yes = prob_forward_below_strike(spot, market.strike, sigma, years_to_expiry)

        if model_prob_yes > market_yes_price:
            favored_side, favored_probability, trade_price = "yes", model_prob_yes, market_yes_price
        else:
            favored_side, favored_probability, trade_price = "no", 1.0 - model_prob_yes, 1.0 - market_yes_price

        fee_dollars = fees.estimate_fee_dollars(trade_price, 1.0)
        edge_after_fee = (favored_probability - trade_price) - fee_dollars
        if edge_after_fee < edge_threshold:
            tick += tick_seconds
            continue

        correct = favored_side == market.result
        return Trial(
            market=market,
            entry_epoch=tick,
            seconds_to_close=seconds_to_close,
            favored_side=favored_side,
            favored_probability=favored_probability,
            trade_price=trade_price,
            edge_after_fee=edge_after_fee,
            fee_dollars=fee_dollars,
            correct=correct,
            pnl_per_contract=(1.0 if correct else 0.0) - trade_price - fee_dollars,
        )

    return None


# Lower bound is 0.0, not 0.5: favored_probability is whichever side the model
# disagrees with the *market price* on (see find_first_signal), same as
# strategy.py's _evaluate -- unlike a raw "which side does the model itself
# think is more likely" call, that's not bounded below by 0.5 (the model can
# favor a side it only gives 30% to, if the market is pricing it even lower).
PROB_BUCKETS = [(0.0, 0.90), (0.90, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 0.995), (0.995, 1.0)]


def print_calibration_table(trials: list[Trial]) -> None:
    print(f"{'model prob bucket':<20}{'n':>6}{'actual win rate':>18}{'avg model prob':>16}")
    for lo, hi in PROB_BUCKETS:
        bucket = [t for t in trials if lo <= t.favored_probability < hi]
        if not bucket:
            continue
        win_rate = sum(t.correct for t in bucket) / len(bucket)
        avg_model_prob = sum(t.favored_probability for t in bucket) / len(bucket)
        print(f"[{lo:.3f},{hi:.3f})".ljust(20) + f"{len(bucket):>6}{win_rate:>17.1%}{avg_model_prob:>16.4f}")


def _trial_to_json(trial: Trial) -> dict:
    return {
        "favored_side": trial.favored_side,
        "favored_probability": trial.favored_probability,
        "trade_price": trial.trade_price,
        "edge_after_fee": trial.edge_after_fee,
        "fee_dollars": trial.fee_dollars,
        "seconds_to_close": trial.seconds_to_close,
        "correct": trial.correct,
        "pnl_per_contract": trial.pnl_per_contract,
    }


def _trial_from_json(d: dict) -> Trial:
    return Trial(
        market=None, entry_epoch=0.0,
        seconds_to_close=d["seconds_to_close"], favored_side=d["favored_side"],
        favored_probability=d["favored_probability"], trade_price=d["trade_price"],
        edge_after_fee=d["edge_after_fee"], fee_dollars=d["fee_dollars"],
        correct=d["correct"], pnl_per_contract=d["pnl_per_contract"],
    )


def _load_checkpoint(checkpoint_path: Path, valid_tickers: set[str]) -> tuple[set[str], list[Trial], int, int]:
    """Resumes a prior run of this exact --hours/--tick-seconds/--edge-threshold
    combination (the checkpoint filename encodes all three, see run()) --
    one crashed run (network blip, killed process, etc.) on a job spanning
    hours and ~10^5 requests shouldn't mean starting over from market 1. Each
    line is one already-processed ticker's outcome; a market with no trades
    and one that had trades but never cleared edge_threshold are both
    recorded (signal: null) so they're correctly skipped on resume too, not
    just successful signals.

    `valid_tickers` is this run's freshly-discovered settled-market set:
    find_settled_btc_markets' cutoff is relative to datetime.now(), so a
    resumed run's window has slid forward by however long since the
    original run started. A checkpointed ticker that's aged out of the
    new window is still skipped (harmless -- it just won't be in `settled`
    either), but its stats are excluded here so no_signal_count + len(trials)
    stays exactly equal to len(settled), not inflated by markets outside
    the window this particular run is actually reporting on.
    """
    processed: set[str] = set()
    trials: list[Trial] = []
    no_signal_count = 0
    no_trades_count = 0
    if not checkpoint_path.exists():
        return processed, trials, no_signal_count, no_trades_count

    with checkpoint_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            processed.add(row["ticker"])  # skip re-fetching regardless of window drift
            if row["ticker"] not in valid_tickers:
                continue  # aged out of this run's window -- don't count it
            if not row["had_trades"]:
                no_trades_count += 1
            if row["signal"] is None:
                no_signal_count += 1
            else:
                trials.append(_trial_from_json(row["signal"]))
    return processed, trials, no_signal_count, no_trades_count


def run(hours: float, tick_seconds: float, edge_threshold: float, checkpoint_path: Path) -> None:
    settled = find_settled_btc_markets(hours)
    print(f"\n{len(settled)} total settled BTC interval markets in the last {hours}h\n")
    if not settled:
        print("nothing to backtest -- widen --hours")
        return

    start = min(m.open_time for m in settled) - timedelta(minutes=2)
    end = max(m.close_time for m in settled) + timedelta(minutes=1)
    print(f"fetching BTC-USD candles: {start} .. {end}")
    candles = fetch_candles(start, end)

    print("fetching Deribit BTC historical (realized) volatility...")
    hv_raw = get_historical_volatility(config.DERIBIT_CURRENCY)
    hv_series = sorted((ts_ms / 1000.0, value) for ts_ms, value in hv_raw)
    if hv_series:
        print(f"  {len(hv_series)} points, {datetime.fromtimestamp(hv_series[0][0], tz=timezone.utc)} "
              f".. {datetime.fromtimestamp(hv_series[-1][0], tz=timezone.utc)}")

    processed, trials, no_signal_count, no_trades_count = _load_checkpoint(
        checkpoint_path, {m.ticker for m in settled},
    )
    if processed:
        print(f"resuming from checkpoint {checkpoint_path}: {len(processed)} markets already processed")
    remaining = [m for m in settled if m.ticker not in processed]

    print(f"fetching real Kalshi trade history for {len(remaining)} settled markets ({len(settled)} total, "
          f"{len(processed)} already done)...")
    with checkpoint_path.open("a", encoding="utf-8") as ckpt:
        for i, market in enumerate(remaining):
            trade_prices = fetch_trade_prices(market.ticker, market.open_time, market.close_time)
            had_trades = bool(trade_prices)
            if not had_trades:
                no_trades_count += 1
            time.sleep(0.05)  # be polite to the shared Kalshi rate limit budget

            trial = find_first_signal(market, candles, hv_series, trade_prices, tick_seconds, edge_threshold)
            if trial is None:
                no_signal_count += 1
            else:
                trials.append(trial)

            ckpt.write(json.dumps({
                "ticker": market.ticker, "had_trades": had_trades,
                "signal": _trial_to_json(trial) if trial is not None else None,
            }) + "\n")
            ckpt.flush()  # survive a hard kill, not just a clean exit -- this is the whole point of checkpointing

            if (i + 1) % 100 == 0:
                print(f"  ...{i + 1}/{len(remaining)} remaining markets processed "
                      f"({len(processed) + i + 1}/{len(settled)} total)")

    print(
        f"\n{len(settled)} settled markets: {no_trades_count} had no real trades at all, "
        f"{no_signal_count} never cleared edge_threshold={edge_threshold} at any tick, "
        f"{len(trials)} would-have-traded signals\n"
    )
    if not trials:
        print("no signals fired -- nothing to report")
        return

    print(f"=== calibration among the {len(trials)} fired signals ===")
    print_calibration_table(trials)

    win_rate = sum(t.correct for t in trials) / len(trials)
    avg_edge = sum(t.edge_after_fee for t in trials) / len(trials)
    avg_pnl = sum(t.pnl_per_contract for t in trials) / len(trials)
    total_pnl = sum(t.pnl_per_contract for t in trials)
    avg_seconds_to_close = sum(t.seconds_to_close for t in trials) / len(trials)
    print(
        f"\nwould-have-traded (buy first tick edge_after_fee >= {edge_threshold}): {len(trials)} signals, "
        f"win rate {win_rate:.1%}, avg edge_after_fee {avg_edge:+.4f}, "
        f"avg entry {avg_seconds_to_close:.0f}s before close\n"
        f"buy-and-hold-to-settlement P&L per contract: avg {avg_pnl:+.4f}, total {total_pnl:+.2f} "
        f"(1 contract per signal; last-trade price stands in for a real fill, so this is optimistic "
        f"versus actually crossing a historical spread)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24.0, help="lookback window of settled markets to test")
    parser.add_argument(
        "--tick-seconds", type=float, default=30.0,
        help="how often strategy.py would have re-scanned each market for a signal (its --loop cadence)",
    )
    parser.add_argument(
        "--edge-threshold", type=float, default=None,
        help="model-vs-market edge (after fees) required to signal a trade -- defaults to config.EDGE_THRESHOLD",
    )
    parser.add_argument(
        "--checkpoint", type=str, default=None,
        help="path to a JSONL checkpoint file -- re-running with the same --hours/--tick-seconds/--edge-threshold "
             "(hence the same default path) resumes instead of re-fetching already-processed markets. Needed for "
             "any run spanning more than a few thousand markets: a single network blip hours in otherwise means "
             "starting over from scratch, see fetch_historical.py's _get_with_retry docstring for the incident.",
    )
    args = parser.parse_args()

    edge_threshold = args.edge_threshold if args.edge_threshold is not None else config.EDGE_THRESHOLD
    if args.checkpoint:
        checkpoint_path = Path(args.checkpoint)
    else:
        checkpoint_path = Path(f"backtest_runs/checkpoint_{args.hours:g}h_{args.tick_seconds:g}s_{edge_threshold:g}edge.jsonl")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    run(args.hours, args.tick_seconds, edge_threshold, checkpoint_path)
