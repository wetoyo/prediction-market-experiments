"""Turns a golf field's quotes into a concrete basket of YES legs to buy.

Two methods (config.SELECTION_METHOD), both operating on a list of
FieldQuote(ticker, name, buy_price, implied):

  devig_edge
  ----------
  De-vig the whole field's `implied` prices to fair win probabilities
  (devig.py), then include every player, priced in [MIN_PRICE, MAX_PRICE],
  whose fair prob beats its buy price by at least EDGE_THRESHOLD per
  contract *after* the fee at the size actually bought. Each included leg
  is sized by fractional Kelly on its own edge; the whole basket is then
  scaled down if it exceeds MAX_EVENT_COST_DOLLARS. This is a bet that the
  market has left specific names underpriced relative to its own consensus.

  favorites_basket
  ----------------
  Sort players (priced in [MIN_PRICE, MAX_PRICE]) by buy price descending.
  Walk from the favorite down, accumulating per-unit cost (buy_price + fee)
  until it would exceed 1 - EDGE_THRESHOLD, then stop. Every included leg
  gets the SAME contract count (`unit_contracts`), so exactly one of them
  winning returns `unit_contracts` dollars against a per-unit cost of
  <= 1 - EDGE_THRESHOLD. Loses only if the tournament winner is one of the
  skipped cheap longshots; `skipped_prob` reports that de-vigged tail mass.

Both return a BasketPlan (possibly with zero legs, plus a `reason`), and
neither has any exit logic -- a golf basket rides to the tournament's
resolution. Fees are always the per-contract-at-size fee from fees.py.
"""

import logging
import math
from dataclasses import dataclass, field

import config
from devig import devig
from fees import estimate_fee_dollars, fee_per_contract

logger = logging.getLogger("golf_field_alpha.selection")


@dataclass
class FieldQuote:
    ticker: str
    name: str
    buy_price: float | None   # cost to buy 1 YES; None if no usable ask
    implied: float | None     # raw win-prob estimate for the de-vig; None if unusable


@dataclass
class BasketLeg:
    ticker: str
    name: str
    buy_price: float
    fair_prob: float
    contracts: float
    edge_per_contract: float   # fair - buy_price - fee_per_contract, at `contracts`
    cost: float                # buy_price * contracts + total fee


@dataclass
class BasketPlan:
    method: str
    legs: list[BasketLeg] = field(default_factory=list)
    field_size: int = 0            # players with a usable `implied`
    overround: float = 0.0         # sum of raw implied over the field
    devig_method: str = ""
    basket_prob: float = 0.0       # summed fair prob of the legs = P(basket wins)
    skipped_prob: float = 0.0      # summed fair prob of everything NOT in the basket
    total_cost: float = 0.0        # sum of leg cost (incl. fees)
    unit_contracts: float = 0.0    # favorites_basket only: contracts per leg
    reason: str = ""               # why the basket is empty / was capped, if so

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def expected_value(self) -> float:
        """E[payoff] - cost, using the de-vigged fair probs as truth. For
        favorites_basket, payoff on a win is unit_contracts; for devig_edge
        each leg pays its own contract count. Diagnostic only -- it's
        circular (fair comes from the same prices), so it measures internal
        consistency, not real alpha.
        """
        if self.method == "favorites_basket":
            return self.basket_prob * self.unit_contracts - self.total_cost
        return sum(leg.fair_prob * leg.contracts for leg in self.legs) - self.total_cost


def _kelly_contracts(fair: float, price: float, bankroll: float, fraction: float) -> float:
    if not (0.0 < price < 1.0) or bankroll <= 0.0:
        return 0.0
    edge = fair - price
    if edge <= 0.0:
        return 0.0
    full_kelly_fraction = edge / (1.0 - price)
    stake_dollars = fraction * full_kelly_fraction * bankroll
    return stake_dollars / price


def _prepare(quotes: list[FieldQuote], devig_method: str):
    """Common first half: keep quotes with a usable `implied`, de-vig them
    to fair probs, and return (field_quotes, fair_by_ticker, overround).
    """
    usable = [q for q in quotes if q.implied is not None and 0.0 < q.implied < 1.0]
    overround = sum(q.implied for q in usable)
    if len(usable) < 2:
        return usable, {}, overround
    fair_list = devig([q.implied for q in usable], devig_method)
    fair_by_ticker = {q.ticker: f for q, f in zip(usable, fair_list)}
    return usable, fair_by_ticker, overround


def _tradeable(q: FieldQuote) -> bool:
    return (
        q.buy_price is not None
        and config.MIN_PRICE <= q.buy_price <= config.MAX_PRICE
    )


def _empty_plan(method: str, field_size: int, overround: float, devig_method: str, reason: str) -> BasketPlan:
    return BasketPlan(
        method=method, field_size=field_size, overround=overround,
        devig_method=devig_method, reason=reason,
    )


def _plan_devig_edge(
    quotes: list[FieldQuote], *, edge_threshold: float, devig_method: str,
    bankroll: float, kelly_fraction: float, max_contracts_per_player: float,
    max_event_cost: float,
) -> BasketPlan:
    usable, fair_by_ticker, overround = _prepare(quotes, devig_method)
    plan = BasketPlan(
        method="devig_edge", field_size=len(usable), overround=overround, devig_method=devig_method,
    )
    if not fair_by_ticker:
        plan.reason = "field too small to de-vig"
        return plan

    # First pass: pick legs and a Kelly-desired size for each.
    raw_legs: list[BasketLeg] = []
    for q in usable:
        if not _tradeable(q):
            continue
        fair = fair_by_ticker.get(q.ticker, 0.0)
        desired = _kelly_contracts(fair, q.buy_price, bankroll, kelly_fraction)
        contracts = min(math.floor(desired), math.floor(max_contracts_per_player))
        if contracts < 1:
            continue
        fpc = fee_per_contract(q.buy_price, contracts)
        edge = fair - q.buy_price - fpc
        if edge < edge_threshold:
            continue
        raw_legs.append(BasketLeg(
            ticker=q.ticker, name=q.name, buy_price=q.buy_price, fair_prob=fair,
            contracts=float(contracts), edge_per_contract=edge,
            cost=q.buy_price * contracts + estimate_fee_dollars(q.buy_price, contracts),
        ))

    if not raw_legs:
        plan.reason = "no leg clears edge threshold"
        return plan

    # Second pass: cap the whole basket at max_event_cost by scaling all
    # legs' contract counts by the same factor, then re-flooring and
    # re-checking each leg still clears the edge at its new (smaller) size.
    gross = sum(leg.cost for leg in raw_legs)
    scale = 1.0 if gross <= max_event_cost else max_event_cost / gross
    for leg in raw_legs:
        contracts = math.floor(leg.contracts * scale) if scale < 1.0 else leg.contracts
        if contracts < 1:
            continue
        fpc = fee_per_contract(leg.buy_price, contracts)
        edge = leg.fair_prob - leg.buy_price - fpc
        if edge < edge_threshold:
            continue
        plan.legs.append(BasketLeg(
            ticker=leg.ticker, name=leg.name, buy_price=leg.buy_price, fair_prob=leg.fair_prob,
            contracts=float(contracts), edge_per_contract=edge,
            cost=leg.buy_price * contracts + estimate_fee_dollars(leg.buy_price, contracts),
        ))

    plan.total_cost = sum(leg.cost for leg in plan.legs)
    leg_tickers = {leg.ticker for leg in plan.legs}
    plan.basket_prob = sum(f for t, f in fair_by_ticker.items() if t in leg_tickers)
    plan.skipped_prob = sum(f for t, f in fair_by_ticker.items() if t not in leg_tickers)
    if not plan.legs:
        plan.reason = "all legs fell below edge threshold after cost cap"
    return plan


def _plan_favorites_basket(
    quotes: list[FieldQuote], *, edge_threshold: float, devig_method: str,
    max_contracts_per_player: float, max_event_cost: float,
) -> BasketPlan:
    usable, fair_by_ticker, overround = _prepare(quotes, devig_method)
    plan = BasketPlan(
        method="favorites_basket", field_size=len(usable), overround=overround, devig_method=devig_method,
    )
    if not fair_by_ticker:
        plan.reason = "field too small to de-vig"
        return plan

    tradeable = sorted(
        (q for q in usable if _tradeable(q)),
        key=lambda q: q.buy_price, reverse=True,
    )
    if not tradeable:
        plan.reason = "no players priced in [MIN_PRICE, MAX_PRICE]"
        return plan

    budget = 1.0 - edge_threshold
    chosen: list[FieldQuote] = []
    per_unit_cost = 0.0
    for q in tradeable:
        # per-unit cost uses the 1-contract fee; the real fee at unit_contracts
        # is slightly higher (quadratic), re-checked after sizing below.
        marginal = q.buy_price + estimate_fee_dollars(q.buy_price, 1.0)
        if per_unit_cost + marginal > budget:
            break
        chosen.append(q)
        per_unit_cost += marginal

    if not chosen:
        plan.reason = f"even the cheapest tradeable player exceeds budget 1 - {edge_threshold:.3f}"
        # still report the tail we'd have skipped
        plan.skipped_prob = sum(fair_by_ticker.values())
        return plan

    # Size: largest whole unit count whose real (quadratic-fee) basket cost
    # stays within max_event_cost and max_contracts_per_player.
    def _real_unit_cost(n: float) -> float:
        return sum(q.buy_price * n + estimate_fee_dollars(q.buy_price, n) for q in chosen)

    unit = min(
        math.floor(max_contracts_per_player),
        math.floor(max_event_cost / _real_unit_cost(1.0)) if _real_unit_cost(1.0) > 0 else 0,
    )
    while unit > 1 and _real_unit_cost(unit) > max_event_cost:
        unit -= 1
    if unit < 1:
        plan.reason = "unit size floors to 0 under MAX_EVENT_COST_DOLLARS"
        return plan

    plan.unit_contracts = float(unit)
    for q in chosen:
        fpc = fee_per_contract(q.buy_price, unit)
        fair = fair_by_ticker.get(q.ticker, 0.0)
        plan.legs.append(BasketLeg(
            ticker=q.ticker, name=q.name, buy_price=q.buy_price, fair_prob=fair,
            contracts=float(unit), edge_per_contract=fair - q.buy_price - fpc,
            cost=q.buy_price * unit + estimate_fee_dollars(q.buy_price, unit),
        ))
    plan.total_cost = sum(leg.cost for leg in plan.legs)
    leg_tickers = {leg.ticker for leg in plan.legs}
    plan.basket_prob = sum(f for t, f in fair_by_ticker.items() if t in leg_tickers)
    plan.skipped_prob = sum(f for t, f in fair_by_ticker.items() if t not in leg_tickers)
    return plan


def plan_basket(
    quotes: list[FieldQuote],
    *,
    method: str | None = None,
    edge_threshold: float | None = None,
    devig_method: str | None = None,
    bankroll: float | None = None,
    kelly_fraction: float | None = None,
    max_contracts_per_player: float | None = None,
    max_event_cost: float | None = None,
    min_field_size: float | None = None,
    max_overround: float | None = None,
) -> BasketPlan:
    """Builds a BasketPlan for one event's field. Every parameter defaults
    to the matching config.py value, so live code can call this with just
    `quotes` and the backtest can override per-sweep.

    Returns a plan with zero legs and a populated `reason` when the field
    is too thin, the overround is too wide, or nothing clears the edge --
    callers check `plan.n_legs`.
    """
    method = method or config.SELECTION_METHOD
    edge_threshold = config.EDGE_THRESHOLD if edge_threshold is None else edge_threshold
    devig_method = devig_method or config.DEVIG_METHOD
    bankroll = config.DRY_RUN_SIMULATED_BALANCE_DOLLARS if bankroll is None else bankroll
    kelly_fraction = config.KELLY_FRACTION if kelly_fraction is None else kelly_fraction
    max_contracts_per_player = (
        config.MAX_CONTRACTS_PER_PLAYER if max_contracts_per_player is None else max_contracts_per_player
    )
    max_event_cost = config.MAX_EVENT_COST_DOLLARS if max_event_cost is None else max_event_cost
    min_field_size = config.MIN_FIELD_SIZE if min_field_size is None else min_field_size
    max_overround = config.MAX_OVERROUND if max_overround is None else max_overround

    usable, _fair, overround = _prepare(quotes, devig_method)
    if len(usable) < min_field_size:
        return _empty_plan(method, len(usable), overround, devig_method,
                           f"field size {len(usable)} < MIN_FIELD_SIZE {min_field_size:g}")
    if overround > max_overround:
        return _empty_plan(method, len(usable), overround, devig_method,
                           f"overround {overround:.3f} > MAX_OVERROUND {max_overround:.3f}")

    if method == "devig_edge":
        return _plan_devig_edge(
            quotes, edge_threshold=edge_threshold, devig_method=devig_method, bankroll=bankroll,
            kelly_fraction=kelly_fraction, max_contracts_per_player=max_contracts_per_player,
            max_event_cost=max_event_cost,
        )
    if method == "favorites_basket":
        return _plan_favorites_basket(
            quotes, edge_threshold=edge_threshold, devig_method=devig_method,
            max_contracts_per_player=max_contracts_per_player, max_event_cost=max_event_cost,
        )
    raise ValueError(f"unknown selection method {method!r} (expected 'devig_edge' or 'favorites_basket')")
