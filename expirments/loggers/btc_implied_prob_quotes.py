"""Logs what btc_implied_prob sees each minute and can't be fetched again
later, for backtesting it (and its perp hedge) on its real inputs instead of
backtest.py's proxies (realized vol, last-trade prices).

Streams (see runner.py / store.py for the format):
  quotes   every tick, per open BTC interval market worth trading (see
           _worth_logging -- ~75 of ~325 are dead 0/1c strikes): yes bid/ask
           and the live model's price for it -- probability, sigma and
           forward from the Deribit surface, YES delta. Compact keys:
           {ts, t: ticker, b: yes_bid, a: yes_ask, p: model P(yes),
            s: sigma, f: forward, d: dP(yes)/dF, x: 1 if extrapolated}
  markets  once per ticker: series, direction, strike, open/close time.
  spot     every tick: Deribit's BTC index.
  perp     every tick: KXBTCPERP bid/ask, leverage, funding estimate (the
           candles are fetchable later; this is the exact quote the hedge
           would have priced against).
  smile    every SMILE_EVERY: each Deribit expiry's forward and OTM smile
           (strike, iv) -- enough to rebuild deribit_iv.Surface offline.
  hv       every HV_EVERY: Deribit's realized-vol series' newest points
           (it only reaches ~16 days back).

Not logged, because it can be fetched afterwards: Kalshi trades and
settlements, KXBTCPERP candles and funding history, Coinbase candles.
"""

import sys
from pathlib import Path

_STRATEGY_DIR = Path(__file__).resolve().parents[1] / "btc_implied_prob"
if str(_STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(_STRATEGY_DIR))

import config  # noqa: E402
import deribit_iv  # noqa: E402
import hedger  # noqa: E402
from derbit.fetch_historical import get_historical_volatility  # noqa: E402
from kalshi_btc_markets import find_active_btc_markets  # noqa: E402

SMILE_EVERY = 600.0
HV_EVERY = 3600.0


def _r(x: float, digits: int) -> float:
    return round(float(x), digits)


def _worth_logging(yes_bid: float, yes_ask: float, model_prob: float | None) -> bool:
    """Mid-range quotes, or ones the model disagrees with by at least a cent.
    The rest are 0/1c strikes far from the money that no entry rule can
    trade -- three quarters of the rows, for nothing."""
    if 0.02 <= yes_ask and yes_bid <= 0.98 and (yes_bid > 0.0 or yes_ask < 1.0):
        return True
    if model_prob is None:
        return False
    return model_prob - yes_ask >= 0.01 or yes_bid - model_prob >= 0.01


def collect(ctx):
    ts = int(ctx.now)
    surface = deribit_iv.build_surface(config.DERIBIT_CURRENCY)
    yield "spot", {"ts": ts, "index": surface.spot}

    if surface.expiries and ctx.due("smile", SMILE_EVERY):
        for e in surface.expiries:
            yield "smile", {
                "ts": ts, "expiry_ms": e.expiration_timestamp_ms, "forward": _r(e.forward, 2),
                "strikes": e.strikes, "ivs": [_r(v, 4) for v in e.ivs],
            }

    if ctx.due("hv", HV_EVERY):
        for ts_ms, value in get_historical_volatility(config.DERIBIT_CURRENCY)[-3:]:
            yield "hv", {"ts": ts, "point_ts": int(ts_ms / 1000), "hv": value}

    try:
        perp = hedger.fetch_perp_quote(config.PERP_HEDGE_TICKER, config.HEDGE_TAKER_FEE_RATE)
        yield "perp", {"ts": ts, "b": perp.bid, "a": perp.ask, "funding": perp.funding_rate,
                       "lev_long": perp.long_leverage, "lev_short": perp.short_leverage}
    except Exception as exc:  # the binaries are worth logging even without a perp quote
        yield "perp", {"ts": ts, "error": repr(exc)[:200]}

    seen = ctx.state.setdefault("markets_seen", set())
    for m in find_active_btc_markets():
        if not (0.0 < m.yes_ask < 1.0 or 0.0 < m.yes_bid < 1.0):
            continue  # no quote on either side
        row = {"ts": ts, "t": m.ticker, "b": m.yes_bid, "a": m.yes_ask}
        seconds_left = m.close_time.timestamp() - ctx.now
        if surface.expiries and seconds_left > 0:
            est = deribit_iv.estimate_probability(
                surface, direction=m.direction, strike=m.strike, seconds_to_expiry=seconds_left,
            )
            delta = deribit_iv.estimate_delta_yes(
                surface, direction=m.direction, strike=m.strike, seconds_to_expiry=seconds_left,
            )
            row.update(p=_r(est.prob_yes, 5), s=_r(est.sigma_used, 4), f=_r(est.forward_used, 1),
                       d=float(f"{delta:.4g}"))
            if est.extrapolated:
                row["x"] = 1
        if not _worth_logging(m.yes_bid, m.yes_ask, row.get("p")):
            continue
        if m.ticker not in seen:
            seen.add(m.ticker)
            yield "markets", {
                "ts": ts, "t": m.ticker, "series": m.series_ticker, "event": m.event_ticker,
                "direction": m.direction, "strike": m.strike, "open": int(m.open_time.timestamp()),
                "close": int(m.close_time.timestamp()), "settle_avg_s": m.settlement_average_seconds,
            }
        yield "quotes", row
