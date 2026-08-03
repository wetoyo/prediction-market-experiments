"""Two-stage validation pipeline (spec section 4).

Stage 1 (soft match): cosine similarity of `underlying_asset` embeddings.
Stage 2 (hard match): deterministic equality/proximity gates on the
extracted contract terms. Both stages must pass for two contracts to be
considered the same real-world event.
"""

import numpy as np

from .schema import MarketSchema

SOFT_MATCH_THRESHOLD = 0.85
DATE_PROXIMITY_DAYS = 1


def cosine_similarity(vector_a: np.ndarray, vector_b: np.ndarray) -> float:
    return float(
        np.dot(vector_a, vector_b) / (np.linalg.norm(vector_a) * np.linalg.norm(vector_b))
    )


def is_soft_match(vector_a: np.ndarray, vector_b: np.ndarray, threshold: float = SOFT_MATCH_THRESHOLD) -> bool:
    """Stage 1: are the entity embeddings semantically close enough to consider?"""
    return cosine_similarity(vector_a, vector_b) > threshold


def is_hard_match(market_a: MarketSchema, market_b: MarketSchema) -> bool:
    """Stage 2: deterministic logic gates that eliminate false positives."""
    target_equivalence = market_a.target_value == market_b.target_value
    directional_equivalence = market_a.condition == market_b.condition
    date_proximity = abs((market_a.resolution_date - market_b.resolution_date).days) <= DATE_PROXIMITY_DAYS
    return target_equivalence and directional_equivalence and date_proximity


def is_same_event(
    vector_a: np.ndarray,
    vector_b: np.ndarray,
    market_a: MarketSchema,
    market_b: MarketSchema,
) -> bool:
    """Full two-stage check: both the soft and hard gates must pass."""
    return is_soft_match(vector_a, vector_b) and is_hard_match(market_a, market_b)
