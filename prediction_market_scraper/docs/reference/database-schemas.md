# Database schemas

Three separate SQLite databases live under `Data/`, one per data source.
None of them share a connection; the UME reads raw titles produced by the
platform databases but stores its own copies rather than joining across
files.

## `kalshi.db`

Written by `Clients.Kalshi.clean_historical`.

```sql
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    raw_title TEXT NOT NULL,
    status TEXT,
    close_time TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `ticker` — Kalshi's market identifier, e.g. `KXFED-26JUL-T4.25`.
- `raw_title` — `title` + `yes_sub_title` concatenated (see
  [`build_raw_title`](kalshi-client.md#build_raw_titlemarket-dict---str)).
- `raw_json` — the full untouched API response for that market.
- Upserted on `ticker`; re-fetching updates `status`/`close_time`/`raw_json`/`fetched_at`.

## `polymarket.db`

Written by `Clients.Polymarket.clean_historical`.

```sql
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    raw_title TEXT NOT NULL,
    status TEXT,
    close_time TEXT,
    clob_token_ids TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `id` — Polymarket's market id.
- `raw_title` — the market's `question` field, stripped.
- `status` — derived: `"closed"` if `closed`, else `"active"` if `active`,
  else `"inactive"`.
- `clob_token_ids` — JSON string of CLOB token (asset) ids, needed for
  [streaming that market's order book](../how-to/stream-live-order-books.md).
- Upserted on `id`.

## `market_embedder.db`

Written by `Clients.Utils.universal_market_embedder.database`. Parent/child
schema: one `events` row per real-world event, many `markets` rows (one per
platform contract) pointing back at it.

```sql
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_fingerprint TEXT UNIQUE NOT NULL,
    entity_embedding BLOB NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    raw_title TEXT NOT NULL,
    condition_type TEXT NOT NULL,
    target_value REAL NOT NULL,
    resolution_date TEXT NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(event_id) REFERENCES events(id)
);
```

- `events.event_fingerprint` — see
  [`build_fingerprint`](universal-market-embedder.md#build_fingerprintmarket-marketschema---str);
  unique, used as an exact-duplicate short-circuit.
- `events.entity_embedding` — `float32` numpy vector of the event's
  `underlying_asset`, stored as raw bytes (`vector_to_blob`/`blob_to_vector`
  in `database.py`).
- `markets.id` — the *source* platform's own id (Kalshi ticker or
  Polymarket id), not a new synthetic id — same value space as the
  `ticker`/`id` columns in `kalshi.db`/`polymarket.db`.
- `markets.platform` — `"kalshi"` or `"polymarket"`.
- Note this table is named `markets` in both `kalshi.db`/`polymarket.db`
  (raw, per-platform) and here (parsed, cross-platform) — they are
  different tables in different files with different columns despite the
  shared name.
