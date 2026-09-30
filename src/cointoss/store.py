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
from cointoss.prices import (
    Bar,
    CorporateAction,
    CorporateActionKind,
    Restatement,
    as_utc,
    detect_restatements,
    is_provisional,
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
from cointoss.series import (
    DatedMatrix,
    DatedUniverse,
    EntryOrder,
    ExposureSemantics,
    ExposureSeries,
    NotYetStarted,
    UniverseSeries,
)
from cointoss.universe import (
    EvaluationRun,
    MemberRole,
    RunOutcome,
    UniverseDefinition,
    UniverseDefinitionRevision,
    UniverseMemberRecord,
    UniverseParameters,
    UnknownRevision,
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
    "BarRow",
    "CorporateActionRow",
    "CovarianceEntryRow",
    "EvaluationRunRow",
    "ExposureEntryRow",
    "Index",
    "InstrumentReferenceRow",
    "InstrumentRow",
    "Migration",
    "MigrationGap",
    "Persistence",
    "RestatementRow",
    "RiskModelRevisionRow",
    "RiskModelRow",
    "SchemaTooNew",
    "SchemaVersion",
    "Store",
    "StoreError",
    "UnknownInstrument",
    "UnknownUniverse",
    "TickerRecordRow",
    "UniverseDefinitionRow",
    "UniverseEntryRow",
    "UniverseMemberRow",
    "UniverseMembershipRow",
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


class UnknownUniverse(StoreError):
    """A Universe Definition name the store does not hold."""


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


class UniverseEntryRow(DbModel["UniverseEntryRow"]):
    """One Universe Series entry: a date the membership changed, and the revision behind it.

    The intervals say who was a member; this says when the Series moved and under which recipe.
    Both are needed. An entry that only closed intervals, and a first entry with nobody in it,
    have no interval row of their own to carry a Revision Stamp or mark where the Series
    begins, so without this row "nothing qualified" would read as "not yet started". It is
    durable for the same reason the intervals are: hysteresis makes neither re-derivable.
    """

    universe_entry_id: int = Field(default=-1, description="(PK)")
    definition: int = Field(
        description="(FK:UniverseDefinition.universe_definition_id)(AK) Definition it belongs to"
    )
    as_of: date = Field(description="(AK) Date the membership changed")
    revision: int = Field(description="Definition revision the entry was produced under")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "UniverseEntry"


class UniverseMembershipRow(DbModel["UniverseMembershipRow"]):
    """One Instrument's stretch inside a Universe Series. `valid_to` null means still a member.

    Half-open, `[valid_from, valid_to)`, so a member dropped on a date is absent on that date,
    matching `UniverseSeries.as_of`, whose entry is in force from its own date onward.
    """

    universe_membership_id: int = Field(default=-1, description="(PK)")
    definition: int = Field(
        description="(FK:UniverseDefinition.universe_definition_id)(AK) Definition it belongs to"
    )
    instrument: int = Field(description="(FK:Instrument.instrument_row_id)(AK) The member")
    valid_from: date = Field(description="(AK) First date the Instrument is a member")
    valid_to: date | None = Field(default=None, description="First date it no longer is")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "UniverseMembership"


class EvaluationRunRow(DbModel["EvaluationRunRow"]):
    """One execution of a Universe Definition. Liveness, not truth: see `EvaluationRun`."""

    evaluation_run_id: int = Field(default=-1, description="(PK)")
    definition: int = Field(
        description="(FK:UniverseDefinition.universe_definition_id)(AK) Definition evaluated"
    )
    run_at: datetime = Field(description="(AK) When the job executed")
    revision: int = Field(description="Definition revision it ran under")
    source_asof: date = Field(description="Date of the source data the membership is for")
    outcome: RunOutcome = Field(description="Whether a Series entry was written")
    n_admitted: int = Field(description="Instruments that joined")
    n_dropped: int = Field(description="Instruments that left")
    n_unresolved: int = Field(description="Standing members with no Instrument to evaluate")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "EvaluationRun"


class BarRow(DbModel["BarRow"]):
    """One session's prices for one Instrument, as one source last reported them.

    Source is part of the natural key rather than a tiebreak (ADR-0009), so two vendors
    disagreeing about a close each keep their row and the choice between them is made at read
    time through the Instrument's Price Sources.
    """

    bar_id: int = Field(default=-1, description="(PK)")
    instrument: int = Field(description="(FK:Instrument.instrument_row_id)(AK) Instrument priced")
    source: Source = Field(description="(AK) Source that reported the bar")
    bar_date: date = Field(description="(AK) Session date, in the venue's own calendar")
    close: float = Field(description="Close, the only price every source supplies")
    open: float | None = Field(default=None, description="Open, null for a close-only source")
    high: float | None = Field(default=None, description="High, null for a close-only source")
    low: float | None = Field(default=None, description="Low, null for a close-only source")
    adj_close: float | None = Field(default=None, description="Dividend-adjusted close")
    volume: float | None = Field(default=None, description="Traded volume")
    fetched_at: datetime = Field(description="When the stored vintage was fetched")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "Bar"


class CorporateActionRow(DbModel["CorporateActionRow"]):
    """One dividend or split, the only record of why a stored price history changed."""

    corporate_action_id: int = Field(default=-1, description="(PK)")
    instrument: int = Field(
        description="(FK:Instrument.instrument_row_id)(AK) Instrument the action applies to"
    )
    source: Source = Field(description="(AK) Source that reported the action")
    action_date: date = Field(description="(AK) First session at the new terms")
    kind: CorporateActionKind = Field(description="(AK) Dividend or split")
    value: float = Field(description="Amount per share for a dividend, ratio for a split")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "CorporateAction"


class RestatementRow(DbModel["RestatementRow"]):
    """One field of a stored Bar a re-fetch changed with no Corporate Action to explain it."""

    restatement_id: int = Field(default=-1, description="(PK)")
    instrument: int = Field(
        description="(FK:Instrument.instrument_row_id)(AK) Instrument whose bar changed"
    )
    source: Source = Field(description="(AK) Source whose bar changed")
    bar_date: date = Field(description="(AK) Session date of the changed bar")
    field: str = Field(description="(AK) Bar field that changed")
    detected_at: datetime = Field(description="(AK) Fetch time of the vintage that changed it")
    old: float | None = Field(default=None, description="Value before the re-fetch")
    new: float | None = Field(default=None, description="Value after the re-fetch")

    @classmethod
    @override
    def get_table_name(cls) -> str:
        return "Restatement"


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
    UniverseEntryRow: Persistence.DURABLE,
    UniverseMembershipRow: Persistence.DURABLE,
    EvaluationRunRow: Persistence.REBUILDABLE,
    ExposureEntryRow: Persistence.DURABLE,
    RiskModelRow: Persistence.DURABLE,
    RiskModelRevisionRow: Persistence.DURABLE,
    CovarianceEntryRow: Persistence.DURABLE,
    BarRow: Persistence.REBUILDABLE,
    CorporateActionRow: Persistence.REBUILDABLE,
    RestatementRow: Persistence.REBUILDABLE,
}

SCHEMA = Schema(list(TABLES))


class Index(NamedTuple):
    """A secondary index, and the table it needs in the schema before it can be created."""

    table: str
    ddl: str


# The alternative keys lead with the Instrument, so neither supports a lookup that starts from
# what the caller actually has: a symbol, or an external identifier. These do. Membership is
# the same shape: its key leads with the Definition then the Instrument, while a point read
# starts from a Definition and a date.
#
# `universes_containing` starts from an Instrument alone, which neither membership index leads
# with.
#
# Supersession is chased forwards through the Instrument's alternative key, but gathering
# everything superseded into a survivor walks `superseded_by` backwards, which nothing else
# indexes.
INDEXES: tuple[Index, ...] = (
    Index(
        "Instrument",
        "CREATE INDEX IF NOT EXISTS Instrument_by_superseded_by ON Instrument (superseded_by)",
    ),
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
    Index(
        "UniverseMembership",
        "CREATE INDEX IF NOT EXISTS UniverseMembership_by_date "
        "ON UniverseMembership (definition, valid_from, valid_to)",
    ),
    Index(
        "UniverseMembership",
        "CREATE INDEX IF NOT EXISTS UniverseMembership_by_instrument "
        "ON UniverseMembership (instrument, valid_from, valid_to)",
    ),
)

SCHEMA_VERSION = 1

# The ids of every Instrument superseded, directly or through a chain, into the survivor bound
# as the parameter, and the survivor's own. Seeded with the bare id rather than its row, since
# `superseded_by` may name a survivor not yet stored. Walks `superseded_by` backwards; UNION
# rather than UNION ALL keeps a cycle from recursing forever.
_FAMILY_CTE = (
    "WITH RECURSIVE family(instrument_id) AS (SELECT ? UNION "
    "SELECT i.instrument_id FROM Instrument i JOIN family f ON i.superseded_by = f.instrument_id)"
)


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

    def _require_instrument(self, instrument_id: InstrumentId | str) -> InstrumentRow:
        row = self._instrument_row(instrument_id)
        if row is None:
            raise UnknownInstrument(f"{instrument_id} is not stored")
        return row

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

    def _fold_ids(self, instrument_ids: Iterable[InstrumentId]) -> dict[InstrumentId, InstrumentId]:
        """Each distinct id mapped to its survivor, chasing each chain once."""
        return {i: self._survivor_id(i) for i in set(instrument_ids)}

    def _family(self, survivor: InstrumentId) -> dict[int, tuple[InstrumentId, int]]:
        """The stored Instruments that fold into `survivor`, by row id, with their mint order."""
        cursor = self.conn.cursor()
        execute_sql(
            cursor,
            f"{_FAMILY_CTE} SELECT i.instrument_row_id, i.instrument_id, i.mint_seq "
            "FROM family f JOIN Instrument i ON i.instrument_id = f.instrument_id",
            [str(survivor)],
        )
        return {pk: (InstrumentId(i), seq) for pk, i, seq in cursor.fetchall()}

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
            instrument_row_id = self._require_instrument(record.instrument_id).instrument_row_id
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

    # -- Universe Membership and Evaluation Runs, ADR-0007 and ADR-0008 --

    def record_membership(
        self,
        definition: str,
        when: date,
        members: Iterable[InstrumentId],
        *,
        revision: int | None = None,
        run_at: datetime | None = None,
    ) -> EvaluationRun:
        """Record what an evaluation produced for `when`, and that it ran.

        Never goes through `UniverseSeries.append`, but follows the same rule. The open
        intervals are diffed against `members`: the dropped are closed at `when`, the admitted
        opened from it, and a Series entry stamped with `revision` is written. A membership equal to
        the one in force writes no interval and no entry, only the Run, which is then the sole
        evidence the job is alive -- even under a newer revision, whose stamp then lives on the
        Run alone, as `UniverseSeries.append` keeps the stamp of the entry in force. The first
        evaluation always writes an entry, empty or not.

        The diff is taken after folding Supersession on both sides, so a member re-expressed
        under its survivor is not a change and dropping a survivor closes the intervals recorded
        under its predecessors. A newly admitted member's interval is opened under its survivor.

        `when` is the source data's as-of date; `run_at` is the wall clock and defaults to now.
        `revision` defaults to the Definition's latest.

        Refused with `EntryOrder`, and nothing written:

        - `when` before the latest Run or the latest entry. Hysteresis makes each evaluation
          depend on the membership before it, so a change slotted in behind a later run would
          contradict the input that run was computed from.
        - `when` equal to the latest entry's date with a different membership: two entries
          would claim one date. A repeat of the same result on that date is a heartbeat, and a
          change on a date only a quiet run has touched is accepted, as `append` accepts it.
        """
        definition_id = self._require_universe(definition)
        revisions = {
            r.revision for r in UniverseRevisionRow.select(self.conn, definition=definition_id)
        }
        stamp = max(revisions) if revision is None else revision
        if stamp not in revisions:
            raise UnknownRevision(f"{definition} has no revision {stamp}")
        wanted = self._survivor_row_ids(members)

        latest_entry = self._latest(UniverseEntryRow, "as_of", definition_id)
        latest_run = self._latest(EvaluationRunRow, "source_asof", definition_id)
        for latest in (latest_entry, latest_run):
            if latest is not None and when < latest:
                raise EntryOrder(f"{definition}: {when} precedes an evaluation for {latest}")

        # Open intervals grouped by survivor, so a member recorded under a since-superseded id
        # and now produced under its survivor is not a change.
        open_rows: dict[InstrumentId, list[UniverseMembershipRow]] = {}
        for row in UniverseMembershipRow.select(self.conn, definition=definition_id, valid_to=None):
            held = InstrumentRow.load_by_id(self.conn, row.instrument)
            assert held is not None  # the foreign key guarantees it
            survivor = self._survivor_id(InstrumentId(held.instrument_id))
            open_rows.setdefault(survivor, []).append(row)
        admitted = wanted.keys() - open_rows.keys()
        dropped = open_rows.keys() - wanted.keys()
        changed = latest_entry is None or bool(admitted or dropped)
        if changed and latest_entry == when:
            raise EntryOrder(f"{definition}: {when} already holds a different membership")

        run = EvaluationRun(
            definition=definition,
            revision=stamp,
            run_at=datetime.now(UTC) if run_at is None else run_at,
            source_asof=when,
            outcome=RunOutcome.CHANGED if changed else RunOutcome.UNCHANGED,
            n_admitted=len(admitted),
            n_dropped=len(dropped),
            n_unresolved=self._unresolved_at(definition, stamp),
        )
        try:
            for instrument in dropped:
                for row in open_rows[instrument]:
                    row.model_copy(update={"valid_to": when}).save(self.conn)
            for instrument in admitted:
                UniverseMembershipRow(
                    definition=definition_id, instrument=wanted[instrument], valid_from=when
                ).save(self.conn)
            if changed:
                UniverseEntryRow(definition=definition_id, as_of=when, revision=stamp).save(
                    self.conn
                )
            EvaluationRunRow(definition=definition_id, **self._run_fields(run)).save(self.conn)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return run

    def members_at(self, definition: str, when: date) -> Universe:
        """The membership of a universe on a date, as one indexed query.

        Builds no Series. A date before the first entry raises `NotYetStarted` rather than
        answering empty, so "not yet started" stays distinguishable from "nothing qualified".
        Members come back ordered by Instrument Id, since stored membership has no order of its
        own.

        Supersession is folded (ADR-0004): each member is reported as its survivor, once, however
        many of the recorded members turned out to be the same Instrument. The chase is a
        recursive step inside the same statement, seeded only from the members in force, so the
        read stays one indexed query. A chain that never ends raises `SupersessionCycle`.
        """
        stamp = when.isoformat()
        cursor = self.conn.cursor()
        # `chase` walks each member forwards along `superseded_by`; a row is terminal when its
        # id is not itself superseded, which also covers a survivor that is not stored. UNION
        # rather than UNION ALL is what stops a cycle from recursing forever.
        execute_sql(
            cursor,
            "WITH RECURSIVE chase(start, current) AS ("
            "SELECT i.instrument_id, i.instrument_id FROM UniverseDefinition d "
            "JOIN UniverseMembership m ON m.definition = d.universe_definition_id "
            "AND m.valid_from <= ? AND (m.valid_to IS NULL OR m.valid_to > ?) "
            "JOIN Instrument i ON i.instrument_row_id = m.instrument "
            "WHERE d.name = ? "
            "UNION "
            "SELECT c.start, i.superseded_by FROM chase c "
            "JOIN Instrument i ON i.instrument_id = c.current "
            "WHERE i.superseded_by IS NOT NULL) "
            "SELECT (SELECT MIN(e.as_of) FROM UniverseEntry e "
            "WHERE e.definition = d.universe_definition_id), c.start, c.current, "
            "NOT EXISTS (SELECT 1 FROM Instrument s "
            "WHERE s.instrument_id = c.current AND s.superseded_by IS NOT NULL) "
            "FROM UniverseDefinition d LEFT JOIN chase c "
            "WHERE d.name = ?",
            [stamp, stamp, definition, definition],
        )
        rows = cursor.fetchall()
        if not rows:
            raise UnknownUniverse(f"{definition} is not stored")
        started = rows[0][0]
        if started is None:
            raise NotYetStarted(f"{definition} has no entries")
        if when < date.fromisoformat(started):
            raise NotYetStarted(f"{definition} begins {started}, which is after {when}")
        starts = {start for _, start, _, _ in rows if start is not None}
        ended = {start: current for _, start, current, end in rows if start is not None and end}
        if starts - ended.keys():
            raise SupersessionCycle(f"supersession cycle through {sorted(starts - ended.keys())}")
        return Universe(sorted(set(ended.values())))

    def revision_at(self, definition: str, when: date) -> int:
        """The Definition revision the Series entry in force on `when` was produced under."""
        definition_id = self._require_universe(definition)
        cursor = self.conn.cursor()
        execute_sql(
            cursor,
            "SELECT revision FROM UniverseEntry WHERE definition = ? AND as_of <= ? "
            "ORDER BY as_of DESC LIMIT 1",
            [definition_id, when.isoformat()],
        )
        row = cursor.fetchone()
        if row is None:
            raise NotYetStarted(f"{definition} has no entry on or before {when}")
        return row[0]

    def load_evaluation_runs(self, definition: str) -> list[EvaluationRun]:
        """Every Evaluation Run of a Definition, in the order the jobs executed."""
        definition_id = self._require_universe(definition)
        rows = sorted(
            EvaluationRunRow.select(self.conn, definition=definition_id), key=lambda r: r.run_at
        )
        return [self._run_from(r, definition) for r in rows]

    def universes_containing(
        self, instrument_id: InstrumentId | str, start: date, end: date
    ) -> tuple[str, ...]:
        """Names of the universes the Instrument was a member of on any day of `[start, end]`.

        The cross-cutting read ADR-0008 shaped the storage around: one indexed range query over
        membership intervals, building no Series. Both ends are inclusive, so a single-day range
        is a point query and "in 2024" is `(date(2024, 1, 1), date(2024, 12, 31))`. Against the
        half-open `[valid_from, valid_to)` intervals, that is overlap when `valid_from <= end`
        and `valid_to > start`, an open interval reaching every later date.

        Names come back sorted, each once however often the Instrument rejoined. An unstored
        Instrument raises `UnknownInstrument`, so a typo is not read as "in none".

        Supersession is folded (ADR-0004): the Instrument is first chased to its survivor, and
        membership recorded under the survivor or anything superseded into it counts. Asking
        with a superseded id therefore gives the same answer as asking with its survivor.
        """
        if end < start:
            raise ValueError(f"range end {end} precedes its start {start}")
        survivor = self._survivor_id(
            InstrumentId(self._require_instrument(instrument_id).instrument_id)
        )
        cursor = self.conn.cursor()
        execute_sql(
            cursor,
            # CROSS JOIN pins the family as the outer loop; left to itself the planner may
            # scan every Definition instead of seeking membership by Instrument.
            f"{_FAMILY_CTE} SELECT DISTINCT d.name FROM family f "
            "CROSS JOIN Instrument i ON i.instrument_id = f.instrument_id "
            "CROSS JOIN UniverseMembership m ON m.instrument = i.instrument_row_id "
            "JOIN UniverseDefinition d ON d.universe_definition_id = m.definition "
            "WHERE m.valid_from <= ? AND (m.valid_to IS NULL OR m.valid_to > ?) ORDER BY d.name",
            [str(survivor), end.isoformat(), start.isoformat()],
        )
        return tuple(name for (name,) in cursor.fetchall())

    def load_universe_series(self, name: str) -> UniverseSeries:
        """The whole Universe Series of a Definition, as the value object.

        The expensive path: it reads every entry and every interval the Definition has, where
        `members_at` answers one date with one indexed query. Use it only where a caller is
        typed on `UniverseSeries`. Each entry's members are ordered by Instrument Id and folded
        to their survivors, as `members_at` returns them, and each entry carries the Revision
        Stamp it was recorded with. A Definition never evaluated loads as a Series with no
        entries, whose `as_of` raises `NotYetStarted`.
        """
        definition_id = self._require_universe(name)
        stamps = {
            row.as_of: row.revision
            for row in UniverseEntryRow.select(self.conn, definition=definition_id)
        }
        cursor = self.conn.cursor()
        execute_sql(
            cursor,
            "SELECT i.instrument_id, m.valid_from, m.valid_to FROM UniverseMembership m "
            "JOIN Instrument i ON i.instrument_row_id = m.instrument "
            "WHERE m.definition = ? ORDER BY i.instrument_id",
            [definition_id],
        )
        fetched = cursor.fetchall()
        fold = self._fold_ids(InstrumentId(row[0]) for row in fetched)
        intervals = [
            (
                fold[InstrumentId(instrument_id)],
                date.fromisoformat(start),
                None if end is None else date.fromisoformat(end),
            )
            for instrument_id, start, end in fetched
        ]
        entries = tuple(
            DatedUniverse(
                as_of=when,
                universe=Universe(
                    sorted(
                        {
                            instrument_id
                            for instrument_id, start, end in intervals
                            if start <= when and (end is None or when < end)
                        }
                    )
                ),
                revision=stamps[when],
            )
            for when in sorted(stamps)
        )
        return UniverseSeries(name=name, entries=entries)

    def _require_universe(self, name: str) -> int:
        definition_id = self._universe_definition_id(name)
        if definition_id is None:
            raise UnknownUniverse(f"{name} is not stored")
        return definition_id

    def _survivor_row_ids(self, members: Iterable[InstrumentId]) -> dict[InstrumentId, int]:
        """An evaluated membership folded to survivors, each with its surrogate key.

        Refuses any member not stored, and any survivor not stored, since a new interval can
        only be opened on a row.
        """
        found: dict[InstrumentId, int] = {}
        for instrument_id in members:
            self._require_instrument(instrument_id)
            survivor = self._survivor_id(InstrumentId(instrument_id))
            found[survivor] = self._require_instrument(survivor).instrument_row_id
        return found

    @staticmethod
    def _run_fields(run: EvaluationRun) -> dict[str, Any]:
        return run.model_dump(exclude={"definition"})

    @staticmethod
    def _run_from(row: EvaluationRunRow, definition: str) -> EvaluationRun:
        return EvaluationRun(
            definition=definition, **row.model_dump(exclude={"evaluation_run_id", "definition"})
        )

    def _latest(self, table: type[DbModel[Any]], column: str, definition_id: int) -> date | None:
        cursor = self.conn.cursor()
        execute_sql(
            cursor,
            f"SELECT MAX({column}) FROM {table.get_table_name()} WHERE definition = ?",
            [definition_id],
        )
        latest = cursor.fetchone()[0]
        return None if latest is None else date.fromisoformat(latest)

    def _unresolved_at(self, definition: str, revision: int) -> int:
        return sum(
            1
            for record in self.load_universe_members(definition)
            if record.instrument_id is None and record.covers(revision)
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

    # -- Bars, Corporate Actions and Restatements, ADR-0009 --

    def upsert_bars(
        self, bars: Iterable[Bar], actions: Iterable[CorporateAction] = ()
    ) -> list[Restatement]:
        """Write bars and the Corporate Actions fetched with them, returning what was restated.

        Idempotent on `(instrument, source, bar_date)`: a bar already held is overwritten by the
        incoming vintage rather than duplicated, so a backfill may overlap freely. Before a
        stored bar is overwritten it is compared through `detect_restatements`, with every
        action on file for that Instrument and source as the explanation, and each unexplained
        change is written as a Restatement. `actions` are written first, so a split arriving in
        the same fetch as the rescaled history explains it.

        Two overwrites are not compared. A stored bar that `is_provisional` was fetched before
        its session closed, so its replacement is the day finishing rather than history being
        rewritten; filing it would put a false Restatement in the log on every daily run. An
        incoming vintage older than the stored one is skipped, so replaying an old fetch cannot
        roll the current vintage back.

        Every Instrument must already be stored, or `UnknownInstrument` is raised and nothing
        is written.
        """
        bars = list(bars)
        actions = list(actions)
        pks = {
            instrument_id: self._require_instrument(instrument_id).instrument_row_id
            for instrument_id in {b.instrument_id for b in bars}
            | {a.instrument_id for a in actions}
        }
        explanations: dict[tuple[int, Source], list[CorporateAction]] = {}
        written: list[Restatement] = []
        try:
            for action in actions:
                self._save_corporate_action(pks[action.instrument_id], action)
            for incoming in bars:
                pk = pks[incoming.instrument_id]
                held = BarRow.select(
                    self.conn, instrument=pk, source=incoming.source, bar_date=incoming.bar_date
                )
                row = BarRow(instrument=pk, **self._bar_fields(incoming))
                if held:
                    stored = self._bar_from(held[0], incoming.instrument_id)
                    if as_utc(incoming.fetched_at) < as_utc(stored.fetched_at):
                        continue
                    if not is_provisional(stored):
                        key = (pk, incoming.source)
                        if key not in explanations:
                            explanations[key] = self._actions(pk, incoming)
                        for change in detect_restatements(stored, incoming, explanations[key]):
                            self._save_restatement(pk, change)
                            written.append(change)
                    row = held[0].model_copy(update=self._bar_fields(incoming))
                row.save(self.conn)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return written

    def bars_for(
        self,
        instrument_id: InstrumentId | str,
        start: date,
        end: date,
        *,
        source: Source | None = None,
    ) -> list[Bar]:
        """The bars for one Instrument from `start` to `end` inclusive, one per date.

        Each date is resolved separately through the Instrument's Price Sources: the first
        source holding a bar for that date supplies it, so a coin Yahoo lists only from its
        listing date still has its earlier history from CoinGecko. Each bar says which source
        it came from. A bar from a source outside the Price Sources is kept but never chosen.

        `source` bypasses the preference and reads that one source's bars, which is how a
        disagreement between vendors is inspected.

        Supersession is folded (ADR-0004). The Instrument is chased to its survivor, whose Price
        Sources decide, and bars stored under anything superseded into it are candidates too;
        every bar returned carries the survivor's id. Source preference is applied first, so a
        predecessor's bar from a preferred source beats the survivor's from a fallback one.
        Where two of them hold a bar for one date from one source, the survivor's own wins,
        then the earliest-minted predecessor's.
        """
        asked = self._require_instrument(instrument_id)
        survivor = self._require_instrument(self._survivor_id(InstrumentId(asked.instrument_id)))
        family = self._family(InstrumentId(survivor.instrument_id))
        preference = (
            (source,)
            if source is not None
            else tuple(Source(s) for s in survivor.price_sources.split(","))
        )
        rank = {s: i for i, s in enumerate(preference)}

        def precedence(held: BarRow) -> tuple[int, bool, int]:
            return (
                rank[held.source],
                held.instrument != survivor.instrument_row_id,
                family[held.instrument][1],
            )

        chosen: dict[date, BarRow] = {}
        for held in BarRow.select(
            self.conn,
            instrument=list(family),
            source=list(preference),
            gte__bar_date=start,
            lte__bar_date=end,
        ):
            current = chosen.get(held.bar_date)
            if current is None or precedence(held) < precedence(current):
                chosen[held.bar_date] = held
        instrument = InstrumentId(survivor.instrument_id)
        return [self._bar_from(chosen[day], instrument) for day in sorted(chosen)]

    def member_bars(self, definition: str, when: date) -> dict[InstrumentId, Bar | None]:
        """The resolved bar on `when` for every member of a universe on that date.

        Keys are survivors, as `members_at` returns them, and each bar is read through
        `bars_for`, so bars stored under a superseded id count. Every member is a key, and one
        with no bar from any of its Price Sources maps to None,
        so a gap in the aligned input is visible rather than silently narrowing the universe.
        A date before the universe's first entry raises `NotYetStarted`, as `members_at` does.
        """
        held: dict[InstrumentId, Bar | None] = {}
        for member in self.members_at(definition, when):
            instrument_id = InstrumentId(member)
            bars = self.bars_for(instrument_id, when, when)
            held[instrument_id] = bars[0] if bars else None
        return held

    def corporate_actions_for(
        self, instrument_id: InstrumentId | str, start: date, end: date
    ) -> list[CorporateAction]:
        """Every source's Corporate Actions for one Instrument, `start` to `end` inclusive."""
        row = self._require_instrument(instrument_id)
        held = CorporateActionRow.select(
            self.conn,
            instrument=row.instrument_row_id,
            gte__action_date=start,
            lte__action_date=end,
        )
        instrument = InstrumentId(row.instrument_id)
        return [
            self._action_from(r, instrument)
            for r in sorted(held, key=lambda r: (r.action_date, r.kind, r.source))
        ]

    def restatements_for(self, instrument_id: InstrumentId | str) -> list[Restatement]:
        """Every Restatement filed against one Instrument, in the order they were detected."""
        row = self._require_instrument(instrument_id)
        held = RestatementRow.select(self.conn, instrument=row.instrument_row_id)
        return [
            Restatement(
                instrument_id=InstrumentId(row.instrument_id),
                source=r.source,
                bar_date=r.bar_date,
                field=r.field,
                old=r.old,
                new=r.new,
                detected_at=r.detected_at,
            )
            for r in sorted(held, key=lambda r: r.restatement_id)
        ]

    def _actions(self, pk: int, bar: Bar) -> list[CorporateAction]:
        """The actions on file that can explain a change to `bar`: same Instrument and source."""
        return [
            self._action_from(r, bar.instrument_id)
            for r in CorporateActionRow.select(self.conn, instrument=pk, source=bar.source)
        ]

    def _save_corporate_action(self, pk: int, action: CorporateAction) -> None:
        """Insert or update one action by its natural key. Does not commit."""
        held = CorporateActionRow.select(
            self.conn,
            instrument=pk,
            source=action.source,
            action_date=action.action_date,
            kind=action.kind,
        )
        if held:
            row = held[0].model_copy(update={"value": action.value})
        else:
            row = CorporateActionRow(
                instrument=pk,
                source=action.source,
                action_date=action.action_date,
                kind=action.kind,
                value=action.value,
            )
        row.save(self.conn)

    def _save_restatement(self, pk: int, change: Restatement) -> None:
        RestatementRow(
            instrument=pk,
            source=change.source,
            bar_date=change.bar_date,
            field=change.field,
            detected_at=change.detected_at,
            old=change.old,
            new=change.new,
        ).save(self.conn)

    @staticmethod
    def _bar_fields(bar: Bar) -> dict[str, Any]:
        return bar.model_dump(exclude={"instrument_id"})

    @staticmethod
    def _bar_from(row: BarRow, instrument_id: InstrumentId) -> Bar:
        return Bar(instrument_id=instrument_id, **row.model_dump(exclude={"bar_id", "instrument"}))

    @staticmethod
    def _action_from(row: CorporateActionRow, instrument_id: InstrumentId) -> CorporateAction:
        return CorporateAction(
            instrument_id=instrument_id,
            source=row.source,
            action_date=row.action_date,
            kind=row.kind,
            value=row.value,
        )

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
