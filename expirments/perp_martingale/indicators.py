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


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def _sign(x, deadband=0.0) -> int:
    return 0 if x is None or abs(x) <= deadband else (1 if x > 0 else -1)


def _macd(closes, fast, slow, signal):
    return [_sign(h) for h in macd_histogram(closes, fast, slow, signal)]


def _rsi_side(closes, period):
    return [_sign(None if r is None else r - 50) for r in rsi(closes, period)]


def _ema_cross(closes, fast=9, slow=21):
    return [_sign(None if f is None or s is None else f - s) for f, s in zip(ema(closes, fast), ema(closes, slow))]


def _tsmom(closes, lookback=12):
    return [0 if i < lookback else _sign(closes[i] / closes[i - lookback] - 1) for i in range(len(closes))]


def _donchian(closes, period=20):
    """Breakout: close above the prior `period` closes' max is long, below their min short."""
    out = []
    for i, c in enumerate(closes):
        if i < period:
            out.append(0)
            continue
        w = closes[i - period:i]
        out.append(1 if c > max(w) else -1 if c < min(w) else 0)
    return out


def _bb_revert(closes, period=20, k=2.0):
    """Fade a close more than k standard deviations from its SMA."""
    out = []
    for i, c in enumerate(closes):
        if i < period - 1:
            out.append(0)
            continue
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        sd = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        out.append(0 if sd == 0 else (-1 if c > m + k * sd else 1 if c < m - k * sd else 0))
    return out


def _rsi_revert(closes, period, lo=30, hi=70):
    return [0 if r is None else (1 if r < lo else -1 if r > hi else 0) for r in rsi(closes, period)]


def _stoch_revert(closes, period=14, smooth=3, lo=20, hi=80):
    """Stochastic %K (on closes) oversold and turning up is long, overbought
    and turning down short."""
    k: list[float | None] = []
    for i, c in enumerate(closes):
        if i < period - 1:
            k.append(None)
            continue
        w = closes[i - period + 1:i + 1]
        rng = max(w) - min(w)
        k.append(50.0 if rng == 0 else 100.0 * (c - min(w)) / rng)
    d = [None] * len(k)
    for i in range(len(k)):
        w = k[max(0, i - smooth + 1):i + 1]
        if len(w) == smooth and None not in w:
            d[i] = sum(w) / smooth
    return [0 if ki is None or di is None else (1 if ki < lo and ki > di else -1 if ki > hi and ki < di else 0)
            for ki, di in zip(k, d)]


def _streak_revert(closes, n=3):
    """After n bars in a row the same way, bet on the reversal."""
    out = []
    for i in range(len(closes)):
        if i < n:
            out.append(0)
            continue
        moves = [_sign(closes[j] - closes[j - 1]) for j in range(i - n + 1, i + 1)]
        out.append(-moves[0] if moves[0] != 0 and all(m == moves[0] for m in moves) else 0)
    return out


def _trend_vote(closes, fast, slow, signal):
    """At least 3 of the 4 trend signals (MACD, EMA cross, 12-bar momentum,
    Donchian breakout) agree."""
    votes = zip(_macd(closes, fast, slow, signal), _ema_cross(closes), _tsmom(closes), _donchian(closes))
    return [_sign(sum(v), 2) for v in votes]


SIGNALS = ("macd_rsi", "macd", "rsi", "ema_cross", "tsmom", "donchian", "trend_vote", "bb_revert", "rsi_revert",
           "stoch_revert", "streak_revert", "funding_revert", "always_long", "random")


def directions(closes: list[float], kind: str, fast=12, slow=26, signal=9, rsi_period=14,
               seed: int = 0, funding: list[float | None] | None = None) -> list[int]:
    """+1 long, -1 short, 0 no signal, per bar.

    Momentum: macd_rsi (MACD histogram and RSI-vs-50 agree, otherwise wait),
    macd, rsi, ema_cross (EMA 9 vs 21), tsmom (12-bar return), donchian
    (20-bar breakout), trend_vote (3 of macd/ema_cross/tsmom/donchian).
    Mean reversion: bb_revert (2-sigma Bollinger), rsi_revert (30/70),
    stoch_revert (%K 20/80 turning), streak_revert (3 bars one way),
    funding_revert (against the perp's latest funding rate, outside its 1bp
    deadband: positive funding means crowded longs, so short). Baselines:
    always_long, random. A "_fade" suffix flips any of them. All of them use
    closes only (the bars are built from 1-minute mids).
    """
    if kind.endswith("_fade"):
        return [-d for d in directions(closes, kind[:-5], fast, slow, signal, rsi_period, seed, funding)]
    n = len(closes)
    if kind == "always_long":
        return [1] * n
    if kind == "random":
        rng = random.Random(seed)
        return [rng.choice((1, -1)) for _ in range(n)]
    if kind == "macd_rsi":
        return [m if m == r else 0 for m, r in zip(_macd(closes, fast, slow, signal), _rsi_side(closes, rsi_period))]
    if kind == "macd":
        return _macd(closes, fast, slow, signal)
    if kind == "rsi":
        return _rsi_side(closes, rsi_period)
    if kind == "rsi_revert":
        return _rsi_revert(closes, rsi_period)
    if kind == "trend_vote":
        return _trend_vote(closes, fast, slow, signal)
    if kind == "funding_revert":
        return [-_sign(f, 0.0001) for f in (funding or [None] * n)]
    simple = {"ema_cross": _ema_cross, "tsmom": _tsmom, "donchian": _donchian, "bb_revert": _bb_revert,
              "stoch_revert": _stoch_revert, "streak_revert": _streak_revert}
    if kind not in simple:
        raise ValueError(f"unknown signal {kind!r}")
    return simple[kind](closes)
