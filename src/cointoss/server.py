"""Tornado API server for the cointoss application."""

import contextlib
import json
import sqlite3
from pathlib import Path

import tornado.ioloop
import tornado.web

from cointoss.models import Coin, CoinPrice, Portfolio, create_schema, seed_relationship_types
from cointoss.models.ontology import CoinRelationship, RelationshipType
from cointoss.ontology import get_coin_relationships


class BaseHandler(tornado.web.RequestHandler):
    """Base handler with shared DB and header utilities."""

    def initialize(self, db_path: Path) -> None:  # pyright: ignore[reportImplicitOverride]
        self.db_path: Path = db_path  # pyright: ignore[reportUninitializedInstanceVariable]

    def get_conn(self) -> sqlite3.Connection:
        """Open a SQLite connection and ensure schema exists."""
        conn = sqlite3.connect(str(self.db_path))
        schema = create_schema()
        if not schema.check_all_tables_exist(conn):
            schema.create_tables(conn)
        return conn

    def set_default_headers(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def options(self, *args: object, **kwargs: object) -> None:
        self.set_status(204)
        self.finish()


class CoinsListHandler(BaseHandler):
    """GET /api/coins — return all coins."""

    def get(self) -> None:
        with contextlib.closing(self.get_conn()) as conn:
            coins = Coin.select(conn)
        result = [
            {
                "id": c.id,
                "symbol": c.symbol,
                "name": c.name,
                "market_cap_rank": c.market_cap_rank,
            }
            for c in coins
        ]
        self.write(json.dumps(result))


class CoinDetailHandler(BaseHandler):
    """GET /api/coins/<coin_id> — return detail for a single coin."""

    def get(self, coin_id: str) -> None:
        with contextlib.closing(self.get_conn()) as conn:
            matches = Coin.select(conn, id=coin_id)
            if not matches:
                self.set_status(404)
                self.write(json.dumps({"error": f"Coin '{coin_id}' not found"}))
                return
            coin = matches[0]

            prices = CoinPrice.select(conn, coin_id=coin_id)
            latest_price = None
            if prices:
                latest = max(prices, key=lambda p: p.timestamp)
                latest_price = latest.price_usd

            relationships = get_coin_relationships(conn, coin_id)

        result = {
            "id": coin.id,
            "symbol": coin.symbol,
            "name": coin.name,
            "description": coin.description,
            "genesis_date": coin.genesis_date.isoformat() if coin.genesis_date else None,
            "market_cap_rank": coin.market_cap_rank,
            "homepage": coin.homepage,
            "repo_url": coin.repo_url,
            "latest_price": latest_price,
            "relationships": relationships,
        }
        self.write(json.dumps(result))


class CoinPricesHandler(BaseHandler):
    """GET /api/coins/<coin_id>/prices — return price history."""

    def get(self, coin_id: str) -> None:
        with contextlib.closing(self.get_conn()) as conn:
            prices = CoinPrice.select(conn, coin_id=coin_id)

        sorted_prices = sorted(prices, key=lambda p: p.timestamp)
        result = [
            {
                "timestamp": p.timestamp.isoformat(),
                "price_usd": p.price_usd,
                "market_cap": p.market_cap,
                "total_volume": p.total_volume,
            }
            for p in sorted_prices
        ]
        self.write(json.dumps(result))


class PortfolioListHandler(BaseHandler):
    """GET /api/portfolio — return all portfolios."""

    def get(self) -> None:
        with contextlib.closing(self.get_conn()) as conn:
            portfolios = Portfolio.select(conn)
        result = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "created_at": p.created_at.isoformat(),
            }
            for p in portfolios
        ]
        self.write(json.dumps(result))


class OntologyGraphHandler(BaseHandler):
    """GET /api/ontology/graph — return nodes and edges."""

    def get(self) -> None:
        with contextlib.closing(self.get_conn()) as conn:
            coins = Coin.select(conn)
            relationships = CoinRelationship.select(conn)
            rel_types = {rt.id: rt for rt in RelationshipType.select(conn)}

        nodes = [{"id": c.id, "label": c.name, "symbol": c.symbol} for c in coins]

        edges = []
        for rel in relationships:
            rt = rel_types.get(rel.relationship_type)
            edges.append(
                {
                    "from": rel.coin_from,
                    "to": rel.coin_to,
                    "type": rt.name if rt else str(rel.relationship_type),
                    "confidence": rel.confidence,
                }
            )

        self.write(json.dumps({"nodes": nodes, "edges": edges}))


def make_app(db_path: Path, frontend_dir: Path | None = None) -> tornado.web.Application:
    """Create and return the Tornado web application."""
    handler_kwargs = {"db_path": db_path}

    routes: list[tuple[str, type, dict[str, str | Path]]] = [  # pyright: ignore[reportAssignmentType]
        (r"/api/coins", CoinsListHandler, handler_kwargs),
        (r"/api/coins/([^/]+)/prices", CoinPricesHandler, handler_kwargs),
        (r"/api/coins/([^/]+)", CoinDetailHandler, handler_kwargs),
        (r"/api/portfolio", PortfolioListHandler, handler_kwargs),
        (r"/api/ontology/graph", OntologyGraphHandler, handler_kwargs),
    ]

    if frontend_dir is not None and frontend_dir.exists():
        routes.append(
            (
                r"/(.*)",
                tornado.web.StaticFileHandler,
                {"path": str(frontend_dir), "default_filename": "index.html"},
            )
        )

    return tornado.web.Application(routes)  # type: ignore[arg-type]


_DEFAULT_DB_PATH = Path("./cointoss.db")


def start_server(db_path: Path = _DEFAULT_DB_PATH, port: int = 8888) -> None:
    """Start the Tornado API server."""
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        schema = create_schema()
        schema.create_tables(conn)
        seed_relationship_types(conn)

    app = make_app(db_path)
    app.listen(port)
    print(f"Cointoss API server running on http://localhost:{port}")
    tornado.ioloop.IOLoop.current().start()
