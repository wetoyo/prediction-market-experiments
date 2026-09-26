"""Places (or, by default, simulates) orders on the favored side of a
market. Defaults to dry-run: real order placement requires both
RESOLUTION_ALPHA_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH) -- see live/README.md.

Endpoint verified against Kalshi's current V2 API docs on 2026-08-05
(docs.kalshi.com/api-reference/orders/*): KalshiTradingClient.place_order's
`/portfolio/events/orders` (POST/DELETE) and get_orders' `/portfolio/orders`
(GET) are both correct -- an earlier note here claiming a path mismatch was
wrong and has been removed.

The *host* was a real bug, found live 2026-08-27: `/portfolio/events/orders`
is only served from `external-api.kalshi.com`, and the read-only portfolio
endpoints' host (`api.elections.kalshi.com`) 404s on it. Fixed in
live_execution.py's BASE_URL -- see that module and docs/reference/
kalshi-client.md.

What *was* a real bug, also caught while verifying against the docs before
this endpoint check turned into a live test order: Kalshi's order `side`
field is always `"bid"` (buy YES) or `"ask"` (sell YES, i.e. economically
buy NO at `1 - price`) -- "For event markets, this refers to the YES leg
only." This module's `side` param is the model's favored side, `"yes"` or
`"no"`; that was previously being passed straight through as the API's
`side` field, which would have either been rejected outright (invalid enum)
or, worse, silently misinterpreted. `_to_api_order` below does the
translation.

Second real bug, found live 2026-08-06 once Kelly sizing started producing
multi-contract trades that routinely spanned more than one order-book price
level: Kalshi rejects any price that isn't tick-aligned with
`400 {"error":{"code":"invalid_price", ...}}`. `limit_price` here MUST be the
boundary (worst-acceptable) price of a walked fill -- i.e.
`fill.levels_used[-1][0]` from orderbook.walk_book -- not `fill.avg_price`.
avg_price is a blended average across however many levels got walked, which
lands off-tick as soon as a fill spans multiple levels; the boundary price is
always one of the book's own already-tick-aligned quoted levels, and Kalshi's
own matching engine walks the book itself from there, filling each contract
at its own (better-or-equal) price -- exactly the maker+taker split already
observed on this account's real multi-level fills.

The tick size is NOT always a whole cent. When first checked (2026-08-06) the
crypto interval series were `price_level_structure: "linear_cent"`
(`step: "0.0100"`); by late Aug 2026 KXBTC15M/KXETH15M report
`tapered_deci_cent` -- 0.001 steps in [0, 0.10] and [0.90, 1.00], 0.01 in
between -- and this strategy only ever trades the favored side at >= 0.90, so
it lives entirely in a deci-cent band. `_to_api_order` therefore rounds only
to 4 decimals (float-noise hygiene), never to the cent: `limit_price` is a
real book level and is already correctly tick-aligned for whatever structure
the market uses. Rounding it to the cent (the old behaviour) submitted a
marketable limit below the boundary it was meant to clear -> under-fill with
an unfillable remainder, or, above ~0.995, rounded to 1.0000/0.0000 ->
`invalid_price` -> the ticker blacklisted for the rest of the cycle.
"""

import logging

from config import DRY_RUN
from kalshi_gateway import KalshiTradingClient

logger = logging.getLogger("resolution_alpha.order_manager")


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
    """Translates a favored-side ("yes"/"no") + its quoted price into
    Kalshi's API terms: (api_side "bid"/"ask", price always in YES-dollar
    terms). Buying NO at price P == selling YES at (1 - P).

    Rounds to 4 decimals (0.0001) purely as a float-noise net -- NOT to the
    whole cent. `limit_price` is already a real order-book level
    (`fill.levels_used[-1][0]`, a boundary price the book itself quoted), so
    it is already tick-aligned for whatever `price_level_structure` the
    market uses. The crypto interval series this strategy trades migrated
    from `linear_cent` (0.01 tick) to `tapered_deci_cent` (0.001 tick in
    [0, 0.10] and [0.90, 1.00]) -- and this strategy only ever trades the
    favored side at >= 0.90 (config.MIN_MARKET_IMPLIED_PROBABILITY), i.e.
    squarely inside a deci-cent band. The old `round(_, 2)` here silently
    un-aligned every one of those prices (0.973 -> 0.97): a marketable limit
    submitted below the boundary it was meant to clear -> partial/under-fill
    with a GTC remainder resting unfillable, or, for the favored side above
    ~0.995, `round` -> 1.0000 (yes) / 0.0000 (no) -> Kalshi `invalid_price`
    -> the ticker gets blacklisted for the rest of the cycle (runner.py).
    Subtraction preserves tick alignment (1 - 0.973 = 0.027 is still
    deci-cent-aligned), so `round(_, 4)` is safe for both structures.
    """
    if side == "yes":
        price = round(limit_price, 4)
    elif side == "no":
        price = round(1.0 - limit_price, 4)
    else:
        raise ValueError(f"unknown side {side!r}, expected 'yes' or 'no'")
    if not (0.0 < price < 1.0):
        raise ValueError(
            f"{side} limit_price {limit_price!r} -> api price {price!r}, outside (0, 1); not a tradeable level"
        )
    return ("bid" if side == "yes" else "ask"), price


class OrderManager:
    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run
        self._client = None
        if not dry_run:
            self._client = KalshiTradingClient()

    def get_balance_dollars(self) -> float:
        """Real account balance, for Kelly sizing (runner.py's
        _kelly_contracts). Only call when dry_run is False -- there's no
        client to query otherwise; runner.py uses
        config.DRY_RUN_SIMULATED_BALANCE_DOLLARS in that case instead.
        """
        return float(self._client.get_balance()["balance_dollars"])

    def get_shard_balances(self) -> dict[int, float]:
        """Per-exchange-shard available balance, in dollars, keyed by
        `exchange_index` (Kalshi Exchange Sharding). Collateral is local to a
        shard: an order routed to a shard with $0 here is rejected outright
        (`404 user_not_found`). runner.py uses this to warn/skip before
        attempting an order on an unfunded shard rather than letting every
        such attempt 404 and blacklist the ticker. Only call when dry_run is
        False.
        """
        breakdown = self._client.get_balance().get("balance_breakdown", [])
        return {int(row["exchange_index"]): float(row["balance"]) for row in breakdown}

    def buy_favored_side(
        self,
        ticker: str,
        side: str,
        contracts: float,
        limit_price: float,
        exchange_index: int | None = None,
        time_in_force: str = "immediate_or_cancel",
        reduce_only: bool | None = None,
    ) -> dict:
        """Places a marketable limit order on `side` ("yes"/"no") for
        `contracts` at `limit_price` (dollars, 0-1, quoted in that side's own
        terms -- see orderbook.walk_book). `limit_price` must be the boundary
        (worst-acceptable) price of the walked fill, i.e.
        `fill.levels_used[-1][0]`, NOT `fill.avg_price` -- see module
        docstring, an averaged multi-level price gets rejected by Kalshi as
        a non-tick-aligned price.

        `time_in_force` defaults to `immediate_or_cancel`: this strategy is
        pure taker (a marketable limit at the book's own boundary price) and
        never wants a remainder resting -- a partial fill from being beaten to
        the book used to leave a GTC order sitting at a now-unfillable price
        with nothing in the loop to cancel it. IOC fills what it can right now
        and Kalshi drops the rest.

        `exchange_index` routes the order to a specific Kalshi exchange shard
        (see live_execution.place_order / config note) -- pass the market's own
        index; None auto-routes by ticker. `reduce_only` forbids opening or
        growing a position (exit sells only).

        In dry-run mode, only logs the intended trade and returns a synthetic
        response.
        """
        api_side, api_price = _to_api_order(side, limit_price)
        price_str = f"{api_price:.4f}"
        count_str = f"{contracts:.2f}"

        if self.dry_run:
            logger.info(
                "[DRY RUN] would BUY %s %s %s @ %s (api side=%s price=%s tif=%s shard=%s)",
                count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
                time_in_force, exchange_index,
            )
            return {"dry_run": True, "ticker": ticker, "side": side, "count": count_str, "price": price_str}

        logger.warning(
            "LIVE ORDER: BUY %s %s %s @ %s (api side=%s price=%s tif=%s shard=%s)",
            count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
            time_in_force, exchange_index,
        )
        return self._client.place_order(
            ticker=ticker, side=api_side, count=count_str, price=price_str,
            time_in_force=time_in_force, exchange_index=exchange_index, reduce_only=reduce_only,
        )
