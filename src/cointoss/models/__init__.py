"""Schema registry for all cointoss database models."""

from __future__ import annotations

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
