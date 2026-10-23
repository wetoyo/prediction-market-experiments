"""MACD / RSI direction signals on closed bars. Pure, no network.

Every function takes closes oldest-first and returns one value per close,
None until there's enough history.
"""

import random


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd_histogram(closes: list[float], fast: int, slow: int, signal: int) -> list[float | None]:
    ef, es = ema(closes, fast), ema(closes, slow)
    macd = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, m in enumerate(macd) if m is not None), len(macd))
    sig = ema([m for m in macd[start:]], signal)
    hist: list[float | None] = [None] * len(closes)
    for j, s in enumerate(sig):
        if s is not None:
            hist[start + j] = macd[start + j] - s
    return hist


def rsi(closes: list[float], period: int) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period

    def value():
        return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    out[period] = value()
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = value()
    return out


def directions(closes: list[float], kind: str, fast=12, slow=26, signal=9, rsi_period=14,
               seed: int = 0) -> list[int]:
    """+1 long, -1 short, 0 no signal, per bar.

    macd_rsi: momentum when both agree -- MACD histogram > 0 and RSI > 50
    is long, < 0 and < 50 short, otherwise wait. macd / rsi use one alone.
    always_long and random are baselines. A "_fade" suffix flips any of
    them (mean reversion instead of momentum).
    """
    if kind.endswith("_fade"):
        return [-d for d in directions(closes, kind[:-5], fast, slow, signal, rsi_period, seed)]
    n = len(closes)
    if kind == "always_long":
        return [1] * n
    if kind == "random":
        rng = random.Random(seed)
        return [rng.choice((1, -1)) for _ in range(n)]
    h = macd_histogram(closes, fast, slow, signal) if kind in ("macd_rsi", "macd") else [None] * n
    r = rsi(closes, rsi_period) if kind in ("macd_rsi", "rsi") else [None] * n
    out = []
    for hi, ri in zip(h, r):
        m = 0 if hi is None else (1 if hi > 0 else -1 if hi < 0 else 0)
        s = 0 if ri is None else (1 if ri > 50 else -1 if ri < 50 else 0)
        if kind == "macd":
            out.append(m)
        elif kind == "rsi":
            out.append(s)
        else:
            out.append(m if m == s else 0)
    return out
