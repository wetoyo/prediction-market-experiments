"""A fake Kalshi account for the ledger tests: GTC orders that fill partly at
once and rest, later (maker) fills, cancels, holds on resting orders, yes+no
pair redemption, settlements. Record and response shapes are copied from the
real payloads (resolution_alpha/tests/test_sim_bankroll.py, and GET
/portfolio/orders records read on the Pi 2026-09-26: `outcome_side`,
`status` resting/canceled/executed, `remaining_count_fp`, `*_price_dollars`).

Resting orders hold (uncovered remaining) x limit out of the available
balance -- the same unverified assumption tagged_ledger.py makes, so these
tests check the wiring, not Kalshi's rules.
"""

import time
import uuid
from datetime import datetime, timezone


class FakeKalshi:
    def __init__(self, balance: float = 100.0, fee_per_contract: float = 0.0123):
        self.cash = balance  # excluding holds
        self.fee_per_contract = fee_per_contract
        self.orders: dict[str, dict] = {}
        self.yes: dict[str, float] = {}
        self.no: dict[str, float] = {}
        self.settlements: list[dict] = []
        self.requests: list[str] = []
        self.next_fill: float | None = None  # contracts the next POST fills at once (None: all)
        self._n = 0

    # -- helpers for tests -----------------------------------------------------

    def holds(self) -> float:
        covered: dict[tuple[str, str], float] = {}
        total = 0.0
        for o in self.orders.values():
            if o["status"] != "resting":
                continue
            side, ticker = o["outcome_side"], o["ticker"]
            opposite = self.no if side == "yes" else self.yes
            key = (ticker, side)
            free = max(0.0, opposite.get(ticker, 0.0) - covered.get(key, 0.0))
            remaining = float(o["remaining_count_fp"])
            cover = min(remaining, free)
            covered[key] = covered.get(key, 0.0) + cover
            total += (remaining - cover) * o["_limit"]
        return total

    def available(self) -> float:
        return self.cash - self.holds()

    def fill_resting(self, order_id: str, count: float) -> None:
        o = self.orders[order_id]
        self._fill(o, min(count, float(o["remaining_count_fp"])), maker=True)

    def cancel(self, order_id: str) -> None:
        o = self.orders[order_id]
        o["status"] = "canceled"
        o["remaining_count_fp"] = "0.00"

    def settle(self, ticker: str, result: str) -> None:
        payout = (self.yes if result == "yes" else self.no).pop(ticker, 0.0)
        (self.no if result == "yes" else self.yes).pop(ticker, None)
        self.cash += payout
        self.settlements.append({"ticker": ticker, "market_result": result, "settled_time": time.time()})

    def outside_fill(self, ticker: str, side: str, count: float, price: float) -> None:
        """A fill nobody's ledger knows about (a manual trade on the website)."""
        self.cash -= count * price
        book = self.yes if side == "yes" else self.no
        book[ticker] = book.get(ticker, 0.0) + count

    # -- KalshiTradingClient surface ---------------------------------------------

    def get_balance(self) -> dict:
        return {"balance_dollars": f"{self.available():.4f}", "balance": int(round(self.available() * 100))}

    def get_positions(self, ticker=None, event_ticker=None) -> dict:
        rows = []
        for t in set(self.yes) | set(self.no):
            net = self.yes.get(t, 0.0) - self.no.get(t, 0.0)
            if net:
                rows.append({"ticker": t, "position_fp": f"{net:.2f}", "total_traded_dollars": "1.00"})
        return {"market_positions": rows, "event_positions": []}

    def get_orders(self, status=None, ticker=None) -> dict:
        return {"orders": [self._public(o) for o in self.orders.values()
                           if (status is None or o["status"] == status) and (ticker is None or o["ticker"] == ticker)]}

    def cancel_order(self, order_id: str) -> dict:
        self.cancel(order_id)
        return {"order": self._public(self.orders[order_id])}

    def place_order(self, ticker, side, count, price, time_in_force="good_till_canceled",
                    client_order_id=None, exchange_index=None, reduce_only=None) -> dict:
        outcome = "yes" if side == "bid" else "no"
        yes_price = float(price)
        limit = yes_price if outcome == "yes" else round(1.0 - yes_price, 4)
        count = float(count)
        order_id = f"o{self._n}"
        self._n += 1
        o = {
            "order_id": order_id, "ticker": ticker, "client_order_id": client_order_id or str(uuid.uuid4()),
            "outcome_side": outcome, "side": "yes", "action": "buy" if outcome == "yes" else "sell",
            "status": "resting", "type": "limit", "_limit": limit, "_created": time.time(),
            "yes_price_dollars": f"{yes_price:.4f}", "no_price_dollars": f"{1.0 - yes_price:.4f}",
            "initial_count_fp": f"{count:.2f}", "fill_count_fp": "0.00", "remaining_count_fp": f"{count:.2f}",
            "taker_fill_cost_dollars": "0.000000", "maker_fill_cost_dollars": "0.000000",
            "taker_fees_dollars": "0.000000", "maker_fees_dollars": "0.000000",
            "created_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        self.orders[order_id] = o
        fill = count if self.next_fill is None else min(self.next_fill, count)
        self.next_fill = None
        if fill > 0:
            self._fill(o, fill, maker=False)
        if time_in_force == "immediate_or_cancel" and o["status"] == "resting":
            self.cancel(order_id)
        fee_avg = self.fee_per_contract if fill > 0 else 0.0
        return {  # POST /portfolio/events/orders: 4-decimal per-contract averages only
            "order_id": order_id, "fill_count": f"{fill:.2f}",
            "average_fill_price": f"{yes_price:.4f}" if fill > 0 else None,
            "average_fee_paid": f"{fee_avg:.4f}", "remaining_count": o["remaining_count_fp"],
        }

    def _request(self, method, path, params=None, json_body=None) -> dict:
        self.requests.append(path)
        params = params or {}
        if path.startswith("/portfolio/orders/"):
            return {"order": self._public(self.orders[path.rsplit("/", 1)[1]])}
        if path == "/portfolio/orders":
            rows = [o for o in self.orders.values()
                    if ("status" not in params or o["status"] == params["status"])
                    and ("min_ts" not in params or o["_created"] >= params["min_ts"])]
            return {"orders": [self._public(o) for o in rows], "cursor": ""}
        if path == "/portfolio/settlements":
            rows = [s for s in self.settlements if s["settled_time"] >= params.get("min_ts", 0)]
            return {"settlements": rows, "cursor": ""}
        raise AssertionError(path)

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _public(o: dict) -> dict:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _fill(self, o: dict, count: float, maker: bool) -> None:
        price, fee = o["_limit"], self.fee_per_contract * count
        self.cash -= price * count + fee
        kind = "maker" if maker else "taker"
        o[f"{kind}_fill_cost_dollars"] = f"{float(o[f'{kind}_fill_cost_dollars']) + price * count:.6f}"
        o[f"{kind}_fees_dollars"] = f"{float(o[f'{kind}_fees_dollars']) + fee:.6f}"
        o["fill_count_fp"] = f"{float(o['fill_count_fp']) + count:.2f}"
        remaining = float(o["remaining_count_fp"]) - count
        o["remaining_count_fp"] = f"{remaining:.2f}"
        if remaining <= 1e-9:
            o["status"] = "executed"
        ticker, side = o["ticker"], o["outcome_side"]
        book, other = (self.yes, self.no) if side == "yes" else (self.no, self.yes)
        book[ticker] = book.get(ticker, 0.0) + count
        pairs = min(book[ticker], other.get(ticker, 0.0))
        if pairs > 0:  # a matched yes+no pair redeems for $1
            self.cash += pairs
            book[ticker] -= pairs
            other[ticker] -= pairs
        for b in (book, other):
            if b.get(ticker, 1.0) <= 1e-9:
                b.pop(ticker, None)
