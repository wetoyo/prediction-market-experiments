# Universal Market Embedder (UME) reference

Package: `Clients.Utils.universal_market_embedder`.

## `schema.py`

### `MarketSchema` (pydantic `BaseModel`)

Structured representation of a parsed market title.

| Field | Type | Notes |
|---|---|---|
| `category` | `Literal["macro", "crypto", "politics", "sports", "weather"]` | High-level market category |
| `underlying_asset` | `str` | The core noun/entity/ticker (e.g. `"Federal Reserve"`, `"Bitcoin"`) |
| `condition` | `Literal["greater_than", "less_than", "equal_to", "bracket"]` | Direction of the contract's conditional logic |
| `target_value` | `float` | The strike price or target rate |
| `unit` | `str` | Unit of `target_value` (e.g. `"bps"`, `"USD"`, `"percent"`) |
| `resolution_date` | `date` | When the contract resolves |

## `matching.py`

Two-stage validation pipeline. See
[The two-stage matching algorithm](../explanation/matching-algorithm.md)
for the rationale.

- `SOFT_MATCH_THRESHOLD = 0.85`
- `DATE_PROXIMITY_DAYS = 1`

### `cosine_similarity(vector_a, vector_b) -> float`

Standard cosine similarity between two numpy vectors.

### `is_soft_match(vector_a, vector_b, threshold=SOFT_MATCH_THRESHOLD) -> bool`

Stage 1. True if `cosine_similarity(...) > threshold`.

### `is_hard_match(market_a: MarketSchema, market_b: MarketSchema) -> bool`

Stage 2. True only if all of:
- `target_value` is exactly equal on both sides,
- `condition` is exactly equal on both sides,
- `resolution_date`s differ by at most `DATE_PROXIMITY_DAYS` day.

### `is_same_event(vector_a, vector_b, market_a, market_b) -> bool`

True only if both `is_soft_match` and `is_hard_match` pass.

## `onboarder.py`

Requires `GROQ_API_KEY` in the environment (loaded via `python-dotenv`).

- `GROQ_MODEL = "llama-3.1-8b-instant"`
- `EMBEDDING_MODEL = "all-MiniLM-L6-v2"`

### `get_instructor_client()`

Lazily constructs and caches a Groq client wrapped with
[Instructor](https://python.useinstructor.com/) in JSON mode, used to
force `MarketSchema`-shaped output from the LLM.

### `get_embedder() -> SentenceTransformer`

Lazily constructs and caches the local `all-MiniLM-L6-v2` sentence
transformer. Runs in-memory; no network calls after the model is
downloaded.

### `parse_market_title(title: str) -> MarketSchema`

Sends `title` to Groq at `temperature=0.0` and returns a validated
`MarketSchema`. The system prompt includes today's date so the model can
resolve bare month/day references (titles that omit the year) to the
nearest matching date on or after today.

### `get_vector_embedding(text: str) -> np.ndarray`

Encodes `text` with the sentence transformer, returned as `float32`.

### `build_fingerprint(market: MarketSchema) -> str`

Builds a deterministic string key:
`{category}_{sorted_nouns_from_underlying_asset}_{condition}_{target_value}_{resolution_date}`.
Two titles that produce the same fingerprint are treated as an exact
duplicate of the same event without running the full match scan. Includes
`condition`/`target_value` (not just entity + date) so that a ladder of
strikes on the same underlying and date — e.g. "BTC above $68k" vs. "BTC
above $72k" on the same day — is **not** collapsed into one event.

### `find_matching_event(conn, market: MarketSchema, embedding) -> int | None`

1. Looks up `build_fingerprint(market)` directly — if found, returns that
   event's id immediately.
2. Otherwise scans every stored event: for each whose entity embedding
   passes `is_soft_match` against `embedding`, checks each of that event's
   existing markets with `is_hard_match`. Returns the first event id where
   both stages pass.
3. Returns `None` if nothing matches (caller should create a new event).

### `onboard_market(conn, market_id: str, platform: str, raw_title: str) -> tuple[int, MarketSchema]`

End-to-end: parses `raw_title`, embeds the extracted `underlying_asset`,
finds or creates the matching event, upserts the market row under that
event, and returns `(event_id, parsed_market)`.

## `database.py`

SQLite persistence for `Data/market_embedder.db`. See
[Database schemas](database-schemas.md#market_embedderdb) for the table
definitions.

| Function | Purpose |
|---|---|
| `get_connection(db_path=DEFAULT_DB_PATH)` | Opens a connection with `PRAGMA foreign_keys = ON`; creates parent dirs |
| `init_db(conn)` | Runs `SCHEMA` (creates `events`/`markets` tables if absent) |
| `vector_to_blob(vector)` / `blob_to_vector(blob)` | `float32` numpy array ↔ `BLOB` round-trip |
| `get_event_by_fingerprint(conn, fingerprint)` | Row lookup by unique fingerprint, or `None` |
| `get_all_events(conn)` | All events (`id`, `event_fingerprint`, `entity_embedding`) |
| `get_markets_by_event(conn, event_id)` | All market rows under one event |
| `insert_event(conn, fingerprint, embedding)` | Creates a new event, returns its id |
| `upsert_market(conn, market_id, platform, event_id, raw_title, condition_type, target_value, resolution_date)` | Insert-or-update one market row |
