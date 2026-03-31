"""Tests for cointoss database models and schema registry."""

from __future__ import annotations

import contextlib
import math
import sqlite3
import tempfile
from datetime import UTC, date
from pathlib import Path

import pytest

from cointoss.models import (
    Coin,
    CoinCategory,
    CoinCategoryLink,
    CoinPrice,
    CoinRelationship,
    Holding,
    Portfolio,
    PortfolioSnapshot,
    RelationshipType,
    create_schema,
)


@pytest.fixture
def db_conn():
    """Provide a fresh in-memory-backed SQLite connection with all tables created."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        schema = create_schema()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            schema.create_tables(conn)
            yield conn


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def test_schema_creates_all_tables(db_conn: sqlite3.Connection):
    """All nine model tables should exist after create_tables()."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}

    expected = {
        "Coin",
        "CoinPrice",
        "CoinCategory",
        "CoinCategoryLink",
        "RelationshipType",
        "CoinRelationship",
        "Portfolio",
        "Holding",
        "PortfolioSnapshot",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_create_schema_returns_schema_with_nine_tables():
    """create_schema() must return a Schema covering all nine models."""
    schema = create_schema()
    table_names = {t.get_table_name() for t in schema.tables}
    assert len(table_names) == 9


# ---------------------------------------------------------------------------
# Coin CRUD
# ---------------------------------------------------------------------------


def test_coin_insert_and_load_by_id(db_conn: sqlite3.Connection):
    """Insert a Coin and load it back — all fields must match."""
    from lythonic import utc_now

    now = utc_now()
    coin = Coin(
        id="bitcoin",
        symbol="btc",
        name="Bitcoin",
        description="The original cryptocurrency",
        genesis_date=date(2009, 1, 3),
        market_cap_rank=1,
        homepage="https://bitcoin.org",
        repo_url="https://github.com/bitcoin/bitcoin",
        last_fetched=now,
    )
    coin.insert(db_conn)
    db_conn.commit()

    results = Coin.select(db_conn, id="bitcoin")
    assert len(results) == 1
    loaded = results[0]
    assert loaded.id == "bitcoin"
    assert loaded.symbol == "btc"
    assert loaded.name == "Bitcoin"
    assert loaded.description == "The original cryptocurrency"
    assert loaded.genesis_date == date(2009, 1, 3)
    assert loaded.market_cap_rank == 1
    assert loaded.homepage == "https://bitcoin.org"
    assert loaded.repo_url == "https://github.com/bitcoin/bitcoin"
    # Datetime round-trips through ISO string; compare to-the-second
    assert loaded.last_fetched.replace(tzinfo=UTC).isoformat()[:19] == now.isoformat()[:19]


def test_coin_nullable_fields_default_to_none(db_conn: sqlite3.Connection):
    """A Coin with only required fields should have None for nullable columns."""
    from lythonic import utc_now

    coin = Coin(
        id="litecoin",
        symbol="ltc",
        name="Litecoin",
        description="",
        last_fetched=utc_now(),
    )
    coin.insert(db_conn)
    db_conn.commit()

    results = Coin.select(db_conn, id="litecoin")
    assert len(results) == 1
    loaded = results[0]
    assert loaded.genesis_date is None
    assert loaded.market_cap_rank is None
    assert loaded.homepage is None
    assert loaded.repo_url is None


def test_coin_select_all(db_conn: sqlite3.Connection):
    """Select without filters should return all inserted coins."""
    from lythonic import utc_now

    now = utc_now()
    for slug, sym, nm in [("eth", "eth", "Ethereum"), ("sol", "sol", "Solana")]:
        Coin(id=slug, symbol=sym, name=nm, description="", last_fetched=now).insert(db_conn)
    db_conn.commit()

    all_coins = Coin.select(db_conn)
    ids = {c.id for c in all_coins}
    assert {"eth", "sol"}.issubset(ids)


# ---------------------------------------------------------------------------
# Auto-increment PK models
# ---------------------------------------------------------------------------


def test_coin_price_auto_increment(db_conn: sqlite3.Connection):
    """CoinPrice PKs should be assigned by auto-increment on save()."""
    from lythonic import utc_now

    Coin(id="bitcoin", symbol="btc", name="Bitcoin", description="", last_fetched=utc_now()).insert(
        db_conn
    )

    p1 = CoinPrice(coin_id="bitcoin", price_usd=50000.0)
    p2 = CoinPrice(coin_id="bitcoin", price_usd=51000.0)
    p1.save(db_conn)
    p2.save(db_conn)
    db_conn.commit()

    assert p1.id != -1
    assert p2.id != -1
    assert p1.id != p2.id


def test_portfolio_and_holding(db_conn: sqlite3.Connection):
    """Portfolio and Holding inserts should set PKs and FK references correctly."""
    from lythonic import utc_now

    Coin(id="btc", symbol="btc", name="Bitcoin", description="", last_fetched=utc_now()).insert(
        db_conn
    )

    portfolio = Portfolio(name="My Portfolio")
    portfolio.save(db_conn)
    assert portfolio.id != -1

    holding = Holding(portfolio_id=portfolio.id, coin_id="btc", amount=0.5)
    holding.save(db_conn)
    db_conn.commit()
    assert holding.id != -1

    loaded_holdings = Holding.select(db_conn, portfolio_id=portfolio.id)
    assert len(loaded_holdings) == 1
    assert loaded_holdings[0].coin_id == "btc"
    assert math.isclose(loaded_holdings[0].amount, 0.5)


def test_relationship_type_and_coin_relationship(db_conn: sqlite3.Connection):
    """RelationshipType and CoinRelationship inserts should work end-to-end."""
    from lythonic import utc_now

    now = utc_now()
    for slug, name in [("bitcoin", "Bitcoin"), ("litecoin", "Litecoin")]:
        Coin(id=slug, symbol=slug[:3], name=name, description="", last_fetched=now).insert(db_conn)

    rel_type = RelationshipType(name="fork_of", description="Forked from", is_symmetric=False)
    rel_type.save(db_conn)

    rel = CoinRelationship(
        coin_from="litecoin",
        coin_to="bitcoin",
        relationship_type=rel_type.id,
        confidence=0.95,
        source="manual",
    )
    rel.save(db_conn)
    db_conn.commit()

    assert rel.id != -1
    loaded = CoinRelationship.load_by_id(db_conn, rel.id)
    assert loaded is not None
    assert loaded.coin_from == "litecoin"
    assert loaded.coin_to == "bitcoin"
    assert math.isclose(loaded.confidence, 0.95)


def test_coin_category_and_link(db_conn: sqlite3.Connection):
    """CoinCategory and CoinCategoryLink should insert and be selectable."""
    from lythonic import utc_now

    Coin(id="eth", symbol="eth", name="Ethereum", description="", last_fetched=utc_now()).insert(
        db_conn
    )

    cat = CoinCategory(name="Layer 1", description="Layer 1 blockchains")
    cat.save(db_conn)

    link = CoinCategoryLink(coin_id="eth", category_id=cat.id)
    link.save(db_conn)
    db_conn.commit()

    links = CoinCategoryLink.select(db_conn, coin_id="eth")
    assert len(links) == 1
    assert links[0].category_id == cat.id


def test_portfolio_snapshot(db_conn: sqlite3.Connection):
    """PortfolioSnapshot should store total_value_usd and round-trip correctly."""
    portfolio = Portfolio(name="Snap Test")
    portfolio.save(db_conn)

    snap = PortfolioSnapshot(portfolio_id=portfolio.id, total_value_usd=12345.67)
    snap.save(db_conn)
    db_conn.commit()

    snaps = PortfolioSnapshot.select(db_conn, portfolio_id=portfolio.id)
    assert len(snaps) == 1
    assert math.isclose(snaps[0].total_value_usd, 12345.67)
