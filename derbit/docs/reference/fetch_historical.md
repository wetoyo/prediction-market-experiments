# Reference: `fetch_historical`

Synchronous REST client for Deribit's public v2 API. All functions issue a single `GET` request (20s timeout), call `response.raise_for_status()`, and return `response.json()["result"]` (or an empty/default value if `result` is absent).

`BASE_URL = "https://www.deribit.com/api/v2"`

No authentication is required — every endpoint used here is under `/public/`.

## Functions

| Function | Parameters | Returns | Endpoint |
|---|---|---|---|
| `get_index_price(index_name="btc_usd")` | `index_name: str` | `dict` | `/public/get_index_price` |
| `get_instruments(currency="BTC", kind="option", expired=False)` | `currency: str`, `kind: str`, `expired: bool` | `list[dict]` | `/public/get_instruments` |
| `get_order_book(instrument_name)` | `instrument_name: str` | `dict` | `/public/get_order_book` |
| `get_book_summary_by_currency(currency="BTC", kind="option")` | `currency: str`, `kind: str` | `list[dict]` | `/public/get_book_summary_by_currency` |
| `get_book_summary_by_instrument(instrument_name)` | `instrument_name: str` | `list[dict]` | `/public/get_book_summary_by_instrument` |
| `get_ticker(instrument_name)` | `instrument_name: str` | `dict` | `/public/ticker` |
| `get_last_trades_by_instrument(instrument_name, count=10)` | `instrument_name: str`, `count: int` | `dict` | `/public/get_last_trades_by_instrument` |
| `get_last_trades_by_currency(currency="BTC", count=10)` | `currency: str`, `count: int` | `dict` | `/public/get_last_trades_by_currency` |
| `get_funding_rate_value(instrument_name, start_timestamp, end_timestamp)` | `instrument_name: str`, `start_timestamp: int`, `end_timestamp: int` (ms since epoch) | `float` | `/public/get_funding_rate_value` |
| `get_index(index_name="btc_usd")` | `index_name: str` | `dict` | `/public/get_index` |
| `get_delivery_prices(index_name="btc_usd")` | `index_name: str` | `dict` | `/public/get_delivery_prices` |
| `get_historical_volatility(currency="BTC")` | `currency: str` | `list` | `/public/get_historical_volatility` |
| `get_time()` | — | `int` (ms since epoch) | `/public/get_time` |
| `test()` | — | `dict` | `/public/test` |
| `status()` | — | `dict` | `/public/status` |
| `get_supported_index_names()` | — | `list[str]` | `/public/get_supported_index_names` |
| `get_trade_volumes()` | — | `list[dict]` | `/public/get_trade_volumes` |
| `get_currencies()` | — | `list[dict]` | `/public/get_currencies` |

## Notes

- `kind` for instrument/book-summary calls is typically `"option"` or `"future"`.
- `get_instruments(expired=True)` returns settled/expired contracts; fields on expired instruments' order books may be incomplete.
- All functions raise `requests.HTTPError` on a non-2xx response (via `raise_for_status()`); there is no retry logic — callers are responsible for retrying on failure.
- Running the module directly (`python fetch_historical.py`) executes a smoke test that calls `get_index_price`, `get_instruments`, and `get_historical_volatility`.
