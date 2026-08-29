"""Live end-to-end smoke test for the buy path.

Places ONE real order for ONE contract at $0.01 through the exact code the
runner uses -- OrderManager(dry_run=False).buy_favored_side ->
KalshiTradingClient.place_order -> Kalshi trade API -- then reports what
happened (order status, fill, resulting position). Max outlay is a couple of
cents (1c notional + up to 1c fee).

Real money. Without --yes-real-money it does everything EXCEPT the place_order
call (loads creds, picks a market, prints the plan). With it, it fires, and
then cancels any unfilled remainder so nothing is left resting.

Run from expirments/resolution_alpha/ with the repo-root venv:

    ../../.venv/Scripts/python.exe tests/live_buy_smoke.py                  # dry (plan only)
    ../../.venv/Scripts/python.exe tests/live_buy_smoke.py --yes-real-money
    ../../.venv/Scripts/python.exe tests/live_buy_smoke.py --yes-real-money --ticker KXBTCD-... --side yes

Credentials + RESOLUTION_ALPHA_* come from live/.env (this project does not
auto-load it, so this script does).
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))


def _load_dotenv(path: Path) -> None:
    import os

    if not path.exists():
        print(f"!! {path} not found -- relying on ambient environment")
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ[key.strip()] = val.strip()


def _pick_ticker(min_seconds_to_close: int = 1800) -> tuple[str, str]:
    """Auto-pick a liquid market with a genuine $0.01 ask, closing far enough
    out that a marketable order fills well before any settlement halt. Returns
    (ticker, side) where `side` is the one offered at 1c.
    """
    from kalshi_gateway import fetch_markets

    now = dt.datetime.now(dt.timezone.utc)

    def f(m, k):
        try:
            return float(m.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    best = None
    for series in ("KXBTCD", "KXETHD", "KXBTC", "KXETH"):
        for m in fetch_markets(series_ticker=series, status="open", limit=200, max_pages=6):
            close = m.get("close_time", "")
            try:
                secs = (dt.datetime.fromisoformat(close.replace("Z", "+00:00")) - now).total_seconds()
            except ValueError:
                continue
            if secs < min_seconds_to_close:
                continue
            for side, ask_key, sz_key in (
                ("yes", "yes_ask_dollars", "yes_ask_size_fp"),
                ("no", "no_ask_dollars", "no_ask_size_fp"),
            ):
                if round(f(m, ask_key), 4) == 0.01 and f(m, sz_key) >= 10:
                    score = f(m, "volume_fp")
                    if best is None or score > best[0]:
                        best = (score, m["ticker"], side, close, f(m, sz_key))
    if best is None:
        raise SystemExit("no market found with a real $0.01 ask closing >30min out")
    _, ticker, side, close, sz = best
    print(f"auto-picked {ticker}  side={side}  (1c ask size={sz:.0f}, closes {close})")
    return ticker, side


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-real-money", action="store_true", help="actually place the order")
    ap.add_argument("--ticker", help="market ticker (default: auto-pick a liquid 1c market)")
    ap.add_argument("--side", choices=("yes", "no"), help="favored side (default: the 1c side)")
    ap.add_argument("--price", type=float, default=0.01, help="limit price in that side's terms (default 0.01)")
    ap.add_argument("--contracts", type=float, default=1, help="contract count (default 1)")
    args = ap.parse_args()

    _load_dotenv(_PKG_DIR / "live" / ".env")

    import config
    from order_manager import OrderManager

    if args.ticker:
        ticker, side = args.ticker, (args.side or "yes")
    else:
        ticker, side = _pick_ticker()
        if args.side:
            side = args.side

    print("\n=== plan ===")
    print(f"  ticker      {ticker}")
    print(f"  side        {side}")
    print(f"  contracts   {args.contracts}")
    print(f"  limit price {args.price}  ({side}-side terms)")
    print(f"  config.DRY_RUN (env RESOLUTION_ALPHA_DRY_RUN) = {config.DRY_RUN}")

    if not args.yes_real_money:
        print("\n--yes-real-money not set -- stopping before place_order. Nothing was sent.")
        return 0

    om = OrderManager(dry_run=False)
    client = om._client

    bal_before = client.get_balance()
    print(f"\nbalance before: ${bal_before['balance_dollars']}")

    print("\n=== placing live order ===")
    resp = om.buy_favored_side(ticker, side, args.contracts, args.price)
    # A fully-matched order comes back flat (fields at top level, no nested
    # "order" and often no "status"); a resting one carries status/remaining.
    order = resp.get("order", resp) or {}
    print("place_order response:")
    for k, v in order.items():
        print(f"  {k}: {v}")

    order_id = order.get("order_id") or order.get("id")
    status = order.get("status")
    try:
        remaining = float(order.get("remaining_count") or 0)
    except (TypeError, ValueError):
        remaining = 0.0
    print(f"\norder_id={order_id}  status={status}  remaining_count={remaining}")

    # Give the match a moment, then report fill + position, and clean up any
    # unfilled remainder so nothing is left resting.
    time.sleep(2.0)

    if order_id and (status == "resting" or remaining > 0):
        try:
            live = client._request("GET", f"/portfolio/orders/{order_id}")
            o = live.get("order", live)
            print(
                "order now: status=%s remaining=%s"
                % (o.get("status"), o.get("remaining_count"))
            )
            if o.get("status") == "resting":
                print("still resting -> cancelling remainder")
                print("cancel:", client.cancel_order(order_id))
        except Exception as e:  # noqa: BLE001 -- best-effort reporting only
            print(f"(could not re-fetch order {order_id}: {e!r})")

    print("\nfills for this ticker:")
    for fl in client._request("GET", "/portfolio/fills", params={"ticker": ticker}).get("fills", []):
        print(f"  {fl}")

    print("\npositions for this ticker:")
    pos = client.get_positions(ticker=ticker)
    for p in pos.get("market_positions", []):
        print(f"  {p}")

    bal_after = client.get_balance()
    print(f"\nbalance after:  ${bal_after['balance_dollars']}")
    print(f"delta: ${int(bal_after['balance']) - int(bal_before['balance'])} cents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
