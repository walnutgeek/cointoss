"""Tests for the CoinGecko data collection pipeline."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path

from cointoss.models import Coin, CoinPrice, create_schema
from cointoss.pipeline import PipelineDescriptor, build_fetch_pipeline, store_coin_from_list_entry


def test_store_coin_from_list_entry() -> None:
    """store_coin_from_list_entry should insert a coin and set all fields correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        schema = create_schema()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            schema.create_tables(conn)

            entry = {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}
            store_coin_from_list_entry(conn, entry)
            conn.commit()

            results = Coin.select(conn, id="bitcoin")
            assert len(results) == 1
            coin = results[0]
            assert coin.id == "bitcoin"
            assert coin.symbol == "btc"
            assert coin.name == "Bitcoin"
            assert coin.last_fetched is not None


def test_store_coin_from_list_entry_updates_existing() -> None:
    """store_coin_from_list_entry should update symbol/name if coin already exists."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        schema = create_schema()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            schema.create_tables(conn)

            # Insert initial entry
            entry = {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}
            store_coin_from_list_entry(conn, entry)
            conn.commit()

            # Update with new name
            updated_entry = {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin (Updated)"}
            store_coin_from_list_entry(conn, updated_entry)
            conn.commit()

            results = Coin.select(conn, id="bitcoin")
            assert len(results) == 1
            coin = results[0]
            assert coin.symbol == "BTC"
            assert coin.name == "Bitcoin (Updated)"


def test_build_fetch_pipeline() -> None:
    """build_fetch_pipeline() should return a dict with a 'stages' key."""
    pipeline: PipelineDescriptor = build_fetch_pipeline()
    assert isinstance(pipeline, dict)
    assert "stages" in pipeline
    stages = pipeline["stages"]
    assert isinstance(stages, list)
    assert len(stages) > 0


def test_store_coin_detail() -> None:
    """store_coin_detail should update coin fields and create a CoinPrice record."""
    from cointoss.pipeline import store_coin_detail

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        schema = create_schema()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            schema.create_tables(conn)

            detail = {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "description": {"en": "The original cryptocurrency"},
                "genesis_date": "2009-01-03",
                "market_cap_rank": 1,
                "links": {
                    "homepage": ["https://bitcoin.org"],
                    "repos_url": {
                        "github": ["https://github.com/bitcoin/bitcoin"],
                    },
                },
                "market_data": {
                    "current_price": {"usd": 50000.0},
                },
            }

            store_coin_detail(conn, detail)
            conn.commit()

            coins = Coin.select(conn, id="bitcoin")
            assert len(coins) == 1
            coin = coins[0]
            assert coin.description == "The original cryptocurrency"
            assert coin.market_cap_rank == 1
            assert coin.homepage == "https://bitcoin.org"
            assert coin.repo_url == "https://github.com/bitcoin/bitcoin"

            prices = CoinPrice.select(conn, coin_id="bitcoin")
            assert len(prices) == 1
            assert prices[0].price_usd == 50000.0
