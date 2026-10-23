"""Martingale ladder sizing with a Kelly-sized cycle. Pure, no network.

In general terms (the `_w` functions): a bet risks r and wins w * r. A
binary contract bought at price a (plus fee f per contract) has
w = (1 - a - f) / (a + f); a perp bracket has w = (b - c) / (b + c). After
each loss the next risk is (G + losses) / w, a growth of (1 + w) / w per step.

For the perp: a cycle is a run of bracket bets (win +b, lose -b of notional, minus costs c).
After each loss the next stake is sized so that winning it recovers every
loss so far plus the cycle's base profit G = n0 * (b - c); in the plan that's
a growth factor of 2b / (b - c) per step (just over 2x once costs count).
The cycle ends at the first win (+G) or after DEPTH + 1 losses (the bust,
-R, the whole ladder).

Seen as one bet, a cycle wins G with probability 1 - q^(DEPTH+1) and loses R
otherwise, so its Kelly fraction of the bankroll to put at risk is
p_c - q_c * R / G. At p = 0.5 that's never positive once costs count -- the
martingale itself adds no edge -- so a cycle only gets funded while the
estimated per-bet win rate is high enough to pay for the costs.
"""

from dataclasses import dataclass


def step_notionals(n0: float, bracket: float, cost: float, depth: int) -> list[float]:
    """Planned notional for each of the DEPTH + 1 bets of a cycle."""
    if bracket <= cost:
        raise ValueError("bracket must exceed the round-trip cost")
    g = 2.0 * bracket / (bracket - cost)
    return [n0 * g ** k for k in range(depth + 1)]


def next_notional(base_profit: float, realized_losses: float, bracket: float, cost: float) -> float:
    """Stake whose win nets the cycle base_profit after realized_losses."""
    return (base_profit + realized_losses) / (bracket - cost)


@dataclass
class CyclePlan:
    n0: float  # first bet's notional, dollars
    base_profit: float  # G: what a won cycle nets
    risk: float  # R: what a bust loses
    win_prob: float  # 1 - q^(DEPTH+1)
    kelly: float  # Kelly fraction of the bankroll at risk (can be <= 0)
    risk_fraction: float  # what's actually put at risk, after KELLY_FRACTION/caps


def cycle_shape(bracket: float, cost: float, depth: int) -> tuple[float, float, float]:
    """(G, R, max notional) per $1 of first-bet notional."""
    ns = step_notionals(1.0, bracket, cost, depth)
    return bracket - cost, sum(n * (bracket + cost) for n in ns), ns[-1]


def cycle_win_prob(p: float, depth: int) -> float:
    return 1.0 - (1.0 - p) ** (depth + 1)


def bracket_w(bracket: float, cost: float) -> float:
    return (bracket - cost) / (bracket + cost)


def kelly_cycle(p: float, bracket: float, cost: float, depth: int) -> float:
    return kelly_cycle_w(p, bracket_w(bracket, cost), depth)


def kelly_single(p: float, bracket: float, cost: float) -> float:
    """Kelly fraction of the bankroll at risk on one bracket bet (no ladder)."""
    return kelly_single_w(p, bracket_w(bracket, cost))


def ladder_growth(w: float) -> float:
    return (1.0 + w) / w


def cycle_shape_w(w: float, depth: int) -> tuple[float, float]:
    """(G, R) per $1 of first-bet risk."""
    g = ladder_growth(w)
    return w, sum(g ** k for k in range(depth + 1))


def kelly_cycle_w(p: float, w: float, depth: int) -> float:
    g, r = cycle_shape_w(w, depth)
    pc = cycle_win_prob(p, depth)
    return pc - (1.0 - pc) * r / g


def kelly_single_w(p: float, w: float) -> float:
    return p - (1.0 - p) / w


def plan_cycle_w(bankroll: float, p: float, w: float, depth: int, sizing: str, kelly_fraction: float,
                 max_cycle_risk: float) -> CyclePlan:
    """Size a cycle of fully-funded bets (no margin: the ladder's total risk
    is cash). n0 is the first bet's risk in dollars; 0 = don't fund."""
    if w <= 0:
        return CyclePlan(0.0, 0.0, 0.0, cycle_win_prob(p, depth), -1.0, 0.0)
    if sizing == "flat":
        depth = 0
        k = kelly_single_w(p, w)
    else:
        k = kelly_cycle_w(p, w, depth)
    g, r = cycle_shape_w(w, depth)
    frac = 1.0 if sizing == "full" else max(0.0, min(k * kelly_fraction, max_cycle_risk))
    n0 = frac * bankroll / r
    return CyclePlan(n0=n0, base_profit=n0 * g, risk=n0 * r, win_prob=cycle_win_prob(p, depth), kelly=k,
                     risk_fraction=frac)


def plan_cycle(bankroll: float, p: float, bracket: float, cost: float, depth: int, sizing: str,
               kelly_fraction: float, max_cycle_risk: float, leverage: float) -> CyclePlan:
    """Size a new cycle. n0 == 0 means don't fund it (paper bets only).

    The deepest bet's initial margin (notional / leverage) has to fit in what
    the bankroll still holds after the ladder's earlier losses.
    """
    if sizing == "flat":
        depth = 0
        k = kelly_single(p, bracket, cost)
    else:
        k = kelly_cycle(p, bracket, cost, depth)
    g, r, n_max = cycle_shape(bracket, cost, depth)
    if sizing == "full":
        frac = 1.0
    else:
        frac = max(0.0, min(k * kelly_fraction, max_cycle_risk))
    n0 = frac * bankroll / r if r > 0 else 0.0
    # margin: the last bet's margin + earlier losses <= bankroll
    last_loss = n_max * (bracket + cost)
    per_n0 = (r - last_loss) + n_max / leverage
    n0 = min(n0, bankroll / per_n0)
    return CyclePlan(n0=n0, base_profit=n0 * g, risk=n0 * r, win_prob=cycle_win_prob(p, depth), kelly=k,
                     risk_fraction=n0 * r / bankroll if bankroll > 0 else 0.0)


class WinRateEstimator:
    """Walk-forward per-bet win rate: the last `window` resolved bets, shrunk
    toward 0.5 with `prior` pseudo-bets."""

    def __init__(self, prior: float, window: int = 0):
        self.prior = prior
        self.window = window
        self.results: list[int] = []
        self.wins = 0

    def add(self, won: bool):
        self.results.append(1 if won else 0)
        self.wins += 1 if won else 0
        if self.window and len(self.results) > self.window:
            self.wins -= self.results[-self.window - 1]

    @property
    def n(self) -> int:
        return min(len(self.results), self.window) if self.window else len(self.results)

    @property
    def p(self) -> float:
        return (self.wins + 0.5 * self.prior) / (self.n + self.prior)
