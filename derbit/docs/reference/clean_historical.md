# Reference: `clean_historical`

Normalizes raw Deribit order book payloads and persists them to SQLite.

`DEFAULT_DB_PATH = <package_dir>/Data/derbit.db`

## Functions

### `clean_order_book(ob: dict) -> dict`

Flattens a raw `get_order_book` response into the column shape used by the `order_books` table. Pulls `delta`, `gamma`, `vega`, `theta`, `rho` out of the nested `greeks` dict, and `volume`, `high`, `low`, `price_change` out of the nested `stats` dict. Also adds `raw_json`, a JSON-serialized copy of the entire input payload. Missing keys resolve to `None` via `.get()`, except `instrument_name`, which is required and raises `KeyError` if absent.

### `get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection`

Ensures the parent directory of `db_path` exists, runs the `CREATE TABLE IF NOT EXISTS` schema against it, and returns an open `sqlite3.Connection`. Safe to call repeatedly — schema creation is idempotent.

### `save_order_books(order_books: list[dict], conn: sqlite3.Connection | None = None) -> int`

Cleans and upserts a batch of raw order book payloads.

- Filters out any entries missing `instrument_name` before cleaning.
- If `conn` is not provided, opens a connection via `get_connection()`, commits, and closes it before returning. If `conn` is provided, the caller owns its lifecycle (this function will still `commit()` but will not close it).
- Upsert key is `instrument_name`: on conflict, every column except `instrument_name` is overwritten with the new values, and `fetched_at` is reset to `CURRENT_TIMESTAMP`.
- Returns the number of rows processed (not necessarily the number of *new* rows, since updates count too).

## Schema

```sql
CREATE TABLE IF NOT EXISTS order_books (
    instrument_name TEXT PRIMARY KEY,
    best_bid_price REAL,
    best_ask_price REAL,
    best_bid_amount REAL,
    best_ask_amount REAL,
    mark_price REAL,
    mark_iv REAL,
    delta REAL,
    gamma REAL,
    vega REAL,
    theta REAL,
    rho REAL,
    underlying_price REAL,
    underlying_index TEXT,
    open_interest REAL,
    volume REAL,
    high REAL,
    low REAL,
    price_change REAL,
    timestamp INTEGER,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

| Column | Type | Source |
|---|---|---|
| `instrument_name` | `TEXT` (primary key) | `ob["instrument_name"]` (required) |
| `best_bid_price` / `best_ask_price` | `REAL` | top-level |
| `best_bid_amount` / `best_ask_amount` | `REAL` | top-level |
| `mark_price` | `REAL` | top-level |
| `mark_iv` | `REAL` | top-level |
| `delta`, `gamma`, `vega`, `theta`, `rho` | `REAL` | `ob["greeks"]` |
| `underlying_price` / `underlying_index` | `REAL` / `TEXT` | top-level |
| `open_interest` | `REAL` | top-level |
| `volume` | `REAL` | `ob["stats"]["volume"]` |
| `high` / `low` / `price_change` | `REAL` | `ob["stats"]` |
| `timestamp` | `INTEGER` | top-level (exchange timestamp, ms since epoch) |
| `raw_json` | `TEXT` (not null) | full input payload, JSON-encoded |
| `fetched_at` | `TIMESTAMP` | set on insert, refreshed on every update |

There is exactly one row per instrument — the table stores latest state, not a time series. To keep historical snapshots over time, either persist `raw_json` elsewhere per fetch or extend the schema to a non-unique key (see [Explanation: architecture](../explanation/architecture.md)).

## Self-test

Running `python clean_historical.py` directly fetches one live BTC option order book via `fetch_historical` and upserts it, printing the result — a quick end-to-end integration check against the real API and local DB.
