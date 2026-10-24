"""Full-set arithmetic for mutually exclusive, exhaustive bucket ladders.

An event with n buckets, exactly one of which resolves YES:

- **YES set**: buy one YES in every bucket. Pays exactly $1. Riskless profit
  if  sum_i (ask_i + fee(ask_i)) < 1.
- **NO set**: buy one NO in every bucket. Exactly n - 1 NOs pay, so it pays
  exactly $(n - 1). NO ask_i = 1 - yes_bid_i. Riskless profit if
  sum_i (1 - bid_i + fee(1 - bid_i)) < n - 1, i.e. roughly sum of yes bids > 1.

Both are only riskless if the ladder really is exhaustive (exactly one YES)
and all n buckets are bought. `exhaustiveness` checks the first from
settled results; the screen only sums minutes at which every bucket was open.
"""

from __future__ import annotations

import math

from fees import fee_per_contract_raw

NO_ASK = 1.0  # a missing YES ask (no sellers) is treated as unbuyable at $1
NO_BID = 0.0


def yes_set_edge(asks: list[float], fee_mult: float = 1.0) -> float:
    """$ profit per YES set, after taker fees, at these asks. Negative = no arb.
    Any ask >= $1 (unbuyable) makes the set unbuyable -> -inf."""
    if any(a >= 1.0 or a <= 0.0 for a in asks):
        return -math.inf
    return 1.0 - sum(a + fee_per_contract_raw(a, multiplier=fee_mult) for a in asks)


def no_set_edge(bids: list[float], fee_mult: float = 1.0) -> float:
    """$ profit per NO set (n NO contracts, pays n - 1), after taker fees."""
    if any(b <= 0.0 or b >= 1.0 for b in bids):
        return -math.inf
    n = len(bids)
    return (n - 1) - sum((1 - b) + fee_per_contract_raw(1 - b, multiplier=fee_mult) for b in bids)


def exhaustiveness(markets: list[dict]) -> dict:
    """From settled market dicts (Kalshi `result`): exactly one 'yes' and the
    rest 'no' is a clean exhaustive ladder. Anything else (0 or 2+ YES, a void
    or scalar settlement, a bucket listed after the others opened) is a risk
    to a full-set trade and is reported."""
    results = [m.get("result") or "" for m in markets]
    n_yes = sum(r == "yes" for r in results)
    other = sorted({r for r in results if r not in ("yes", "no")})
    opens = sorted(m["open_ts"] for m in markets if m.get("open_ts") is not None)
    late = sum(1 for t in opens if opens and t - opens[0] > 300)
    # A "scalar" settlement (tie, cancellation, retirement) pays each bucket a
    # fraction. If the fractions sum to $1 a full set is still made whole.
    vals = [m.get("settle") for m in markets]
    payout = sum(vals) if all(v is not None for v in vals) else None
    return {"n": len(markets), "n_yes": n_yes, "other_results": other, "late_listed": late,
            "clean": n_yes == 1 and not other,
            "payout_sum": payout, "payout_ok": payout is not None and abs(payout - 1.0) < 1e-6}


# ---- order-book depth -----------------------------------------------------

def asks_from_book(opposite_bids: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Kalshi books list bids only. The asks on one side are the other side's
    bids: a NO bid at q is a YES ask at 1 - q. Returns [(price, size)] best
    (cheapest) first. `opposite_bids` is [(price, size)] in any order."""
    return sorted(((round(1.0 - p, 6), s) for p, s in opposite_bids if s > 0), key=lambda x: x[0])


def full_set_depth(ask_ladders: list[list[tuple[float, float]]], payout_per_set: float,
                   fee_mult: float = 1.0) -> dict:
    """Walk every bucket's ask book together (each list best price first).

    Returns the edge per set at the best level of every leg (`top_edge`), the
    size available there (`top_size`, the thinnest leg), and the largest whole
    number of sets (`sets`) that can be bought sweeping deeper levels while
    every marginal set still nets > 0, with the total `profit` and the
    marginal set's worst leg prices (`limits`, the IOC limit for each leg).
    `payout_per_set` is 1 for a YES set and n - 1 for a NO set. Fees are the
    per-contract taker fee at each fill's price."""
    if not ask_ladders or any(not lv for lv in ask_ladders):
        return {"top_edge": -math.inf, "top_size": 0.0, "sets": 0, "profit": 0.0, "limits": []}

    def leg_cost(p: float) -> float:
        return p + fee_per_contract_raw(p, multiplier=fee_mult)

    top_edge = payout_per_set - sum(leg_cost(lv[0][0]) for lv in ask_ladders)
    top_size = min(lv[0][1] for lv in ask_ladders)
    idx = [0] * len(ask_ladders)
    left = [lv[0][1] for lv in ask_ladders]
    qty, profit, last_m = 0.0, 0.0, 0.0
    limits = [lv[0][0] for lv in ask_ladders]
    while True:
        marginal = payout_per_set - sum(leg_cost(lv[i][0]) for lv, i in zip(ask_ladders, idx))
        if marginal <= 0:
            break
        seg = min(left)
        qty += seg
        profit += seg * marginal
        last_m = marginal
        limits = [lv[i][0] for lv, i in zip(ask_ladders, idx)]
        done = False
        for j, lv in enumerate(ask_ladders):
            left[j] -= seg
            if left[j] <= 1e-9:
                idx[j] += 1
                if idx[j] >= len(lv):
                    done = True
                else:
                    left[j] = lv[idx[j]][1]
        if done:
            break
    sets = int(math.floor(qty + 1e-9))
    # Kalshi sizes can be fractional; trade whole sets only. The trimmed tail
    # was bought last, at the lowest marginal edge (exact if the last segment
    # is at least the fractional part).
    profit = profit - (qty - sets) * last_m if sets else 0.0
    return {"top_edge": top_edge, "top_size": top_size, "sets": sets, "profit": profit,
            "limits": limits if sets else []}


def runs(edges: list[float], min_edge: float = 0.0) -> list[tuple[int, int]]:
    """[(start_index, length)] of maximal runs with edge > min_edge."""
    out, start = [], None
    for i, e in enumerate(edges):
        if e > min_edge:
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i - start))
            start = None
    if start is not None:
        out.append((start, len(edges) - start))
    return out
