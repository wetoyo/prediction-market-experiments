# How to fetch and store order books for a currency

**Goal:** pull order books for every active option instrument of a currency and persist them all to the local database in one pass.

## Steps

1. Get the list of active instruments for the currency and kind you care about:

   ```python
   from fetch_historical import get_instruments

   instruments = get_instruments(currency="BTC", kind="option", expired=False)
   names = [i["instrument_name"] for i in instruments]
   ```

2. Fetch an order book for each instrument. `get_order_book` makes one HTTP request per call, so batch responsibly (Deribit's public endpoints are rate-limited):

   ```python
   from fetch_historical import get_order_book

   order_books = [get_order_book(name) for name in names]
   ```

3. Upsert the whole batch in a single database call:

   ```python
   from clean_historical import save_order_books

   written = save_order_books(order_books)
   print(f"Upserted {written} instruments")
   ```

   `save_order_books` opens its own connection, writes with `executemany`, commits, and closes — you don't need to manage a connection yourself for a one-off batch.

## Reusing a connection across multiple batches

If you're calling `save_order_books` repeatedly (e.g. in a polling loop), open one connection up front and pass it in so you're not re-running the schema/`CREATE TABLE IF NOT EXISTS` check and reconnecting every time:

```python
from clean_historical import get_connection, save_order_books

conn = get_connection()
try:
    for _ in range(10):
        order_books = [get_order_book(n) for n in names]
        save_order_books(order_books, conn=conn)
        # ... sleep / wait for next poll ...
finally:
    conn.close()
```

## Fetching expired instruments

Pass `expired=True` to `get_instruments` to pull settled/expired contracts instead of active ones — useful for backfilling historical data:

```python
expired_instruments = get_instruments(currency="BTC", kind="option", expired=True)
```

Note that `get_order_book` on an expired instrument may return partial or empty fields depending on how long ago it settled.

## Related

- [Query the local database](query-the-database.md)
- [`fetch_historical` reference](../reference/fetch_historical.md)
- [`clean_historical` reference](../reference/clean_historical.md)
