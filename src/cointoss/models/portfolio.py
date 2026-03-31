"""Portfolio-related database models."""

from __future__ import annotations

from datetime import datetime

from lythonic import utc_now
from lythonic.state import DbModel
from pydantic import Field


class Portfolio(DbModel["Portfolio"]):
    """A named portfolio of coin holdings."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    name: str
    description: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class Holding(DbModel["Holding"]):
    """A coin holding within a portfolio."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    portfolio_id: int = Field(description="(FK:Portfolio.id) Reference to portfolio")
    coin_id: str = Field(description="(FK:Coin.id) Reference to coin")
    amount: float
    cost_basis_usd: float | None = Field(default=None)
    acquired_at: datetime | None = Field(default=None)


class PortfolioSnapshot(DbModel["PortfolioSnapshot"]):
    """A point-in-time snapshot of the total value of a portfolio."""

    id: int = Field(default=-1, description="(PK) Auto-increment ID")
    portfolio_id: int = Field(description="(FK:Portfolio.id) Reference to portfolio")
    timestamp: datetime = Field(default_factory=utc_now)
    total_value_usd: float
