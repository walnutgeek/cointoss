"""Portfolio storage, ADR-0001 as amended 2026-10-30, through the public seam of `cointoss.store`.

A Portfolio belongs to one woodglue User and holds single-leg Trades. Positions are derived on
read: a position as of a date counts every Trade up to the end of that date in UTC, skips voided
Trades, folds Instruments through Supersession, and averages cost over the Trades that carry one.

The migration test builds a file at the previous schema version by opening a `Store` over the
schema as it stood then, writes data into it, and reopens it with the shipped schema.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from lythonic.state import Schema

from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.portfolio import PortfolioKind, PortfolioPosition, TradeKind
from cointoss.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    TABLES,
    DuplicatePortfolio,
    PortfolioRow,
    SchemaVersion,
    Store,
    TradeAlreadyVoided,
    TradeRow,
    UnknownInstrument,
    UnknownPortfolio,
    UnknownTrade,
)
from cointoss.universe import UniverseDefinition, UniverseParameters

D1 = date(2024, 1, 1)
ALICE = "usr_a11ce0000001"
BOB = "usr_b0b000000002"


@pytest.fixture
def db_path():
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "cointoss.db"


def coins(store: Store, *symbols: str) -> list[InstrumentId]:
    registry = InstrumentRegistry()
    ids: list[InstrumentId] = []
    for symbol in symbols:
        outcome = registry.observe(
            Observation(
                type=InstrumentType.CRYPTO,
                symbol=symbol,
                scope="native",
                observed_at=D1,
                source=Source.COINGECKO,
                references=(ExternalReference.provider_id("coingecko", symbol.lower()),),
                figi_resolution=FigiResolution.NOT_FOUND,
            )
        )
        assert outcome.instrument is not None
        store.save_instrument(outcome.instrument)
        ids.append(outcome.instrument.id)
    return ids


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def test_a_users_portfolios_are_their_own(db_path: Path):
    with Store(db_path) as store:
        main = store.create_portfolio(ALICE, "main", PortfolioKind.REAL, custodian="coinbase")
        store.create_portfolio(ALICE, "ideas", PortfolioKind.WHAT_IF)
        store.create_portfolio(BOB, "main", PortfolioKind.PAPER)
    with Store(db_path) as store:
        assert [p.name for p in store.portfolios_of(ALICE)] == ["ideas", "main"]
        assert [p.kind for p in store.portfolios_of(BOB)] == [PortfolioKind.PAPER]
        assert store.portfolios_of("usr_nobody") == []
        assert store.load_portfolio(main.id) == main
        assert main.custodian == "coinbase"


def test_one_user_cannot_hold_two_portfolios_of_one_name(db_path: Path):
    with Store(db_path) as store:
        store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        with pytest.raises(DuplicatePortfolio):
            store.create_portfolio(ALICE, "main", PortfolioKind.PAPER)


def test_a_trade_reads_back_as_entered(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        eastern = timezone(timedelta(hours=-5))
        recorded = store.record_trade(
            p.id,
            btc,
            datetime(2024, 3, 1, 22, 0, tzinfo=eastern),
            0.5,
            kind=TradeKind.OPENING_BALANCE,
            cost=20_000.0,
            note="carried over",
        )
    with Store(db_path) as store:
        (trade,) = store.trades_of(p.id)
    assert trade == recorded
    assert trade.instrument_id == btc
    assert trade.traded_at == datetime(2024, 3, 2, 3, 0, tzinfo=UTC)
    assert (trade.quantity, trade.kind, trade.cost, trade.note) == (
        0.5,
        TradeKind.OPENING_BALANCE,
        20_000.0,
        "carried over",
    )
    assert not trade.voided


def test_a_trade_needs_a_stored_portfolio_and_instrument(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        with pytest.raises(UnknownPortfolio):
            store.record_trade(p.id + 1, btc, at(D1), 1.0)
        with pytest.raises(UnknownInstrument):
            store.record_trade(p.id, InstrumentId("crypto.native.nosuch"), at(D1), 1.0)


def test_a_position_as_of_a_date_counts_trades_to_the_end_of_that_utc_day(db_path: Path):
    day = date(2024, 3, 1)
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        store.record_trade(p.id, btc, at(day, 0, 0), 1.0)
        store.record_trade(p.id, btc, datetime(2024, 3, 1, 23, 59, 59, 999999, tzinfo=UTC), 2.0)
        store.record_trade(p.id, btc, at(day + timedelta(days=1), 0, 0), 4.0)
        # 20:00 in New York on the 1st is 01:00 UTC on the 2nd.
        new_york = timezone(timedelta(hours=-5))
        store.record_trade(p.id, btc, datetime(2024, 3, 1, 20, 0, tzinfo=new_york), 8.0)
        assert store.positions_at(p.id, day - timedelta(days=1)) == []
        assert [pos.quantity for pos in store.positions_at(p.id, day)] == [3.0]
        assert [pos.quantity for pos in store.positions_at(p.id, day + timedelta(days=1))] == [15.0]


def test_a_voided_trade_changes_no_position_and_reads_back_as_voided(db_path: Path):
    day = date(2024, 3, 1)
    voided_at = datetime(2024, 3, 5, 9, 0, tzinfo=UTC)
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        store.record_trade(p.id, btc, at(day), 1.0, cost=40_000.0)
        typo = store.record_trade(p.id, btc, at(day, 13), 10.0, cost=1.0)
        voided = store.void_trade(typo.id, at=voided_at)
        assert voided.voided and voided.voided_at == voided_at
    with Store(db_path) as store:
        assert store.positions_at(p.id, day) == [PortfolioPosition(btc, 1.0, 40_000.0)]
        trades = store.trades_of(p.id)
    assert [t.voided for t in trades] == [False, True]
    assert trades[1] == voided


def test_voiding_a_trade_twice_is_refused(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        trade = store.record_trade(p.id, btc, at(D1), 1.0)
        first = store.void_trade(trade.id)
        with pytest.raises(TradeAlreadyVoided):
            store.void_trade(trade.id)
        with pytest.raises(UnknownTrade):
            store.void_trade(trade.id + 1)
        assert store.trades_of(p.id) == [first]


def test_trades_under_a_superseded_instrument_aggregate_under_its_survivor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        store.record_trade(p.id, btc, at(D1), 1.0, cost=30_000.0)
        store.record_trade(p.id, wbtc, at(D1, 13), 3.0, cost=150_000.0)
        merged = store.load_instrument(wbtc)
        assert merged is not None
        merged.superseded_by = btc
        store.save_instrument(merged)
        assert store.positions_at(p.id, D1) == [PortfolioPosition(btc, 4.0, 45_000.0)]
        assert {t.instrument_id for t in store.trades_of(p.id)} == {btc}


def test_positions_are_per_portfolio(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        main = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        paper = store.create_portfolio(ALICE, "paper", PortfolioKind.PAPER)
        store.record_trade(main.id, btc, at(D1), 1.0)
        store.record_trade(main.id, eth, at(D1), 2.0, cost=4_000.0)
        store.record_trade(paper.id, btc, at(D1), 9.0)
        assert store.positions_at(main.id, D1) == [
            PortfolioPosition(btc, 1.0, None),
            PortfolioPosition(eth, 2.0, 2_000.0),
        ]
        assert store.positions_at(paper.id, D1) == [PortfolioPosition(btc, 9.0, None)]


def test_a_store_at_the_previous_schema_version_migrates_and_keeps_its_data(db_path: Path):
    previous = Schema([t for t in TABLES if t not in (PortfolioRow, TradeRow)])
    with Store(db_path, schema=previous, version=SCHEMA_VERSION - 1, migrations=[]) as old:
        (btc,) = coins(old, "BTC")
        old.save_universe(UniverseDefinition.create("top", UniverseParameters(), D1))
        assert PortfolioRow.get_table_name() not in old._existing_tables()  # pyright: ignore[reportPrivateUsage]
    with Store(db_path) as store:
        assert [row.version for row in SchemaVersion.select(store.conn)] == [
            SCHEMA_VERSION - 1,
            SCHEMA_VERSION,
        ]
        assert store.load_instrument(btc) is not None
        assert store.universe_names() == ["top"]
        p = store.create_portfolio(ALICE, "main", PortfolioKind.REAL)
        store.record_trade(p.id, btc, at(D1), 1.0)
        assert store.positions_at(p.id, D1) == [PortfolioPosition(btc, 1.0, None)]


def test_the_migration_step_adds_the_portfolio_tables_without_relying_on_open(db_path: Path):
    previous = Schema([t for t in TABLES if t not in (PortfolioRow, TradeRow)])
    with Store(db_path, schema=previous, version=SCHEMA_VERSION - 1, migrations=[]):
        pass
    (step,) = [m for m in MIGRATIONS if m.to_version == SCHEMA_VERSION]
    with closing(sqlite3.connect(db_path)) as conn:
        step.apply(conn)
        step.apply(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"Portfolio", "Trade"} <= tables
