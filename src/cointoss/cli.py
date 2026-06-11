"""CLI for the cointoss application, built with lythonic's ActionTree."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from lythonic import utc_now
from lythonic.compose.cli import ActionTree, Main, RunContext
from pydantic import BaseModel, Field

from cointoss.models import create_schema, seed_relationship_types
from cointoss.models import portfolio as portfolio_models
from cointoss.models.coin import Coin, CoinPrice
from cointoss.ontology import get_coin_relationships
from cointoss.sources.coingecko import CoinGeckoClient

DEFAULT_DB_PATH = Path("./cointoss.db")


def ensure_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database, create the schema, and seed relationship types."""
    conn = sqlite3.connect(str(db_path))
    schema = create_schema()
    if not schema.check_all_tables_exist(conn):
        schema.create_tables(conn)
    seed_relationship_types(conn)
    return conn


# ---- Root ----

root = ActionTree(Main)


# ---- coins group ----


class Coins(BaseModel):
    """Coin management commands."""

    db: str = Field(default=str(DEFAULT_DB_PATH), description="Path to database file")


coins = root.actions.add(Coins)


@coins.actions.wrap
def fetch(ctx: RunContext) -> None:
    """Fetch top coins from CoinGecko."""
    config = ctx.path.get("/coins")
    db_path = Path(config.db) if config else DEFAULT_DB_PATH
    conn = ensure_db(db_path)
    try:
        client = CoinGeckoClient()
        items = asyncio.run(client.fetch_coins_markets(per_page=50))
        for item in items:
            existing = Coin.select(conn, id=item.id)
            if existing:
                coin = existing[0]
                coin.symbol = item.symbol
                coin.name = item.name
                coin.last_fetched = utc_now()
                coin.update(conn, id=coin.id)
            else:
                Coin(
                    id=item.id,
                    symbol=item.symbol,
                    name=item.name,
                    description="",
                    last_fetched=utc_now(),
                ).insert(conn)
            if item.current_price is not None:
                CoinPrice(
                    coin_id=item.id,
                    price_usd=item.current_price,
                    market_cap=item.market_cap,
                    total_volume=item.total_volume,
                    price_change_24h=item.price_change_24h,
                ).save(conn)
        conn.commit()
        ctx.print(f"Fetched {len(items)} coins.")
    finally:
        conn.close()


@coins.actions.wrap
def list(ctx: RunContext) -> None:  # noqa: A001
    """List all tracked coins."""
    config = ctx.path.get("/coins")
    db_path = Path(config.db) if config else DEFAULT_DB_PATH
    conn = ensure_db(db_path)
    try:
        all_coins = Coin.select(conn)
        if not all_coins:
            ctx.print("No coins tracked yet. Run 'cointoss coins fetch' first.")
            return
        ctx.print(f"{'Rank':<6} {'Symbol':<10} {'Name'}")
        ctx.print("-" * 40)
        for c in sorted(all_coins, key=lambda x: x.market_cap_rank or 9999):
            rank = str(c.market_cap_rank) if c.market_cap_rank is not None else "-"
            ctx.print(f"{rank:<6} {c.symbol:<10} {c.name}")
    finally:
        conn.close()


@coins.actions.wrap
def info(ctx: RunContext, coin_id: str) -> None:
    """Show detailed info for a coin."""
    config = ctx.path.get("/coins")
    db_path = Path(config.db) if config else DEFAULT_DB_PATH
    conn = ensure_db(db_path)
    try:
        matches = Coin.select(conn, id=coin_id)
        if not matches:
            ctx.print(f"Coin '{coin_id}' not found.")
            return
        coin = matches[0]
        ctx.print(f"ID:          {coin.id}")
        ctx.print(f"Symbol:      {coin.symbol}")
        ctx.print(f"Name:        {coin.name}")
        ctx.print(f"Rank:        {coin.market_cap_rank or '-'}")
        ctx.print(f"Genesis:     {coin.genesis_date or '-'}")
        ctx.print(f"Homepage:    {coin.homepage or '-'}")
        ctx.print(f"Repo:        {coin.repo_url or '-'}")
        ctx.print(f"Description: {coin.description[:100] if coin.description else '-'}")

        # Latest price
        prices = CoinPrice.select(conn, coin_id=coin_id)
        if prices:
            latest = max(prices, key=lambda p: p.timestamp)
            ctx.print(f"Price (USD): ${latest.price_usd:,.2f}")
            if latest.market_cap is not None:
                ctx.print(f"Market Cap:  ${latest.market_cap:,.0f}")
        else:
            ctx.print("Price (USD): -")

        # Relationships
        rels = get_coin_relationships(conn, coin_id)
        if rels:
            ctx.print("Relationships:")
            for r in rels:
                ctx.print(f"  {r['direction']}: {r['relationship']} -> {r['coin']}")
    finally:
        conn.close()


# ---- portfolio group ----


class Portfolio(BaseModel):
    """Portfolio management commands."""

    db: str = Field(default=str(DEFAULT_DB_PATH), description="Path to database file")


portfolio = root.actions.add(Portfolio)


@portfolio.actions.wrap
def create(ctx: RunContext, name: str) -> None:
    """Create a named portfolio."""
    config = ctx.path.get("/portfolio")
    db_path = Path(config.db) if config else DEFAULT_DB_PATH
    conn = ensure_db(db_path)
    try:
        p = portfolio_models.Portfolio(name=name)
        p.save(conn)
        conn.commit()
        ctx.print(f"Portfolio '{name}' created.")
    finally:
        conn.close()


@portfolio.actions.wrap
def value(ctx: RunContext) -> None:
    """Show portfolio values."""
    config = ctx.path.get("/portfolio")
    db_path = Path(config.db) if config else DEFAULT_DB_PATH
    conn = ensure_db(db_path)
    try:
        portfolios = portfolio_models.Portfolio.select(conn)
        if not portfolios:
            ctx.print("No portfolios found. Run 'cointoss portfolio create <name>' first.")
            return
        for p in portfolios:
            holdings = portfolio_models.Holding.select(conn, portfolio_id=p.id)
            total = 0.0
            for h in holdings:
                prices = CoinPrice.select(conn, coin_id=h.coin_id)
                if prices:
                    latest = max(prices, key=lambda pr: pr.timestamp)
                    total += h.amount * latest.price_usd
            ctx.print(f"{p.name}: ${total:,.2f}")
    finally:
        conn.close()


# ---- Entry point ----


def run_cli() -> None:
    """Run the cointoss CLI."""
    result = root.run_args(sys.argv)
    sys.exit(0 if result.success else 1)


# Entry point referenced by pyproject.toml [project.scripts]
main = run_cli
