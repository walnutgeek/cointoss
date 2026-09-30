"""Bars accumulate, ADR-0009 through `cointoss.store`.

Driven by the same recorded fixtures `test_prices.py` replays -- NVIDIA around its 2024 split,
and a trimmed Bitcoin `market_chart` tail -- mapped through `cointoss.prices` and written with
`upsert_bars`. The tests read back through `bars_for`, `member_bars`, `corporate_actions_for`
and `restatements_for`, and look at rows only where the guarantee is about storage itself: that
an overlapping re-fetch leaves no duplicate.

The re-fetch tests replay what a daily CoinGecko job actually sees: the fetch on one day ends
with a point at the current time, and the next day's fetch has dropped it. A CoinGecko bar is
the day's 00:00 point (#18), so the trailing point never reaches a bar and the re-fetch
restates nothing. A Yahoo bar fetched during its own session is provisional instead, and its
replacement is the day finishing, not a Restatement.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cointoss import FrameData
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    Instrument,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.prices import (
    Bar,
    CorporateActionKind,
    bars_from_market_chart,
    bars_from_yahoo,
)
from cointoss.series import NotYetStarted
from cointoss.sources.coingecko import MarketChart, MarketChartPoint
from cointoss.store import (
    TABLES,
    BarRow,
    CorporateActionRow,
    Persistence,
    RestatementRow,
    Store,
    UnknownInstrument,
)
from cointoss.universe import UniverseDefinition, UniverseParameters

DATA = Path(__file__).parent / "data"
SPLIT_DATE = date(2024, 6, 10)
BEFORE_SPLIT = datetime(2024, 6, 8, 12, 0, tzinfo=UTC)
AFTER_SPLIT = datetime(2024, 7, 1, 12, 0, tzinfo=UTC)
# The recorded market chart's trailing point, which is when that fetch happened.
CG_FETCHED = datetime(2026, 9, 29, 8, 0, 40, tzinfo=UTC)
CG_NEXT_DAY = CG_FETCHED + timedelta(days=1)


@pytest.fixture
def db_path():
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "cointoss.db"


def stock(store: Store, symbol: str = "NVDA") -> InstrumentId:
    registry = InstrumentRegistry()
    outcome = registry.observe(
        Observation(
            type=InstrumentType.STOCK,
            symbol=symbol,
            scope="us",
            observed_at=date(1999, 1, 22),
            source=Source.YAHOO,
        )
    )
    assert outcome.instrument is not None
    store.save_instrument(outcome.instrument)
    return outcome.instrument.id


def coins(store: Store, *symbols: str) -> list[InstrumentId]:
    """Native coins, whose Price Sources prefer Yahoo and fall back to CoinGecko."""
    registry = InstrumentRegistry()
    ids: list[InstrumentId] = []
    for symbol in symbols:
        outcome = registry.observe(
            Observation(
                type=InstrumentType.CRYPTO,
                symbol=symbol,
                scope="native",
                observed_at=date(2013, 4, 28),
                source=Source.COINGECKO,
                references=(ExternalReference.provider_id("coingecko", symbol.lower()),),
                figi_resolution=FigiResolution.NOT_FOUND,
            )
        )
        instrument: Instrument | None = outcome.instrument
        assert instrument is not None
        assert instrument.price_sources == (Source.YAHOO, Source.COINGECKO)
        store.save_instrument(instrument)
        ids.append(instrument.id)
    return ids


def nvda_frame() -> FrameData:
    return FrameData.model_validate(json.loads((DATA / "yahoo_nvda_2024_split.json").read_text()))


def market_chart() -> MarketChart:
    return MarketChart.model_validate(
        json.loads((DATA / "coingecko_bitcoin_market_chart_365d.json").read_text())
    )


def next_day_chart(payload: MarketChart, close: float, volume: float) -> MarketChart:
    """What the same fetch returns a day later: the trailing point gone, a new day begun.

    Yesterday keeps only its 00:00 point, and today gets its own 00:00 point plus a new
    trailing one, so the last bar is again partial.
    """
    now_ms = int(CG_NEXT_DAY.timestamp() * 1000)
    midnight_ms = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp() * 1000)

    def roll(series: list[MarketChartPoint], today: float) -> list[MarketChartPoint]:
        return [
            *series[:-1],
            MarketChartPoint(timestamp=midnight_ms, value=today),
            MarketChartPoint(timestamp=now_ms, value=today * 1.01),
        ]

    return payload.model_copy(
        update={
            "prices": roll(payload.prices, close),
            "total_volumes": roll(payload.total_volumes, volume),
        }
    )


def bar(
    instrument_id: InstrumentId,
    source: Source,
    bar_date: date,
    close: float,
    fetched_at: datetime | None = None,
) -> Bar:
    return Bar(
        instrument_id=instrument_id,
        source=source,
        bar_date=bar_date,
        close=close,
        fetched_at=fetched_at
        or datetime.combine(bar_date + timedelta(days=1), datetime.min.time(), UTC),
    )


def pre_split_vintage(b: Bar) -> Bar:
    """What Yahoo reported for a session before the split rescaled its history."""

    def times10(v: float | None) -> float | None:
        return None if v is None else v * 10.0

    return b.model_copy(
        update={
            "open": times10(b.open),
            "high": times10(b.high),
            "low": times10(b.low),
            "close": b.close * 10.0,
            "adj_close": times10(b.adj_close),
            "volume": None if b.volume is None else b.volume / 10.0,
            "fetched_at": BEFORE_SPLIT,
        }
    )


def test_bar_tables_are_rebuildable():
    """ADR-0008: bars, actions and restatements are re-fetchable, so need no migration."""
    for table in (BarRow, CorporateActionRow, RestatementRow):
        assert TABLES[table] is Persistence.REBUILDABLE


def test_bars_round_trip_with_close_and_adjusted_close(db_path: Path):
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    with Store(db_path) as store:
        nvda = stock(store)
        assert store.upsert_bars(bars, actions) == []
    with Store(db_path) as store:
        read = store.bars_for(nvda, date(2024, 6, 1), date(2024, 6, 30))

    assert read == bars
    # Both conventions survive: price return and total return stay separately computable.
    assert all(b.adj_close is not None and b.adj_close != b.close for b in read)


def test_range_is_inclusive_at_both_ends(db_path: Path):
    bars, _ = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars(bars)
        read = store.bars_for(nvda, date(2024, 6, 6), date(2024, 6, 10))

    assert [b.bar_date for b in read] == [date(2024, 6, 6), date(2024, 6, 7), SPLIT_DATE]


def test_overlapping_refetch_is_idempotent(db_path: Path):
    """A backfill overlapping what is held duplicates nothing and loses nothing."""
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    later = [b.model_copy(update={"fetched_at": AFTER_SPLIT + timedelta(days=1)}) for b in bars]
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars(bars[:4], actions)
        assert store.upsert_bars(later[2:], actions) == []
        assert store.upsert_bars(later[2:], actions) == []

        assert BarRow.select_count(store.conn) == len(bars)
        assert CorporateActionRow.select_count(store.conn) == len(actions)
        read = store.bars_for(nvda, date(2024, 6, 1), date(2024, 6, 30))

    assert read == bars[:2] + later[2:]


def test_two_sources_disagreeing_each_keep_their_bar(db_path: Path):
    day = date(2024, 3, 1)
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.upsert_bars(
            [bar(btc, Source.YAHOO, day, 62000.0), bar(btc, Source.COINGECKO, day, 61500.0)]
        )

        yahoo = store.bars_for(btc, day, day, source=Source.YAHOO)
        coingecko = store.bars_for(btc, day, day, source=Source.COINGECKO)

    assert [b.close for b in yahoo] == [62000.0]
    assert [b.close for b in coingecko] == [61500.0]


def test_read_falls_back_per_date_through_price_sources(db_path: Path):
    """Yahoo lists the coin only from D3; CoinGecko supplies the earlier stretch."""
    d1, d2, d3, d4 = (date(2024, 3, n) for n in (1, 2, 3, 4))
    with Store(db_path) as store:
        (coin,) = coins(store, "NEW")
        store.upsert_bars(
            [
                *(bar(coin, Source.COINGECKO, d, 1.0 + i) for i, d in enumerate((d1, d2, d3, d4))),
                *(bar(coin, Source.YAHOO, d, 10.0 + i) for i, d in enumerate((d3, d4))),
            ]
        )
        read = store.bars_for(coin, d1, d4)

    assert [(b.bar_date, b.source, b.close) for b in read] == [
        (d1, Source.COINGECKO, 1.0),
        (d2, Source.COINGECKO, 2.0),
        (d3, Source.YAHOO, 10.0),
        (d4, Source.YAHOO, 11.0),
    ]


def test_a_source_outside_price_sources_is_not_read(db_path: Path):
    """A stock prices from Yahoo only; a stray CoinGecko bar is kept but never chosen."""
    day = date(2024, 3, 1)
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars([bar(nvda, Source.COINGECKO, day, 1.0)])

        assert store.bars_for(nvda, day, day) == []
        assert len(store.bars_for(nvda, day, day, source=Source.COINGECKO)) == 1


def test_corporate_actions_are_readable_over_a_range(db_path: Path):
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars(bars, actions)
    with Store(db_path) as store:
        held = store.corporate_actions_for(nvda, date(2024, 6, 1), date(2024, 6, 30))
        split_only = store.corporate_actions_for(nvda, date(2024, 6, 1), SPLIT_DATE)

    assert held == actions
    assert [(a.action_date, a.kind) for a in split_only] == [
        (SPLIT_DATE, CorporateActionKind.SPLIT)
    ]


def test_unexplained_change_writes_a_restatement_and_overwrites(db_path: Path):
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    target = bars[-1]
    corrected = target.model_copy(
        update={"close": target.close * 1.05, "fetched_at": AFTER_SPLIT + timedelta(days=3)}
    )
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars(bars, actions)
        written = store.upsert_bars([corrected])
    with Store(db_path) as store:
        held = store.restatements_for(nvda)
        (current,) = store.bars_for(nvda, target.bar_date, target.bar_date)

    assert [(r.bar_date, r.field, r.old, r.new) for r in written] == [
        (target.bar_date, "close", target.close, corrected.close)
    ]
    assert held == written
    assert held[0].detected_at == corrected.fetched_at
    assert current == corrected


def test_split_rescale_writes_no_restatement(db_path: Path):
    """A re-fetch across a split rewrites every earlier bar tenfold; none of it is filed."""
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    before = [pre_split_vintage(b) for b in bars if b.bar_date < SPLIT_DATE]
    with Store(db_path) as store:
        nvda = stock(store)
        store.upsert_bars(before)
        written = store.upsert_bars(bars, actions)

        assert written == []
        assert store.restatements_for(nvda) == []
        assert store.bars_for(nvda, date(2024, 6, 1), date(2024, 6, 30)) == bars


def test_split_already_on_file_explains_a_later_rescale(db_path: Path):
    """The explanation need not arrive in the same call: stored actions count too."""
    bars, actions = bars_from_yahoo(InstrumentId("stock.us.nvda"), nvda_frame(), AFTER_SPLIT)
    before = [pre_split_vintage(b) for b in bars if b.bar_date < SPLIT_DATE]
    with Store(db_path) as store:
        stock(store)
        store.upsert_bars(before, actions)

        assert store.upsert_bars(bars) == []


def test_a_daily_market_chart_refetch_is_not_a_restatement(db_path: Path):
    """The trailing "now" point is gone next day; the 00:00 point it sat beside was the bar."""
    first = market_chart()
    second = next_day_chart(first, close=83900.0, volume=4.0e10)
    btc_id = InstrumentId("crypto.native.btc")
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        assert btc == btc_id
        store.upsert_bars(bars_from_market_chart(btc, first, CG_FETCHED))
        written = store.upsert_bars(bars_from_market_chart(btc, second, CG_NEXT_DAY))
        read = store.bars_for(btc, date(2026, 9, 23), date(2026, 9, 30))

    assert written == []
    finished = next(b for b in read if b.bar_date == date(2026, 9, 29))
    assert finished.close == pytest.approx(83479.2543356063)
    assert finished.fetched_at == CG_NEXT_DAY
    assert read[-1].bar_date == date(2026, 9, 30)


def test_a_provisional_bar_completing_is_not_a_restatement(db_path: Path):
    """A Yahoo bar fetched during its session is replaced by the finished one, unreported."""
    with Store(db_path) as store:
        nvda = stock(store)
        day = date(2024, 6, 12)
        store.upsert_bars([bar(nvda, Source.YAHOO, day, 120.0, datetime(2024, 6, 12, 15, 0))])
        written = store.upsert_bars([bar(nvda, Source.YAHOO, day, 125.2)])
        (held,) = store.bars_for(nvda, day, day)

    assert written == []
    assert held.close == 125.2


def test_a_finished_bar_changing_is_still_a_restatement(db_path: Path):
    """Suppression covers the bar that was in progress, not the finished days around it."""
    first = market_chart()
    second = next_day_chart(first, close=83900.0, volume=4.0e10)
    tampered = second.model_copy(
        update={
            "prices": [
                p if i != 5 else p.model_copy(update={"value": p.value * 0.9})
                for i, p in enumerate(second.prices)
            ]
        }
    )
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.upsert_bars(bars_from_market_chart(btc, first, CG_FETCHED))
        written = store.upsert_bars(bars_from_market_chart(btc, tampered, CG_NEXT_DAY))

    assert [(r.bar_date, r.field) for r in written] == [(date(2026, 9, 28), "close")]


def test_an_older_vintage_does_not_replace_a_newer_one(db_path: Path):
    """Replaying an old fetch leaves the current vintage alone and files nothing."""
    day = date(2024, 3, 1)
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        newer = bar(btc, Source.YAHOO, day, 62000.0, datetime(2024, 3, 5, tzinfo=UTC))
        older = bar(btc, Source.YAHOO, day, 60000.0, datetime(2024, 3, 3, tzinfo=UTC))
        store.upsert_bars([newer])

        assert store.upsert_bars([older]) == []
        assert store.bars_for(btc, day, day) == [newer]


def test_bars_for_an_unknown_instrument_are_refused(db_path: Path):
    ghost = InstrumentId("stock.us.ghost")
    with Store(db_path) as store:
        with pytest.raises(UnknownInstrument):
            store.upsert_bars([bar(ghost, Source.YAHOO, date(2024, 3, 1), 1.0)])
        with pytest.raises(UnknownInstrument):
            store.bars_for(ghost, date(2024, 3, 1), date(2024, 3, 1))
        assert BarRow.select_count(store.conn) == 0


def test_member_bars_cover_every_member_on_a_date(db_path: Path):
    """The aligned input an estimator needs: each member's resolved bar, or None if absent."""
    day = date(2024, 3, 1)
    with Store(db_path) as store:
        btc, eth, sol = coins(store, "BTC", "ETH", "SOL")
        store.save_universe(UniverseDefinition.create("top", UniverseParameters(), day))
        store.record_membership("top", day, [btc, eth])
        store.record_membership("top", day + timedelta(days=1), [btc, eth, sol])
        store.upsert_bars(
            [
                bar(btc, Source.YAHOO, day, 62000.0),
                bar(btc, Source.COINGECKO, day, 61500.0),
                bar(sol, Source.COINGECKO, day, 130.0),
            ]
        )
        held = store.member_bars("top", day)

        with pytest.raises(NotYetStarted):
            store.member_bars("top", day - timedelta(days=1))

    assert set(held) == {btc, eth}
    btc_bar = held[btc]
    assert btc_bar is not None and btc_bar.source is Source.YAHOO and btc_bar.close == 62000.0
    assert held[eth] is None
