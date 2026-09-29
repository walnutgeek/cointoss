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

The schema starts with the meta table alone. Later tickets add their tables to `TABLES` and
nothing else changes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, NamedTuple, Self

from lythonic.state import DbModel, Schema, execute_sql
from pydantic import Field

__all__ = [
    "MIGRATIONS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TABLES",
    "Migration",
    "MigrationGap",
    "Persistence",
    "SchemaTooNew",
    "SchemaVersion",
    "Store",
    "StoreError",
]


class StoreError(Exception):
    """Something is wrong with the database file itself, not with the data in it."""


class SchemaTooNew(StoreError):
    """The file was written by a newer schema than this build knows how to read."""


class MigrationGap(StoreError):
    """No ordered path of migration steps leads from the stored version to this one."""


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


# Every table in the database, with the classification that decides whether a schema change
# must migrate it or may drop and backfill it. Later tickets add their tables here; this
# mapping is both the schema and the ADR-0008 classification, so the two cannot drift.
TABLES: dict[type[DbModel[Any]], Persistence] = {
    SchemaVersion: Persistence.META,
}

SCHEMA = Schema(list(TABLES))

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
