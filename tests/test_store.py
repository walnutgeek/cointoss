"""Operating guarantees of ADR-0008, driven through the public seam of `cointoss.store`.

Every test opens a real `Store` on a real SQLite file in a temporary directory and asserts on
what an operator can observe: that opening twice works, that the pragmas the layout depends on
are actually in force, and that a version mismatch is a named refusal rather than a confusing
error. Connections are closed explicitly -- the store's context manager, or `closing` around a
raw one -- so Windows can delete the directory.

Foreign key enforcement is shown over the real schema, against the Instrument an External
Reference points at.

The identity tests drive the same seam: Instruments are minted through `InstrumentRegistry`,
written, and read back from a reopened file. What they assert on is what a caller sees -- the
Instrument that comes back, the id a symbol resolves to on a date -- not the rows behind it,
except where the ticket's guarantee is about decomposition itself.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    Instrument,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    ReferenceKind,
    Source,
    SupersessionCycle,
)
from cointoss.store import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    TABLES,
    InstrumentReferenceRow,
    InstrumentRow,
    Migration,
    MigrationGap,
    Persistence,
    SchemaTooNew,
    SchemaVersion,
    Store,
    TickerRecordRow,
)


def sighting(
    symbol: str,
    observed_at: date,
    *,
    source: Source = Source.YAHOO,
    figi: str | None = None,
    issuer_name: str | None = None,
    scope: str = "us",
) -> Observation:
    """A listed stock sighting. A FIGI keeps two sightings of one symbol apart."""
    references = () if figi is None else (ExternalReference.composite_figi(figi),)
    return Observation(
        type=InstrumentType.STOCK,
        symbol=symbol,
        scope=scope,
        observed_at=observed_at,
        source=source,
        issuer_name=issuer_name,
        references=references,
        figi_resolution=(FigiResolution.NOT_ATTEMPTED if figi is None else FigiResolution.RESOLVED),
    )


def minted(registry: InstrumentRegistry, obs: Observation) -> Instrument:
    instrument = registry.observe(obs).instrument
    assert instrument is not None
    return instrument


@pytest.fixture
def db_path():
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "cointoss.db"


def test_first_open_creates_the_file_and_its_schema(db_path: Path):
    assert not db_path.exists()
    with Store(db_path) as store:
        assert SchemaVersion.select_count(store.conn) == 1
    assert db_path.exists()


def test_reopening_succeeds_unchanged(db_path: Path):
    with Store(db_path) as store:
        first = SchemaVersion.select(store.conn)
    with Store(db_path) as store:
        assert SchemaVersion.select(store.conn) == first


def test_pragmas_are_in_force(db_path: Path):
    with Store(db_path) as store:
        cursor = store.conn.cursor()
        assert cursor.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert cursor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_reference_to_a_missing_instrument_is_rejected(db_path: Path):
    with Store(db_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            InstrumentReferenceRow(
                instrument=404,
                kind=ReferenceKind.COMPOSITE_FIGI,
                value="BBG000BVPV84",
                qualifier="",
            ).save(store.conn)


def test_a_reference_to_a_present_instrument_is_accepted(db_path: Path):
    with Store(db_path) as store:
        registry = InstrumentRegistry()
        store.save_instrument(minted(registry, sighting("AAPL", date(2020, 1, 2))))
        row = InstrumentRow.load_by_ak(store.conn, instrument_id="stock.us.aapl")
        assert row is not None
        reference = InstrumentReferenceRow(
            instrument=row.instrument_row_id,
            kind=ReferenceKind.PROVIDER_ID,
            value="AAPL",
            qualifier="yahoo",
        ).save(store.conn)
        assert (
            InstrumentReferenceRow.load_by_id(store.conn, reference.instrument_reference_id)
            == reference
        )


def test_a_newer_schema_version_is_refused(db_path: Path):
    with Store(db_path, version=SCHEMA_VERSION + 1):
        pass
    with pytest.raises(SchemaTooNew):
        Store(db_path, version=SCHEMA_VERSION)


def test_a_refused_open_leaves_no_connection_behind(db_path: Path):
    with Store(db_path, version=SCHEMA_VERSION + 1):
        pass
    with pytest.raises(SchemaTooNew):
        Store(db_path)
    # Nothing holds the file, so a plain connection can still read it.
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT max(version) FROM SchemaVersion").fetchone()[0] == (
            SCHEMA_VERSION + 1
        )


def test_the_shipped_migration_list_is_empty():
    assert MIGRATIONS == []


def test_an_older_version_runs_the_steps_in_order(db_path: Path):
    with Store(db_path, version=1):
        pass
    order: list[int] = []

    def step(to_version: int):
        def apply(conn: sqlite3.Connection) -> None:
            order.append(to_version)
            conn.execute(f"CREATE TABLE Step{to_version} (x INTEGER)")

        return apply

    # Deliberately out of order in the list, to show the run order comes from `to_version`.
    migrations = [Migration(3, step(3)), Migration(2, step(2))]
    with Store(db_path, version=3, migrations=migrations) as store:
        assert order == [2, 3]
        assert [row.version for row in SchemaVersion.select(store.conn)] == [1, 2, 3]


def test_a_migrated_file_reopens_without_re_running_the_steps(db_path: Path):
    ran: list[int] = []

    def apply(_conn: sqlite3.Connection) -> None:
        ran.append(2)

    migrations = [Migration(2, apply)]
    with Store(db_path, version=1):
        pass
    with Store(db_path, version=2, migrations=migrations):
        pass
    with Store(db_path, version=2, migrations=migrations):
        pass
    assert ran == [2]


def test_a_missing_migration_step_is_refused(db_path: Path):
    with Store(db_path, version=1):
        pass
    with pytest.raises(MigrationGap):
        Store(db_path, version=2, migrations=[])


def test_the_version_is_stamped_with_a_time(db_path: Path):
    before = datetime.now(UTC)
    with Store(db_path) as store:
        (recorded,) = SchemaVersion.select(store.conn)
    assert recorded.version == SCHEMA_VERSION
    assert before <= recorded.applied_at.replace(tzinfo=UTC)


def test_every_table_is_classified():
    assert set(SCHEMA.tables) == set(TABLES)
    assert TABLES[SchemaVersion] is Persistence.META
    assert all(isinstance(p, Persistence) for p in TABLES.values())


META_FIGI = "BBG000MM2P62"
PROSHARES_FIGI = "BBG00PROSHARE1"


def meta_registry() -> InstrumentRegistry:
    """Meta, seen as FB and then as META, and the unrelated issuer that later took FB."""
    registry = InstrumentRegistry()
    minted(registry, sighting("FB", date(2012, 5, 18), figi=META_FIGI))
    minted(registry, sighting("META", date(2022, 6, 9), figi=META_FIGI))
    minted(
        registry,
        sighting("FB", date(2023, 6, 1), figi=PROSHARES_FIGI, issuer_name="ProShares Trust"),
    )
    return registry


def test_an_instrument_reloads_with_every_field_intact(db_path: Path):
    registry = InstrumentRegistry()
    original = minted(registry, sighting("FB", date(2012, 5, 18), figi=META_FIGI))
    minted(registry, sighting("META", date(2022, 6, 9), figi=META_FIGI))
    with Store(db_path) as store:
        store.save_instrument(original)
    with Store(db_path) as store:
        assert store.load_instrument(original.id) == original


def test_an_unknown_instrument_id_loads_as_nothing(db_path: Path):
    with Store(db_path) as store:
        assert store.load_instrument("stock.us.nope") is None


def test_saving_the_same_instrument_twice_does_not_duplicate_it(db_path: Path):
    registry = meta_registry()
    meta = registry.get("stock.us.fb")
    with Store(db_path) as store:
        store.save_instrument(meta)
        store.save_instrument(meta)
        assert InstrumentRow.select_count(store.conn) == 1
        assert store.load_instrument(meta.id) == meta


def test_references_and_ticker_history_are_rows_of_their_own(db_path: Path):
    registry = meta_registry()
    meta = registry.get("stock.us.fb")
    with Store(db_path) as store:
        store.save_instrument(meta)
        row = InstrumentRow.load_by_ak(store.conn, instrument_id=str(meta.id))
        assert row is not None
        records = TickerRecordRow.select(store.conn, instrument=row.instrument_row_id)
        references = InstrumentReferenceRow.select(store.conn, instrument=row.instrument_row_id)
    assert [r.symbol for r in records] == ["FB", "META"]
    assert len(references) == len(meta.references) == 1


def test_minting_order_survives_a_reopen(db_path: Path):
    registry = meta_registry()
    before = [(i.id, i.mint_seq) for i in registry.instruments()]
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
    with Store(db_path) as store:
        reloaded = store.load_registry()
    assert [(i.id, i.mint_seq) for i in reloaded.instruments()] == before


def test_a_reopened_registry_mints_nothing_it_already_holds(db_path: Path):
    registry = meta_registry()
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
    with Store(db_path) as store:
        reloaded = store.load_registry()
    held = registry.instruments()
    # The same sighting again matches by FIGI rather than minting a second Instrument.
    again = minted(reloaded, sighting("META", date(2024, 1, 2), figi=META_FIGI))
    assert again.id == InstrumentId("stock.us.fb")
    # A sighting of something genuinely new continues the sequence rather than restarting it.
    fresh = minted(reloaded, sighting("AAPL", date(2024, 1, 2), figi="BBG000B9XRY4"))
    assert fresh.mint_seq == max(i.mint_seq for i in held) + 1
    assert [i.id for i in reloaded.instruments()] == [i.id for i in held] + [fresh.id]


def test_a_symbol_resolves_to_what_it_meant_on_a_date(db_path: Path):
    with Store(db_path) as store:
        store.save_instruments(meta_registry().instruments())
    with Store(db_path) as store:
        assert store.resolve_symbol(Source.YAHOO, "FB", date(2019, 3, 1)) == "stock.us.fb"
        assert store.resolve_symbol(Source.YAHOO, "META", date(2024, 3, 1)) == "stock.us.fb"


def test_a_reused_symbol_resolves_by_date_rather_than_ambiguously(db_path: Path):
    registry = meta_registry()
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
    with Store(db_path) as store:
        then = store.resolve_symbol(Source.YAHOO, "FB", date(2019, 3, 1))
        later = store.resolve_symbol(Source.YAHOO, "FB", date(2023, 9, 1))
    assert then == "stock.us.fb"
    assert later == "stock.us.fb_proshares"
    # The same two answers the in-memory registry gives, from one indexed query instead.
    in_memory = [
        registry.resolve_symbol(Source.YAHOO, InstrumentType.STOCK, "FB", "us", as_of)
        for as_of in (date(2019, 3, 1), date(2023, 9, 1))
    ]
    assert [i.id for i in in_memory if i is not None] == [then, later]


def test_a_symbol_with_no_record_on_that_date_returns_nothing(db_path: Path):
    with Store(db_path) as store:
        store.save_instruments(meta_registry().instruments())
    with Store(db_path) as store:
        # Between Meta's rename and the day an unrelated issuer took the symbol.
        assert store.resolve_symbol(Source.YAHOO, "FB", date(2022, 9, 1)) is None
        # Before Meta was ever seen.
        assert store.resolve_symbol(Source.YAHOO, "FB", date(2009, 1, 2)) is None
        # A symbol nothing ever held.
        assert store.resolve_symbol(Source.YAHOO, "ZZZZ", date(2019, 3, 1)) is None


def test_a_symbol_does_not_resolve_under_a_source_that_never_reported_it(db_path: Path):
    with Store(db_path) as store:
        store.save_instruments(meta_registry().instruments())
    with Store(db_path) as store:
        assert store.resolve_symbol(Source.COINGECKO, "FB", date(2019, 3, 1)) is None


def test_two_instruments_holding_one_symbol_at_once_is_refused(db_path: Path):
    registry = InstrumentRegistry()
    minted(registry, sighting("FB", date(2012, 5, 18), figi=META_FIGI))
    minted(
        registry,
        sighting("FB", date(2013, 1, 2), figi=PROSHARES_FIGI, issuer_name="ProShares Trust"),
    )
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
        with pytest.raises(AmbiguousSymbol):
            store.resolve_symbol(Source.YAHOO, "FB", date(2014, 1, 2))


def test_supersession_is_folded_on_read_without_rewriting_rows(db_path: Path):
    registry = meta_registry()
    survivor = registry.get("stock.us.fb")
    merged = registry.get("stock.us.fb_proshares")
    # A late resolution reveals the two are one; the earliest-minted survives.
    merged.superseded_by = survivor.id
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
    with Store(db_path) as store:
        assert store.resolve_symbol(Source.YAHOO, "FB", date(2023, 9, 1)) == survivor.id
        # Nothing the merged Instrument recorded was rewritten to say so.
        assert store.load_instrument(merged.id) == merged
        row = InstrumentRow.load_by_ak(store.conn, instrument_id=str(merged.id))
        assert row is not None
        assert [
            r.symbol for r in TickerRecordRow.select(store.conn, instrument=row.instrument_row_id)
        ] == ["FB"]


def test_a_reloaded_instrument_keeps_its_source_attribution(db_path: Path):
    registry = InstrumentRegistry()
    coin = minted(
        registry,
        Observation(
            type=InstrumentType.CRYPTO,
            symbol="BTC",
            scope="native",
            observed_at=date(2013, 4, 28),
            source=Source.COINGECKO,
            references=(ExternalReference.provider_id("coingecko", "bitcoin"),),
        ),
    )
    with Store(db_path) as store:
        store.save_instrument(coin)
    with Store(db_path) as store:
        reloaded = store.load_instrument(coin.id)
    assert reloaded is not None
    assert reloaded.identity_source is Source.COINGECKO
    assert reloaded.price_sources == (Source.YAHOO, Source.COINGECKO)
    assert reloaded.references == [ExternalReference.provider_id("coingecko", "bitcoin")]


def test_a_scope_separates_one_symbol_two_authorities_issue(db_path: Path):
    registry = InstrumentRegistry()
    minted(registry, sighting("SHOP", date(2015, 5, 21), figi="BBG00US0SHOP1"))
    minted(registry, sighting("SHOP", date(2015, 5, 21), figi="BBG00CA0SHOP1", scope="ca"))
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
        with pytest.raises(AmbiguousSymbol):
            store.resolve_symbol(Source.YAHOO, "SHOP", date(2020, 1, 2))
        assert (
            store.resolve_symbol(Source.YAHOO, "SHOP", date(2020, 1, 2), scope="ca")
            == "stock.ca.shop"
        )


def test_a_supersession_cycle_is_refused_rather_than_looped_on(db_path: Path):
    registry = meta_registry()
    first = registry.get("stock.us.fb")
    second = registry.get("stock.us.fb_proshares")
    first.superseded_by = second.id
    second.superseded_by = first.id
    with Store(db_path) as store:
        store.save_instruments(registry.instruments())
        with pytest.raises(SupersessionCycle):
            store.resolve_symbol(Source.YAHOO, "META", date(2024, 3, 1))
