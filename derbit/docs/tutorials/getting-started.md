# Getting started with derbit

This tutorial walks you through fetching live data from Deribit and saving it to a local database. By the end, you'll have queried Deribit's REST API, written an order book to SQLite, and read it back.

## Prerequisites

- Python 3.10+ (the project's `.venv` uses 3.14)
- Internet access to `www.deribit.com` (no API key needed — these are public endpoints)

## 1. Install dependencies

From the `derbit/` directory, create a virtual environment and install the two runtime dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install requests websockets
```

## 2. Fetch the BTC index price

Open a Python shell in the `derbit` directory (or one level above, if importing as the `derbit` package) and run:

```python
from fetch_historical import get_index_price

print(get_index_price("btc_usd"))
```

You should see a dict containing the current BTC index price, e.g. `{'index_name': 'btc_usd', 'estimated_delivery_price': ..., 'price': ...}`.

## 3. List available option instruments

```python
from fetch_historical import get_instruments

instruments = get_instruments(currency="BTC", kind="option")
print(len(instruments), "active BTC options")
print(instruments[0]["instrument_name"])
```

## 4. Fetch an order book

Pick an instrument name from the previous step and request its order book:

```python
from fetch_historical import get_order_book

ob = get_order_book(instruments[0]["instrument_name"])
print(ob["best_bid_price"], ob["best_ask_price"])
```

The raw payload includes nested `greeks` and `stats` dictionaries alongside top-level fields like `mark_price` and `underlying_price`.

## 5. Save it to the database

`clean_historical` flattens that nested payload into a single row and upserts it into `Data/derbit.db`:

```python
from clean_historical import save_order_books

written = save_order_books([ob])
print(f"Wrote {written} row(s)")
```

Running this again with a fresh order book for the same instrument updates the existing row (keyed by `instrument_name`) rather than duplicating it — see [How the database upsert works](../explanation/architecture.md#upsert-by-instrument_name) for why.

## 6. Read it back

```python
import sqlite3
from clean_historical import DEFAULT_DB_PATH

conn = sqlite3.connect(DEFAULT_DB_PATH)
row = conn.execute(
    "SELECT instrument_name, mark_price, mark_iv, delta FROM order_books LIMIT 1"
).fetchone()
print(row)
```

## Next steps

- To fetch and store every instrument for a currency in one pass, see [Fetch and store order books for a currency](../how-to-guides/fetch-and-store-order-books.md).
- To receive live updates instead of one-off snapshots, see [Stream live updates over WebSocket](../how-to-guides/stream-live-updates.md).
- For the full list of available REST calls, see the [`fetch_historical` reference](../reference/fetch_historical.md).
