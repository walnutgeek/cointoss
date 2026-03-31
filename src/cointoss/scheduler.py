"""Periodic fetch scheduler for CoinGecko data collection."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from lythonic.periodic import PeriodicTask

from cointoss.models.coin import Coin
from cointoss.pipeline import run_fetch_coin_details, run_fetch_top_coins
from cointoss.sources.coingecko import CoinGeckoClient, RateLimiter

_4_HOURS = 4 * 60 * 60
_8_HOURS = 8 * 60 * 60


class FetchScheduler:
    """Manages periodic data collection from CoinGecko."""

    db_path: Path
    rate_limiter: RateLimiter
    client: CoinGeckoClient
    _tracked_coin_ids: list[str]

    def __init__(self, db_path: Path, rate_limiter: RateLimiter | None = None) -> None:
        self.db_path = db_path
        self.rate_limiter = rate_limiter or RateLimiter()
        self.client = CoinGeckoClient(rate_limiter=self.rate_limiter)
        self._tracked_coin_ids = []

    async def fetch_top_coins_task(self) -> None:
        """Fetch top coins by market cap and store coin IDs for detail fetching."""
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            coin_ids = await run_fetch_top_coins(conn, self.client, per_page=50)
            self._tracked_coin_ids = coin_ids

    async def fetch_details_task(self) -> None:
        """Fetch detailed info for the first 10 tracked coins."""
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            if not self._tracked_coin_ids:
                coins = Coin.select(conn)
                self._tracked_coin_ids = [c.id for c in coins]
            await run_fetch_coin_details(conn, self.client, self._tracked_coin_ids[:10])

    def create_periodic_tasks(self) -> list[PeriodicTask]:
        """Return a list of PeriodicTask instances for periodic scheduling."""
        return [
            PeriodicTask(freq=_4_HOURS, logic=self.fetch_top_coins_task),
            PeriodicTask(freq=_8_HOURS, logic=self.fetch_details_task),
        ]
