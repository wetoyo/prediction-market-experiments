"""Read-only check of the Kalshi payload fields Phase 3 relies on but
hasn't yet seen on this account (live/SIM_BANKROLL_HANDOFF.md, "Open
caveat"). Only issues GET requests; safe while the runner is trading.

Checks, on GET /portfolio/orders:
  1. records carry `client_order_id` (restart recovery + reconciler attribution)
  2. records carry `ticker`, `order_id`, `fill_count_fp` and the exact-cost fields
  3. `min_ts` is honoured (fewer/older-free results with a recent min_ts)
  4. orders placed since the tagging deploy show the "ra-..." tag, and in
     which format ("ra-<uuid>" from f16bb50, "ra-<y|n>-<hex>" from 2fb9c95)

Prints field names, counts and client_order_id prefixes only -- no balances,
prices or credentials.

Usage (on the Pi): ../../.venv/bin/python inspect_order_records.py
"""

import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / "live" / ".env"
KALSHI_CLIENT_DIR = HERE.parent.parent / "prediction_market_scraper" / "Clients" / "Kalshi"

NEEDED = ("order_id", "ticker", "client_order_id", "fill_count_fp", "taker_fill_cost_dollars",
          "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars")


def _load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _shape(cid: str | None) -> str:
    if not cid:
        return "<missing>"
    parts = cid.split("-")
    if len(parts) == 3 and parts[1] in ("y", "n"):
        return f"{parts[0]}-<y|n>-<hex>"
    if len(parts) == 2:
        return f"{parts[0]}-<hex>"
    return "<untagged uuid or other>"


def main() -> None:
    _load_env(ENV_PATH)
    sys.path.insert(0, str(KALSHI_CLIENT_DIR))
    from live_execution import KalshiTradingClient

    client = KalshiTradingClient()
    orders = client._request("GET", "/portfolio/orders", params={"limit": 200}).get("orders", [])
    print(f"GET /portfolio/orders (limit 200): {len(orders)} records")
    if not orders:
        return
    keys = Counter(k for o in orders for k in o)
    print("fields present (records having it / total):")
    for k in sorted(keys):
        print(f"  {k}: {keys[k]}/{len(orders)}")
    print("fields Phase 3 needs:")
    for k in NEEDED:
        print(f"  {'OK     ' if keys.get(k) else 'MISSING'} {k}")
    print("client_order_id shapes:", dict(Counter(_shape(o.get("client_order_id")) for o in orders)))

    recent = client._request(
        "GET", "/portfolio/orders", params={"limit": 200, "min_ts": int(time.time()) - 3600},
    ).get("orders", [])
    print(f"with min_ts = 1h ago: {len(recent)} records "
          f"({'looks honoured' if len(recent) < len(orders) or len(orders) < 200 else 'check: same size as unfiltered'})")
    print("  their client_order_id shapes:", dict(Counter(_shape(o.get("client_order_id")) for o in recent)))


if __name__ == "__main__":
    main()
