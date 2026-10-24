"""Pulls this account's full Kalshi fill and settlement history via the
read-only portfolio REST endpoints and reconstructs a per-market trade
ledger: entry price, realized P&L, and win/loss.

Read-only: only issues GET requests (fills, settlements). Safe to run
anytime, including while the live runner is trading.

P&L reconstruction (v2 -- v1 was wrong, see below): every fill, on either
side, is a real cost (you pay yes_price_dollars or no_price_dollars per
contract, whichever `side` the fill is on -- there is no "sell for a
credit"). Closing a position by transacting the opposite side does not
receive that side's price as a credit; instead, each matched yes+no pair
you've accumulated on a ticker redeems for exactly $1 combined, as its own
cash event. Settlement's `revenue` (cents) then only pays out on the
*unmatched* remainder still held at close (0 when yes_count_fp ==
no_count_fp, i.e. fully paired off -- confirmed empirically). So per
ticker: net P&L = revenue/100 + min(yes_count_fp, no_count_fp)*1.00 -
yes_total_cost_dollars - no_total_cost_dollars - fee_cost.

v1 of this script instead treated a fill on the side opposite the initial
entry as a credit at that side's own price (e.g. closing 6 YES bought @0.86
by transacting 6 NO @0.96 was scored as +$0.53). That happened to look
plausible in isolation, but was never checked against real ground truth,
and the aggregate result it produced (+$566 to +$822 total realized P&L)
flatly contradicted the account's actual balance ($3.24). Re-derived from
that contradiction, not from further guessing at Kalshi's fill schema.

Usage: python reconstruct_trade_history.py [--out-dir DIR]
Writes fills.json, settlements.json, and trades.csv into --out-dir
(default: live/logs/ next to this file).
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / "live" / ".env"
KALSHI_CLIENT_DIR = HERE.parent.parent / "prediction_market_scraper" / "Clients" / "Kalshi"


def _load_env(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"missing .env at {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env(ENV_PATH)
sys.path.insert(0, str(KALSHI_CLIENT_DIR))
from live_execution import KalshiTradingClient  # noqa: E402


def _paginate(client: KalshiTradingClient, path: str, item_key: str, params: dict | None = None) -> list[dict]:
    items = []
    cursor = None
    params = dict(params or {})
    params.setdefault("limit", 1000)
    while True:
        if cursor:
            params["cursor"] = cursor
        page = client._request("GET", path, params=params)
        items.extend(page.get(item_key, []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


def fetch_all(client: KalshiTradingClient) -> tuple[list[dict], list[dict]]:
    fills = _paginate(client, "/portfolio/fills", "fills")
    settlements = _paginate(client, "/portfolio/settlements", "settlements")
    return fills, settlements


def _fill_price(fill: dict) -> float:
    key = "yes_price_dollars" if fill["side"] == "yes" else "no_price_dollars"
    return float(fill[key])


def build_trade_ledger(fills: list[dict], settlements: list[dict]) -> list[dict]:
    settlement_by_ticker = {s["ticker"]: s for s in settlements}

    fills_by_ticker: dict[str, list[dict]] = {}
    for f in fills:
        fills_by_ticker.setdefault(f["ticker"], []).append(f)

    rows = []
    for ticker, ticker_fills in fills_by_ticker.items():
        ticker_fills.sort(key=lambda f: f.get("created_time", ""))
        settlement = settlement_by_ticker.get(ticker)
        if settlement is None:
            continue  # not yet resolved -- can't compute realized P&L

        entry_side = ticker_fills[0]["side"]
        entry_count = 0.0
        total_fees = sum(float(f.get("fee_cost", 0.0)) for f in ticker_fills)
        for f in ticker_fills:
            if f["side"] == entry_side:
                entry_count += float(f["count_fp"])

        yes_cost = float(settlement["yes_total_cost_dollars"])
        no_cost = float(settlement["no_total_cost_dollars"])
        yes_count = float(settlement["yes_count_fp"])
        no_count = float(settlement["no_count_fp"])
        revenue = settlement["revenue"] / 100.0
        matched_pairs = min(yes_count, no_count)

        net_pnl = revenue + matched_pairs * 1.0 - yes_cost - no_cost - total_fees
        entry_cost = yes_cost if entry_side == "yes" else no_cost

        rows.append(
            {
                "ticker": ticker,
                "entry_side": entry_side,
                "first_entry_price": _fill_price(ticker_fills[0]),
                "avg_entry_price": (entry_cost / entry_count) if entry_count else None,
                "entry_count": entry_count,
                "num_fills": len(ticker_fills),
                "first_fill_time": ticker_fills[0].get("created_time"),
                "market_result": settlement.get("market_result"),
                "settlement_revenue_dollars": revenue,
                "fees_dollars": round(total_fees, 6),
                "net_pnl_dollars": round(net_pnl, 6),
                "won": net_pnl > 0,
            }
        )

    rows.sort(key=lambda r: r["first_fill_time"] or "")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(HERE / "live" / "logs"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = KalshiTradingClient()
    fills, settlements = fetch_all(client)
    print(f"fetched {len(fills)} fills, {len(settlements)} settlements")

    (out_dir / "fills.json").write_text(json.dumps(fills, indent=2))
    (out_dir / "settlements.json").write_text(json.dumps(settlements, indent=2))

    rows = build_trade_ledger(fills, settlements)
    csv_path = out_dir / "trades.csv"
    if rows:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(f"wrote {len(rows)} reconstructed trades to {csv_path}")

    total_pnl = sum(r["net_pnl_dollars"] for r in rows)
    print(f"total realized P&L across all reconstructed trades: ${total_pnl:.2f}")


if __name__ == "__main__":
    main()
