"""Data-collection sampling for future ML work (added 2026-08-07, see
config.SAMPLING_ENABLED's docstring). Records one row per market per
evaluation tick it spends inside the entry window, independent of whether it
ever clears any trading gate -- the goal is a raw time-series dataset of
(market conditions, time to expiry) -> (eventual resolution), not a log of
what the strategy actually did. check_resolutions.py fills in the
`resolution`/`resolution_checked_at` columns once each sampled market
settles.

SQLite, not a heavier DB: this is a single-machine research dataset with one
writer (the live runner) and one occasional reader/updater
(check_resolutions.py) -- no server, no concurrent-writer story needed, and
it's the only DB already implicitly expected by this project's tooling
(nothing else here talks to a database).
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    category TEXT NOT NULL,
    underlying TEXT NOT NULL,
    direction TEXT NOT NULL,
    strike REAL NOT NULL,
    sample_time_utc TEXT NOT NULL,
    close_time_utc TEXT NOT NULL,
    seconds_to_expiry REAL NOT NULL,
    spot REAL NOT NULL,
    favored_side TEXT NOT NULL,
    model_prob REAL NOT NULL,
    market_price REAL,
    z REAL,
    traded INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT,
    edge_per_contract REAL,
    resolution TEXT,
    resolution_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_ticker ON samples (ticker);
CREATE INDEX IF NOT EXISTS idx_samples_pending ON samples (resolution);
"""

# Columns added after the table's original 2026-08-07 release -- existing
# on-disk DBs already have the table, so CREATE TABLE IF NOT EXISTS above is
# a no-op for them and these need an explicit migration. (name, DDL-suffix)
# pairs; applied only if the column isn't already present.
_MIGRATIONS = [
    ("traded", "ALTER TABLE samples ADD COLUMN traded INTEGER NOT NULL DEFAULT 0"),
    ("skip_reason", "ALTER TABLE samples ADD COLUMN skip_reason TEXT"),
    ("edge_per_contract", "ALTER TABLE samples ADD COLUMN edge_per_contract REAL"),
]


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        for column_name, ddl in _MIGRATIONS:
            if column_name not in existing_columns:
                conn.execute(ddl)
        conn.commit()


def record_sample(
    db_path: str,
    *,
    ticker: str,
    series_ticker: str,
    category: str,
    underlying: str,
    direction: str,
    strike: float,
    close_time: datetime,
    seconds_to_expiry: float,
    spot: float,
    favored_side: str,
    model_prob: float,
    market_price: float | None,
    z: float | None,
) -> int:
    """Returns the new row's id -- callers hang onto it and pass it to
    record_trade_outcome once the evaluation for this tick finishes, so the
    eventual traded/skip_reason update lands on exactly this row and not some
    other sample of the same ticker from a different tick.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO samples (ticker, series_ticker, category, underlying, direction, strike, "
            "sample_time_utc, close_time_utc, seconds_to_expiry, spot, favored_side, model_prob, "
            "market_price, z) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker, series_ticker, category, underlying, direction, strike,
                datetime.now(timezone.utc).isoformat(), close_time.isoformat(), seconds_to_expiry,
                spot, favored_side, model_prob, market_price, z,
            ),
        )
        conn.commit()
        return cur.lastrowid


def record_trade_outcome(db_path: str, *, sample_id: int, traded: bool, skip_reason: str | None) -> None:
    """Fills in traded/skip_reason on a specific row once the market's gate
    evaluation for that tick has finished. traded=True implies skip_reason is
    None (a filled order needs no skip reason); traded=False carries a
    comma-separated list of every gate that applied, not just whichever one
    the real trading flow's short-circuiting logic happened to check first
    (2026-08-07 -- see runner.py's _diagnostic_skip_reasons docstring for
    why). Query with e.g. `skip_reason LIKE '%no_liquidity%'` or
    `.str.split(',')` in pandas, not equality, since a row can carry more
    than one reason. Reason names match the stats[] counter names in
    runner.py (minus the "skip_" prefix), so the two stay easy to
    cross-reference.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE samples SET traded = ?, skip_reason = ? WHERE id = ?",
            (1 if traded else 0, skip_reason, sample_id),
        )
        conn.commit()


def rows_missing_edge(db_path: str) -> list[tuple[int, float, float]]:
    """(id, model_prob, market_price) for every row that has a market_price
    to compute from but no edge_per_contract yet -- rows sampled with no
    cached order book (market_price NULL, see runner.py's
    _maybe_sample_market) can never get one and are correctly excluded, not
    endlessly re-selected on every check_resolutions.py run.
    """
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT id, model_prob, market_price FROM samples "
            "WHERE edge_per_contract IS NULL AND market_price IS NOT NULL"
        ).fetchall()


def record_edge(db_path: str, sample_id: int, edge_per_contract: float) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE samples SET edge_per_contract = ? WHERE id = ?",
            (edge_per_contract, sample_id),
        )
        conn.commit()


def pending_tickers(db_path: str) -> list[str]:
    """Distinct tickers with at least one row still missing a resolution."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM samples WHERE resolution IS NULL").fetchall()
    return [r[0] for r in rows]


def record_resolution(db_path: str, ticker: str, result: str) -> int:
    """Sets `resolution` on every still-pending row for `ticker`. Returns the
    number of rows updated.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE samples SET resolution = ?, resolution_checked_at = ? "
            "WHERE ticker = ? AND resolution IS NULL",
            (result, datetime.now(timezone.utc).isoformat(), ticker),
        )
        conn.commit()
        return cur.rowcount
