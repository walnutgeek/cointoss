"""Tests for CoinGecko API adapter with mocked HTTP."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cointoss.sources.coingecko import (
    CoinDetail,
    CoinGeckoClient,
    CoinListItem,
    CoinsMarketItem,
    OhlcCandle,
)


def _mock_tornado_fetch(expected: object) -> AsyncMock:
    """Return an AsyncMock that mimics AsyncHTTPClient.fetch returning JSON body."""
    response = MagicMock()
    response.body = json.dumps(expected).encode()
    mock_client = MagicMock()
    mock_client.fetch = AsyncMock(return_value=response)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_coin_list():
    """fetch_coin_list should return CoinListItem instances."""
    expected = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=_mock_tornado_fetch(expected)
    ):
        client = CoinGeckoClient()
        result = await client.fetch_coin_list()
    assert len(result) == 1
    assert isinstance(result[0], CoinListItem)
    assert result[0].id == "bitcoin"
    assert result[0].symbol == "btc"


@pytest.mark.asyncio
async def test_fetch_coin():
    """fetch_coin should return a CoinDetail instance."""
    expected = {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "market_data": {"current_price": {"usd": 50000}},
    }
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=_mock_tornado_fetch(expected)
    ):
        client = CoinGeckoClient()
        result = await client.fetch_coin("bitcoin")
    assert isinstance(result, CoinDetail)
    assert result.id == "bitcoin"
    assert result.market_data is not None
    assert result.market_data["current_price"]["usd"] == 50000


@pytest.mark.asyncio
async def test_fetch_coins_markets():
    """fetch_coins_markets should return CoinsMarketItem instances."""
    expected = [
        {
            "id": "bitcoin",
            "symbol": "btc",
            "name": "Bitcoin",
            "current_price": 50000,
            "market_cap": 1000000000,
            "market_cap_rank": 1,
            "total_volume": 30000000,
        }
    ]
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=_mock_tornado_fetch(expected)
    ):
        client = CoinGeckoClient()
        result = await client.fetch_coins_markets()
    assert len(result) == 1
    assert isinstance(result[0], CoinsMarketItem)
    assert result[0].current_price == 50000
    assert result[0].market_cap_rank == 1


@pytest.mark.asyncio
async def test_fetch_coin_ohlc():
    """fetch_coin_ohlc should parse array items into OhlcCandle instances."""
    expected = [
        [1709395200000, 61942, 62211, 61721, 61845],
        [1709409600000, 61828, 62139, 61726, 62139],
    ]
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=_mock_tornado_fetch(expected)
    ):
        client = CoinGeckoClient()
        result = await client.fetch_coin_ohlc("bitcoin")
    assert len(result) == 2
    assert isinstance(result[0], OhlcCandle)
    assert result[0].timestamp == 1709395200000
    assert result[0].open == 61942
    assert result[0].close == 61845


def test_build_params_drops_none():
    """_build_params should exclude None values and convert bools."""
    from cointoss.sources.coingecko import _build_params  # pyright: ignore[reportPrivateUsage]

    params = _build_params(a="hello", b=None, c=True, d=False, e=42)
    assert params == {"a": "hello", "c": "true", "d": "false", "e": "42"}
