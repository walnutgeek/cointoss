"""CoinGecko API client with rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "https://api.coingecko.com/api/v3"


@dataclass
class RateLimiter:
    """Tracks daily API call budget for CoinGecko free tier."""

    daily_budget: int = 300
    calls_today: int = 0
    day_start: float = field(default_factory=time.time)

    def _reset_if_new_day(self) -> None:
        """Reset counter if 86400 seconds have elapsed since day_start."""
        if time.time() - self.day_start >= 86400:
            self.calls_today = 0
            self.day_start = time.time()

    def can_call(self) -> bool:
        """Return True if calls_today is under the daily_budget."""
        self._reset_if_new_day()
        return self.calls_today < self.daily_budget

    def record_call(self) -> None:
        """Increment calls_today by one."""
        self.calls_today += 1

    @property
    def remaining(self) -> int:
        """Return the number of remaining calls in the current budget."""
        return self.daily_budget - self.calls_today


class CoinGeckoClient:
    """Async HTTP client for the CoinGecko public API."""

    rate_limiter: RateLimiter

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self.rate_limiter = rate_limiter or RateLimiter()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Check rate limit, make a GET request, record the call, and return JSON."""
        if not self.rate_limiter.can_call():
            raise RuntimeError(
                f"CoinGecko daily budget of {self.rate_limiter.daily_budget} calls exhausted."
            )
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}{path}", params=params)
            response.raise_for_status()
            self.rate_limiter.record_call()
            return response.json()

    async def fetch_coin_list(self) -> list[dict[str, Any]]:
        """Fetch the full list of coins from /coins/list."""
        return await self._get("/coins/list")  # type: ignore[no-any-return]

    async def fetch_coin_detail(self, coin_id: str) -> dict[str, Any]:
        """Fetch details for a single coin from /coins/{id}."""
        params: dict[str, Any] = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
        }
        return await self._get(f"/coins/{coin_id}", params=params)  # type: ignore[no-any-return]

    async def fetch_price_history(
        self, coin_id: str, vs_currency: str = "usd", days: str = "30"
    ) -> dict[str, Any]:
        """Fetch market chart data from /coins/{id}/market_chart."""
        params: dict[str, Any] = {"vs_currency": vs_currency, "days": days}
        return await self._get(f"/coins/{coin_id}/market_chart", params=params)  # type: ignore[no-any-return]

    async def fetch_top_coins(
        self, vs_currency: str = "usd", per_page: int = 50, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch top coins by market cap from /coins/markets."""
        params: dict[str, Any] = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
        }
        return await self._get("/coins/markets", params=params)  # type: ignore[no-any-return]
