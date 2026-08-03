"""SQLite persistence layer for the Universal Market Embedder.

Parent/child schema: one `events` row represents a single real-world
event; many `markets` rows (one per platform contract) point back at it
via `event_id`.
"""

import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "Data" / "market_embedder.db"

SCHEMA = """
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
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def vector_to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def get_event_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM events WHERE event_fingerprint = ?", (fingerprint,)
    ).fetchone()


def get_all_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT id, event_fingerprint, entity_embedding FROM events").fetchall()


def get_markets_by_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM markets WHERE event_id = ?", (event_id,)
    ).fetchall()


def insert_event(conn: sqlite3.Connection, fingerprint: str, embedding: np.ndarray) -> int:
    cursor = conn.execute(
        "INSERT INTO events (event_fingerprint, entity_embedding) VALUES (?, ?)",
        (fingerprint, vector_to_blob(embedding)),
    )
    conn.commit()
    return cursor.lastrowid


def upsert_market(
    conn: sqlite3.Connection,
    market_id: str,
    platform: str,
    event_id: int,
    raw_title: str,
    condition_type: str,
    target_value: float,
    resolution_date: str,
) -> None:
    conn.execute(
        """
        INSERT INTO markets (id, platform, event_id, raw_title, condition_type, target_value, resolution_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            platform=excluded.platform,
            event_id=excluded.event_id,
            raw_title=excluded.raw_title,
            condition_type=excluded.condition_type,
            target_value=excluded.target_value,
            resolution_date=excluded.resolution_date,
            last_updated=CURRENT_TIMESTAMP
        """,
        (market_id, platform, event_id, raw_title, condition_type, target_value, resolution_date),
    )
    conn.commit()
