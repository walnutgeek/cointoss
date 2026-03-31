"""Ontology-related database models for coin relationships."""

from __future__ import annotations

from datetime import datetime

from lythonic import utc_now
from lythonic.state import DbModel
from pydantic import Field


class RelationshipType(DbModel["RelationshipType"]):
    """A type of relationship that can exist between coins."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    name: str
    description: str
    is_symmetric: bool


class CoinRelationship(DbModel["CoinRelationship"]):
    """A directional relationship between two coins."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    coin_from: str = Field(description="(FK:Coin.id) Source coin")
    coin_to: str = Field(description="(FK:Coin.id) Target coin")
    relationship_type: int = Field(description="(FK:RelationshipType.id) Type of relationship")
    confidence: float
    source: str
    notes: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
