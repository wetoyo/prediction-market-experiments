# How to query the local database

**Goal:** read back order book data that's been saved by `clean_historical.save_order_books`.

## Connect to the database

By default, everything writes to `Data/derbit.db` relative to the `derbit` package directory. Reuse the same constant so you don't hardcode the path:

```python
import sqlite3
from clean_historical import DEFAULT_DB_PATH

conn = sqlite3.connect(DEFAULT_DB_PATH)
conn.row_factory = sqlite3.Row  # optional: access columns by name
```

If you called `save_order_books` with a custom `db_path` via `get_connection(db_path=...)`, connect to that same path instead.

## Read the latest data for an instrument

Because rows are upserted on `instrument_name`, there's exactly one row per instrument — the most recent fetch:

```python
row = conn.execute(
    "SELECT * FROM order_books WHERE instrument_name = ?",
    ("BTC-27JUL26-58000-C",),
).fetchone()
print(dict(row))
```

## Find instruments by criteria

```python
rows = conn.execute(
    """
    SELECT instrument_name, mark_price, mark_iv, delta
    FROM order_books
    WHERE underlying_index = ?
    ORDER BY mark_iv DESC
    LIMIT 10
    """,
    ("btc_usd",),
).fetchall()
```

## Recover fields that weren't flattened into columns

`clean_order_book` only extracts a fixed set of fields (see the [schema reference](../reference/clean_historical.md#schema)). The full original API response is preserved in `raw_json`, so anything not promoted to its own column is still recoverable:

```python
import json

row = conn.execute(
    "SELECT raw_json FROM order_books WHERE instrument_name = ?",
    ("BTC-27JUL26-58000-C",),
).fetchone()
full_payload = json.loads(row["raw_json"])
print(full_payload.get("bids"))  # e.g. full bid depth, not just best_bid_price
```

## Check when a row was last updated

```python
row = conn.execute(
    "SELECT instrument_name, fetched_at FROM order_books ORDER BY fetched_at DESC LIMIT 5"
).fetchall()
```

`fetched_at` is set to `CURRENT_TIMESTAMP` on insert and refreshed on every subsequent upsert, so it reflects the last time that instrument was fetched — not when it was first seen.

## Related

- [`clean_historical` reference](../reference/clean_historical.md) for the full table schema
- [Fetch and store order books for a currency](fetch-and-store-order-books.md)
