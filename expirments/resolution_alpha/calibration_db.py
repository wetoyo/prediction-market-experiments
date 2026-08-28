"""Dedicated SQLite dataset for resolution_alpha's probability-model
calibration research -- deliberately separate from live/logs/samples.db,
which is the live trading loop's own dataset (tied to actual trade
decisions/outcomes). This one exists purely to answer "does
live/probability.py's claimed confidence match reality," fed from two
sources, both writing into the same `calibration_samples` table (see
`source` column):

  - "backfill" (backfill_calibration.py): replays already-settled markets
    against historical Coinbase 1-minute candles, same approach as
    ../backtest.py but persisted instead of printed, and evaluated at a fine
    grid of decision points concentrated in 0-90s before close -- that's
    resolution_alpha's actual entry regime (median observed live entry is
    ~22s before close, see live/logs/samples.db), far shorter than a generic
    options-style calibration would use, so the grid stays anchored there
    rather than a longer/broader horizon.
  - "live" (collect_calibration_live.py): a standalone, no-Kalshi-auth-needed
    poller that watches currently-open markets in real time and records the
    same fields at native ~2s granularity -- finer than backfill's 1-minute
    candles, which matters most exactly in the last 30-60s where the
    backfill's temporal resolution is coarsest.

Every row stores the raw z-score (unbounded, never saturates) alongside the
naive Gaussian-CDF-derived favored_probability, specifically so calibration
can be fit against z instead of the saturating probability -- see
fit_calibration.py's docstring for why probability-bucketed calibration
silently hides most of the data once z exceeds ~10.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    ticker TEXT NOT NULL,
    underlying TEXT NOT NULL,
    direction TEXT NOT NULL,
    strike REAL NOT NULL,
    close_time_utc TEXT NOT NULL,
    decision_seconds REAL NOT NULL,
    sample_time_utc TEXT NOT NULL,
    spot REAL NOT NULL,
    settlement_estimate REAL NOT NULL,
    sigma_used REAL NOT NULL,
    z REAL NOT NULL,
    favored_side TEXT NOT NULL,
    favored_probability REAL NOT NULL,
    resolution TEXT,
    resolution_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_calibration_ticker ON calibration_samples (ticker);
CREATE INDEX IF NOT EXISTS idx_calibration_pending ON calibration_samples (resolution);
CREATE INDEX IF NOT EXISTS idx_calibration_source ON calibration_samples (source);
"""


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
        conn.commit()


def record_sample(
    db_path: str,
    *,
    source: str,
    ticker: str,
    underlying: str,
    direction: str,
    strike: float,
    close_time: datetime,
    decision_seconds: float,
    sample_time: datetime,
    spot: float,
    settlement_estimate: float,
    sigma_used: float,
    z: float,
    favored_side: str,
    favored_probability: float,
    resolution: str | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO calibration_samples (source, ticker, underlying, direction, strike, "
            "close_time_utc, decision_seconds, sample_time_utc, spot, settlement_estimate, sigma_used, "
            "z, favored_side, favored_probability, resolution, resolution_checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source, ticker, underlying, direction, strike,
                close_time.isoformat(), decision_seconds, sample_time.isoformat(),
                spot, settlement_estimate, sigma_used, z, favored_side, favored_probability,
                resolution, datetime.now(timezone.utc).isoformat() if resolution else None,
            ),
        )
        conn.commit()
        return cur.lastrowid


def pending_tickers(db_path: str) -> list[str]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM calibration_samples WHERE resolution IS NULL"
        ).fetchall()
    return [r[0] for r in rows]


def record_resolution(db_path: str, ticker: str, result: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE calibration_samples SET resolution = ?, resolution_checked_at = ? "
            "WHERE ticker = ? AND resolution IS NULL",
            (result, datetime.now(timezone.utc).isoformat(), ticker),
        )
        conn.commit()
        return cur.rowcount
