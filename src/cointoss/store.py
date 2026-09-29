"""The SQLite store: one database file, opened safely, versioned explicitly.

Implements ADR-0008. This is the only module in cointoss that touches SQL. Every table is a
`lythonic.state.DbModel` with a surrogate integer primary key and `(AK)` on its natural key,
and `Schema` assembles them.

`lythonic.state` issues no pragmas -- `open_sqlite_db` is a bare `sqlite3.connect` -- so the
`REFERENCES` clauses its DDL generates are documentation rather than constraints, and the
default rollback journal gives none of the reader concurrency the layout assumes. `Store`
therefore opens its own connection and sets `foreign_keys` and `journal_mode` itself.

`DbFile.check()` carries a `TODO` where migration belongs, so the version lives here instead:
a `SchemaVersion` row records what wrote the file, and opening compares it against
`SCHEMA_VERSION`. Equal proceeds, older migrates through `MIGRATIONS` in order, newer is
refused -- an old binary must not write into a file it does not understand.

Every table is classified durable or rebuildable in `TABLES`, because only durable tables ever
need a migration written: bars, corporate actions, restatements and run logs can be dropped and
re-fetched, while definitions, revisions and the instrument registry have no source to
re-derive them from.

The identity tables are the first domain tables here. `Instrument`, `InstrumentReference` and
`TickerRecord` are decomposed into rows rather than kept as a payload on the Instrument,
because resolution by reference and resolution by symbol both query them directly. Mint order
is what makes them durable: ids are minted from the first symbol seen in `mint_seq` order, so a
registry rehydrated from these rows must remint nothing.

Later tickets add their tables to `TABLES` and nothing else changes.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, NamedTuple, Self

from lythonic.exposure import ExposureMatrix
from lythonic.state import DbModel, Schema, execute_sql
from lythonic.symmetric import SymmetricMatrix
from lythonic.universe import Universe
from pydantic import Field
from typing_extensions import override

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    Instrument,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    ReferenceKind,
    Scope,
    Source,
    SupersessionCycle,
    TickerRecord,
    normalize_symbol,
)
from cointoss.risk import (
    CovarianceSeries,
    DateOccupied,
    DeclaredCovariance,
    Estimator,
    ReturnFrequency,
    RiskModel,
    RiskModelRevision,
    RiskParameters,
)
from cointoss.series import DatedMatrix, ExposureSemantics, ExposureSeries
from cointoss.universe import (
    MemberRole,
    UniverseDefinition,
    UniverseDefinitionRevision,
    UniverseMemberRecord,
    UniverseParameters,
    resolved_members,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)


__all__ = [
    "INDEXES",
    "MIGRATIONS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TABLES",
    "CovarianceEntryRow",
    "ExposureEntryRow",
    "Index",
    "InstrumentReferenceRow",
    "InstrumentRow",
    "Migration",
    "MigrationGap",
    "Persistence",
    "RiskModelRevisionRow",
    "RiskModelRow",
    "SchemaTooNew",
    "SchemaVersion",
    "Store",
    "StoreError",
    "UnknownInstrument",
    "TickerRecordRow",
    "UniverseDefinitionRow",
    "UniverseMemberRow",
    "UniverseRevisionRow",
]


class StoreError(Exception):
    """Something is wrong with the database file itself, not with the data in it."""


class SchemaTooNew(StoreError):
    """The file was written by a newer schema than this build knows how to read."""


class MigrationGap(StoreError):
    """No ordered path of migration steps leads from the stored version to this one."""


class UnknownInstrument(StoreError):
    """A member pins an Instrument Id the store does not hold."""


class Persistence(StrEnum):
    """Whether a table's contents can be re-derived, per ADR-0008.

    Only `DURABLE` tables need a migration written. `REBUILDABLE` ones are re-fetchable from a
    source, so an awkward schema change drops and backfills them. `META` is the store's own
    bookkeeping.
    """

    DURABLE = "durable"
    REBUILDABLE = "rebuildable"
    META = "meta"


class SchemaVersion(DbModel["SchemaVersion"]):
    """One row per version this file has ever been at; the highest is the current one."""

    schema_version_id: int = Field(default=-1, description="(PK)")
    version: int = Field(description="(AK) Schema version the file was brought to")
    applied_at: datetime = Field(description="When the file reached this version")


class InstrumentRow(DbModel["InstrumentRow"]):
    """An Instrument's own fields. `instrument_id` is the minted slug, unique by constraint.

    The Python class is named for the row and the table for the entity, because
    `cointoss.instrument.Instrument` is the value type this row projects and the two must be
    distinguishable in this module.

    `scope` and `superseded_by` are stored as plain text: `Scope` and `InstrumentId` are `str`
    subclasses that `lythonic.types` does not register, and both parse back from their text.
    `price_sources` is comma-separated for the same reason -- there is no list column type.
    """

    instrument_row_id: int = Field(default=-1, description="(PK)")
    instrument_id: str = Field(description="(AK) Minted Instrument Id")
    type: InstrumentType = Field(description="Instrument type")
    scope: str = Field(description="Authority under which the symbol is unique")
    symbol: str = Field(description="Display symbol, as the Identity Source reports it")
    mint_seq: int = Field(description="Position in mint order, which ids depend on")
    minted_at: date = Field(description="Observation date the Instrument was minted from")
    identity_source: Source = Field(description="Source the display name and symbol come from")
    price_sources: str = Field(description="Ordered Price Sources, comma separated")
    name: str | None = Field(default=None, description="Display name")
    issuer_name: str | None = Field(default=None, description="Issuer, when a source reports one")
    figi_resolution: FigiResolution = Field(description="Outcome of the FIGI attempt")
    superseded_by: str | None = Field(default=None, description="Instrument Id that survives")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "Instrument"


class InstrumentReferenceRow(DbModel["InstrumentReferenceRow"]):
    """One External Reference. A row rather than a payload, because identity queries it."""

    instrument_reference_id: int = Field(default=-1, description="(PK)")
    instrument: int = Field(
        description="(FK:Instrument.instrument_row_id)(AK) Instrument the reference identifies"
    )
    kind: ReferenceKind = Field(description="(AK) Kind of external identifier")
    value: str = Field(description="(AK) The identifier itself")
    qualifier: str = Field(description="(AK) Namespace: provider, chain, or empty for a FIGI")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "InstrumentReference"


class TickerRecordRow(DbModel["TickerRecordRow"]):
    """One stretch of Ticker History, as reported by one source. `valid_to` null means current.

    The natural key ends at `valid_from`: one source cannot open two records for the same
    symbol on the same day, while two sources naming the asset differently, and two Instruments
    holding the same symbol over disjoint periods, are both expected.
    """

    ticker_record_id: int = Field(default=-1, description="(PK)")
    instrument: int = Field(
        description="(FK:Instrument.instrument_row_id)(AK) Instrument that held the symbol"
    )
    source: Source = Field(description="(AK) Source that reported the symbol")
    symbol: str = Field(description="(AK) Symbol as that source spells it")
    valid_from: date = Field(description="(AK) First date the record covers")
    scope: str = Field(description="Scope the symbol was reported under")
    valid_to: date | None = Field(default=None, description="First date it no longer covers")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "TickerRecord"


class ExposureEntryRow(DbModel["ExposureEntryRow"]):
    """One dated entry of an Exposure Series, matrix and all.

    ADR-0008 stores a matrix whole rather than as one row per cell. The cross-cutting query
    that earns decomposition -- which universes held an instrument -- has no counterpart here;
    nobody asks which matrices had a given cell above a threshold, and an N-by-N matrix spread
    over N-squared-over-two rows per date is how a small database becomes a large one.

    The Series header repeats on every entry. There are few of them, and it keeps an entry
    self-describing: its declared semantics travel with the matrix they give meaning to.
    """

    exposure_entry_id: int = Field(default=-1, description="(PK)")
    series_name: str = Field(description="(AK) Exposure Series this entry belongs to")
    as_of: date = Field(description="(AK) Date the matrix came into force")
    targets: str = Field(description="Fixed Target axis, comma separated")
    semantics: ExposureSemantics = Field(description="Declared meaning of the values")
    rows_sum_to_one: bool = Field(description="Whether rows are constrained to sum to one")
    matrix: ExposureMatrix = Field(description="The matrix itself, stored whole as JSON")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "ExposureEntry"


class RiskModelRow(DbModel["RiskModelRow"]):
    """A Risk Model's identity. Its recipe lives entirely in its revisions."""

    risk_model_id: int = Field(default=-1, description="(PK)")
    name: str = Field(description="(AK) Risk Model name")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "RiskModel"


class RiskModelRevisionRow(DbModel["RiskModelRevisionRow"]):
    """One recorded edit to a Risk Model, holding the whole recipe in force after it.

    The revision log is persisted because a Covariance Series holds its Risk Model rather than
    merely naming it, and because ADR-0005's Revision Stamp is uninterpretable without the
    parameters it points at. `RiskParameters` is decomposed into columns rather than stored as
    a payload: it is a flat, closed set of five fields, and a stamp resolving to a recipe is
    the one thing a reader of an old entry actually needs to query.
    """

    risk_model_revision_id: int = Field(default=-1, description="(PK)")
    model: int = Field(description="(FK:RiskModel.risk_model_id)(AK) Model the edit belongs to")
    revision: int = Field(description="(AK) Position in the revision sequence")
    changed_at: date = Field(description="Date the edit was made")
    changed: str = Field(description="Fields the edit touched, comma separated")
    estimator: Estimator = Field(description="Estimator in force after the edit")
    universe: str | None = Field(default=None, description="Universe reference")
    lookback: int | None = Field(default=None, description="Lookback, in return periods")
    frequency: ReturnFrequency | None = Field(default=None, description="Return frequency")
    source: str | None = Field(default=None, description="Source, for an external estimator")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "RiskModelRevision"


class CovarianceEntryRow(DbModel["CovarianceEntryRow"]):
    """One declared covariance: a matrix, a date, and the revision it was declared under.

    `revision` is ADR-0005's Revision Stamp. It is a plain integer rather than a foreign key to
    `RiskModelRevision`, because the pair `(model, revision)` already resolves there and a
    second path to the same fact is a second thing that can disagree.
    """

    covariance_entry_id: int = Field(default=-1, description="(PK)")
    model: int = Field(description="(FK:RiskModel.risk_model_id)(AK) Model that declared it")
    as_of: date = Field(description="(AK) Date the covariance is declared for")
    revision: int = Field(description="Risk Model revision it was produced under")
    matrix: SymmetricMatrix = Field(description="The matrix itself, stored whole as JSON")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "CovarianceEntry"


class UniverseDefinitionRow(DbModel["UniverseDefinitionRow"]):
    """A Universe Definition's identity. Its recipe lives in its revisions and member rows."""

    universe_definition_id: int = Field(default=-1, description="(PK)")
    name: str = Field(description="(AK) Universe Definition name")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "UniverseDefinition"


class UniverseRevisionRow(DbModel["UniverseRevisionRow"]):
    """One recorded edit to a Universe Definition.

    Only the rank rule is stored here. Inclusions and Exclusions are member rows, and the sets
    on `UniverseParameters` are their resolved projection -- storing both would be two places
    for one fact, and the member rows are the ones that carry provenance.
    """

    universe_revision_id: int = Field(default=-1, description="(PK)")
    definition: int = Field(
        description="(FK:UniverseDefinition.universe_definition_id)(AK) Definition edited"
    )
    revision: int = Field(description="(AK) Position in the revision sequence")
    changed_at: date = Field(description="Date the edit was made")
    changed: str = Field(description="Fields the edit touched, comma separated")
    enter_rank: int | None = Field(default=None, description="Rank at which a member is admitted")
    exit_rank: int | None = Field(default=None, description="Rank past which a member is dropped")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "UniverseRevision"


class UniverseMemberRow(DbModel["UniverseMemberRow"]):
    """One hand-pinned member, in the words it was typed in.

    Versioned by revision rather than by date -- the same interval trick as membership, one
    axis over -- so a revision need not copy the whole override set. Removal sets
    `removed_in_revision` instead of deleting, which is why the natural key carries
    `added_in_revision`: removing and re-adding one ticker is two records, not one rewritten.

    `instrument` is null while resolution has not succeeded. That null is the whole unresolved
    queue: there is no second table, because a second place to record the same fact is a second
    place for it to be wrong.
    """

    universe_member_id: int = Field(default=-1, description="(PK)")
    definition: int = Field(
        description="(FK:UniverseDefinition.universe_definition_id)(AK) Definition it belongs to"
    )
    role: MemberRole = Field(description="(AK) Whether the member is forced in or held out")
    source: Source = Field(description="(AK) Source the symbol was typed against")
    symbol_as_typed: str = Field(description="(AK) Symbol exactly as the researcher wrote it")
    added_in_revision: int = Field(description="(AK) Revision the member was added in")
    instrument: int | None = Field(
        default=None,
        description="(FK:Instrument.instrument_row_id) Instrument pinned at edit time",
    )
    removed_in_revision: int | None = Field(
        default=None, description="Revision the member was removed in"
    )

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "UniverseMember"


# Every table in the database, with the classification that decides whether a schema change
# must migrate it or may drop and backfill it. Later tickets add their tables here; this
# mapping is both the schema and the ADR-0008 classification, so the two cannot drift.
TABLES: dict[type[DbModel[Any]], Persistence] = {
    SchemaVersion: Persistence.META,
    InstrumentRow: Persistence.DURABLE,
    InstrumentReferenceRow: Persistence.DURABLE,
    TickerRecordRow: Persistence.DURABLE,
    UniverseDefinitionRow: Persistence.DURABLE,
    UniverseRevisionRow: Persistence.DURABLE,
    UniverseMemberRow: Persistence.DURABLE,
    ExposureEntryRow: Persistence.DURABLE,
    RiskModelRow: Persistence.DURABLE,
    RiskModelRevisionRow: Persistence.DURABLE,
    CovarianceEntryRow: Persistence.DURABLE,
}

SCHEMA = Schema(list(TABLES))


class Index(NamedTuple):
    """A secondary index, and the table it needs in the schema before it can be created."""

    table: str
    ddl: str


# The alternative keys lead with the Instrument, so neither supports a lookup that starts from
# what the caller actually has: a symbol, or an external identifier. These do.
INDEXES: tuple[Index, ...] = (
    Index(
        "TickerRecord",
        "CREATE INDEX IF NOT EXISTS TickerRecord_by_symbol "
        "ON TickerRecord (source, symbol, valid_from)",
    ),
    Index(
        "InstrumentReference",
        "CREATE INDEX IF NOT EXISTS InstrumentReference_by_value "
        "ON InstrumentReference (kind, value, qualifier)",
    ),
)

SCHEMA_VERSION = 1


class Migration(NamedTuple):
    """One ordered step, bringing a file up to `to_version`."""

    to_version: int
    apply: Callable[[sqlite3.Connection], None]


# Ordered, and empty: there is no prior schema to migrate from. Append steps with strictly
# increasing `to_version` as the schema changes, and raise `SCHEMA_VERSION` to match.
MIGRATIONS: list[Migration] = []


class Store:
    """The cointoss database, opened on a path and owning its connection.

    Opening creates whatever the schema needs and leaves the file at `version`, so there is no
    separate setup step. Use it as a context manager; the connection stays open until `close`,
    because a single owned connection is what makes the per-connection pragmas meaningful.

    `schema`, `version` and `migrations` are parameters rather than constants so that a test
    can drive the same open-and-check logic over a schema of its own.
    """

    path: Path
    conn: sqlite3.Connection
    schema: Schema
    version: int
    migrations: list[Migration]

    def __init__(
        self,
        path: Path,
        *,
        schema: Schema = SCHEMA,
        version: int = SCHEMA_VERSION,
        migrations: list[Migration] | None = None,
    ) -> None:
        self.path = path
        self.schema = schema
        self.version = version
        self.migrations = MIGRATIONS if migrations is None else migrations
        self.conn = self._connect()
        try:
            self._create_missing_tables()
            self._create_indexes()
            self._check_version()
        except BaseException:
            self.conn.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        # Neither pragma is set by lythonic. `foreign_keys` is per-connection and off by
        # default; `journal_mode` persists in the file but costs nothing to re-assert.
        execute_sql(cursor, "PRAGMA foreign_keys = ON")
        execute_sql(cursor, "PRAGMA journal_mode = WAL")
        cursor.close()
        return conn

    def _existing_tables(self) -> set[str]:
        cursor = self.conn.cursor()
        execute_sql(cursor, "SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in cursor.fetchall()}

    def _create_missing_tables(self) -> None:
        # `Schema.create_tables` issues unconditional CREATE TABLE, which fails on reopen, so
        # the store creates only what is absent.
        existing = self._existing_tables()
        cursor = self.conn.cursor()
        for table in self.schema.tables:
            if table.get_table_name() not in existing:
                execute_sql(cursor, table.create_ddl())
        self.conn.commit()

    def _create_indexes(self) -> None:
        cursor = self.conn.cursor()
        for index in INDEXES:
            if index.table in self.schema.table_map:
                execute_sql(cursor, index.ddl)
        self.conn.commit()

    def _stored_version(self) -> int | None:
        rows = SchemaVersion.select(self.conn)
        return max((row.version for row in rows), default=None)

    def _record_version(self, version: int) -> None:
        SchemaVersion(version=version, applied_at=datetime.now(UTC)).save(self.conn)
        self.conn.commit()

    def _check_version(self) -> None:
        stored = self._stored_version()
        if stored is None:
            self._record_version(self.version)
        elif stored > self.version:
            raise SchemaTooNew(
                f"{self.path} is at schema version {stored}, this build reads {self.version}"
            )
        elif stored < self.version:
            self._migrate(stored)

    def _migrate(self, stored: int) -> None:
        steps = sorted(
            (m for m in self.migrations if stored < m.to_version <= self.version),
            key=lambda m: m.to_version,
        )
        if not steps or steps[-1].to_version != self.version:
            reached = [m.to_version for m in steps]
            raise MigrationGap(
                f"{self.path} is at schema version {stored} and this build is at "
                f"{self.version}, but the steps available reach {reached}"
            )
        for step in steps:
            step.apply(self.conn)
            self._record_version(step.to_version)

    # -- Identity --

    def save_instrument(self, instrument: Instrument) -> None:
        """Write an Instrument and replace its External References and Ticker History.

        Idempotent on the Instrument Id. The child rows are rewritten wholesale rather than
        diffed: absorbing an Observation closes an open Ticker History record in place, and
        neither a reference nor a ticker record has an identity outside its Instrument.
        """
        row = self._instrument_row(instrument.id)
        fields = self._instrument_fields(instrument)
        row = InstrumentRow(**fields) if row is None else row.model_copy(update=fields)
        row.save(self.conn)
        pk = row.instrument_row_id
        InstrumentReferenceRow.delete(self.conn, instrument=pk)
        TickerRecordRow.delete(self.conn, instrument=pk)
        for ref in instrument.references:
            InstrumentReferenceRow(
                instrument=pk, kind=ref.kind, value=ref.value, qualifier=ref.qualifier
            ).save(self.conn)
        for record in instrument.ticker_history:
            TickerRecordRow(
                instrument=pk,
                source=record.source,
                symbol=record.symbol,
                scope=str(record.scope),
                valid_from=record.valid_from,
                valid_to=record.valid_to,
            ).save(self.conn)
        self.conn.commit()

    def save_instruments(self, instruments: Iterable[Instrument]) -> None:
        """Write several Instruments. Supersession targets need not be saved first."""
        for instrument in instruments:
            self.save_instrument(instrument)

    def load_instrument(self, instrument_id: InstrumentId | str) -> Instrument | None:
        """Load one Instrument exactly as stored, Supersession unfolded.

        The stored `superseded_by` is handed back as it is, because the rehydrated registry
        needs the relation itself; `resolve_symbol` is the read that folds it.
        """
        row = self._instrument_row(instrument_id)
        return None if row is None else self._instrument_from(row)

    def load_instruments(self) -> list[Instrument]:
        """Every stored Instrument in mint order, superseded ones included."""
        rows = sorted(InstrumentRow.select(self.conn), key=lambda r: r.mint_seq)
        return [self._instrument_from(row) for row in rows]

    def load_registry(self) -> InstrumentRegistry:
        """Rehydrate the registry, minting nothing.

        `mint_seq` is stored and restored, so the registry resumes counting where it left off
        and every id stays the id it was minted as. This is why ADR-0008 classifies the
        registry durable even though it is derived from source data.
        """
        return InstrumentRegistry(self.load_instruments())

    def resolve_symbol(
        self,
        source: Source,
        symbol: str,
        as_of: date,
        *,
        scope: str | None = None,
    ) -> InstrumentId | None:
        """Resolve a symbol one source reported on a date, as one indexed lookup.

        A miss returns None rather than the nearest thing: a broker CSV naming a symbol that
        nothing held on that date is a miss. Two Instruments that held the symbol over disjoint
        periods are separated by the date; an overlap within one source on one date is a
        genuine ambiguity and is refused, as it is in `InstrumentRegistry.resolve_symbol`.
        `scope` narrows a symbol that two authorities both issue.

        Supersession is folded here, so a late merge is answered from the stored relation
        rather than by rewriting the Ticker History rows.
        """
        sql = (
            "SELECT i.instrument_id FROM TickerRecord t "
            "JOIN Instrument i ON i.instrument_row_id = t.instrument "
            "WHERE t.source = ? AND t.symbol = ? "
            "AND t.valid_from <= ? AND (t.valid_to IS NULL OR t.valid_to > ?)"
        )
        stamp = as_of.isoformat()
        args: list[Any] = [source.value, normalize_symbol(symbol), stamp, stamp]
        if scope is not None:
            sql += " AND t.scope = ?"
            args.append(scope)
        cursor = self.conn.cursor()
        execute_sql(cursor, sql, args)
        found = {self._survivor_id(InstrumentId(row[0])) for row in cursor.fetchall()}
        if not found:
            return None
        if len(found) > 1:
            raise AmbiguousSymbol(f"{symbol} at {source} on {as_of}: {sorted(found)}")
        return next(iter(found))

    def _instrument_row(self, instrument_id: InstrumentId | str) -> InstrumentRow | None:
        return InstrumentRow.load_by_ak(self.conn, instrument_id=str(instrument_id))

    @staticmethod
    def _instrument_fields(instrument: Instrument) -> dict[str, Any]:
        return {
            "instrument_id": str(instrument.id),
            "type": instrument.type,
            "scope": str(instrument.scope),
            "symbol": instrument.symbol,
            "mint_seq": instrument.mint_seq,
            "minted_at": instrument.minted_at,
            "identity_source": instrument.identity_source,
            "price_sources": ",".join(s.value for s in instrument.price_sources),
            "name": instrument.name,
            "issuer_name": instrument.issuer_name,
            "figi_resolution": instrument.figi_resolution,
            "superseded_by": None
            if instrument.superseded_by is None
            else str(instrument.superseded_by),
        }

    def _instrument_from(self, row: InstrumentRow) -> Instrument:
        scope = Scope(row.type, row.scope)
        references = sorted(
            InstrumentReferenceRow.select(self.conn, instrument=row.instrument_row_id),
            key=lambda r: r.instrument_reference_id,
        )
        records = sorted(
            TickerRecordRow.select(self.conn, instrument=row.instrument_row_id),
            key=lambda r: r.ticker_record_id,
        )
        return Instrument(
            id=InstrumentId(row.instrument_id),
            type=row.type,
            scope=scope,
            symbol=row.symbol,
            mint_seq=row.mint_seq,
            minted_at=row.minted_at,
            identity_source=row.identity_source,
            price_sources=tuple(Source(s) for s in row.price_sources.split(",")),
            name=row.name,
            issuer_name=row.issuer_name,
            references=[ExternalReference(r.kind, r.value, r.qualifier) for r in references],
            figi_resolution=row.figi_resolution,
            superseded_by=None if row.superseded_by is None else InstrumentId(row.superseded_by),
            ticker_history=[
                TickerRecord(r.source, r.symbol, r.scope, r.valid_from, r.valid_to) for r in records
            ],
        )

    def _survivor_id(self, instrument_id: InstrumentId) -> InstrumentId:
        """Chase a stored Supersession chain to its fixed point."""
        seen: set[InstrumentId] = set()
        current = instrument_id
        while True:
            row = self._instrument_row(current)
            if row is None or row.superseded_by is None:
                return current
            if current in seen:
                raise SupersessionCycle(f"supersession cycle through {sorted(seen)}")
            seen.add(current)
            current = InstrumentId(row.superseded_by)

    def save_universe(
        self, definition: UniverseDefinition, members: Iterable[UniverseMemberRecord] = ()
    ) -> None:
        """Persist a Definition, its revision log and its pinned members.

        The member records are the system of record for Inclusions and Exclusions; the sets on
        `UniverseParameters` are their resolved projection and are not written separately. A
        member whose `instrument_id` is None is stored with a null pin rather than rejected, so
        a cold registry delays membership instead of blocking the edit.
        """
        definition_id = self._universe_definition_id(definition.name)
        if definition_id is None:
            row = UniverseDefinitionRow(name=definition.name)
            row.save(self.conn)
            definition_id = row.universe_definition_id
        held = {r.revision for r in UniverseRevisionRow.select(self.conn, definition=definition_id)}
        for revision in definition.revisions:
            if revision.revision in held:
                continue
            UniverseRevisionRow(
                definition=definition_id,
                revision=revision.revision,
                changed_at=revision.changed_at,
                changed=",".join(revision.changed),
                enter_rank=revision.parameters.enter_rank,
                exit_rank=revision.parameters.exit_rank,
            ).save(self.conn)
        for record in members:
            self._save_universe_member(definition_id, record)
        self.conn.commit()
        self._warn_on_contradicting_members(definition)

    def _warn_on_contradicting_members(self, definition: UniverseDefinition) -> None:
        """Warn where an Instrument is both included and excluded at the current revision.

        Exclusions are applied last, so the Instrument is dropped and the Inclusion has no
        effect. That is unambiguous to the code and ambiguous to a reader, who cannot tell a
        deliberate override from a forgotten one. Removing the Inclusion states the same
        outcome and silences the warning.
        """
        records = self.load_universe_members(definition.name)
        revision = definition.revision
        contested = sorted(
            resolved_members(records, MemberRole.INCLUSION, revision)
            & resolved_members(records, MemberRole.EXCLUSION, revision)
        )
        if contested:
            log.warning(
                "%s revision %d: %s both included and excluded, so excluded; "
                "remove the Inclusion to settle it",
                definition.name,
                revision,
                ", ".join(contested),
            )

    def load_universe(self, name: str) -> UniverseDefinition | None:
        """Rebuild a Definition, its Inclusions and Exclusions projected from the member rows.

        Each revision's parameters are assembled from that revision's rank rule and the members
        in force at it, so the reconstructed Definition says what it said at the time rather
        than what it says now.
        """
        definition_id = self._universe_definition_id(name)
        if definition_id is None:
            return None
        records = self.load_universe_members(name)
        rows = sorted(
            UniverseRevisionRow.select(self.conn, definition=definition_id),
            key=lambda r: r.revision,
        )
        return UniverseDefinition(
            name=name,
            revisions=tuple(
                UniverseDefinitionRevision(
                    revision=row.revision,
                    changed_at=row.changed_at,
                    changed=tuple(f for f in row.changed.split(",") if f),
                    parameters=UniverseParameters(
                        enter_rank=row.enter_rank,
                        exit_rank=row.exit_rank,
                        inclusions=resolved_members(records, MemberRole.INCLUSION, row.revision),
                        exclusions=resolved_members(records, MemberRole.EXCLUSION, row.revision),
                    ),
                )
                for row in rows
            ),
        )

    def load_universe_members(self, name: str) -> list[UniverseMemberRecord]:
        """Every member record ever added to a Definition, removed ones included."""
        definition_id = self._universe_definition_id(name)
        if definition_id is None:
            return []
        rows = sorted(
            UniverseMemberRow.select(self.conn, definition=definition_id),
            key=lambda r: (r.added_in_revision, r.symbol_as_typed),
        )
        return [self._member_from(row) for row in rows]

    def unresolved_members(self, name: str | None = None) -> list[UniverseMemberRecord]:
        """Members still awaiting an Instrument, across one Definition or all of them.

        The unresolved queue is this predicate, not a table: a member with no pin is the same
        fact as a member waiting to be resolved, and recording it twice invites disagreement.
        """
        filters: dict[str, Any] = {"instrument": None, "removed_in_revision": None}
        if name is not None:
            definition_id = self._universe_definition_id(name)
            if definition_id is None:
                return []
            filters["definition"] = definition_id
        return [self._member_from(row) for row in UniverseMemberRow.select(self.conn, **filters)]

    def _universe_definition_id(self, name: str) -> int | None:
        rows = UniverseDefinitionRow.select(self.conn, name=name)
        return rows[0].universe_definition_id if rows else None

    def _save_universe_member(self, definition_id: int, record: UniverseMemberRecord) -> None:
        """Insert or update one member row by its natural key. Does not commit."""
        instrument_row_id: int | None = None
        if record.instrument_id is not None:
            row = self._instrument_row(record.instrument_id)
            if row is None:
                raise UnknownInstrument(f"{record.instrument_id} is not stored")
            instrument_row_id = row.instrument_row_id
        UniverseMemberRow(
            definition=definition_id,
            role=record.role,
            source=record.source,
            symbol_as_typed=record.symbol_as_typed,
            added_in_revision=record.added_in_revision,
            instrument=instrument_row_id,
            removed_in_revision=record.removed_in_revision,
        ).save(self.conn)

    def _member_from(self, row: UniverseMemberRow) -> UniverseMemberRecord:
        instrument_id: InstrumentId | None = None
        if row.instrument is not None:
            stored = InstrumentRow.load_by_id(self.conn, row.instrument)
            if stored is not None:
                instrument_id = InstrumentId(stored.instrument_id)
        return UniverseMemberRecord(
            role=row.role,
            source=row.source,
            symbol_as_typed=row.symbol_as_typed,
            instrument_id=instrument_id,
            added_in_revision=row.added_in_revision,
            removed_in_revision=row.removed_in_revision,
        )

    def save_exposure_series(self, series: ExposureSeries) -> None:
        """Write every entry of an Exposure Series that is not already stored.

        Entries already held are left alone rather than rewritten, so a re-save is a no-op and
        an entry on an occupied date is refused. That matches `ExposureSeries` itself, whose
        `_check_dates` treats two entries claiming one date as an error rather than a
        replacement.
        """
        targets = ",".join(series.targets)
        for entry in series.entries:
            stored = ExposureEntryRow.select(self.conn, series_name=series.name, as_of=entry.as_of)
            if stored:
                if stored[0].matrix != entry.matrix:
                    raise DateOccupied(f"{series.name}: {entry.as_of} is already declared")
                continue
            ExposureEntryRow(
                series_name=series.name,
                as_of=entry.as_of,
                targets=targets,
                semantics=series.semantics,
                rows_sum_to_one=series.rows_sum_to_one,
                matrix=entry.matrix,
            ).save(self.conn)
        self.conn.commit()

    def load_exposure_series(self, name: str) -> ExposureSeries | None:
        """Rebuild an Exposure Series from its rows. A Series with no entries is unknowable."""
        rows = sorted(ExposureEntryRow.select(self.conn, series_name=name), key=lambda r: r.as_of)
        if not rows:
            return None
        header = rows[0]
        return ExposureSeries(
            name=name,
            targets=Universe(header.targets.split(",")) if header.targets else Universe(()),
            semantics=header.semantics,
            rows_sum_to_one=header.rows_sum_to_one,
            entries=tuple(DatedMatrix(as_of=r.as_of, matrix=r.matrix) for r in rows),
        )

    def save_covariance_series(self, series: CovarianceSeries) -> None:
        """Write a Covariance Series: its Risk Model, the revision log, and the declarations.

        The model is persisted whole because `CovarianceSeries` holds a `RiskModel` rather than
        naming one, and because a Revision Stamp resolves to nothing without the log behind it.
        """
        model_id = self._save_risk_model(series.model)
        for entry in series.entries:
            stored = CovarianceEntryRow.select(self.conn, model=model_id, as_of=entry.as_of)
            if stored:
                if stored[0].matrix != entry.matrix or stored[0].revision != entry.revision:
                    raise DateOccupied(f"{series.model.name}: {entry.as_of} is already declared")
                continue
            CovarianceEntryRow(
                model=model_id,
                as_of=entry.as_of,
                revision=entry.revision,
                matrix=entry.matrix,
            ).save(self.conn)
        self.conn.commit()

    def load_covariance_series(self, name: str) -> CovarianceSeries | None:
        """Rebuild a Covariance Series, model and revision log included."""
        model = self.load_risk_model(name)
        if model is None:
            return None
        model_id = self._risk_model_id(name)
        rows = sorted(CovarianceEntryRow.select(self.conn, model=model_id), key=lambda r: r.as_of)
        return CovarianceSeries(
            model=model,
            entries=tuple(
                DeclaredCovariance(as_of=r.as_of, revision=r.revision, matrix=r.matrix)
                for r in rows
            ),
        )

    def save_risk_model(self, model: RiskModel) -> None:
        """Persist a Risk Model and its revision log on its own, without any declarations."""
        self._save_risk_model(model)
        self.conn.commit()

    def load_risk_model(self, name: str) -> RiskModel | None:
        """Rebuild a Risk Model from its revision rows, in revision order."""
        model_id = self._risk_model_id(name)
        if model_id is None:
            return None
        rows = sorted(
            RiskModelRevisionRow.select(self.conn, model=model_id), key=lambda r: r.revision
        )
        return RiskModel(
            name=name,
            revisions=tuple(
                RiskModelRevision(
                    revision=r.revision,
                    changed_at=r.changed_at,
                    changed=tuple(f for f in r.changed.split(",") if f),
                    parameters=RiskParameters(
                        estimator=r.estimator,
                        universe=r.universe,
                        lookback=r.lookback,
                        frequency=r.frequency,
                        source=r.source,
                    ),
                )
                for r in rows
            ),
        )

    def _risk_model_id(self, name: str) -> int | None:
        rows = RiskModelRow.select(self.conn, name=name)
        return rows[0].risk_model_id if rows else None

    def _save_risk_model(self, model: RiskModel) -> int:
        """Upsert the model row and append revisions not yet stored. Does not commit."""
        model_id = self._risk_model_id(model.name)
        if model_id is None:
            row = RiskModelRow(name=model.name)
            row.save(self.conn)
            model_id = row.risk_model_id
        held = {r.revision for r in RiskModelRevisionRow.select(self.conn, model=model_id)}
        for revision in model.revisions:
            if revision.revision in held:
                continue
            parameters = revision.parameters
            RiskModelRevisionRow(
                model=model_id,
                revision=revision.revision,
                changed_at=revision.changed_at,
                changed=",".join(revision.changed),
                estimator=parameters.estimator,
                universe=parameters.universe,
                lookback=parameters.lookback,
                frequency=parameters.frequency,
                source=parameters.source,
            ).save(self.conn)
        return model_id

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
