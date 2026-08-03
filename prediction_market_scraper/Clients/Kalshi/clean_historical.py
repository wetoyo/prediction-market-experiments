"""Normalizes raw Kalshi market payloads and persists them to Data/kalshi.db."""

import json
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "Data" / "kalshi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    raw_title TEXT NOT NULL,
    status TEXT,
    close_time TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    yes_dollars TEXT NOT NULL,
    no_dollars TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_ticker_time
    ON orderbook_snapshots(ticker, fetched_at);
"""


def build_raw_title(market: dict) -> str:
    """Kalshi often puts the strike/threshold in yes_sub_title rather than title
    (e.g. title="Bitcoin price on Jul 19, 2026?", yes_sub_title="$72,000 or above").
    Concatenating both gives the LLM extractor the full contract terms.
    """
    title = (market.get("title") or "").strip()
    sub_title = (market.get("yes_sub_title") or "").strip()
    if sub_title and sub_title not in title:
        return f"{title} {sub_title}"
    return title


def clean_market(market: dict) -> dict:
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "raw_title": build_raw_title(market),
        "status": market.get("status"),
        "close_time": market.get("close_time"),
        "raw_json": json.dumps(market),
    }


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def save_markets(markets: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Cleans and upserts raw Kalshi markets. Returns the number of rows written."""
    owns_connection = conn is None
    conn = conn or get_connection()

    cleaned = [clean_market(m) for m in markets]
    conn.executemany(
        """
        INSERT INTO markets (ticker, event_ticker, raw_title, status, close_time, raw_json)
        VALUES (:ticker, :event_ticker, :raw_title, :status, :close_time, :raw_json)
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker,
            raw_title=excluded.raw_title,
            status=excluded.status,
            close_time=excluded.close_time,
            raw_json=excluded.raw_json,
            fetched_at=CURRENT_TIMESTAMP
        """,
        cleaned,
    )
    conn.commit()
    if owns_connection:
        conn.close()
    return len(cleaned)


def clean_orderbook_snapshot(ticker: str, orderbook: dict) -> dict:
    return {
        "ticker": ticker,
        "yes_dollars": json.dumps(orderbook.get("yes_dollars", [])),
        "no_dollars": json.dumps(orderbook.get("no_dollars", [])),
        "raw_json": json.dumps(orderbook),
    }


def save_orderbook_snapshots(
    snapshots: list[tuple[str, dict]], conn: sqlite3.Connection | None = None
) -> int:
    """Appends one row per (ticker, orderbook) snapshot. Unlike markets, snapshots are
    never upserted -- each fetch is a new point in the order book's time series.
    """
    owns_connection = conn is None
    conn = conn or get_connection()

    cleaned = [clean_orderbook_snapshot(ticker, orderbook) for ticker, orderbook in snapshots]
    conn.executemany(
        """
        INSERT INTO orderbook_snapshots (ticker, yes_dollars, no_dollars, raw_json)
        VALUES (:ticker, :yes_dollars, :no_dollars, :raw_json)
        """,
        cleaned,
    )
    conn.commit()
    if owns_connection:
        conn.close()
    return len(cleaned)


if __name__ == "__main__":
    from fetch_historical import fetch_markets, fetch_orderbook

    fed_markets = fetch_markets(series_ticker="KXFED")
    written = save_markets(fed_markets)
    print(f"Wrote {written} markets to {DEFAULT_DB_PATH}")

    snapshots = [(m["ticker"], fetch_orderbook(m["ticker"])) for m in fed_markets[:5]]
    written = save_orderbook_snapshots(snapshots)
    print(f"Wrote {written} orderbook snapshots to {DEFAULT_DB_PATH}")
