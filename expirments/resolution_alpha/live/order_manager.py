"""Places (or, by default, simulates) orders on the favored side of a
market. Defaults to dry-run: real order placement requires both
RESOLUTION_ALPHA_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH) -- see ./README.md.

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
level: Kalshi rejects any price that isn't tick-aligned (whole cents --
`price_level_structure: "linear_cent"`, `step: "0.0100"` on every market
checked) with `400 {"error":{"code":"invalid_price", ...}}`. `limit_price`
here MUST be the boundary (worst-acceptable) price of a walked fill --
i.e. `fill.levels_used[-1][0]` from orderbook.walk_book -- not
`fill.avg_price`. avg_price is a blended average across however many levels
got walked, which lands on fractional cents (e.g. 0.0243) as soon as a fill
spans multiple levels; the boundary price is always one of the book's own
already-cent-aligned quoted levels, and Kalshi's own matching engine walks
the book itself from there, filling each contract at its own (better-or-
equal) price -- exactly the maker+taker split already observed on this
account's real multi-level fills. `_to_api_order` also defensively rounds to
the cent as a hygiene net against float noise, not as the primary fix.
"""

import logging

from config import DRY_RUN
from kalshi_gateway import KalshiTradingClient

logger = logging.getLogger("resolution_alpha.order_manager")


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
    """Translates a favored-side ("yes"/"no") + its quoted price into
    Kalshi's API terms: (api_side "bid"/"ask", price always in YES-dollar
    terms). Buying NO at price P == selling YES at (1 - P). Rounds to the
    cent -- Kalshi rejects non-tick-aligned prices (see module docstring);
    `limit_price` should already be a tick-aligned boundary price, this is
    just a defensive net against float noise, not the primary fix.
    """
    if side == "yes":
        return "bid", round(limit_price, 2)
    if side == "no":
        return "ask", round(1.0 - limit_price, 2)
    raise ValueError(f"unknown side {side!r}, expected 'yes' or 'no'")


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

    def buy_favored_side(self, ticker: str, side: str, contracts: float, limit_price: float) -> dict:
        """Places a marketable limit order on `side` ("yes"/"no") for
        `contracts` at `limit_price` (dollars, 0-1, quoted in that side's own
        terms -- see orderbook.walk_book). `limit_price` must be the boundary
        (worst-acceptable) price of the walked fill, i.e.
        `fill.levels_used[-1][0]`, NOT `fill.avg_price` -- see module
        docstring, an averaged multi-level price gets rejected by Kalshi as
        a non-tick-aligned price. In dry-run mode, only logs the intended
        trade and returns a synthetic response.
        """
        api_side, api_price = _to_api_order(side, limit_price)
        price_str = f"{api_price:.4f}"
        count_str = f"{contracts:.2f}"

        if self.dry_run:
            logger.info(
                "[DRY RUN] would BUY %s %s %s @ %s (api side=%s price=%s)",
                count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
            )
            return {"dry_run": True, "ticker": ticker, "side": side, "count": count_str, "price": price_str}

        logger.warning(
            "LIVE ORDER: BUY %s %s %s @ %s (api side=%s price=%s)",
            count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
        )
        return self._client.place_order(ticker=ticker, side=api_side, count=count_str, price=price_str)
