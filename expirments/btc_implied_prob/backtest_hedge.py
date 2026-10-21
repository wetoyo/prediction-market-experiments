"""Backtest of the perp delta hedge (hedger.py) against history: does
hedging with KXBTCPERP help, and what does the perp's leverage buy?

Same signal replay as backtest.py (realized-vol Black-76 vs real Kalshi
trade prints, first qualifying tick, hold to settlement -- read its
docstring for what that does and doesn't validate), run twice per market:

  plain   -- the pre-hedge strategy: edge net of Kalshi's fee only.
  hedged  -- edge also net of the expected hedge cost
             (hedger.hedge_cost_per_contract, the live function, priced off
             the real perp quote and latest funding rate at that minute).

Each signal is then held to settlement with and without a simulated hedge,
using the live rules (hedger.target_perp_contracts, the rebalance band, the
stop window) on Kalshi's own KXBTCPERP history: 1-minute candlesticks (real
bid/ask closes -- buys fill at the ask, sells at the bid -- and trade
high/low for intra-minute moves) and the real 8-hourly funding payments.
Delta comes from the same realized-vol model the signal used. The perp
taker fee is swept over --perp-fees (Kalshi's filed crypto-perp schedules
run 1-12bp of notional by volume tier; the account's real rate needs auth),
and since it also changes which signals clear the hedge-cost-aware entry
rule, that rule is replayed once per fee.

Leverage doesn't change the hedge's P&L -- only how much cash sits on the
margin instance and whether a move can liquidate it. Kalshi doesn't cross-
margin the perp with the binaries: a move that makes the binary worth more
(on the event side, paid only at settlement) is a loss on the perp side
right now. So for each effective leverage (exchange max leverage /
margin buffer) the run replays the live top-up/sweep rule (top up below
0.75x the buffer, sweep above 1.5x; 1.5/2/3 at the live default 2x), checks
every minute's adverse extreme against the liquidation margin
(notional / the exchange's leverage estimate), and reports peak capital
(binary cost + margin, across overlapping positions) and liquidations.

Not modeled: netting deltas across simultaneously open positions (each
signal is hedged on its own, which overstates turnover), Kelly sizing (every
signal is --contracts binaries), what a liquidation itself costs, and
historical leverage limits (today's are used).

Usage:
    python backtest_hedge.py [--hours 48] [--contracts 10] [--perp-fees 0,0.0004,0.0012]
"""

import argparse
import bisect
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import backtest as bt
import config
import hedger
from black_scholes import digital_above_delta
from derbit.fetch_historical import get_historical_volatility
from kalshi_gateway import BASE_URL, fetch_margin_market

PERP = config.PERP_HEDGE_TICKER
STEP_SECONDS = 60.0  # the live loop's cadence, and the candle resolution
MAX_QUOTE_AGE = 300.0  # a perp candle older than this doesn't count as a quote
hedger_contract_btc = 0.0001  # KXBTCPERP contract size


class Series:
    """Ascending (ts, value) pairs with an at-or-before lookup."""

    def __init__(self, rows):
        rows = sorted(rows, key=lambda r: r[0])
        self.ts = [r[0] for r in rows]
        self.rows = rows

    def at(self, epoch, max_age=None):
        i = bisect.bisect_right(self.ts, epoch) - 1
        if i < 0 or (max_age is not None and epoch - self.ts[i] > max_age):
            return None
        return self.rows[i]

    def between(self, lo, hi):
        """Rows with lo < ts <= hi."""
        return self.rows[bisect.bisect_right(self.ts, lo):bisect.bisect_right(self.ts, hi)]


def fetch_perp_candles(start: datetime, end: datetime) -> Series:
    """(end_ts, bid, ask, low, high) per minute, a day per request."""
    rows = []
    t = int(start.timestamp())
    stop = int(end.timestamp())
    while t < stop:
        chunk_end = min(t + 86400, stop)
        resp = requests.get(
            f"{BASE_URL}/margin/markets/{PERP}/candlesticks",
            params={"period_interval": 1, "start_ts": t, "end_ts": chunk_end}, timeout=30,
        )
        resp.raise_for_status()
        for c in resp.json().get("candlesticks", []):
            try:
                bid, ask = float(c["bid"]["close"]), float(c["ask"]["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 < bid <= ask):
                continue
            price = c.get("price") or {}
            low = float(price["low"]) if price.get("low") else bid
            high = float(price["high"]) if price.get("high") else ask
            rows.append((float(c["end_period_ts"]), bid, ask, min(low, bid), max(high, ask)))
        t = chunk_end
        time.sleep(0.1)
    return Series(rows)


def fetch_funding(start: datetime, end: datetime) -> Series:
    """(ts, rate, mark per contract) per funding event (Kalshi returns <= 60 per call)."""
    rows = {}
    t = int(start.timestamp()) - 86400
    stop = int(end.timestamp())
    while t < stop:
        chunk_end = min(t + 15 * 86400, stop)
        resp = requests.get(
            f"{BASE_URL}/margin/funding_rates/historical",
            params={"ticker": PERP, "start_ts": t, "end_ts": chunk_end}, timeout=30,
        )
        resp.raise_for_status()
        for f in resp.json().get("funding_rates", []):
            ts = datetime.fromisoformat(f["funding_time"].replace("Z", "+00:00")).timestamp()
            rows[ts] = (ts, float(f["funding_rate"]), float(f["mark_price"]))
        t = chunk_end
    return Series(rows.values())


@dataclass
class Signal:
    ticker: str
    direction: str
    strike: float
    close_epoch: float
    result: str
    entry_epoch: float
    favored_side: str
    trade_price: float
    fee_dollars: float
    edge_after_fee: float
    hedge_cost: float
    correct: bool
    pnl_per_contract: float

    @classmethod
    def from_trial(cls, market, trial):
        return cls(
            ticker=market.ticker, direction=market.direction, strike=market.strike,
            close_epoch=market.close_time.timestamp(), result=market.result, entry_epoch=trial.entry_epoch,
            favored_side=trial.favored_side, trade_price=trial.trade_price, fee_dollars=trial.fee_dollars,
            edge_after_fee=trial.edge_after_fee, hedge_cost=trial.hedge_cost, correct=trial.correct,
            pnl_per_contract=trial.pnl_per_contract,
        )


@dataclass
class HedgeRun:
    pnl: float = 0.0  # perp trading P&L incl. fees and funding
    fees: float = 0.0
    spread: float = 0.0  # paid crossing to bid/ask, vs mid
    funding: float = 0.0  # paid (negative = received)
    turnover_contracts: float = 0.0
    trades: int = 0
    peak_notional: float = 0.0
    # per effective leverage: [(epoch, change in margin posted)], liquidated?
    margin_events: dict = field(default_factory=dict)
    liquidated: dict = field(default_factory=dict)


def simulate_hedge(sig: Signal, contracts: float, spot: Series, hv: Series, perp: Series, funding: Series,
                   lev_ex: float, leverages: list[float], fee_rate: float) -> HedgeRun:
    run = HedgeRun()
    scen = {}
    for lev in leverages:
        ratio = lev_ex / lev  # margin posted, in multiples of the liquidation margin
        scen[lev] = {"topup": max(1.05, 0.75 * ratio), "target": max(1.1, ratio), "sweep": max(1.5, 1.5 * ratio),
                     "posted": 0.0, "events": [], "liquidated": False}
    stop = sig.close_epoch - config.HEDGE_STOP_SECONDS_BEFORE_CLOSE
    signed = contracts if sig.favored_side == "yes" else -contracts
    sign_dir = 1.0 if sig.direction == "above" else -1.0
    pos, cash = 0, 0.0
    last_t = sig.entry_epoch
    t = sig.entry_epoch
    while t <= sig.close_epoch + STEP_SECONDS:
        q = perp.at(t, MAX_QUOTE_AGE)
        if q is None:
            t += STEP_SECONDS
            if t > stop and pos == 0:
                break
            continue
        _, bid, ask, _, _ = q
        mid = (bid + ask) / 2.0

        # Funding, and the worst intra-minute price since the last step.
        for ft, rate, mark in funding.between(last_t, t):
            pay = pos * mark * rate  # longs pay shorts when the rate is positive
            cash -= pay
            run.funding += pay
        if pos:
            window = perp.between(last_t, t)
            worst = min(c[3] for c in window) if pos > 0 and window else (max(c[4] for c in window) if window else mid)
            worst_pnl = cash + pos * worst
            liq_margin = abs(pos) * worst / lev_ex
            for s in scen.values():
                if not s["liquidated"] and s["posted"] + worst_pnl < liq_margin:
                    s["liquidated"] = True

        target = 0
        if t < stop:
            s_px = (spot.at(t) or (None, None))[1]
            vol = (hv.at(t) or (None, None))[1]
            if s_px and vol:
                years = (sig.close_epoch - t) / bt.SECONDS_PER_YEAR
                delta_yes = sign_dir * digital_above_delta(s_px, sig.strike, vol / 100.0, years)
                target = hedger.target_perp_contracts(signed * delta_yes, hedger_contract_btc)
            else:
                target = pos  # no model input this minute: hold
        diff = target - pos
        if diff and (target == 0 or abs(diff) >= config.HEDGE_REBALANCE_CONTRACTS):
            px = ask if diff > 0 else bid
            fee = fee_rate * abs(diff) * px
            cash -= diff * px + fee
            run.fees += fee
            run.spread += abs(diff) * abs(px - mid)
            run.turnover_contracts += abs(diff)
            run.trades += 1
            pos = target
        run.peak_notional = max(run.peak_notional, abs(pos) * mid)

        # Margin top-up / sweep for each leverage scenario (lands by the next step).
        need = abs(pos) * mid / lev_ex
        equity_pnl = cash + pos * mid
        for s in scen.values():
            equity = s["posted"] + equity_pnl
            move = 0.0
            if pos and equity < s["topup"] * need:
                move = s["target"] * need - equity
            elif equity > s["sweep"] * need:
                move = (s["target"] * need if pos else 0.0) - equity
            if move:
                s["posted"] += move
                s["events"].append((t, move))

        if t >= stop and pos == 0:
            break
        last_t = t
        t += STEP_SECONDS

    run.pnl = cash  # flat by now
    for lev, s in scen.items():
        # Whatever's left on the margin side comes back at the end.
        if s["posted"]:
            s["events"].append((t, -s["posted"]))
        run.margin_events[lev] = s["events"]
        run.liquidated[lev] = s["liquidated"]
    return run



def _stats(pnls: list[float]) -> tuple[float, float, float, float]:
    """(total, mean, stdev, mean/stdev)."""
    if not pnls:
        return 0.0, 0.0, 0.0, 0.0
    mean = statistics.fmean(pnls)
    sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    return sum(pnls), mean, sd, (mean / sd if sd else float("nan"))


def _max_drawdown(points: list[tuple[float, float]]) -> float:
    """Worst peak-to-trough of cumulative P&L booked at each settlement time."""
    peak = cum = worst = 0.0
    for _, pnl in sorted(points):
        cum += pnl
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def _peak_capital(events: list[tuple[float, float]]) -> float:
    level = peak = 0.0
    for _, d in sorted(events):
        level += d
        peak = max(peak, level)
    return peak


def report(label: str, signals: list[Signal], runs: list[HedgeRun], contracts: float, leverages: list[float],
           show_leverage: bool = True) -> None:
    n = len(signals)
    print(f"\n=== {label}: {n} signals, {contracts:g} contracts each ===")
    if not n:
        return
    late = sum(s.close_epoch - s.entry_epoch <= config.HEDGE_STOP_SECONDS_BEFORE_CLOSE for s in signals)
    print(f"  {late} entered inside the {config.HEDGE_STOP_SECONDS_BEFORE_CLOSE:g}s stop window (never hedged); "
          f"median entry {statistics.median(s.close_epoch - s.entry_epoch for s in signals):.0f}s before close")
    binary = [s.pnl_per_contract * contracts for s in signals]
    hedged = [b + r.pnl for b, r in zip(binary, runs)]
    for name, pnls in (("unhedged", binary), ("hedged", hedged)):
        total, mean, sd, ratio = _stats(pnls)
        dd = _max_drawdown([(s.close_epoch, p) for s, p in zip(signals, pnls)])
        print(f"  {name:9s} total ${total:+9.2f}  mean ${mean:+.4f}  stdev ${sd:.4f}  mean/stdev {ratio:+.3f}  "
              f"max drawdown ${dd:.2f}")
    hp = sum(r.pnl for r in runs)
    fees, spread, funding = sum(r.fees for r in runs), sum(r.spread for r in runs), sum(r.funding for r in runs)
    expected = sum(s.hedge_cost for s in signals) * contracts
    print(f"  hedge P&L ${hp:+.2f} = price moves ${hp + fees + spread + funding:+.2f} - fees ${fees:.2f} "
          f"- spread ${spread:.2f} - funding ${funding:+.2f}; expected cost priced into entries ${expected:.2f}")
    print(f"  perp turnover {sum(r.turnover_contracts for r in runs):.0f} contracts in "
          f"{sum(r.trades for r in runs)} trades; peak hedge notional per signal "
          f"${max(r.peak_notional for r in runs):.0f}")
    if len(binary) > 1:
        corr = statistics.correlation(binary, [r.pnl for r in runs]) if statistics.pstdev([r.pnl for r in runs]) else 0.0
        print(f"  correlation(binary P&L, hedge P&L) = {corr:+.3f}  (-1 = the hedge offsets the binary perfectly)")

    if not show_leverage:
        return
    binary_events = []
    for s in signals:
        cost = (s.trade_price + s.fee_dollars) * contracts
        binary_events += [(s.entry_epoch, cost), (s.close_epoch, -cost)]
    base_cap = _peak_capital(binary_events)
    total_unhedged, total_hedged = sum(binary), sum(hedged)
    print(f"  capital: unhedged peak ${base_cap:.2f} (return on it {total_unhedged / base_cap:+.1%})" if base_cap else "")
    print(f"  {'leverage':>9s} {'peak capital':>13s} {'return on it':>13s} {'liquidated':>11s}")
    for lev in leverages:
        events = binary_events + [e for r in runs for e in r.margin_events[lev]]
        cap = _peak_capital(events)
        liq = sum(r.liquidated[lev] for r in runs)
        print(f"  {lev:8.2f}x ${cap:12.2f} {total_hedged / cap if cap else float('nan'):+13.1%} {liq:6d} of {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--tick-seconds", type=float, default=60.0, help="signal scan cadence (live --loop)")
    parser.add_argument("--edge-threshold", type=float, default=config.EDGE_THRESHOLD)
    parser.add_argument("--contracts", type=float, default=config.MAX_CONTRACTS_PER_TRADE,
                        help="binaries per signal (live caps at MAX_CONTRACTS_PER_TRADE)")
    parser.add_argument("--workers", type=int, default=8, help="parallel Kalshi trade fetches")
    parser.add_argument("--perp-fees", type=str, default="0,0.0004,0.0012",
                        help="perp taker fee rates (fraction of notional) to sweep")
    parser.add_argument("--checkpoint", type=str, default=None)
    args = parser.parse_args()

    perp_fees = [float(f) for f in args.perp_fees.split(",")]
    ckpt_path = Path(args.checkpoint or f"backtest_runs/hedge_{args.hours:g}h_{args.tick_seconds:g}s_"
                                         f"{args.edge_threshold:g}edge_{args.perp_fees}.jsonl")
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    settled = bt.find_settled_btc_markets(args.hours)
    if not settled:
        print("nothing to backtest")
        return
    start = min(m.open_time for m in settled) - timedelta(minutes=5)
    end = max(m.close_time for m in settled) + timedelta(minutes=5)
    print(f"{len(settled)} settled markets, {start} .. {end}")

    print("fetching Coinbase spot, Deribit realized vol, KXBTCPERP candles and funding...")
    spot = Series(bt.fetch_candles(start, end))
    hv = Series((ts_ms / 1000.0, v) for ts_ms, v in get_historical_volatility(config.DERIBIT_CURRENCY))
    perp = fetch_perp_candles(start, end)
    funding = fetch_funding(start, end)
    market = fetch_margin_market(PERP)
    lev_ex = min(hedger._leverage(market, "long_leverage_estimates"), hedger._leverage(market, "short_leverage_estimates"))
    print(f"  spot {len(spot.ts)} min, vol {len(hv.ts)} pts from "
          f"{datetime.fromtimestamp(hv.ts[0], timezone.utc) if hv.ts else '-'}, perp {len(perp.ts)} min, "
          f"funding {len(funding.ts)} events, exchange leverage {lev_ex:.2f}x")

    def hedge_cost_fn(fee_rate):
        def fn(epoch, abs_delta, seconds_to_close):
            q = perp.at(epoch, MAX_QUOTE_AGE)
            if q is None:
                return None
            f = funding.at(epoch)
            quote = hedger.PerpQuote(
                ticker=PERP, contract_btc=hedger_contract_btc, bid=q[1], ask=q[2], tick=0.0001,
                long_leverage=lev_ex, short_leverage=lev_ex, taker_fee_rate=fee_rate,
                funding_rate=f[1] if f else 0.0,
            )
            return hedger.hedge_cost_per_contract(abs_delta, seconds_to_close, quote)
        return fn

    done = {}
    if ckpt_path.exists():
        for line in ckpt_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["ticker"]] = row
    by_ticker = {m.ticker: m for m in settled}
    todo = [m for m in settled if m.ticker not in done]
    print(f"replaying signals: {len(done)} markets from checkpoint {ckpt_path}, {len(todo)} to fetch")

    def process(m):
        prices = bt.fetch_trade_prices(m.ticker, m.open_time, m.close_time)
        variants = [("plain", None)] + [(f"hedged@{fee:g}", hedge_cost_fn(fee)) for fee in perp_fees]
        row = {"ticker": m.ticker, **{key: None for key, _ in variants}}
        if prices:
            for key, fn in variants:
                trial = bt.find_first_signal(m, spot.rows, hv.rows, prices, args.tick_seconds,
                                             args.edge_threshold, hedge_cost_fn=fn)
                if trial is not None:
                    row[key] = Signal.from_trial(m, trial).__dict__
        return row

    with ckpt_path.open("a", encoding="utf-8") as ckpt, ThreadPoolExecutor(args.workers) as pool:
        for i, row in enumerate(pool.map(process, todo)):
            done[row["ticker"]] = row
            ckpt.write(json.dumps(row) + "\n")
            ckpt.flush()
            if (i + 1) % 250 == 0:
                print(f"  ...{i + 1}/{len(todo)}")

    rows = [done[t] for t in by_ticker if t in done]
    leverages = sorted({1.0, 2.0, 3.0, round(lev_ex / config.HEDGE_MARGIN_TARGET_RATIO, 2), round(lev_ex, 2)})

    def signals_for(key):
        found = [Signal(**r[key]) for r in rows if r.get(key)]
        return [s for s in found if perp.at(s.entry_epoch, MAX_QUOTE_AGE) is not None]

    plain = signals_for("plain")
    for fee in perp_fees:
        runs = [simulate_hedge(s, args.contracts, spot, hv, perp, funding, lev_ex, leverages, fee) for s in plain]
        report(f"pre-hedge entry rule, hedged at {fee * 1e4:g}bp perp fee", plain, runs, args.contracts, leverages,
               show_leverage=fee == perp_fees[0])
    for fee in perp_fees:
        signals = signals_for(f"hedged@{fee:g}")
        runs = [simulate_hedge(s, args.contracts, spot, hv, perp, funding, lev_ex, leverages, fee) for s in signals]
        report(f"hedge-cost-aware entry rule (live) at {fee * 1e4:g}bp perp fee", signals, runs, args.contracts,
               leverages, show_leverage=False)


if __name__ == "__main__":
    main()
