"""Schema registry for all cointoss database models."""

from __future__ import annotations

import sqlite3

from lythonic.state import Schema

from cointoss.models.coin import Coin, CoinCategory, CoinCategoryLink, CoinPrice
from cointoss.models.ontology import CoinRelationship, RelationshipType
from cointoss.models.portfolio import Holding, Portfolio, PortfolioSnapshot

__all__ = [
    "Coin",
    "CoinPrice",
    "CoinCategory",
    "CoinCategoryLink",
    "RelationshipType",
    "CoinRelationship",
    "Portfolio",
    "Holding",
    "PortfolioSnapshot",
    "create_schema",
    "seed_relationship_types",
]

_DEFAULT_RELATIONSHIP_TYPES: list[tuple[str, str, bool]] = [
    ("fork_of", "One coin forked from another", False),
    ("competes_with", "Coins targeting the same use case", True),
    ("ecosystem_member", "Coin belongs to another coin's ecosystem", False),
    ("depends_on", "Coin depends on another for functionality", False),
    ("bridges_to", "Coin provides bridging to another chain", True),
]


def create_schema() -> Schema:
    """Create and return a Schema with all cointoss models registered."""
    return Schema(
        [
            Coin,
            CoinPrice,
            CoinCategory,
            CoinCategoryLink,
            RelationshipType,
            CoinRelationship,
            Portfolio,
            Holding,
            PortfolioSnapshot,
        ]
    )


def seed_relationship_types(conn: sqlite3.Connection) -> None:
    """Insert the 5 default relationship types if they don't already exist."""
    existing_names = {rt.name for rt in RelationshipType.select(conn)}
    for name, description, is_symmetric in _DEFAULT_RELATIONSHIP_TYPES:
        if name not in existing_names:
            RelationshipType(name=name, description=description, is_symmetric=is_symmetric).save(
                conn
            )
    conn.commit()
