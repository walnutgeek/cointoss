"""Session Offset, ADR-0009 amendment of 2026-10-30, at the resolved-bars read.

A CoinGecko bar stamped D+1 is the 00:00 UTC price that closes D, and Yahoo's bar stamped D
closes D itself. Reads resolved through Price Sources key each bar by the session it closes;
reads of one source return bars under the date they are stored with.

Every case is a native coin, Price Sources Yahoo then CoinGecko, over March 2024. A close is
`10 * stamped day` from CoinGecko and `100 * stamped day` from Yahoo, so a close names the bar
it came from.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cointoss.datasets import DATASETS, PriceParams
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.prices import Bar
from cointoss.store import BarRecord, Store
from cointoss.universe import UniverseDefinition, UniverseParameters

CG, YH = Source.COINGECKO, Source.YAHOO


def day(n: int) -> date:
    return date(2024, 3, 1) + timedelta(days=n - 1)


@pytest.fixture
def store() -> Iterator[Store]:
    with TemporaryDirectory() as tmp, Store(Path(tmp) / "cointoss.db") as opened:
        yield opened


def coin(store: Store, symbol: str = "BTC") -> InstrumentId:
    outcome = InstrumentRegistry().observe(
        Observation(
            type=InstrumentType.CRYPTO,
            symbol=symbol,
            scope="native",
            observed_at=day(1),
            source=CG,
            references=(ExternalReference.provider_id("coingecko", symbol.lower()),),
            figi_resolution=FigiResolution.NOT_FOUND,
        )
    )
    assert outcome.instrument is not None
    store.save_instrument(outcome.instrument)
    return outcome.instrument.id


def stamped(instrument: InstrumentId, source: Source, *days: int) -> list[Bar]:
    scale = 10.0 if source is CG else 100.0
    return [
        Bar(
            instrument_id=instrument,
            source=source,
            bar_date=day(n),
            close=scale * n,
            fetched_at=datetime.combine(day(n + 1), datetime.min.time(), UTC),
        )
        for n in days
    ]


def series(bars: Sequence[Bar | BarRecord]) -> list[tuple[date, Source, float]]:
    return [(b.bar_date, b.source, b.close) for b in bars]


def test_coingecko_then_yahoo_splices_with_no_date_doubled_or_skipped(store: Store):
    """CoinGecko covers stamped days 2-7, closing sessions 1-6; Yahoo lists from session 4."""
    btc = coin(store)
    store.upsert_bars([*stamped(btc, CG, *range(2, 8)), *stamped(btc, YH, 4, 5, 6)])

    expected = [
        (day(1), CG, 20.0),
        (day(2), CG, 30.0),
        (day(3), CG, 40.0),
        (day(4), YH, 400.0),
        (day(5), YH, 500.0),
        (day(6), YH, 600.0),
    ]
    assert series(store.bars_for(btc, day(1), day(6))) == expected
    assert series(store.resolved_bars(day(1), day(6), instruments=[btc])) == expected


def test_yahoo_then_coingecko_splices_with_no_date_doubled_or_skipped(store: Store):
    """Yahoo stops after session 3; CoinGecko's bar stamped 4 also closes 3 and is not shown."""
    btc = coin(store)
    store.upsert_bars([*stamped(btc, YH, 1, 2, 3), *stamped(btc, CG, *range(4, 8))])

    expected = [
        (day(1), YH, 100.0),
        (day(2), YH, 200.0),
        (day(3), YH, 300.0),
        (day(4), CG, 50.0),
        (day(5), CG, 60.0),
        (day(6), CG, 70.0),
    ]
    assert series(store.bars_for(btc, day(1), day(6))) == expected
    assert series(store.resolved_bars(day(1), day(6), instruments=[btc])) == expected


def test_a_coingecko_only_series_resolves_a_day_back_and_reads_as_stored_by_source(
    store: Store,
):
    btc = coin(store)
    stored = stamped(btc, CG, *range(1, 6))
    store.upsert_bars(stored)

    resolved = store.bars_for(btc, day(0), day(5))
    assert series(resolved) == [(b.bar_date - timedelta(days=1), CG, b.close) for b in stored]
    assert store.bars_for(btc, day(1), day(5), source=CG) == stored

    bulk = store.resolved_bars(day(0), day(5), instruments=[btc])
    assert [(b.bar_date, b.close) for b in bulk] == [(b.bar_date, b.close) for b in resolved]
    by_source = store.resolved_bars(day(1), day(5), instruments=[btc], source=CG)
    assert [(b.bar_date, b.close) for b in by_source] == [(b.bar_date, b.close) for b in stored]


def test_member_bars_read_the_bar_that_closes_the_date(store: Store):
    btc = coin(store)
    store.upsert_bars(stamped(btc, CG, 2, 3))
    store.save_universe(UniverseDefinition.create("top", UniverseParameters(), day(1)))
    store.record_membership("top", day(1), [btc])

    held = store.member_bars("top", day(2))[btc]
    assert held is not None and held.close == 30.0


def test_returns_across_a_splice_match_the_aligned_closes(store: Store):
    """Aligned closes, sessions 1-6: CoinGecko 20, 30, 40 then Yahoo 400, 500, 600.

    Session 4 switches source, so it has no return (ADR-0010); the rest are close to close
    over consecutive sessions.
    """
    btc = coin(store)
    store.upsert_bars([*stamped(btc, CG, *range(2, 8)), *stamped(btc, YH, 4, 5, 6)])

    rows = DATASETS["returns"].read(store, PriceParams(start=day(2), end=day(6))).data
    assert [(r[0], r[3], r[4], r[5], r[6]) for r in rows] == [
        (day(2), "coingecko", day(1), 1, 30.0),
        (day(3), "coingecko", day(2), 1, 40.0),
        (day(5), "yahoo", day(4), 1, 500.0),
        (day(6), "yahoo", day(5), 1, 600.0),
    ]
    assert [r[7] for r in rows] == pytest.approx(
        [30.0 / 20.0 - 1, 40.0 / 30.0 - 1, 500.0 / 400.0 - 1, 600.0 / 500.0 - 1]
    )

    named = DATASETS["returns"].read(store, PriceParams(start=day(3), end=day(7), source=CG)).data
    assert [(r[0], r[4], r[6]) for r in named] == [
        (day(n), day(n - 1), 10.0 * n) for n in range(3, 8)
    ]
