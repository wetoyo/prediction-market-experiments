"""Normalizes raw Deribit API payloads and persists them to SQLite."""

import json
import sqlite3
from pathlib import Path

# Places database at Data/derbit.db to align with the rest of the workspace
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "Data" / "derbit.db"

SCHEMA = """
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
"""


def clean_order_book(ob: dict) -> dict:
    """Extracts required metrics from a raw Deribit order book API payload."""
    greeks = ob.get("greeks", {})
    stats = ob.get("stats", {})

    return {
        "instrument_name": ob["instrument_name"],
        "best_bid_price": ob.get("best_bid_price"),
        "best_ask_price": ob.get("best_ask_price"),
        "best_bid_amount": ob.get("best_bid_amount"),
        "best_ask_amount": ob.get("best_ask_amount"),
        "mark_price": ob.get("mark_price"),
        "mark_iv": ob.get("mark_iv"),
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "vega": greeks.get("vega"),
        "theta": greeks.get("theta"),
        "rho": greeks.get("rho"),
        "underlying_price": ob.get("underlying_price"),
        "underlying_index": ob.get("underlying_index"),
        "open_interest": ob.get("open_interest"),
        "volume": ob.get("stats", {}).get("volume"),  # volume is typically inside stats
        "high": stats.get("high"),
        "low": stats.get("low"),
        "price_change": stats.get("price_change"),
        "timestamp": ob.get("timestamp"),
        "raw_json": json.dumps(ob),
    }


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Ensures directories exist, instantiates/executes database schema, and returns connection."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def save_order_books(order_books: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Cleans and upserts multiple Deribit order books to the database."""
    owns_connection = conn is None
    conn = conn or get_connection()

    cleaned = [clean_order_book(ob) for ob in order_books if "instrument_name" in ob]
    conn.executemany(
        """
        INSERT INTO order_books (
            instrument_name, best_bid_price, best_ask_price, best_bid_amount, best_ask_amount,
            mark_price, mark_iv, delta, gamma, vega, theta, rho, underlying_price,
            underlying_index, open_interest, volume, high, low, price_change, timestamp, raw_json
        )
        VALUES (
            :instrument_name, :best_bid_price, :best_ask_price, :best_bid_amount, :best_ask_amount,
            :mark_price, :mark_iv, :delta, :gamma, :vega, :theta, :rho, :underlying_price,
            :underlying_index, :open_interest, :volume, :high, :low, :price_change, :timestamp, :raw_json
        )
        ON CONFLICT(instrument_name) DO UPDATE SET
            best_bid_price=excluded.best_bid_price,
            best_ask_price=excluded.best_ask_price,
            best_bid_amount=excluded.best_bid_amount,
            best_ask_amount=excluded.best_ask_amount,
            mark_price=excluded.mark_price,
            mark_iv=excluded.mark_iv,
            delta=excluded.delta,
            gamma=excluded.gamma,
            vega=excluded.vega,
            theta=excluded.theta,
            rho=excluded.rho,
            underlying_price=excluded.underlying_price,
            underlying_index=excluded.underlying_index,
            open_interest=excluded.open_interest,
            volume=excluded.volume,
            high=excluded.high,
            low=excluded.low,
            price_change=excluded.price_change,
            timestamp=excluded.timestamp,
            raw_json=excluded.raw_json,
            fetched_at=CURRENT_TIMESTAMP
        """,
        cleaned,
    )
    conn.commit()
    if owns_connection:
        conn.close()
    return len(cleaned)


if __name__ == "__main__":
    from fetch_historical import get_instruments, get_order_book

    print("Testing clean_historical database logic...")
    try:
        # Fetch one real active instrument to verify end-to-end integration
        instruments = get_instruments(currency="BTC", kind="option")
        if instruments:
            target_instrument = instruments[0]["instrument_name"]
            print(f"Fetching order book for: {target_instrument}")
            ob = get_order_book(target_instrument)
            written = save_order_books([ob])
            print(f"Successfully upserted {written} order book record to {DEFAULT_DB_PATH}")
        else:
            print("No active BTC option instruments found.")
    except Exception as e:
        print("Error during database test run:", e)
