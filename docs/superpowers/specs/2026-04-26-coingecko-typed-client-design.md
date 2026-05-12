# CoinGecko Typed Client Design

Typed Pydantic response models and refactored method signatures for the CoinGecko API client, replacing untyped dict returns and hardcoded defaults.

## Scope

Four CoinGecko API operations:

| operationId | Path | Method |
|---|---|---|
| `coins-list` | `/coins/list` | `fetch_coin_list` |
| `coins-markets` | `/coins/markets` | `fetch_coins_markets` |
| `coins-id` | `/coins/{id}` | `fetch_coin` |
| `coins-id-ohlc` | `/coins/{id}/ohlc` | `fetch_coin_ohlc` |

API spec reference: `/Users/sergeyk/w/coingecko-api-oas/coingecko-demo.json`

## Response Models

All models live in `src/cointoss/sources/coingecko.py` alongside `CoinGeckoClient`. All use `pydantic.BaseModel` (not `DbModel`) with `model_config = ConfigDict(extra="ignore")`.

### CoinListItem

For `/coins/list` response items.

```
id: str
symbol: str
name: str
platforms: dict[str, str | None] | None = None
```

### CoinRoi

Nested model for ROI data in market responses.

```
times: float
currency: str
percentage: float
```

### CoinsMarketItem

For `/coins/markets` response items. All numeric fields nullable.

```
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
```

### CoinImage

Nested model for coin image URLs.

```
thumb: str | None = None
small: str | None = None
large: str | None = None
```

### CoinLinks

Nested model for coin links. Selectively modeled — common fields only.

```
homepage: list[str] | None = None
whitepaper: list[str] | None = None
blockchain_site: list[str] | None = None
official_forum_url: list[str] | None = None
twitter_screen_name: str | None = None
telegram_channel_identifier: str | None = None
subreddit_url: str | None = None
repos_url: dict[str, list[str]] | None = None
```

### CoinDetail

For `/coins/{id}` response. Selective modeling — commonly used fields typed, rest available via raw dict or re-fetch.

```
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
```

`market_data` stays as `dict[str, Any]` because it contains prices in every supported currency (deeply nested, large). Callers access it as `detail.market_data["current_price"]["usd"]`.

### OhlcCandle

For `/coins/{id}/ohlc` response items. The API returns arrays `[timestamp, open, high, low, close]` — a `model_validator(mode="before")` converts from list to object.

```
timestamp: int
open: float
high: float
low: float
close: float
```

## Method Signatures

### Parameter handling convention

- All API parameters become method parameters.
- Required API params are positional with defaults where sensible (e.g., `vs_currency="usd"`).
- Optional API params default to `None` and are keyword-only (after `*`).
- When a param is `None`, it is excluded from the query string entirely, letting the API use its own default.
- API default values are documented in the method docstring.
- A private helper `_build_params(**kwargs)` drops `None` values, converts bools to lowercase strings, and converts everything else to `str`.

### fetch_coin_list

```python
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
```

### fetch_coins_markets

```python
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
```

### fetch_coin

```python
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
```

### fetch_coin_ohlc

```python
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
```

## Removals

### Files deleted

- `src/cointoss/pipeline.py`
- `tests/test_pipeline.py`

### Removed from `coingecko.py`

- `RateLimiter` class (and its `dataclass`, `time` imports)
- `ENDPOINTS_URL_TO_ID` dict
- `DOCS_URL` constant
- `fetch_coin_detail` method (replaced by `fetch_coin`)
- `fetch_price_history` method (unused)
- `fetch_top_coins` method (replaced by `fetch_coins_markets`)
- `fetch_coin_list` old implementation (replaced by typed version)

### Removed from `test_coingecko.py`

- `RateLimiter` import
- `test_rate_limiter_allows_within_budget`
- `test_rate_limiter_blocks_at_budget`

## Caller Updates

### `scheduler.py`

- Remove `from cointoss.pipeline import ...`
- Remove `RateLimiter` import and all usage
- `CoinGeckoClient()` constructor no longer takes `rate_limiter`
- `fetch_top_coins_task`: call `self.client.fetch_coins_markets(per_page=50)` directly, upsert `Coin` and `CoinPrice` records inline
- `fetch_details_task`: call `self.client.fetch_coin(coin_id)` directly, upsert `Coin` records inline
- Remove `rate_limiter` field from `FetchScheduler`

### `cli.py`

- Remove `from cointoss.pipeline import run_fetch_top_coins`
- `fetch` command: call `client.fetch_coins_markets()` directly, upsert coins/prices inline, same logic as current `run_fetch_top_coins` but using typed model attributes

### `test_coingecko.py`

- Update `test_fetch_coin_list` to assert on `CoinListItem` instances
- Update `test_fetch_coin_detail` to test `fetch_coin` returning `CoinDetail`
- Add tests for `fetch_coins_markets` and `fetch_coin_ohlc`

## Not in scope

- `fetch_price_history` / `coins-id-market-chart` — removed, not replaced
- Onchain/DEX API endpoints (those are in `onchain-demo.json`)
- `RateLimiter` replacement — removed without replacement
