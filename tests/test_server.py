"""Tests for the Tornado API server."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import tornado.testing

from cointoss.models import Coin, CoinPrice, create_schema, seed_relationship_types
from cointoss.server import make_app


class TestCoinsAPI(tornado.testing.AsyncHTTPTestCase):
    """Tests for the /api/coins endpoints."""

    def setUp(self) -> None:  # pyright: ignore[reportImplicitOverride]
        self._tmp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()  # pyright: ignore[reportUninitializedInstanceVariable]
        self.db_path: Path = Path(self._tmp_dir.name) / "test.db"  # pyright: ignore[reportUninitializedInstanceVariable]

        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            create_schema().create_tables(conn)
            seed_relationship_types(conn)

            Coin(
                id="bitcoin",
                symbol="btc",
                name="Bitcoin",
                description="The original cryptocurrency",
                genesis_date=date(2009, 1, 3),
                market_cap_rank=1,
                homepage="https://bitcoin.org",
                repo_url="https://github.com/bitcoin/bitcoin",
            ).insert(conn)

            CoinPrice(
                coin_id="bitcoin",
                price_usd=50000.0,
                market_cap=1_000_000_000.0,
                total_volume=50_000_000.0,
            ).save(conn)

            conn.commit()

        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self._tmp_dir.cleanup()

    def get_app(self) -> tornado.web.Application:
        return make_app(self.db_path)

    def test_list_coins(self) -> None:
        """GET /api/coins returns 200 and includes bitcoin."""
        response = self.fetch("/api/coins")
        self.assertEqual(response.code, 200)

        data = json.loads(response.body)
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) >= 1)

        ids = [c["id"] for c in data]
        self.assertIn("bitcoin", ids)

        btc = next(c for c in data if c["id"] == "bitcoin")
        self.assertEqual(btc["symbol"], "btc")
        self.assertEqual(btc["name"], "Bitcoin")
        self.assertEqual(btc["market_cap_rank"], 1)

    def test_get_coin(self) -> None:
        """GET /api/coins/bitcoin returns 200 with full coin details."""
        response = self.fetch("/api/coins/bitcoin")
        self.assertEqual(response.code, 200)

        data = json.loads(response.body)
        self.assertEqual(data["id"], "bitcoin")
        self.assertEqual(data["symbol"], "btc")
        self.assertEqual(data["name"], "Bitcoin")
        self.assertEqual(data["description"], "The original cryptocurrency")
        self.assertEqual(data["genesis_date"], "2009-01-03")
        self.assertEqual(data["market_cap_rank"], 1)
        self.assertEqual(data["homepage"], "https://bitcoin.org")
        self.assertEqual(data["repo_url"], "https://github.com/bitcoin/bitcoin")
        self.assertAlmostEqual(data["latest_price"], 50000.0)
        self.assertIsInstance(data["relationships"], list)

    def test_get_coin_not_found(self) -> None:
        """GET /api/coins/nonexistent returns 404."""
        response = self.fetch("/api/coins/nonexistent")
        self.assertEqual(response.code, 404)

        data = json.loads(response.body)
        self.assertIn("error", data)

    def test_get_coin_prices(self) -> None:
        """GET /api/coins/bitcoin/prices returns price history."""
        response = self.fetch("/api/coins/bitcoin/prices")
        self.assertEqual(response.code, 200)

        data = json.loads(response.body)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

        price = data[0]
        self.assertIn("timestamp", price)
        self.assertAlmostEqual(price["price_usd"], 50000.0)
        self.assertAlmostEqual(price["market_cap"], 1_000_000_000.0)

    def test_portfolio_list(self) -> None:
        """GET /api/portfolio returns 200 with a list."""
        response = self.fetch("/api/portfolio")
        self.assertEqual(response.code, 200)

        data = json.loads(response.body)
        self.assertIsInstance(data, list)

    def test_ontology_graph(self) -> None:
        """GET /api/ontology/graph returns nodes and edges."""
        response = self.fetch("/api/ontology/graph")
        self.assertEqual(response.code, 200)

        data = json.loads(response.body)
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertIsInstance(data["nodes"], list)
        self.assertIsInstance(data["edges"], list)

        node_ids = [n["id"] for n in data["nodes"]]
        self.assertIn("bitcoin", node_ids)

    def test_cors_headers(self) -> None:
        """Responses include CORS headers."""
        response = self.fetch("/api/coins")
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
