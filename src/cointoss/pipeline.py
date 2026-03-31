"""Data collection pipeline functions for CoinGecko API storage."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, TypedDict, cast

from lythonic import utc_now

from cointoss.models.coin import Coin, CoinPrice
from cointoss.sources.coingecko import CoinGeckoClient


class PipelineStage(TypedDict):
    """A single stage descriptor in the fetch pipeline."""

    name: str
    fn: str
    description: str


class PipelineDescriptor(TypedDict):
    """Descriptor dict returned by build_fetch_pipeline()."""

    description: str
    stages: list[PipelineStage]


def store_coin_from_list_entry(conn: sqlite3.Connection, entry: dict[str, Any]) -> Coin:
    """Store a CoinGecko coin list entry into the database.

    Takes a CoinGecko coin list entry dict with keys: id, symbol, name.
    Checks if coin exists and inserts or updates it, setting last_fetched to now.
    """
    existing = Coin.select(conn, id=entry["id"])
    if existing:
        coin = existing[0]
        coin.symbol = entry["symbol"]
        coin.name = entry["name"]
        coin.last_fetched = utc_now()
        coin.update(conn, id=coin.id)
    else:
        coin = Coin(
            id=entry["id"],
            symbol=entry["symbol"],
            name=entry["name"],
            description="",
            last_fetched=utc_now(),
        )
        coin.insert(conn)
    return coin


def _parse_genesis_date(genesis_date_str: str | None) -> date | None:
    """Parse a genesis date string into a date object, or return None."""
    if not genesis_date_str:
        return None
    try:
        return date.fromisoformat(genesis_date_str)
    except (ValueError, TypeError):
        return None


def store_coin_detail(conn: sqlite3.Connection, detail: dict[str, Any]) -> Coin:
    """Store a CoinGecko /coins/{id} response dict into the database.

    Extracts description, genesis_date, market_cap_rank, homepage, and repo_url.
    Updates or inserts the Coin, and creates a CoinPrice record.
    """
    coin_id: str = detail["id"]

    description: str = ""
    desc_block = detail.get("description")
    if isinstance(desc_block, dict):
        raw_desc = cast(dict[str, object], desc_block).get("en")
        description = str(raw_desc) if raw_desc else ""

    genesis_date = _parse_genesis_date(cast(str | None, detail.get("genesis_date")))
    raw_rank = detail.get("market_cap_rank")
    market_cap_rank: int | None = int(raw_rank) if raw_rank is not None else None

    homepage: str | None = None
    repo_url: str | None = None
    links = detail.get("links")
    if isinstance(links, dict):
        homepages = cast(dict[str, object], links).get("homepage")
        if isinstance(homepages, list) and homepages and isinstance(homepages[0], str):
            homepage = homepages[0]
        repos = cast(dict[str, object], links).get("repos_url")
        if isinstance(repos, dict):
            github_repos = cast(dict[str, object], repos).get("github")
            if isinstance(github_repos, list) and github_repos and isinstance(github_repos[0], str):
                repo_url = github_repos[0]

    existing = Coin.select(conn, id=coin_id)
    if existing:
        coin = existing[0]
        coin.description = description
        coin.genesis_date = genesis_date
        coin.market_cap_rank = market_cap_rank
        coin.homepage = homepage
        coin.repo_url = repo_url
        coin.last_fetched = utc_now()
        coin.update(conn, id=coin.id)
    else:
        symbol: str = cast(str, detail.get("symbol", ""))
        name: str = cast(str, detail.get("name", ""))
        coin = Coin(
            id=coin_id,
            symbol=symbol,
            name=name,
            description=description,
            genesis_date=genesis_date,
            market_cap_rank=market_cap_rank,
            homepage=homepage,
            repo_url=repo_url,
            last_fetched=utc_now(),
        )
        coin.insert(conn)

    # Create a CoinPrice record from market_data.current_price.usd
    price_usd: float | None = None
    market_data = detail.get("market_data")
    if isinstance(market_data, dict):
        current_price = cast(dict[str, object], market_data).get("current_price")
        if isinstance(current_price, dict):
            raw_usd = cast(dict[str, object], current_price).get("usd")
            if isinstance(raw_usd, (int, float)):
                price_usd = float(raw_usd)

    if price_usd is not None:
        CoinPrice(coin_id=coin_id, price_usd=float(price_usd)).save(conn)

    return coin


async def run_fetch_top_coins(
    conn: sqlite3.Connection,
    client: CoinGeckoClient,
    per_page: int = 50,
) -> list[str]:
    """Fetch top coins by market cap and store them in the database.

    Calls client.fetch_top_coins(), stores each coin, creates CoinPrice records,
    commits, and returns a list of coin IDs.
    """
    markets_data = await client.fetch_top_coins(per_page=per_page)

    coin_ids: list[str] = []
    for entry in markets_data:
        coin_id: str = entry["id"]

        # Build a minimal coin list entry and store it
        list_entry = {
            "id": coin_id,
            "symbol": entry.get("symbol", ""),
            "name": entry.get("name", ""),
        }
        store_coin_from_list_entry(conn, list_entry)

        # Create a CoinPrice from the markets data
        price_usd = entry.get("current_price")
        if price_usd is not None:
            CoinPrice(
                coin_id=coin_id,
                price_usd=float(price_usd),
                market_cap=entry.get("market_cap"),
                total_volume=entry.get("total_volume"),
                price_change_24h=entry.get("price_change_24h"),
            ).save(conn)

        coin_ids.append(coin_id)

    conn.commit()
    return coin_ids


async def run_fetch_coin_details(
    conn: sqlite3.Connection,
    client: CoinGeckoClient,
    coin_ids: list[str],
) -> None:
    """Fetch and store detailed info for a list of coin IDs.

    For each coin_id, checks the rate limiter, fetches detail,
    calls store_coin_detail(), and commits.
    """
    for coin_id in coin_ids:
        if not client.rate_limiter.can_call():
            break
        detail = await client.fetch_coin_detail(coin_id)
        store_coin_detail(conn, detail)

    conn.commit()


def build_fetch_pipeline() -> PipelineDescriptor:
    """Return a descriptor dict for the fetch pipeline.

    Placeholder for future lythonic DAG integration.
    """
    return {
        "description": "CoinGecko data collection pipeline",
        "stages": [
            {
                "name": "fetch_top_coins",
                "fn": "run_fetch_top_coins",
                "description": "Fetch top coins by market cap and store them",
            },
            {
                "name": "fetch_coin_details",
                "fn": "run_fetch_coin_details",
                "description": "Fetch detailed info for each coin and store it",
            },
        ],
    }
