"""Coin-related database models."""

from __future__ import annotations

from datetime import date, datetime

from lythonic import utc_now
from lythonic.state import DbModel
from pydantic import Field


class Coin(DbModel["Coin"]):
    """A cryptocurrency coin, identified by its CoinGecko slug."""

    id: str = Field(description="(PK) CoinGecko slug")
    symbol: str
    name: str
    description: str
    genesis_date: date | None = Field(default=None)
    market_cap_rank: int | None = Field(default=None)
    homepage: str | None = Field(default=None)
    repo_url: str | None = Field(default=None)
    last_fetched: datetime = Field(default_factory=utc_now)


class CoinPrice(DbModel["CoinPrice"]):
    """A price snapshot for a coin at a given timestamp."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    coin_id: str = Field(description="(FK:Coin.id) Reference to coin")
    timestamp: datetime = Field(default_factory=utc_now)
    price_usd: float
    market_cap: float | None = Field(default=None)
    total_volume: float | None = Field(default=None)
    price_change_24h: float | None = Field(default=None)


class CoinCategory(DbModel["CoinCategory"]):
    """A category that coins can belong to."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    name: str
    description: str | None = Field(default=None)


class CoinCategoryLink(DbModel["CoinCategoryLink"]):
    """Many-to-many link between coins and categories."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    coin_id: str = Field(description="(FK:Coin.id) Reference to coin")
    category_id: int = Field(description="(FK:CoinCategory.id) Reference to category")
