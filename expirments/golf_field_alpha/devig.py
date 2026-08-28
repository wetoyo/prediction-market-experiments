"""Removes the bookmaker's overround ("vig") from a golf field's raw prices
to get a fair win probability per player.

On a Kalshi outright-winner event, each entrant has a binary YES market.
Exactly one entrant wins, so the *true* win probabilities sum to 1. The
market's own prices don't: buying YES on every player at its mid (or last
trade) sums to 1 + overround -- ~1.27 on one fully-settled event checked
2026-08-27, i.e. a 27% overround. That gap is the market maker's edge plus
whatever mispricing retail flow has left in the book.

De-vigging = rescaling the raw implied probabilities so they sum to 1
again. Two standard methods:

- "proportional" (a.k.a. multiplicative / basic): fair_i = implied_i / S,
  where S = sum(implied). Removes the overround as a flat percentage of
  every price. Simple, unbiased if the overround really is spread evenly.

- "power": fair_i = implied_i ** k, with k > 1 chosen (by bisection) so
  sum(fair) = 1. Because x**k shrinks small x proportionally more than
  large x, this pulls longshots down harder than favorites -- which is the
  direction the favorite-longshot bias goes in real sports books (longshots
  are systematically overbet, so their raw price overstates their true
  chance by more than a favorite's does). If that bias is present here,
  "power" de-vigs it out; "proportional" leaves it in.

Which one is right for Kalshi golf is an empirical question -- that's part
of what backtest.py measures (run it with --devig-method both).
"""


def _normalize_nonneg(values: list[float]) -> list[float]:
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return [0.0 for _ in values]
    return [(v / total if v > 0 else 0.0) for v in values]


def devig_proportional(implied: list[float]) -> list[float]:
    return _normalize_nonneg(implied)


def devig_power(implied: list[float], tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """fair_i = implied_i ** k with k solved so sum == 1. k > 1 when the raw
    prices sum to > 1 (the usual case), which shrinks longshots more than
    favorites. Falls back to proportional if the inputs are degenerate
    (all zero, or a single non-zero entry).
    """
    positives = [max(v, 0.0) for v in implied]
    live = [v for v in positives if 0.0 < v < 1.0]
    if len(live) < 2:
        return devig_proportional(implied)

    def _sum_pow(k: float) -> float:
        return sum(v ** k for v in positives if v > 0.0)

    # sum_pow is monotonically decreasing in k for inputs in (0, 1). Bracket
    # k in [lo, hi] with _sum_pow(lo) >= 1 >= _sum_pow(hi).
    lo, hi = 1e-6, 1.0
    if _sum_pow(hi) > 1.0:
        # raw prices sum to > 1: need k > 1 to bring the sum down
        lo = 1.0
        hi = 2.0
        while _sum_pow(hi) > 1.0 and hi < 1e6:
            hi *= 2.0
    else:
        # raw prices already sum to <= 1: need k < 1 to bring the sum up
        hi = 1.0
        lo = 0.5
        while _sum_pow(lo) < 1.0 and lo > 1e-6:
            lo *= 0.5

    k = 1.0
    for _ in range(max_iter):
        k = 0.5 * (lo + hi)
        s = _sum_pow(k)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k

    return [(v ** k if v > 0.0 else 0.0) for v in positives]


def devig(implied: list[float], method: str = "proportional") -> list[float]:
    """Returns fair probabilities (same order as `implied`, summing to ~1).
    `method` is "proportional" or "power".
    """
    if method == "proportional":
        return devig_proportional(implied)
    if method == "power":
        return devig_power(implied)
    raise ValueError(f"unknown devig method {method!r} (expected 'proportional' or 'power')")


if __name__ == "__main__":
    # Toy field: one favorite, a few mids, a long tail. Raw sum ~1.25.
    raw = [0.35, 0.18, 0.12, 0.10, 0.08, 0.06, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02, 0.02, 0.03]
    print(f"raw sum = {sum(raw):.4f}")
    for m in ("proportional", "power"):
        fair = devig(raw, m)
        print(f"{m:13s} sum={sum(fair):.4f}  favorite {raw[0]:.3f}->{fair[0]:.3f}  "
              f"longshot {raw[-1]:.3f}->{fair[-1]:.3f}")
