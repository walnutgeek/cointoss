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

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from lythonic.exposure import ExposureMatrixBuilder
from lythonic.symmetric import SymmetricMatrixBuilder
from lythonic.universe import Universe

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
from cointoss.risk import (
    CovarianceSeries,
    DateOccupied,
    Estimator,
    ReturnFrequency,
    RiskModel,
    RiskParameters,
)
from cointoss.series import ExposureSemantics, ExposureSeries
from cointoss.store import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    TABLES,
    ExposureEntryRow,
    InstrumentReferenceRow,
    InstrumentRow,
    Migration,
    MigrationGap,
    Persistence,
    SchemaTooNew,
    SchemaVersion,
    Store,
    TickerRecordRow,
    UnknownInstrument,
)
from cointoss.universe import (
    MemberRole,
    UniverseDefinition,
    UniverseMemberRecord,
    UniverseParameters,
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


# -- Exposure and Covariance entries, ADR-0008 --


def exposure_series(name: str = "sector") -> ExposureSeries:
    """A two-entry Series over a fixed Target axis."""
    first = ExposureMatrixBuilder(targets=["tech", "energy"])
    first.set_exposure("stock.us.aapl", "tech", 1.0)
    second = ExposureMatrixBuilder(targets=["tech", "energy"])
    second.set_exposure("stock.us.aapl", "tech", 0.6)
    second.set_exposure("stock.us.aapl", "energy", 0.4)
    return (
        ExposureSeries(
            name=name, targets=Universe(["tech", "energy"]), semantics=ExposureSemantics.PERCENT
        )
        .append(date(2024, 1, 1), first.build())
        .append(date(2024, 4, 1), second.build())
    )


def covariance_series(name: str = "vendor-model") -> CovarianceSeries:
    """A one-entry Series under an external estimator."""
    builder = SymmetricMatrixBuilder()
    builder.set_diagonal({"stock.us.aapl": 0.04, "stock.us.msft": 0.09})
    builder.set_value("stock.us.aapl", "stock.us.msft", 0.02)
    parameters = RiskParameters(estimator=Estimator.EXTERNAL, source="vendor")
    series = CovarianceSeries(model=RiskModel.create(name, parameters, date(2024, 1, 1)))
    return series.declare(date(2024, 3, 31), builder.build())


def test_an_exposure_series_round_trips_whole(db_path: Path):
    with Store(db_path) as store:
        store.save_exposure_series(exposure_series())
    with Store(db_path) as store:
        assert store.load_exposure_series("sector") == exposure_series()


def test_an_unknown_exposure_series_loads_as_nothing(db_path: Path):
    with Store(db_path) as store:
        assert store.load_exposure_series("never-declared") is None


def test_an_exposure_matrix_is_one_row_per_date_not_one_per_cell(db_path: Path):
    with Store(db_path) as store:
        store.save_exposure_series(exposure_series())
        assert len(ExposureEntryRow.select(store.conn, series_name="sector")) == 2


def test_re_saving_an_exposure_series_adds_nothing(db_path: Path):
    with Store(db_path) as store:
        store.save_exposure_series(exposure_series())
        store.save_exposure_series(exposure_series())
        assert len(ExposureEntryRow.select(store.conn, series_name="sector")) == 2


def test_a_different_matrix_on_an_occupied_exposure_date_is_refused(db_path: Path):
    other = ExposureMatrixBuilder(targets=["tech", "energy"])
    other.set_exposure("stock.us.aapl", "energy", 1.0)
    replacement = ExposureSeries(
        name="sector", targets=Universe(["tech", "energy"]), semantics=ExposureSemantics.PERCENT
    ).append(date(2024, 1, 1), other.build())
    with Store(db_path) as store:
        store.save_exposure_series(exposure_series())
        with pytest.raises(DateOccupied):
            store.save_exposure_series(replacement)


def test_a_covariance_series_round_trips_with_its_model(db_path: Path):
    with Store(db_path) as store:
        store.save_covariance_series(covariance_series())
    with Store(db_path) as store:
        assert store.load_covariance_series("vendor-model") == covariance_series()


def test_a_declared_covariance_keeps_its_revision_stamp(db_path: Path):
    with Store(db_path) as store:
        store.save_covariance_series(covariance_series())
        reloaded = store.load_covariance_series("vendor-model")
        assert reloaded is not None
        assert reloaded.revision_at(date(2024, 3, 31)) == 1
        assert reloaded.parameters_at(date(2024, 3, 31)).source == "vendor"


def test_an_edited_risk_model_keeps_every_revision(db_path: Path):
    edited = covariance_series().edit_model(date(2024, 6, 1), source="other-vendor")
    with Store(db_path) as store:
        store.save_covariance_series(edited)
        reloaded = store.load_covariance_series("vendor-model")
        assert reloaded is not None
        assert reloaded.model.revision == 2
        assert reloaded.model.parameters_at(1).source == "vendor"
        assert reloaded.model.parameters_at(2).source == "other-vendor"


def test_an_unknown_covariance_series_loads_as_nothing(db_path: Path):
    with Store(db_path) as store:
        assert store.load_covariance_series("never-declared") is None


def test_a_different_matrix_on_an_occupied_covariance_date_is_refused(db_path: Path):
    builder = SymmetricMatrixBuilder()
    builder.set_diagonal({"stock.us.aapl": 0.05, "stock.us.msft": 0.09})
    parameters = RiskParameters(estimator=Estimator.EXTERNAL, source="vendor")
    replacement = CovarianceSeries(
        model=RiskModel.create("vendor-model", parameters, date(2024, 1, 1))
    ).declare(date(2024, 3, 31), builder.build())
    with Store(db_path) as store:
        store.save_covariance_series(covariance_series())
        with pytest.raises(DateOccupied):
            store.save_covariance_series(replacement)


def test_a_risk_model_persists_without_any_declarations(db_path: Path):
    parameters = RiskParameters(
        estimator=Estimator.SAMPLE, universe="midcap", lookback=250, frequency=ReturnFrequency.DAILY
    )
    model = RiskModel.create("sample-model", parameters, date(2024, 1, 1))
    with Store(db_path) as store:
        store.save_risk_model(model)
    with Store(db_path) as store:
        assert store.load_risk_model("sample-model") == model


# -- Universe Definitions and their members, ADR-0007 --


def stored_pair(store: Store) -> tuple[InstrumentId, InstrumentId]:
    """Two Instruments in the store, so a member has something real to pin to."""
    registry = InstrumentRegistry()
    meta = minted(registry, sighting("FB", date(2012, 5, 18), figi=META_FIGI))
    other = minted(
        registry,
        sighting("FB", date(2023, 6, 1), figi=PROSHARES_FIGI, issuer_name="ProShares Trust"),
    )
    store.save_instruments([meta, other])
    return meta.id, other.id


def manual_definition(name: str = "watchlist") -> UniverseDefinition:
    """A stock universe: no rule at all."""
    return UniverseDefinition.create(name, UniverseParameters(), date(2024, 1, 1))


def test_a_universe_definition_round_trips(db_path: Path):
    with Store(db_path) as store:
        store.save_universe(manual_definition())
    with Store(db_path) as store:
        assert store.load_universe("watchlist") == manual_definition()


def test_an_unknown_universe_loads_as_nothing(db_path: Path):
    with Store(db_path) as store:
        assert store.load_universe("never-defined") is None
        assert store.load_universe_members("never-defined") == []


def test_a_rank_band_survives_a_reopen(db_path: Path):
    banded = UniverseDefinition.create(
        "majors", UniverseParameters(enter_rank=100, exit_rank=120), date(2024, 1, 1)
    )
    with Store(db_path) as store:
        store.save_universe(banded)
    with Store(db_path) as store:
        reloaded = store.load_universe("majors")
        assert reloaded is not None
        assert reloaded.parameters.enter_rank == 100
        assert reloaded.parameters.exit_rank == 120


def test_every_revision_keeps_the_band_it_was_edited_to(db_path: Path):
    banded = UniverseDefinition.create(
        "majors", UniverseParameters(enter_rank=100, exit_rank=120), date(2024, 1, 1)
    ).edited(date(2024, 6, 1), enter_rank=50)
    with Store(db_path) as store:
        store.save_universe(banded)
        reloaded = store.load_universe("majors")
        assert reloaded is not None
        assert reloaded.revision == 2
        assert reloaded.parameters_at(1).enter_rank == 100
        assert reloaded.parameters_at(2).enter_rank == 50
        assert reloaded.revisions[1].changed == ("enter_rank",)


def test_a_member_keeps_the_symbol_as_it_was_typed(db_path: Path):
    with Store(db_path) as store:
        pinned, _ = stored_pair(store)
        record = UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source=Source.YAHOO,
            symbol_as_typed="fb",
            instrument_id=pinned,
        )
        store.save_universe(manual_definition(), [record])
        assert store.load_universe_members("watchlist") == [record]


def test_an_unresolved_member_is_stored_and_visible(db_path: Path):
    unresolved = UniverseMemberRecord(
        role=MemberRole.INCLUSION, source=Source.YAHOO, symbol_as_typed="NOSUCH"
    )
    with Store(db_path) as store:
        store.save_universe(manual_definition(), [unresolved])
        assert store.load_universe_members("watchlist") == [unresolved]
        assert store.unresolved_members("watchlist") == [unresolved]


def test_an_unresolved_member_reaches_no_parameters(db_path: Path):
    unresolved = UniverseMemberRecord(
        role=MemberRole.INCLUSION, source=Source.YAHOO, symbol_as_typed="NOSUCH"
    )
    with Store(db_path) as store:
        store.save_universe(manual_definition(), [unresolved])
        reloaded = store.load_universe("watchlist")
        assert reloaded is not None
        assert reloaded.parameters.inclusions == frozenset()


def test_an_absent_member_differs_from_an_unresolved_one(db_path: Path):
    with Store(db_path) as store:
        store.save_universe(manual_definition())
        assert store.load_universe_members("watchlist") == []
        assert store.unresolved_members("watchlist") == []


def test_removing_and_re_adding_one_ticker_leaves_two_records(db_path: Path):
    with Store(db_path) as store:
        pinned, _ = stored_pair(store)
        removed = UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source=Source.YAHOO,
            symbol_as_typed="fb",
            instrument_id=pinned,
            added_in_revision=1,
            removed_in_revision=2,
        )
        re_added = UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source=Source.YAHOO,
            symbol_as_typed="fb",
            instrument_id=pinned,
            added_in_revision=3,
        )
        store.save_universe(manual_definition(), [removed, re_added])
        assert store.load_universe_members("watchlist") == [removed, re_added]


def test_a_member_removed_in_an_earlier_revision_is_gone_from_the_projection(db_path: Path):
    edited = manual_definition().edited(date(2024, 3, 1), enter_rank=10, exit_rank=20)
    with Store(db_path) as store:
        pinned, _ = stored_pair(store)
        record = UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source=Source.YAHOO,
            symbol_as_typed="fb",
            instrument_id=pinned,
            added_in_revision=1,
            removed_in_revision=2,
        )
        store.save_universe(edited, [record])
        reloaded = store.load_universe("watchlist")
        assert reloaded is not None
        assert reloaded.parameters_at(1).inclusions == frozenset({pinned})
        assert reloaded.parameters_at(2).inclusions == frozenset()


def test_a_removed_member_is_not_in_the_unresolved_queue(db_path: Path):
    record = UniverseMemberRecord(
        role=MemberRole.INCLUSION,
        source=Source.YAHOO,
        symbol_as_typed="NOSUCH",
        added_in_revision=1,
        removed_in_revision=2,
    )
    with Store(db_path) as store:
        store.save_universe(manual_definition(), [record])
        assert store.unresolved_members("watchlist") == []


def test_the_unresolved_queue_spans_every_definition(db_path: Path):
    unresolved = UniverseMemberRecord(
        role=MemberRole.INCLUSION, source=Source.YAHOO, symbol_as_typed="NOSUCH"
    )
    with Store(db_path) as store:
        store.save_universe(manual_definition("one"), [unresolved])
        store.save_universe(manual_definition("two"), [unresolved])
        assert len(store.unresolved_members()) == 2
        assert len(store.unresolved_members("one")) == 1


def test_inclusions_and_exclusions_are_told_apart_by_role(db_path: Path):
    with Store(db_path) as store:
        kept, dropped = stored_pair(store)
        records = [
            UniverseMemberRecord(
                role=MemberRole.INCLUSION,
                source=Source.YAHOO,
                symbol_as_typed="fb",
                instrument_id=kept,
            ),
            UniverseMemberRecord(
                role=MemberRole.EXCLUSION,
                source=Source.YAHOO,
                symbol_as_typed="fb2",
                instrument_id=dropped,
            ),
        ]
        store.save_universe(manual_definition(), records)
        reloaded = store.load_universe("watchlist")
        assert reloaded is not None
        assert reloaded.parameters.inclusions == frozenset({kept})
        assert reloaded.parameters.exclusions == frozenset({dropped})


def test_a_member_pinned_to_an_unstored_instrument_is_refused(db_path: Path):
    record = UniverseMemberRecord(
        role=MemberRole.INCLUSION,
        source=Source.YAHOO,
        symbol_as_typed="fb",
        instrument_id=InstrumentId("stock.us.nothing"),
    )
    with Store(db_path) as store:
        with pytest.raises(UnknownInstrument):
            store.save_universe(manual_definition(), [record])


def test_an_instrument_both_included_and_excluded_warns(
    db_path: Path, caplog: pytest.LogCaptureFixture
):
    with Store(db_path) as store:
        contested, _ = stored_pair(store)
        records = [
            UniverseMemberRecord(
                role=MemberRole.INCLUSION,
                source=Source.YAHOO,
                symbol_as_typed="fb",
                instrument_id=contested,
            ),
            UniverseMemberRecord(
                role=MemberRole.EXCLUSION,
                source=Source.COINGECKO,
                symbol_as_typed="fb",
                instrument_id=contested,
            ),
        ]
        with caplog.at_level(logging.WARNING, logger="cointoss.store"):
            store.save_universe(manual_definition(), records)
        assert "both included and excluded" in caplog.text
        assert str(contested) in caplog.text
        reloaded = store.load_universe("watchlist")
        assert reloaded is not None
        assert reloaded.parameters.exclusions == frozenset({contested})


def test_no_warning_when_the_inclusion_is_removed(db_path: Path, caplog: pytest.LogCaptureFixture):
    with Store(db_path) as store:
        contested, _ = stored_pair(store)
        records = [
            UniverseMemberRecord(
                role=MemberRole.EXCLUSION,
                source=Source.COINGECKO,
                symbol_as_typed="fb",
                instrument_id=contested,
            ),
        ]
        with caplog.at_level(logging.WARNING, logger="cointoss.store"):
            store.save_universe(manual_definition(), records)
        assert "both included and excluded" not in caplog.text
