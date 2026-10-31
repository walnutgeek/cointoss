"""History backfill (#26), through `cointoss.backfill.backfill`.

CoinGecko is answered by patching `AsyncHTTPClient` in `cointoss.sources.coingecko`, as
`tests/test_sweep.py` does, with a synthetic 365-day `market_chart`: one point at each 00:00
UTC from `TODAY - 365` to `TODAY`, plus the trailing point the API appends at the fetch time.
Yahoo is answered by patching `get_prices` where `cointoss.backfill` calls it, with daily frames
back to 2025 that include today's still-running session. Nothing touches the network.

Prices are synthetic and say which source and date they belong to. CoinGecko's bar stamped D+1
is `cg(coin, D+1)`, and Yahoo's close for session D is that price within 0.3%, so the major
coins agree with their Yahoo pairs. `BEAM-USD` is a different, older asset than CoinGecko's
`beam-2` (a real collision) and is quoted at seven times its price.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest

from cointoss import FrameData
from cointoss.backfill import BackfillReport, backfill
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentType,
    Observation,
    Source,
    TickerRecord,
)
from cointoss.prices import Bar
from cointoss.sources.coingecko import CoinGeckoClient
from cointoss.store import Store
from cointoss.universe import UniverseDefinition, UniverseParameters

CG, YH = Source.COINGECKO, Source.YAHOO
TODAY = date(2026, 10, 30)
AT = datetime.combine(TODAY, time(1, 0), UTC)
WINDOW_START = TODAY - timedelta(days=365)
YAHOO_START = date(2025, 1, 1)

# CoinGecko id -> symbol and price level.
COINS: dict[str, tuple[str, float]] = {
    "bitcoin": ("btc", 60000.0),
    "ethereum": ("eth", 3000.0),
    "solana": ("sol", 150.0),
    "beam-2": ("beam", 0.006),
}
BTC = InstrumentId("crypto.native.btc")
ETH = InstrumentId("crypto.native.eth")
SOL = InstrumentId("crypto.native.sol")
BEAM = InstrumentId("crypto.native.beam")
MAJORS = (BTC, ETH, SOL)


def cg(coin_id: str, stamped: date) -> float:
    """CoinGecko's 00:00 UTC price of `coin_id` on `stamped`."""
    return COINS[coin_id][1] * (1 + 0.2 * math.sin(stamped.toordinal() / 9))


def midnight_ms(day: date) -> int:
    return int(datetime.combine(day, time(), UTC).timestamp() * 1000)


def market_chart(coin_id: str) -> dict[str, Any]:
    days = [WINDOW_START + timedelta(days=n) for n in range((TODAY - WINDOW_START).days + 1)]
    prices = [[midnight_ms(d), cg(coin_id, d)] for d in days]
    prices.append([int(AT.timestamp() * 1000), cg(coin_id, TODAY) * 1.01])
    return {
        "prices": prices,
        "market_caps": [[t, p * 1e7] for t, p in prices],
        "total_volumes": [[t, p * 1e3] for t, p in prices],
    }


def coingecko_http() -> MagicMock:
    """An `AsyncHTTPClient` answering `/coins/{id}/market_chart` for the coins in `COINS`."""

    async def fetch(url: str) -> MagicMock:
        coin_id = urlparse(url).path.split("/")[-2]
        response = MagicMock()
        response.body = json.dumps(market_chart(coin_id)).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    return client


def yahoo_frame(close: dict[date, float]) -> FrameData:
    columns = ["date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    columns += ["Dividends", "Stock Splits"]
    return FrameData(
        columns=columns,
        data=[
            [d.isoformat(), c * 0.99, c * 1.02, c * 0.97, c, c, c * 1e4, 0.0, 0.0]
            for d, c in sorted(close.items())
        ],
    )


def yahoo_pair(coin_id: str, factor: float) -> FrameData:
    days = (TODAY - YAHOO_START).days + 1
    sessions = [YAHOO_START + timedelta(days=n) for n in range(days)]
    return yahoo_frame(
        {
            d: cg(coin_id, d + timedelta(days=1)) * factor * (1.003 if d.day % 2 else 0.997)
            for d in sessions
        }
    )


YAHOO: dict[str, FrameData] = {
    "BTC-USD": yahoo_pair("bitcoin", 1.0),
    "ETH-USD": yahoo_pair("ethereum", 1.0),
    "SOL-USD": yahoo_pair("solana", 1.0),
    "BEAM-USD": yahoo_pair("beam-2", 7.0),
}


class FakeYahoo:
    """`get_prices` over `YAHOO`, recording each ticker asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, ticker: str, period: str = "max") -> FrameData:
        assert period == "max"
        self.asked.append(ticker)
        if ticker not in YAHOO:
            raise ValueError(f"{ticker}: no data found, symbol may be delisted")
        return YAHOO[ticker]


@pytest.fixture
def store() -> Iterator[Store]:
    with TemporaryDirectory() as tmp, Store(Path(tmp) / "cointoss.db") as opened:
        yield opened


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cointoss.backfill.YAHOO_INTERVAL", 0.0)


def mint(store: Store, coin_id: str, symbol: str | None = None) -> InstrumentId:
    registry = store.load_registry()
    result = registry.observe(
        Observation(
            type=InstrumentType.CRYPTO,
            symbol=symbol or COINS[coin_id][0],
            scope="native",
            observed_at=date(2026, 10, 1),
            source=CG,
            references=(ExternalReference.provider_id("coingecko", coin_id),),
            figi_resolution=FigiResolution.NOT_ATTEMPTED,
        )
    )
    assert result.instrument is not None
    store.save_instruments(registry.instruments())
    return result.instrument.id


def seed(store: Store, coin_ids: tuple[str, ...] | None = None) -> list[InstrumentId]:
    """The coins, all of `COINS` by default, as members of one universe since 2026-10-01."""
    ids = [mint(store, coin_id) for coin_id in coin_ids or tuple(COINS)]
    store.save_universe(
        UniverseDefinition.create(
            "cg-top-100", UniverseParameters(enter_rank=100, exit_rank=120), date(2026, 1, 1)
        )
    )
    store.record_membership("cg-top-100", date(2026, 10, 1), ids, run_at=AT)
    return ids


def swept(instrument_id: InstrumentId, coin_id: str, stamped: date) -> Bar:
    """The bar a sweep stored, at a price slightly off the chart's 00:00 point."""
    return Bar(
        instrument_id=instrument_id,
        source=CG,
        bar_date=stamped,
        close=cg(coin_id, stamped) * 1.001,
        market_cap=1.0,
        fetched_at=datetime.combine(stamped, time(0, 5), UTC),
    )


@contextmanager
def sources() -> Iterator[FakeYahoo]:
    yahoo = FakeYahoo()
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=coingecko_http()),
        patch("cointoss.backfill.get_prices", side_effect=yahoo),
    ):
        yield yahoo


async def run(store: Store, at: datetime = AT, **kwargs: Any) -> BackfillReport:
    return await backfill(store, at, coingecko=CoinGeckoClient(), **kwargs)


def stamped(store: Store, instrument_id: InstrumentId, source: Source) -> list[date]:
    return [b.bar_date for b in store.bars_for(instrument_id, date.min, date.max, source=source)]


async def test_fills_every_missing_coingecko_date_and_leaves_stored_bars_unchanged(store: Store):
    btc, *_ = seed(store)
    kept = [swept(btc, "bitcoin", TODAY - timedelta(days=n)) for n in (0, 1, 2, 40)]
    store.upsert_bars(kept)

    with sources():
        report = await run(store)

    window = [WINDOW_START + timedelta(days=n) for n in range(366)]
    for instrument_id in (btc, ETH, SOL, BEAM):
        assert stamped(store, instrument_id, CG) == window
    by_date = {b.bar_date: b for b in store.bars_for(btc, date.min, date.max, source=CG)}
    for bar in kept:
        assert by_date[bar.bar_date] == bar
    assert store.restatements_for(btc) == []
    assert report.coingecko_bars == 4 * 366 - len(kept)
    # Shaped as the sweep writes: the 00:00 close, its volume and market cap, final.
    first = by_date[WINDOW_START]
    assert first.close == cg("bitcoin", WINDOW_START)
    assert first.volume is not None and first.market_cap is not None
    assert first.open is None and first.adj_close is None
    assert first.fetched_at == AT


async def test_major_coins_pair_with_yahoo_and_get_history_past_365_days(store: Store):
    seed(store)
    with sources():
        report = await run(store)

    assert sorted(report.paired) == sorted(MAJORS)
    for instrument_id in MAJORS:
        instrument = store.load_instrument(instrument_id)
        assert instrument is not None
        ticker = f"{instrument.symbol}-USD"
        assert ExternalReference.provider_id("yahoo", ticker) in instrument.references
        assert instrument.ticker_history_from(YH) == [TickerRecord(YH, ticker, "native", TODAY)]
        assert instrument.price_sources == (YH, CG)
        dates = stamped(store, instrument_id, YH)
        assert dates[0] == YAHOO_START < WINDOW_START
        assert len(dates) == (TODAY - YAHOO_START).days
    # Resolved reads now prefer Yahoo, and fall back to CoinGecko nowhere in Yahoo's span.
    resolved = store.bars_for(BTC, WINDOW_START, TODAY - timedelta(days=1))
    assert {b.source for b in resolved} == {YH}


async def test_a_yahoo_pair_that_is_a_different_asset_is_rejected_and_logged(
    store: Store, caplog: pytest.LogCaptureFixture
):
    seed(store)
    with sources(), caplog.at_level(logging.WARNING, logger="cointoss.backfill"):
        report = await run(store)

    assert BEAM in report.rejected
    assert "BEAM-USD" in report.rejected[BEAM]
    assert any("BEAM-USD" in r.getMessage() for r in caplog.records)
    beam = store.load_instrument(BEAM)
    assert beam is not None
    assert all(r.qualifier != "yahoo" for r in beam.references)
    assert beam.ticker_history_from(YH) == []
    assert stamped(store, BEAM, YH) == []


async def test_no_yahoo_bar_is_written_for_todays_utc_date(store: Store):
    seed(store)
    with sources():
        await run(store)
    for instrument_id in MAJORS:
        assert stamped(store, instrument_id, YH)[-1] == TODAY - timedelta(days=1)


async def test_a_second_run_writes_nothing_and_does_not_pair_again(store: Store):
    seed(store)
    with sources():
        await run(store)
    held = {i: (stamped(store, i, CG), stamped(store, i, YH)) for i in (*MAJORS, BEAM)}

    with sources() as yahoo:
        again = await run(store, AT + timedelta(hours=2))

    assert again.coingecko_bars == again.yahoo_bars == 0
    assert again.paired == []
    assert set(again.rejected) == {BEAM}
    # One request each: the accepted pairs' history, and the rejected pair tried again.
    assert sorted(yahoo.asked) == ["BEAM-USD", "BTC-USD", "ETH-USD", "SOL-USD"]
    assert {i: (stamped(store, i, CG), stamped(store, i, YH)) for i in held} == held
    for instrument_id in MAJORS:
        instrument = store.load_instrument(instrument_id)
        assert instrument is not None
        assert len(instrument.ticker_history_from(YH)) == 1


async def test_a_yahoo_pair_another_instrument_holds_is_not_paired_twice(store: Store):
    # A second coin under the same symbol and, here, the same prices: pairing it too would
    # make the shared reference a Supersession of two different coins.
    COINS["bitcoin-2"] = ("btc", 60000.0)
    try:
        _, twin = seed(store, ("bitcoin", "bitcoin-2"))
        with sources():
            report = await run(store)
    finally:
        del COINS["bitcoin-2"]

    assert report.paired == [BTC]
    assert "BTC-USD" in report.rejected[twin]
    survivor = store.load_instrument(twin)
    assert survivor is not None and survivor.superseded_by is None
    assert stamped(store, twin, YH) == []


async def test_a_coin_with_a_malformed_bar_fails_alone(store: Store):
    """A negative close from CoinGecko fails that coin's run; the coins after it still fill."""
    btc, *_ = seed(store)
    with sources(), patch.dict(COINS, {"bitcoin": ("btc", -60000.0)}):
        report = await run(store)

    assert "is negative" in report.failed[btc]
    assert stamped(store, btc, CG) == []
    window = [WINDOW_START + timedelta(days=n) for n in range(366)]
    for instrument_id in (ETH, SOL, BEAM):
        assert stamped(store, instrument_id, CG) == window


async def test_a_window_shorter_than_90_days_is_refused(store: Store):
    seed(store)
    with sources(), pytest.raises(ValueError, match="90"):
        await run(store, days=30)
