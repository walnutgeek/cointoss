"""CoinGecko API client."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlencode

from lythonic.compose.namespace import require_cache
from pydantic import BaseModel, ConfigDict, model_validator
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

log = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"


# -- Response models --


class CoinListItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    id: str
    symbol: str
    name: str
    platforms: dict[str, str | None] | None = None


class CoinRoi(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    times: float
    currency: str
    percentage: float


class CoinsMarketItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    id: str
    symbol: str
    name: str
    image: str | None = None
    current_price: float | None = None
    market_cap: float | None = None
    market_cap_rank: int | None = None
    fully_diluted_valuation: float | None = None
    total_volume: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    price_change_24h: float | None = None
    price_change_percentage_24h: float | None = None
    market_cap_change_24h: float | None = None
    market_cap_change_percentage_24h: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None
    ath: float | None = None
    ath_change_percentage: float | None = None
    ath_date: str | None = None
    atl: float | None = None
    atl_change_percentage: float | None = None
    atl_date: str | None = None
    roi: CoinRoi | None = None
    last_updated: str | None = None


class CoinImage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    thumb: str | None = None
    small: str | None = None
    large: str | None = None


class CoinLinks(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    homepage: list[str] | None = None
    whitepaper: str | None = None
    blockchain_site: list[str] | None = None
    official_forum_url: list[str] | None = None
    twitter_screen_name: str | None = None
    telegram_channel_identifier: str | None = None
    subreddit_url: str | None = None
    repos_url: dict[str, list[str]] | None = None


class CoinDetail(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    id: str
    symbol: str
    name: str
    web_slug: str | None = None
    asset_platform_id: str | None = None
    platforms: dict[str, str | None] | None = None
    block_time_in_minutes: int | None = None
    hashing_algorithm: str | None = None
    categories: list[str] | None = None
    preview_listing: bool | None = None
    market_cap_rank: int | None = None
    genesis_date: str | None = None
    country_origin: str | None = None
    sentiment_votes_up_percentage: float | None = None
    sentiment_votes_down_percentage: float | None = None
    watchlist_portfolio_users: int | None = None
    description: dict[str, str] | None = None
    links: CoinLinks | None = None
    image: CoinImage | None = None
    market_data: dict[str, Any] | None = None


class OhlcCandle(BaseModel):
    """OHLC candle. `timestamp` is milliseconds since epoch (CoinGecko convention)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    timestamp: int
    open: float
    high: float
    low: float
    close: float

    @property
    def dt(self) -> datetime:
        """Timestamp as a UTC datetime."""
        return datetime.fromtimestamp(self.timestamp / 1000, tz=UTC)

    @model_validator(mode="before")
    @classmethod
    def from_list(cls, data: Any) -> Any:
        # CoinGecko returns OHLC as [ts, o, h, l, c] arrays
        if isinstance(data, list) and len(data) == 5:  # pyright: ignore[reportUnknownArgumentType]
            return {  # pyright: ignore[reportUnknownVariableType]
                "timestamp": data[0],
                "open": data[1],
                "high": data[2],
                "low": data[3],
                "close": data[4],
            }
        return data  # pyright: ignore[reportUnknownVariableType]


# -- Helpers --


def _build_params(**kwargs: Any) -> dict[str, str]:
    """Build query params, dropping None values and converting types."""
    params: dict[str, str] = {}
    for k, v in kwargs.items():
        if v is not None:
            if isinstance(v, bool):
                params[k] = str(v).lower()
            else:
                params[k] = str(v)
    return params


# -- Client --


class CoinGeckoClient:
    """Async HTTP client for the CoinGecko public API.

    Args:
        rpm: Max requests per minute. None means no local rate limiting.
            CoinGecko demo API allows ~30 rpm.
    """

    def __init__(self, rpm: float | None = None) -> None:
        self._min_interval: float | None = 60.0 / rpm if rpm else None
        self._last_request: float = 0.0

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Make a GET request and return parsed JSON.

        Enforces the rpm interval between requests.
        On HTTP 429, reads the Retry-After header and sleeps before retrying.
        """
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        client = AsyncHTTPClient()
        while True:
            # Enforce local rate limit
            if self._min_interval is not None:
                elapsed = time.monotonic() - self._last_request
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)
            try:
                self._last_request = time.monotonic()
                response = await client.fetch(url)
                return json.loads(response.body)
            except HTTPClientError as exc:
                if exc.code != 429:
                    raise
                retry_after = 60.0
                if exc.response and exc.response.headers.get("Retry-After"):
                    try:
                        retry_after = float(exc.response.headers["Retry-After"])
                    except ValueError:
                        pass
                log.warning("Rate limited on %s, retrying after %.0fs", path, retry_after)
                await asyncio.sleep(retry_after)

    @require_cache
    async def fetch_coin_list(
        self,
        *,
        include_platform: bool | None = None,
    ) -> list[CoinListItem]:
        """Fetch the full list of coins from /coins/list.

        Args:
            include_platform: Include platform contract addresses.
                API default: false.
        """
        params = _build_params(include_platform=include_platform)
        data = await self._get("/coins/list", params or None)
        return [CoinListItem.model_validate(item) for item in data]

    @require_cache
    async def fetch_coins_markets(
        self,
        vs_currency: str = "usd",
        *,
        ids: str | None = None,
        names: str | None = None,
        symbols: str | None = None,
        include_tokens: str | None = None,
        category: str | None = None,
        order: str | None = None,
        per_page: int | None = None,
        page: int | None = None,
        sparkline: bool | None = None,
        price_change_percentage: str | None = None,
        locale: str | None = None,
        precision: str | None = None,
        include_rehypothecated: bool | None = None,
    ) -> list[CoinsMarketItem]:
        """Fetch coin market data from /coins/markets.

        Args:
            vs_currency: Target currency of market data. API default: usd.
            ids: Coin IDs, comma-separated.
            names: Coin names, comma-separated.
            symbols: Coin symbols, comma-separated.
            include_tokens: For symbol lookups: "top" or "all".
            category: Filter by category slug.
            order: Sort order. API default: market_cap_desc.
            per_page: Results per page (1-250). API default: 100.
            page: Page number. API default: 1.
            sparkline: Include 7-day sparkline. API default: false.
            price_change_percentage: Price change timeframes, comma-separated
                (1h, 24h, 7d, 14d, 30d, 200d, 1y).
            locale: Localization language. API default: en.
            precision: Decimal places for price ("full" or "0"-"18").
            include_rehypothecated: Include rehypothecated tokens. API default: false.
        """
        params = _build_params(
            vs_currency=vs_currency,
            ids=ids,
            names=names,
            symbols=symbols,
            include_tokens=include_tokens,
            category=category,
            order=order,
            per_page=per_page,
            page=page,
            sparkline=sparkline,
            price_change_percentage=price_change_percentage,
            locale=locale,
            precision=precision,
            include_rehypothecated=include_rehypothecated,
        )
        data = await self._get("/coins/markets", params or None)
        return [CoinsMarketItem.model_validate(item) for item in data]

    @require_cache
    async def fetch_coin(
        self,
        coin_id: str,
        *,
        localization: bool | None = None,
        tickers: bool | None = None,
        market_data: bool | None = None,
        community_data: bool | None = None,
        developer_data: bool | None = None,
        sparkline: bool | None = None,
        include_categories_details: bool | None = None,
        dex_pair_format: str | None = None,
    ) -> CoinDetail:
        """Fetch coin detail from /coins/{id}.

        Args:
            coin_id: CoinGecko coin ID.
            localization: Include localized languages. API default: true.
            tickers: Include tickers data. API default: true.
            market_data: Include market data. API default: true.
            community_data: Include community data. API default: true.
            developer_data: Include developer data. API default: true.
            sparkline: Include 7-day sparkline. API default: false.
            include_categories_details: Include category details. API default: false.
            dex_pair_format: DEX pair format: "contract_address" or "symbol".
                API default: contract_address.
        """
        params = _build_params(
            localization=localization,
            tickers=tickers,
            market_data=market_data,
            community_data=community_data,
            developer_data=developer_data,
            sparkline=sparkline,
            include_categories_details=include_categories_details,
            dex_pair_format=dex_pair_format,
        )
        data = await self._get(f"/coins/{coin_id}", params or None)
        return CoinDetail.model_validate(data)

    @require_cache
    async def fetch_coin_ohlc(
        self,
        coin_id: str,
        vs_currency: str = "usd",
        days: str = "30",
        *,
        precision: str | None = None,
    ) -> list[OhlcCandle]:
        """Fetch OHLC candle data from /coins/{id}/ohlc.

        Args:
            coin_id: CoinGecko coin ID.
            vs_currency: Target currency. API default: usd.
            days: Data up to N days ago (1, 7, 14, 30, 90, 180, 365).
            precision: Decimal places for price ("full" or "0"-"18").
        """
        params = _build_params(
            vs_currency=vs_currency,
            days=days,
            precision=precision,
        )
        data = await self._get(f"/coins/{coin_id}/ohlc", params or None)
        return [OhlcCandle.model_validate(item) for item in data]
