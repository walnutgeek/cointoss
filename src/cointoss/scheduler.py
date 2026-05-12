"""Periodic fetch scheduler for CoinGecko data collection."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date
from pathlib import Path

from lythonic import utc_now
from lythonic.periodic import PeriodicTask

from cointoss.models.coin import Coin, CoinPrice
from cointoss.sources.coingecko import CoinGeckoClient

_4_HOURS = 4 * 60 * 60
_8_HOURS = 8 * 60 * 60


def _parse_genesis_date(genesis_date_str: str | None) -> date | None:
    """Parse a genesis date string into a date object, or return None."""
    if not genesis_date_str:
        return None
    try:
        return date.fromisoformat(genesis_date_str)
    except (ValueError, TypeError):
        return None


class FetchScheduler:
    """Manages periodic data collection from CoinGecko."""

    db_path: Path
    client: CoinGeckoClient
    _tracked_coin_ids: list[str]

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.client = CoinGeckoClient()
        self._tracked_coin_ids = []

    async def fetch_top_coins_task(self) -> None:
        """Fetch top coins by market cap and store coin IDs for detail fetching."""
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            items = await self.client.fetch_coins_markets(per_page=50)
            coin_ids: list[str] = []
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
                coin_ids.append(item.id)
            conn.commit()
            self._tracked_coin_ids = coin_ids

    async def fetch_details_task(self) -> None:
        """Fetch detailed info for the first 10 tracked coins."""
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            if not self._tracked_coin_ids:
                coins = Coin.select(conn)
                self._tracked_coin_ids = [c.id for c in coins]
            for coin_id in self._tracked_coin_ids[:10]:
                detail = await self.client.fetch_coin(coin_id)
                description = ""
                if detail.description:
                    description = detail.description.get("en", "")
                homepage: str | None = None
                repo_url: str | None = None
                if detail.links:
                    if detail.links.homepage:
                        homepage = next((h for h in detail.links.homepage if h), None)
                    if detail.links.repos_url:
                        github = detail.links.repos_url.get("github", [])
                        repo_url = next((r for r in github if r), None)

                existing = Coin.select(conn, id=detail.id)
                if existing:
                    coin = existing[0]
                    coin.description = description
                    coin.genesis_date = _parse_genesis_date(detail.genesis_date)
                    coin.market_cap_rank = detail.market_cap_rank
                    coin.homepage = homepage
                    coin.repo_url = repo_url
                    coin.last_fetched = utc_now()
                    coin.update(conn, id=coin.id)
                else:
                    Coin(
                        id=detail.id,
                        symbol=detail.symbol,
                        name=detail.name,
                        description=description,
                        genesis_date=_parse_genesis_date(detail.genesis_date),
                        market_cap_rank=detail.market_cap_rank,
                        homepage=homepage,
                        repo_url=repo_url,
                        last_fetched=utc_now(),
                    ).insert(conn)

                # Store price from market_data if available
                if detail.market_data:
                    current_price = detail.market_data.get("current_price")
                    if isinstance(current_price, dict):
                        usd = current_price.get("usd")  # pyright: ignore[reportUnknownVariableType]
                        if isinstance(usd, (int, float)):
                            CoinPrice(coin_id=detail.id, price_usd=float(usd)).save(conn)
            conn.commit()

    def create_periodic_tasks(self) -> list[PeriodicTask]:
        """Return a list of PeriodicTask instances for periodic scheduling."""
        return [
            PeriodicTask(freq=_4_HOURS, logic=self.fetch_top_coins_task),
            PeriodicTask(freq=_8_HOURS, logic=self.fetch_details_task),
        ]
