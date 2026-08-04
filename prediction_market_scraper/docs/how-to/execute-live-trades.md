# How to execute live trades

Both platforms also expose authenticated endpoints for placing orders and
checking your account — the trading side of the Execution Loop described
in [System architecture](../explanation/architecture.md). These move real
money. Start with the read-only calls (`get_balance`, `get_positions`,
`get_orders`) to confirm credentials work before calling `place_order`,
and test with small sizes.

## Kalshi

> **Status:** verified against a real account, including a live
> place-then-cancel round trip (1¢, 1 contract).

Every request is signed the same way as the
[websocket handshake](stream-live-order-books.md#kalshi-requires-a-signed-handshake).
Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` first — see
[Configuration](../reference/configuration.md).

```python
from Clients.Kalshi.live_execution import KalshiTradingClient

client = KalshiTradingClient()

print(client.get_balance())
print(client.get_positions())

order = client.place_order(
    ticker="KXFED-26JUL-T4.25",
    side="bid",       # "bid" (buy yes) or "ask"
    count="10.00",
    price="0.5600",   # count/price are fixed-point dollar strings
)
print(order)  # inspect the response for the order id, then:
# client.cancel_order(order_id)
```

`place_order` posts to `POST /portfolio/events/orders`, the current
(non-deprecated) order-creation endpoint. Full method list in
[Kalshi client reference](../reference/kalshi-client.md#live_executionpy).

## Polymarket

> **Status:** implemented but untested against a real wallet — no
> Polymarket trading credentials are configured in this project yet.
> Verify behavior before relying on it.

Order signing (EIP-712) is handled by the official
[`py-clob-client`](https://pypi.org/project/py-clob-client/) SDK rather
than hand-rolled, since a signing bug here risks real funds. Install it
first — it isn't in any `requirements.txt` yet:

```bash
./venv/Scripts/pip.exe install py-clob-client
```

Set `POLYMARKET_PRIVATE_KEY` to your wallet's 0x-prefixed private key. If
you trade through a Polymarket proxy/Safe wallet (email/Magic login)
rather than a raw browser wallet, also set `POLYMARKET_FUNDER` (the proxy
address) and `POLYMARKET_SIGNATURE_TYPE` (`1` for email/Magic, `2` for
browser-wallet proxy) — see [Configuration](../reference/configuration.md).

```python
from Clients.Polymarket.live_execution import PolymarketTradingClient
from py_clob_client.order_builder.constants import BUY

client = PolymarketTradingClient()

print(client.wallet_address)
print(client.get_balance())
print(client.get_positions())

order = client.place_order(
    token_id="21742633143463906290569050155826241533067272736897614950488156847949938836455",
    price=0.56,
    size=10,
    side=BUY,
)
print(order)  # inspect the response for the order id, then:
# client.cancel_order(order_id)
```

`get_positions()` doesn't go through the CLOB at all — positions aren't a
CLOB-native concept, so it reads Polymarket's public Data API instead. The
`token_id` for a market comes from the `clob_token_ids` column populated by
[fetching Polymarket markets](fetch-polymarket-markets.md). Full method
list in
[Polymarket client reference](../reference/polymarket-client.md#live_executionpy).
