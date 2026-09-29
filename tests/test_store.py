"""Operating guarantees of ADR-0008, driven through the public seam of `cointoss.store`.

Every test opens a real `Store` on a real SQLite file in a temporary directory and asserts on
what an operator can observe: that opening twice works, that the pragmas the layout depends on
are actually in force, and that a version mismatch is a named refusal rather than a confusing
error. Connections are closed explicitly -- the store's context manager, or `closing` around a
raw one -- so Windows can delete the directory.

Foreign key enforcement cannot be shown against the real schema yet, because the schema is the
meta table alone until later tickets add theirs. It is shown instead over a throwaway parent
and child pair passed to `Store` as its schema, which drives exactly the same open path.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from lythonic.state import DbModel, Schema
from pydantic import Field

from cointoss.store import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    TABLES,
    Migration,
    MigrationGap,
    Persistence,
    SchemaTooNew,
    SchemaVersion,
    Store,
)


class Parent(DbModel["Parent"]):
    parent_id: int = Field(default=-1, description="(PK)")
    name: str = Field(description="(AK) Parent name")


class Child(DbModel["Child"]):
    child_id: int = Field(default=-1, description="(PK)")
    parent_id: int = Field(description="(FK:Parent.parent_id) Owning parent")


REFERENTIAL_SCHEMA = Schema([SchemaVersion, Parent, Child])


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


def test_a_row_referencing_a_missing_parent_is_rejected(db_path: Path):
    with Store(db_path, schema=REFERENTIAL_SCHEMA) as store:
        with pytest.raises(sqlite3.IntegrityError):
            Child(parent_id=404).save(store.conn)


def test_a_row_referencing_a_present_parent_is_accepted(db_path: Path):
    with Store(db_path, schema=REFERENTIAL_SCHEMA) as store:
        parent = Parent(name="anchor").save(store.conn)
        child = Child(parent_id=parent.parent_id).save(store.conn)
        assert Child.load_by_id(store.conn, child.child_id) == child


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
