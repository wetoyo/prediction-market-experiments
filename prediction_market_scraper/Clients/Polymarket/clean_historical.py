"""Normalizes raw Polymarket market payloads and persists them to Data/polymarket.db."""

import json
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "Data" / "polymarket.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    raw_title TEXT NOT NULL,
    status TEXT,
    close_time TEXT,
    clob_token_ids TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    bids TEXT NOT NULL,
    asks TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_token_time
    ON orderbook_snapshots(token_id, fetched_at);
"""


def clean_market(market: dict) -> dict:
    if market.get("closed"):
        status = "closed"
    elif market.get("active"):
        status = "active"
    else:
        status = "inactive"

    return {
        "id": market["id"],
        "raw_title": (market.get("question") or "").strip(),
        "status": status,
        "close_time": market.get("endDate"),
        "clob_token_ids": market.get("clobTokenIds"),
        "raw_json": json.dumps(market),
    }


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def save_markets(markets: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Cleans and upserts raw Polymarket markets. Returns the number of rows written."""
    owns_connection = conn is None
    conn = conn or get_connection()

    cleaned = [clean_market(m) for m in markets]
    conn.executemany(
        """
        INSERT INTO markets (id, raw_title, status, close_time, clob_token_ids, raw_json)
        VALUES (:id, :raw_title, :status, :close_time, :clob_token_ids, :raw_json)
        ON CONFLICT(id) DO UPDATE SET
            raw_title=excluded.raw_title,
            status=excluded.status,
            close_time=excluded.close_time,
            clob_token_ids=excluded.clob_token_ids,
            raw_json=excluded.raw_json,
            fetched_at=CURRENT_TIMESTAMP
        """,
        cleaned,
    )
    conn.commit()
    if owns_connection:
        conn.close()
    return len(cleaned)


def clean_order_book(token_id: str, book: dict) -> dict:
    return {
        "token_id": token_id,
        "bids": json.dumps(book.get("bids", [])),
        "asks": json.dumps(book.get("asks", [])),
        "raw_json": json.dumps(book),
    }


def save_order_book_snapshots(
    snapshots: list[tuple[str, dict]], conn: sqlite3.Connection | None = None
) -> int:
    """Appends one row per (token_id, book) snapshot. Unlike markets, snapshots are
    never upserted -- each fetch is a new point in the order book's time series.
    """
    owns_connection = conn is None
    conn = conn or get_connection()

    cleaned = [clean_order_book(token_id, book) for token_id, book in snapshots]
    conn.executemany(
        """
        INSERT INTO orderbook_snapshots (token_id, bids, asks, raw_json)
        VALUES (:token_id, :bids, :asks, :raw_json)
        """,
        cleaned,
    )
    conn.commit()
    if owns_connection:
        conn.close()
    return len(cleaned)


if __name__ == "__main__":
    from fetch_historical import fetch_order_book, search_events

    events = search_events("bitcoin price")
    markets = [m for event in events for m in event.get("markets", [])]
    written = save_markets(markets)
    print(f"Wrote {written} markets to {DEFAULT_DB_PATH}")

    snapshots = []
    for market in markets[:5]:
        token_ids = json.loads(market.get("clobTokenIds") or "[]")
        if token_ids:
            snapshots.append((token_ids[0], fetch_order_book(token_ids[0])))
    written = save_order_book_snapshots(snapshots)
    print(f"Wrote {written} orderbook snapshots to {DEFAULT_DB_PATH}")
