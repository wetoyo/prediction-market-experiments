"""Pydantic schema for structured extraction of prediction market titles."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class MarketSchema(BaseModel):
    category: Literal["macro", "crypto", "politics", "sports", "weather"] = Field(
        description="The high-level category of the prediction market."
    )
    underlying_asset: str = Field(
        description="The core noun, entity, or ticker being tracked (e.g., 'Federal Reserve', 'Bitcoin', 'England Football Team')."
    )
    condition: Literal["greater_than", "less_than", "equal_to", "bracket"] = Field(
        description="The explicit direction of the contract's conditional logic."
    )
    target_value: float = Field(
        description="The explicit numerical strike price or target rate of the contract."
    )
    unit: str = Field(
        description="The unit of measurement of the target value (e.g., 'bps', 'USD', 'percent', 'degrees')."
    )
    resolution_date: date = Field(
        description="The target date or exact end-of-month date when the contract resolves."
    )
