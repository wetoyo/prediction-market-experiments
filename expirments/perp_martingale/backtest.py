"""Backtest: martingale on KXBTCPERP with a momentum direction signal, a
Kelly-sized cycle and the profit skimmed out after every won cycle.

Replays Kalshi's real 1-minute KXBTCPERP bid/ask candles and 8-hourly
funding (data.py). Each bet:
  - enters at the close of the minute it's placed in: longs buy the ask,
    shorts sell the bid, taker fee on the notional;
  - in the direction of the latest *closed* signal bar (indicators.py); no
    signal (e.g. MACD and RSI disagree) means wait, the cycle stays open;
  - exits at +/-BRACKET from its fill: the take-profit as a resting limit
    (fills at its price, or better on a gap), the stop as a stop-market
    (fills at its price minus --stop-slippage, or worse on a gap), taker fee
    on both. A minute whose range touches both counts as the stop;
  - pays/receives funding for every funding time it's held across.
The ladder, the Kelly cycle sizing and the walk-forward win-rate estimate
are martingale.py. Whole contracts only; a funded cycle whose stake rounds
to 0 contracts places a paper bet instead.

Split: indicators and the win-rate estimate warm up on everything before
the trading window (bets there are paper), so the --test-fraction run is
out of sample for the win rate. The sweep ranks configs on the train window
and reports the best on the test window -- picking the best of hundreds of
configs in-sample is itself a fit, which is what the test window checks.

Not modeled: queue position on the take-profit (a touch counts as a fill),
liquidation (the bracket is far inside the ~1/leverage liquidation
distance), fee tiers/rebates, and the margin-transfer mechanics a live
runner would need (btc_implied_prob/hedger.py has them).

Usage:
    python backtest.py                      # the config.py defaults, one run
    python backtest.py --sweep              # grid search, train/test report
"""

import argparse
import bisect
import itertools
import json
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
import data
import indicators
import martingale as mg

START = datetime(2026, 5, 25, tzinfo=timezone.utc)  # before the perp's history begins (2026-06-03)
# The perp's first days had a thin book (multi-% wicks, empty-book minutes);
# spreads settle to ~0.3bp by 06-09. Nothing before this is replayed.
REPLAY_FROM = datetime(2026, 6, 8, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, tzinfo=timezone.utc)
RUNS_DIR = Path(__file__).resolve().parent / "backtest_runs"

TS, BO, BH, BL, BC, AO, AH, AL, AC = range(9)


@dataclass
class Params:
    signal: str = config.SIGNAL
    bar_minutes: int = config.SIGNAL_BAR_MINUTES
    bracket: float = config.BRACKET
    depth: int = config.DEPTH
    sizing: str = config.SIZING
    fee_rate: float = config.FEE_RATE
    cost_estimate: float = config.COST_ESTIMATE
    kelly_fraction: float = config.KELLY_FRACTION
    max_cycle_risk: float = config.MAX_CYCLE_RISK
    leverage: float = config.LEVERAGE
    skim_fraction: float = config.SKIM_FRACTION
    p_prior: float = config.P_PRIOR_STRENGTH
    p_window: int = config.P_WINDOW
    stop_slippage: float = 0.0001
    bankroll: float = config.STARTING_BANKROLL_DOLLARS


@dataclass
class Result:
    params: dict
    start: float = 0.0
    end: float = 0.0
    bankroll: float = 0.0  # working bankroll at the end
    reserve: float = 0.0  # skimmed profit
    total: float = 0.0
    return_pct: float = 0.0
    min_bankroll: float = 0.0
    max_drawdown_pct: float = 0.0  # of total (bankroll + reserve)
    bets: int = 0  # all resolved bets in the window, paper included
    bet_wins: int = 0
    funded_bets: int = 0
    funded_bet_wins: int = 0
    cycles: int = 0  # funded cycles finished
    cycle_wins: int = 0
    busts: int = 0
    max_step: int = 0  # deepest step reached (0 = first bet)
    fees: float = 0.0
    funding: float = 0.0
    final_p: float = 0.0
    mean_risk_fraction: float = 0.0
    breakeven_p: float = 0.0  # per-bet win rate where the bracket is EV 0 after cost_estimate
    steps_hist: dict = field(default_factory=dict)

    @property
    def hit_rate(self):
        return self.bet_wins / self.bets if self.bets else float("nan")


def bar_directions(candles, p: Params) -> tuple[list[float], list[int]]:
    """(bar end ts, direction) per closed signal bar, from mid closes."""
    bar_s = p.bar_minutes * 60
    ends, closes = [], []
    for c in candles:
        bid = math.ceil(c[TS] / bar_s)
        mid = (c[BC] + c[AC]) / 2.0
        if ends and ends[-1] == bid * bar_s:
            closes[-1] = mid
        else:
            ends.append(bid * bar_s)
            closes.append(mid)
    dirs = indicators.directions(closes, p.signal, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL,
                                 config.RSI_PERIOD, seed=p.bar_minutes)
    return ends, dirs


def simulate(candles, funding, p: Params, trade_from: float, trade_to: float) -> Result:
    res = Result(params=asdict(p), start=trade_from, end=trade_to)
    bar_ends, bar_dirs = bar_directions(candles, p)
    f_ts = [f[0] for f in funding]
    est = mg.WinRateEstimator(p.p_prior, p.p_window)
    bankroll, reserve = p.bankroll, 0.0
    peak_total, min_bankroll, max_dd = bankroll, bankroll, 0.0
    risk_fracs = []
    res.breakeven_p = (p.bracket + p.cost_estimate) / (2 * p.bracket)

    cycle = None  # dict(plan, step, losses, pnl)
    pos = None  # dict(dir, entry, contracts, tp, sl, t, funded)
    prev_t = candles[0][TS]

    for c in candles:
        t = c[TS]
        if t > trade_to and pos is None:
            break
        if pos is not None:
            d, n = pos["dir"], pos["contracts"]
            # funding for any funding time crossed (positive rate: longs pay)
            for j in range(bisect.bisect_right(f_ts, prev_t), bisect.bisect_right(f_ts, t)):
                pay = d * funding[j][1] * funding[j][2] * n
                bankroll -= pay
                pos["funding"] += pay
                if pos["counted"]:
                    res.funding += pay
            o, hi, lo = (c[BO], c[BH], c[BL]) if d > 0 else (c[AO], c[AH], c[AL])
            tp, sl = pos["tp"], pos["sl"]
            exit_px = won = None
            if d > 0:
                if o <= sl:
                    exit_px, won = o * (1 - p.stop_slippage), False
                elif o >= tp:
                    exit_px, won = o, True
                elif lo <= sl:
                    exit_px, won = sl * (1 - p.stop_slippage), False
                elif hi >= tp:
                    exit_px, won = tp, True
            else:
                if o >= sl:
                    exit_px, won = o * (1 + p.stop_slippage), False
                elif o <= tp:
                    exit_px, won = o, True
                elif hi >= sl:
                    exit_px, won = sl * (1 + p.stop_slippage), False
                elif lo <= tp:
                    exit_px, won = tp, True
            if exit_px is not None:
                fee = p.fee_rate * exit_px * n
                pnl = d * (exit_px - pos["entry"]) * n - fee
                bankroll += pnl
                est.add(won)
                if pos["counted"]:
                    res.bets += 1
                    res.bet_wins += won
                    res.fees += fee
                    if n:
                        res.funded_bets += 1
                        res.funded_bet_wins += won
                if cycle is not None:
                    # entry fee and funding already left the bankroll
                    cycle["pnl"] += pnl - pos["entry_fee"] - pos["funding"]
                    if won or cycle["step"] >= (0 if p.sizing == "flat" else p.depth):
                        _finish_cycle(res, cycle, won, pos["counted"])
                        if cycle["pnl"] > 0:
                            skim = p.skim_fraction * cycle["pnl"]
                            bankroll -= skim
                            reserve += skim
                        cycle = None
                    else:
                        cycle["losses"] = -cycle["pnl"]
                        cycle["step"] += 1
                pos = None
                total = bankroll + reserve
                peak_total = max(peak_total, total)
                max_dd = max(max_dd, (peak_total - total) / peak_total)
                min_bankroll = min(min_bankroll, bankroll)
        prev_t = t

        if pos is None and t <= trade_to:
            i = bisect.bisect_right(bar_ends, t) - 1
            d = bar_dirs[i] if i >= 0 else 0
            if d == 0:
                continue
            trading = t >= trade_from
            if cycle is None and trading and bankroll > 0:
                plan = mg.plan_cycle(bankroll, est.p, p.bracket, p.cost_estimate, p.depth, p.sizing,
                                     p.kelly_fraction, p.max_cycle_risk, p.leverage)
                if plan.n0 > 0:
                    cycle = {"plan": plan, "step": 0, "losses": 0.0, "pnl": 0.0}
                    risk_fracs.append(plan.risk_fraction)
            entry = c[AC] if d > 0 else c[BC]
            contracts = 0
            if cycle is not None:
                notional = mg.next_notional(cycle["plan"].base_profit, max(cycle["losses"], 0.0), p.bracket,
                                            p.cost_estimate)
                notional = min(notional, max(bankroll, 0.0) * p.leverage)  # margin
                contracts = int(notional / entry)
                if contracts == 0:  # can't place it: the cycle ends where it stands
                    if cycle["step"] > 0:
                        _finish_cycle(res, cycle, False, trading)
                    cycle = None
            entry_fee = p.fee_rate * entry * contracts
            bankroll -= entry_fee
            if trading:
                res.fees += entry_fee
            pos = {"dir": d, "entry": entry, "contracts": contracts, "entry_fee": entry_fee, "funding": 0.0,
                   "tp": entry * (1 + d * p.bracket), "sl": entry * (1 - d * p.bracket), "counted": trading}
            if cycle is not None:
                res.max_step = max(res.max_step, cycle["step"])

    res.bankroll, res.reserve = bankroll, reserve
    res.total = bankroll + reserve
    res.return_pct = 100.0 * (res.total / p.bankroll - 1)
    res.min_bankroll, res.max_drawdown_pct = min_bankroll, 100.0 * max_dd
    res.final_p = est.p
    res.mean_risk_fraction = statistics.fmean(risk_fracs) if risk_fracs else 0.0
    return res


def _finish_cycle(res: Result, cycle: dict, won: bool, counted: bool):
    if not counted:
        return
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
        candles, funding = data.load(config.PERP_TICKER, int(START.timestamp()), int(END.timestamp()))
        _DATA = [c for c in candles if c[TS] >= REPLAY_FROM.timestamp()], funding
    return _DATA


def _run(args):
    p, lo, hi = args
    candles, funding = _load()
    return simulate(candles, funding, p, lo, hi)


def fmt(r: Result) -> str:
    q = r.params
    return (f"{q['signal']:>13} {q['bar_minutes']:>3}m b={q['bracket']*100:.2f}% d={q['depth']} {q['sizing']:>5} "
            f"fee={q['fee_rate']*1e4:.0f}bp | ret {r.return_pct:+7.1f}% (res ${r.reserve:8.2f}, bank ${r.bankroll:8.2f}) "
            f"dd {r.max_drawdown_pct:5.1f}% | hit {r.hit_rate:.3f} (BE {r.breakeven_p:.3f}, p^ {r.final_p:.3f}) "
            f"bets {r.bets} funded {r.funded_bets} | cyc {r.cycles} bust {r.busts} risk {r.mean_risk_fraction:.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--test-fraction", type=float, default=0.3)
    ap.add_argument("--fees", default="0.0001,0.0005,0.001", help="fee rates swept for the top configs")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    candles, _ = _load()
    t0, t1 = candles[0][TS], candles[-1][TS]
    warm = 3 * 86400  # indicator + win-rate warm-up before anything trades
    split = t1 - args.test_fraction * (t1 - t0)
    day = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%d")
    print(f"KXBTCPERP {len(candles)} candles {day(t0)}..{day(t1)}; train {day(t0 + warm)}..{day(split)}, "
          f"test {day(split)}..{day(t1)}")
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    if not args.sweep:
        p = Params()
        for name, lo, hi in (("train", t0 + warm, split), ("test", split, t1), ("all", t0 + warm, t1)):
            r = _run((p, lo, hi))
            print(f"{name:>5}: {fmt(r)}  steps {r.steps_hist}")
        return

    grid = []
    for sig, bar, br, sizing in itertools.product(
            ("macd_rsi", "macd", "rsi", "macd_rsi_fade", "always_long", "random"), (5, 15, 60), (0.0025, 0.005, 0.01, 0.02),
            ("kelly", "full", "flat")):
        for depth in ((0,) if sizing == "flat" else (3, 5, 7)):
            grid.append(Params(signal=sig, bar_minutes=bar, bracket=br, depth=depth, sizing=sizing))
    with ProcessPoolExecutor() as ex:
        train = list(ex.map(_run, [(p, t0 + warm, split) for p in grid], chunksize=4))
        train.sort(key=lambda r: r.total, reverse=True)
        print(f"\n== train: top {args.top} of {len(train)} (fee {config.FEE_RATE*1e4:.0f}bp) ==")
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
            hits.setdefault((q["signal"], q["bar_minutes"], q["bracket"]), (r.hit_rate, r.breakeven_p, r.bets))
        print("\n== train: raw per-bet hit rate by signal/bar/bracket (vs breakeven at the default fee) ==")
        for k, (h, be, n) in sorted(hits.items(), key=lambda kv: kv[1][0] - kv[1][1], reverse=True):
            print(f"{k[0]:>13} {k[1]:>3}m b={k[2]*100:.2f}%  hit {h:.3f}  BE {be:.3f}  edge {h-be:+.3f}  n={n}")

        top = [Params(**{**r.params}) for r in train[:args.top]]
        fees = [float(x) for x in args.fees.split(",")]
        test_args = [(Params(**{**asdict(p), "fee_rate": f, "cost_estimate": 2 * f + 0.0002}), split, t1)
                     for p in top for f in fees]
        test = list(ex.map(_run, test_args))
        print(f"\n== test (out of sample): the train top {args.top}, per fee ==")
        for r in test:
            print(fmt(r))
        base = [Params(signal="random", bar_minutes=15, bracket=0.005, depth=7, sizing=s) for s in ("kelly", "full")]
        print("\n== test: random-direction baseline ==")
        for r in ex.map(_run, [(p, split, t1) for p in base]):
            print(fmt(r))
    (RUNS_DIR / f"sweep_{stamp}.json").write_text(json.dumps(
        {"train": [asdict(r) for r in train], "test": [asdict(r) for r in test]}, indent=1))
    print(f"\nsaved backtest_runs/sweep_{stamp}.json")


if __name__ == "__main__":
    main()
