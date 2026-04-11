"""Tests for CoinGecko API adapter with mocked HTTP."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cointoss.sources.coingecko import CoinGeckoClient, RateLimiter


def test_rate_limiter_allows_within_budget():
    """A fresh RateLimiter should allow calls and increment the counter."""
    limiter = RateLimiter(daily_budget=10)
    assert limiter.can_call() is True
    limiter.record_call()
    assert limiter.calls_today == 1
    assert limiter.remaining == 9


def test_rate_limiter_blocks_at_budget():
    """A limiter that has exhausted its budget should block further calls."""
    limiter = RateLimiter(daily_budget=2)
    limiter.record_call()
    limiter.record_call()
    assert limiter.can_call() is False
    assert limiter.remaining == 0


def _mock_tornado_fetch(expected: object) -> AsyncMock:
    """Return an AsyncMock that mimics AsyncHTTPClient.fetch returning JSON body."""
    response = MagicMock()
    response.body = json.dumps(expected).encode()
    mock_client = MagicMock()
    mock_client.fetch = AsyncMock(return_value=response)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_coin_list():
    """fetch_coin_list should return the JSON list from /coins/list."""
    expected = [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]

    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=_mock_tornado_fetch(expected)
    ):
        client = CoinGeckoClient()
        result = await client.fetch_coin_list()

    assert result == expected


@pytest.mark.asyncio
async def test_fetch_coin_detail():
    """fetch_coin_detail should return the JSON dict from /coins/{id}."""
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
        result = await client.fetch_coin_detail("bitcoin")

    assert result == expected
