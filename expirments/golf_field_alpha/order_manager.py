"""Places (or, by default, simulates) YES orders on golf player markets.
Defaults to dry-run: real order placement requires both
GOLF_FIELD_ALPHA_DRY_RUN=false *and* valid Kalshi trading credentials
(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).

Kalshi's order `side` field is always "bid" (buy YES) or "ask" (sell YES,
i.e. economically buy NO at 1 - price) -- "For event markets, this refers
to the YES leg only." This strategy only ever buys YES, so it always sends
`side="bid"`. `_to_api_order` is kept in the same shape as the crypto
experiments' order managers (which validated the yes/no <-> bid/ask
translation against a real live order on 2026-08-06) in case a NO leg is
ever added.

`limit_price` should be a tick-aligned price already resting on the book
(the `yes_ask` being crossed), not a blended average -- Kalshi rejects
prices that aren't whole-cent aligned (`price_level_structure:
"linear_cent"`). This module does NOT walk order-book depth: a basket leg
is a small marketable limit order at the displayed ask, and a partial fill
is left partial (same simplification as ../btc_implied_prob/order_manager.py;
golf player books are thin, so real slippage/partial-fill behaviour is a
known unmodelled risk -- see ../README.md Limitations).
"""

import logging
import sys
from pathlib import Path

from config import CAPITAL_CAP_DOLLARS, CAPITAL_FRACTION, DRY_RUN, EXPERIMENT_NAME

# kalshi_state.py lives alongside live_execution.py in the Kalshi client
# submodule (bare same-directory imports there, so the dir has to be on
# sys.path) -- same bootstrap as kalshi_gateway.py. See
# prediction_market_scraper/Clients/Kalshi/README.md.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_KALSHI_CLIENT_DIR = _REPO_ROOT / "prediction_market_scraper" / "Clients" / "Kalshi"
if str(_KALSHI_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_KALSHI_CLIENT_DIR))

from kalshi_state import KalshiStateManager  # noqa: E402

logger = logging.getLogger("golf_field_alpha.order_manager")


def _to_api_order(side: str, limit_price: float) -> tuple[str, float]:
    if side == "yes":
        return "bid", round(limit_price, 2)
    if side == "no":
        return "ask", round(1.0 - limit_price, 2)
    raise ValueError(f"unknown side {side!r}, expected 'yes' or 'no'")


class OrderManager:
    # See ../resolution_alpha/order_manager.py's identical class attribute --
    # keeps a __new__-constructed instance (tests injecting a fake _client)
    # from raising AttributeError the first time buy_favored_side touches it.
    _state: KalshiStateManager | None = None

    def __init__(self, dry_run: bool = DRY_RUN, state: KalshiStateManager | None = None):
        self.dry_run = dry_run
        # See ../../prediction_market_scraper/Clients/Kalshi/README.md -- paces this experiment's Kalshi API
        # calls against every other experiment sharing this account, and
        # (once CAPITAL_FRACTION/CAPITAL_CAP_DOLLARS below are configured)
        # caps how much of the real balance counts as this experiment's own
        # bankroll for Kelly sizing.
        self._state = state or KalshiStateManager(
            EXPERIMENT_NAME, dry_run=dry_run,
            capital_fraction=CAPITAL_FRACTION, capital_cap_dollars=CAPITAL_CAP_DOLLARS,
        )
        self._client = None if dry_run else self._state.get_client()

    def get_balance_dollars(self) -> float:
        """This experiment's allocated slice of the real account balance
        (see config.py's CAPITAL_FRACTION/CAPITAL_CAP_DOLLARS -- both default
        to no-op). Kalshi's /portfolio/balance returns `balance` in CENTS
        (confirmed against a real account by
        ../../prediction_market_scraper/Clients/Kalshi/test_live_execution.py
        and by ../btc_implied_prob/order_manager.py) -- not `balance_dollars`.
        Only call when dry_run is False.
        """
        total = float(self._state.get_balance()["balance"]) / 100.0
        return self._state.apply_capital_allocation(total)

    def get_positions(self) -> dict:
        """Real open positions, for positions_store.reconcile_with_kalshi.
        Only call when dry_run is False. market_positions entries carry a
        signed `position_fp` (whole contracts, +YES / -NO) and
        `total_traded_dollars` (cumulative cost basis).
        """
        return self._client.get_positions()

    def buy_favored_side(self, ticker: str, side: str, contracts: float, limit_price: float) -> dict:
        """Places a marketable limit order on `side` ("yes"/"no") for
        `contracts` at `limit_price`. In dry-run, only logs and returns a
        synthetic response.
        """
        api_side, api_price = _to_api_order(side, limit_price)
        price_str = f"{api_price:.4f}"
        count_str = f"{contracts:.2f}"

        if self.dry_run:
            logger.info(
                "[DRY RUN] would BUY %s %s %s @ %s (api side=%s price=%s)",
                count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
            )
            response = {"dry_run": True, "ticker": ticker, "side": side, "count": count_str, "price": price_str}
            if self._state is not None:
                self._state.record_order(
                    ticker=ticker, favored_side=side, api_side=api_side,
                    count=count_str, price=price_str, dry_run=True, response=response,
                )
            return response

        logger.warning(
            "LIVE ORDER: BUY %s %s %s @ %s (api side=%s price=%s)",
            count_str, side.upper(), ticker, f"{limit_price:.4f}", api_side, price_str,
        )
        response = self._client.place_order(ticker=ticker, side=api_side, count=count_str, price=price_str)
        if self._state is not None:
            self._state.record_order(
                ticker=ticker, favored_side=side, api_side=api_side,
                count=count_str, price=price_str, dry_run=False, response=response,
            )
        return response
