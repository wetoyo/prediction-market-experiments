"""Onboarding Loop (spec section 5): Groq + Instructor extraction, local
sentence-transformer embedding, and the two-stage match against the
SQLite event/market cache.

Requires GROQ_API_KEY to be set in the environment before use.
"""

import os
import re
from datetime import date
from types import SimpleNamespace

import instructor
import numpy as np
from dotenv import load_dotenv
from groq import Groq
from sentence_transformers import SentenceTransformer

from . import database
from .matching import is_hard_match, is_soft_match
from .schema import MarketSchema

load_dotenv()

GROQ_MODEL = "llama-3.1-8b-instant"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_instructor_client = None
_embedder = None


def get_instructor_client():
    """Lazily builds the Groq client patched with Instructor's JSON mode."""
    global _instructor_client
    if _instructor_client is None:
        groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        _instructor_client = instructor.from_groq(groq_client, mode=instructor.Mode.JSON)
    return _instructor_client


def get_embedder() -> SentenceTransformer:
    """Lazily loads the local sentence-transformer (runs in-memory, no API calls)."""
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
    return _embedder


def parse_market_title(title: str) -> MarketSchema:
    """Uses Groq to parse a messy market title into a structured, validated statement."""
    client = get_instructor_client()
    return client.chat.completions.create(
        model=GROQ_MODEL,
        response_model=MarketSchema,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a precise data extraction tool designed for quantitative traders. "
                    f"Today's date is {date.today().isoformat()}. Titles often omit the year -- "
                    "resolve bare month/day references to the nearest occurrence on or after today."
                ),
            },
            {"role": "user", "content": f"Parse this market title: '{title}'"},
        ],
        temperature=0.0,
    )


def get_vector_embedding(text: str) -> np.ndarray:
    """Generates a dense vector embedding of a string."""
    vector = get_embedder().encode(text, convert_to_tensor=False)
    return np.asarray(vector, dtype=np.float32)


def build_fingerprint(market: MarketSchema) -> str:
    """category_sorted_nouns_condition_target_date -- exact-duplicate key for the parent event.

    Must include condition/target_value, not just entity+date: a ladder of
    strikes on the same underlying (e.g. "BTC above $68k" vs "BTC above $72k"
    on the same day) shares entity and date but is NOT the same event. Only an
    exact match here should ever bypass the full two-stage scan.
    """
    nouns = sorted(re.findall(r"[a-z0-9]+", market.underlying_asset.lower()))
    return (
        f"{market.category}_{'_'.join(nouns)}_{market.condition}_"
        f"{market.target_value}_{market.resolution_date.isoformat()}"
    )


def find_matching_event(conn, market: MarketSchema, embedding: np.ndarray):
    """Returns the id of an existing event that passes the two-stage match, or None."""
    exact = database.get_event_by_fingerprint(conn, build_fingerprint(market))
    if exact is not None:
        return exact["id"]

    for event in database.get_all_events(conn):
        event_embedding = database.blob_to_vector(event["entity_embedding"])
        if not is_soft_match(embedding, event_embedding):
            continue
        for existing_market in database.get_markets_by_event(conn, event["id"]):
            candidate = SimpleNamespace(
                target_value=existing_market["target_value"],
                condition=existing_market["condition_type"],
                resolution_date=date.fromisoformat(existing_market["resolution_date"]),
            )
            if is_hard_match(market, candidate):
                return event["id"]
    return None


def onboard_market(conn, market_id: str, platform: str, raw_title: str):
    """Parses a raw title, resolves it to a (possibly new) event, and persists it.

    Returns (event_id, parsed MarketSchema).
    """
    market = parse_market_title(raw_title)
    embedding = get_vector_embedding(market.underlying_asset)

    event_id = find_matching_event(conn, market, embedding)
    if event_id is None:
        event_id = database.insert_event(conn, build_fingerprint(market), embedding)

    database.upsert_market(
        conn,
        market_id=market_id,
        platform=platform,
        event_id=event_id,
        raw_title=raw_title,
        condition_type=market.condition,
        target_value=market.target_value,
        resolution_date=market.resolution_date.isoformat(),
    )
    return event_id, market


if __name__ == "__main__":
    conn = database.get_connection()
    database.init_db(conn)

    kalshi_raw = "Will the Fed raise rates by 25 basis points in September 2026?"
    poly_raw = "Fed interest rate hike 25bps in Sept?"

    print("Onboarding Kalshi market...")
    kalshi_event_id, kalshi_parsed = onboard_market(
        conn, market_id="KALSHI-FED-25BPS-SEP26", platform="kalshi", raw_title=kalshi_raw
    )
    print(kalshi_parsed.model_dump_json(indent=2))
    print(f"-> event_id={kalshi_event_id}")

    print("\nOnboarding Polymarket market...")
    poly_event_id, poly_parsed = onboard_market(
        conn, market_id="POLY-FED-25BPS-SEP26", platform="polymarket", raw_title=poly_raw
    )
    print(poly_parsed.model_dump_json(indent=2))
    print(f"-> event_id={poly_event_id}")

    if kalshi_event_id == poly_event_id:
        print(f"\nMatched: both contracts linked under event_id={kalshi_event_id}")
    else:
        print("\nNot matched: contracts stored under separate events")

    conn.close()
