"""Backtest: the same martingale on Kalshi's 15-minute BTC up/down markets
(KXBTC15M), with the direction taken from the perp (KXBTCPERP) signal.

Each KXBTC15M market is one bet, so bets come one after another and never
overlap:
  - at `--entry-minute` minutes after the market opens, read the signal from
    the latest perp bar closed by then (indicators.py, same signals and bars
    as backtest.py);
  - long -> buy YES at that minute's yes ask; short -> buy NO at its ask
    (1 - yes bid). Real 1-minute quotes from Kalshi's candlesticks; Kalshi's
    quadratic taker fee (0.07 * C * P * (1 - P), rounded up to $0.0001);
  - hold to settlement: $1 per contract on a win, nothing on a loss.
No signal, or an entry price outside [--min-price, --max-price], skips the
market and the cycle carries on with the next one.

Sizing is martingale.py's `_w` form: a contract bought at a with fee f risks
a + f and wins 1 - a - f, so w = (1 - a - f) / (a + f), and the next stake
after a loss is (G + losses) / w at the actual price. The cycle is planned
(and Kelly-sized) with w from the mean entry cost of recent bets, and p =
that cost + the walk-forward excess win rate (martingale.EdgeEstimator:
shrunk toward no edge, which for a binary means toward its price, not 0.5).
Breakeven per bet is a win rate of a + f (about 0.51-0.53 near even money).

Not modeled: book depth at the ask (fills of any size at the quoted ask;
these markets trade millions of contracts a day, but a ladder's deepest bet
is ~100x its first), and the latency between reading the signal and buying.

Usage:
    python backtest_15m.py                 # config defaults, entry at minute 1
    python backtest_15m.py --sweep
"""

import argparse
import bisect
import itertools
import json
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import backtest as bt
import config
import data
import martingale as mg

SERIES = "KXBTC15M"
FEE_RATE = 0.07  # Kalshi's quadratic fee multiplier (see ../../resolution_alpha/fees.py)


def kalshi_fee(price: float, contracts: int) -> float:
    if contracts <= 0:
        return 0.0
    return math.ceil(FEE_RATE * contracts * price * (1.0 - price) * 10000) / 10000.0


@dataclass
class Params:
    signal: str = config.SIGNAL
    bar_minutes: int = config.SIGNAL_BAR_MINUTES
    entry_minute: int = 1
    depth: int = config.DEPTH
    sizing: str = config.SIZING
    kelly_fraction: float = config.KELLY_FRACTION
    max_cycle_risk: float = config.MAX_CYCLE_RISK
    skim_fraction: float = config.SKIM_FRACTION
    p_prior: float = config.P_PRIOR_STRENGTH
    p_window: int = config.P_WINDOW
    min_price: float = 0.2
    max_price: float = 0.8
    bankroll: float = config.STARTING_BANKROLL_DOLLARS


@dataclass
class Result:
    params: dict
    bankroll: float = 0.0
    reserve: float = 0.0
    total: float = 0.0
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    bets: int = 0
    bet_wins: int = 0
    edge_sum: float = 0.0  # sum over bets of (outcome - price - fee per contract)
    cost_sum: float = 0.0  # sum of price + fee per contract
    funded_bets: int = 0
    funded_wins: int = 0
    funded_edge_sum: float = 0.0  # per-contract edge, funded bets only
    funded_pnl: float = 0.0
    cycles: int = 0
    cycle_wins: int = 0
    busts: int = 0
    max_contracts: int = 0
    fees: float = 0.0
    final_p: float = 0.0
    steps_hist: dict = field(default_factory=dict)

    @property
    def hit_rate(self):
        return self.bet_wins / self.bets if self.bets else float("nan")

    @property
    def breakeven(self):
        return self.cost_sum / self.bets if self.bets else float("nan")

    @property
    def edge(self):
        """Mean profit per $1-payout contract, after the fee (0 = breakeven)."""
        return self.edge_sum / self.bets if self.bets else float("nan")


def simulate(markets, mcandles, perp, funding, p: Params, trade_from: float, trade_to: float, dirs=None) -> Result:
    res = Result(params=asdict(p))
    bar_ends, bar_dirs = dirs or bt.bar_directions(perp, funding, p.signal, p.bar_minutes)
    est = mg.EdgeEstimator(p.p_prior, p.p_window)
    bankroll, reserve = p.bankroll, 0.0
    peak, max_dd = bankroll, 0.0
    cycle = None

    for m in markets:
        if m["close_ts"] > trade_to:
            break
        t_dec = m["open_ts"] + 60 * p.entry_minute
        i = bisect.bisect_right(bar_ends, t_dec) - 1
        d = bar_dirs[i] if i >= 0 else 0
        if d == 0:
            continue
        quote = next((c for c in mcandles.get(m["ticker"], ()) if c[0] == t_dec), None)
        if quote is None:
            continue
        _, ybid, yask = quote
        price = yask if d > 0 else 1.0 - ybid
        if not (p.min_price <= price <= p.max_price) or ybid <= 0 or yask >= 1:
            continue
        won = (m["result"] == "yes") == (d > 0)
        fee1 = FEE_RATE * price * (1 - price)  # per contract, unrounded, for planning/edge stats
        trading = m["open_ts"] >= trade_from

        if cycle is None and trading and bankroll > 0:
            c0 = est.cost
            plan = mg.plan_cycle_w(bankroll, est.p, (1 - c0) / c0, p.depth, p.sizing, p.kelly_fraction,
                                   p.max_cycle_risk)
            if plan.n0 > 0:
                cycle = {"plan": plan, "step": 0, "pnl": 0.0}
        contracts = 0
        if cycle is not None:
            w = (1 - price - fee1) / (price + fee1)
            stake = (cycle["plan"].base_profit + max(-cycle["pnl"], 0.0)) / w
            contracts = int(min(stake, bankroll) / (price + fee1))
            if contracts == 0:
                if cycle["step"] > 0:
                    _finish(res, cycle, False)
                cycle = None

        fee = kalshi_fee(price, contracts)
        pnl = contracts * ((1.0 if won else 0.0) - price) - fee
        bankroll += pnl
        est.add(won, price + fee1)
        if trading:
            res.bets += 1
            res.bet_wins += won
            res.edge_sum += (1.0 if won else 0.0) - price - fee1
            res.cost_sum += price + fee1
            res.fees += fee
            if contracts:
                res.funded_bets += 1
                res.funded_wins += won
                res.funded_edge_sum += (1.0 if won else 0.0) - price - fee1
                res.funded_pnl += pnl
                res.max_contracts = max(res.max_contracts, contracts)
        if cycle is not None:
            cycle["pnl"] += pnl
            if won or cycle["step"] >= (0 if p.sizing == "flat" else p.depth):
                _finish(res, cycle, won)
                if cycle["pnl"] > 0:
                    skim = p.skim_fraction * cycle["pnl"]
                    bankroll -= skim
                    reserve += skim
                cycle = None
            else:
                cycle["step"] += 1
        total = bankroll + reserve
        peak = max(peak, total)
        max_dd = max(max_dd, (peak - total) / peak)

    res.bankroll, res.reserve, res.total = bankroll, reserve, bankroll + reserve
    res.return_pct = 100.0 * (res.total / p.bankroll - 1)
    res.max_drawdown_pct = 100.0 * max_dd
    res.final_p = est.p
    return res


def _finish(res: Result, cycle: dict, won: bool):
    res.cycles += 1
    res.cycle_wins += won
    res.busts += not won
    k = str(cycle["step"] if won else "bust")
    res.steps_hist[k] = res.steps_hist.get(k, 0) + 1


# --- driver ---------------------------------------------------------------

_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        perp, funding = bt._load()
        markets, mcandles = data.load_15m(SERIES, int(bt.REPLAY_FROM.timestamp()), int(bt.END.timestamp()))
        _DATA = markets, mcandles, perp, funding
    return _DATA


_DIRS = {}


def _run(args):
    p, lo, hi = args
    markets, mcandles, perp, funding = _load()
    key = (p.signal, p.bar_minutes)
    if key not in _DIRS:
        _DIRS[key] = bt.bar_directions(perp, funding, p.signal, p.bar_minutes)
    return simulate(markets, mcandles, perp, funding, p, lo, hi, _DIRS[key])


def fmt(r: Result) -> str:
    q = r.params
    return (f"{q['signal']:>20} {q['bar_minutes']:>3}m @{q['entry_minute']:>2}m d={q['depth']} {q['sizing']:>5} | "
            f"ret {r.return_pct:+7.1f}% (res ${r.reserve:8.2f}, bank ${r.bankroll:8.2f}) dd {r.max_drawdown_pct:5.1f}% | "
            f"hit {r.hit_rate:.3f} BE {r.breakeven:.3f} edge {r.edge*100:+.2f}c n={r.bets} funded {r.funded_bets} | "
            f"cyc {r.cycles} bust {r.busts}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--test-fraction", type=float, default=0.3)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    markets, mcandles, perp, _ = _load()
    t0, t1 = markets[0]["open_ts"], markets[-1]["close_ts"]
    warm = 3 * 86400
    split = t1 - args.test_fraction * (t1 - t0)
    day = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%d")
    quoted = sum(1 for m in markets if mcandles.get(m["ticker"]))
    print(f"{SERIES}: {len(markets)} settled markets ({quoted} with quotes) {day(t0)}..{day(t1)}; "
          f"train {day(t0 + warm)}..{day(split)}, test {day(split)}..{day(t1)}")
    bt.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    if not args.sweep:
        p = Params()
        for name, lo, hi in (("train", t0 + warm, split), ("test", split, t1), ("all", t0 + warm, t1)):
            r = _run((p, lo, hi))
            print(f"{name:>5}: {fmt(r)}  steps {r.steps_hist}")
        return

    grid = []
    for sig, bar, entry, sizing in itertools.product(bt.sweep_signals(), (1, 5, 15), (1, 5), ("kelly", "full", "flat")):
        for depth in ((0,) if sizing == "flat" else (3, 5, 7)):
            grid.append(Params(signal=sig, bar_minutes=bar, entry_minute=entry, depth=depth, sizing=sizing))
    with ProcessPoolExecutor() as ex:
        train = list(ex.map(_run, [(p, t0 + warm, split) for p in grid], chunksize=8))
        train.sort(key=lambda r: r.total, reverse=True)
        print(f"\n== train: top {args.top} of {len(train)} ==")
        for r in train[:args.top]:
            print(fmt(r))
        kelly = [r for r in train if r.params["sizing"] != "full"]
        funded = [r for r in kelly if r.funded_bets]
        print(f"\n== train: Kelly-sized configs (kelly + flat): {len(funded)} of {len(kelly)} ever funded a bet ==")
        for r in sorted(funded, key=lambda r: r.total, reverse=True)[:args.top]:
            print(fmt(r))
        hits = {}
        for r in train:
            q = r.params
            hits.setdefault((q["signal"], q["bar_minutes"], q["entry_minute"]), r)
        best = sorted(hits.items(), key=lambda kv: kv[1].edge, reverse=True)[:30]
        oos = list(ex.map(_run, [(Params(**{**r.params}), split, t1) for _, r in best]))
        print(f"\n== per-bet edge (cents per contract after fee), best 30 signal/bar/entry on train "
              f"(of {len(hits)}), and the same on test ==")
        for (k, r), o in zip(best, oos):
            print(f"{k[0]:>20} {k[1]:>3}m @{k[2]:>2}m | train hit {r.hit_rate:.3f} BE {r.breakeven:.3f} "
                  f"edge {r.edge*100:+.2f}c n={r.bets:<5} | test hit {o.hit_rate:.3f} BE {o.breakeven:.3f} "
                  f"edge {o.edge*100:+.2f}c n={o.bets}")
        test = list(ex.map(_run, [(Params(**{**r.params}), split, t1) for r in train[:args.top]]))
        print(f"\n== test (out of sample): the train top {args.top} ==")
        for r in test:
            print(fmt(r))
    (bt.RUNS_DIR / f"sweep15m_{stamp}.json").write_text(json.dumps(
        {"train": [asdict(r) for r in train], "test": [asdict(r) for r in test]}, indent=1))
    print(f"\nsaved backtest_runs/sweep15m_{stamp}.json")


if __name__ == "__main__":
    main()
