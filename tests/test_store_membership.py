"""Universe Membership and Evaluation Runs, ADR-0007 and ADR-0008, through `cointoss.store`.

An evaluation's membership is written as intervals that grow with change rather than with
dates, and every evaluation leaves an Evaluation Run whether or not it changed anything. The
tests drive `record_membership` and read back through `members_at`, `revision_at` and
`load_evaluation_runs`; they look at rows only where the guarantee is about decomposition
itself -- that a quiet evaluation writes no interval rows.

The store's diff-and-close and `UniverseSeries.append` are two implementations of one rule.
The parity test here replays one sequence through both; issue #13 owns the full property test.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from lythonic.universe import Universe

from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.series import EntryOrder, NotYetStarted, UniverseSeries
from cointoss.store import (
    TABLES,
    EvaluationRunRow,
    Persistence,
    Store,
    UniverseEntryRow,
    UniverseMembershipRow,
    UnknownInstrument,
    UnknownUniverse,
)
from cointoss.universe import (
    EvaluationRun,
    MemberRole,
    RunOutcome,
    UniverseDefinition,
    UniverseMemberRecord,
    UniverseParameters,
    UnknownRevision,
)

D1, D2, D3, D4 = date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2024, 4, 1)
RUN_AT = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def db_path():
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "cointoss.db"


def coins(store: Store, *symbols: str) -> list[InstrumentId]:
    """Store one native coin per symbol, so membership has real Instruments to point at."""
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


def band(name: str = "top") -> UniverseDefinition:
    return UniverseDefinition.create(name, UniverseParameters(enter_rank=2, exit_rank=3), D1)


def runs_at(start: datetime):
    """Distinct, increasing run times, as a scheduler would produce them."""
    moment = start
    while True:
        yield moment
        moment += timedelta(hours=1)


def test_a_first_evaluation_opens_the_series(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        run = store.record_membership("top", D1, [eth, btc], run_at=RUN_AT)
        assert store.members_at("top", D1) == Universe(sorted([btc, eth]))
    assert run == EvaluationRun(
        definition="top",
        revision=1,
        run_at=RUN_AT,
        source_asof=D1,
        outcome=RunOutcome.CHANGED,
        n_admitted=2,
        n_dropped=0,
        n_unresolved=0,
    )


def test_a_leaver_is_closed_and_a_joiner_opened(db_path: Path):
    with Store(db_path) as store:
        btc, eth, sol = coins(store, "BTC", "ETH", "SOL")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth])
        run = store.record_membership("top", D3, [btc, sol])
        assert (run.outcome, run.n_admitted, run.n_dropped) == (RunOutcome.CHANGED, 1, 1)
        assert store.members_at("top", D2) == Universe(sorted([btc, eth]))
        assert store.members_at("top", D3) == Universe(sorted([btc, sol]))
        assert store.members_at("top", D4) == Universe(sorted([btc, sol]))


def test_a_member_that_leaves_and_returns_is_two_intervals(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth])
        store.record_membership("top", D2, [btc])
        store.record_membership("top", D3, [btc, eth])
        assert eth not in store.members_at("top", D2)
        assert eth in store.members_at("top", D3)
        assert UniverseMembershipRow.select_count(store.conn) == 3


def test_an_unchanged_evaluation_writes_a_run_and_no_interval_rows(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth])
        intervals = UniverseMembershipRow.select_count(store.conn)
        entries = UniverseEntryRow.select_count(store.conn)
        quiet = [store.record_membership("top", D1 + timedelta(days=n), [eth, btc]) for n in (1, 2)]
        assert UniverseMembershipRow.select_count(store.conn) == intervals
        assert UniverseEntryRow.select_count(store.conn) == entries
        assert [r.outcome for r in quiet] == [RunOutcome.UNCHANGED] * 2
        assert [(r.n_admitted, r.n_dropped) for r in quiet] == [(0, 0)] * 2
        assert len(store.load_evaluation_runs("top")) == 3


def test_every_run_records_what_it_ran_under(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        definition = band().edited(D2, enter_rank=1)
        unresolved = UniverseMemberRecord(
            role=MemberRole.INCLUSION, source=Source.COINGECKO, symbol_as_typed="NOSUCH"
        )
        store.save_universe(definition, [unresolved])
        times = runs_at(RUN_AT)
        store.record_membership("top", D1, [btc, eth], revision=1, run_at=next(times))
        store.record_membership("top", D3, [btc], run_at=next(times))
        runs = store.load_evaluation_runs("top")
    assert [(r.revision, r.source_asof, r.run_at) for r in runs] == [
        (1, D1, RUN_AT),
        (2, D3, RUN_AT + timedelta(hours=1)),
    ]
    assert [(r.n_admitted, r.n_dropped, r.n_unresolved) for r in runs] == [(2, 0, 1), (0, 1, 1)]


def test_a_run_time_defaults_to_now(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band())
        before = datetime.now(UTC)
        run = store.record_membership("top", D1, [btc])
    assert before <= run.run_at <= datetime.now(UTC)


def test_each_entry_is_stamped_with_the_revision_it_was_produced_under(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band().edited(D2, enter_rank=1))
        store.record_membership("top", D1, [btc, eth], revision=1)
        store.record_membership("top", D2, [btc, eth], revision=2)
        store.record_membership("top", D3, [btc], revision=2)
        assert store.revision_at("top", D1) == 1
        # D2 changed nothing, so the entry in force is still the one produced under revision 1.
        assert store.revision_at("top", D2) == 1
        assert store.revision_at("top", D4) == 2


def test_the_latest_revision_is_the_default(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band().edited(D2, enter_rank=1))
        assert store.record_membership("top", D2, [btc]).revision == 2


def test_a_date_before_the_first_entry_is_refused(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band())
        with pytest.raises(NotYetStarted):
            store.members_at("top", D1)
        store.record_membership("top", D2, [btc])
        with pytest.raises(NotYetStarted):
            store.members_at("top", D1)
        with pytest.raises(NotYetStarted):
            store.revision_at("top", D1)


def test_an_empty_membership_is_a_fact(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band())
        first = store.record_membership("top", D1, [])
        assert first.outcome is RunOutcome.CHANGED
        assert store.members_at("top", D1) == Universe([])
        store.record_membership("top", D2, [btc])
        store.record_membership("top", D3, [])
        assert store.members_at("top", D4) == Universe([])
        assert store.revision_at("top", D1) == 1


def test_an_evaluation_dated_before_the_latest_run_is_refused(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc])
        store.record_membership("top", D3, [btc])
        with pytest.raises(EntryOrder):
            store.record_membership("top", D2, [btc, eth])
        assert store.members_at("top", D4) == Universe([btc])
        assert len(store.load_evaluation_runs("top")) == 2


def test_a_same_day_re_evaluation_with_the_same_result_is_a_heartbeat(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth])
        again = store.record_membership("top", D1, [eth, btc])
        assert again.outcome is RunOutcome.UNCHANGED
        assert len(store.load_evaluation_runs("top")) == 2


def test_a_same_day_re_evaluation_with_a_different_result_is_refused(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc])
        with pytest.raises(EntryOrder):
            store.record_membership("top", D1, [btc, eth])
        assert store.members_at("top", D1) == Universe([btc])


def test_a_same_day_change_after_a_quiet_run_is_accepted(db_path: Path):
    """The quiet run wrote no entry, so the date is unoccupied -- as it is for `append`."""
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc])
        store.record_membership("top", D2, [btc])
        store.record_membership("top", D2, [btc, eth])
        assert store.members_at("top", D2) == Universe(sorted([btc, eth]))


def test_an_unknown_universe_is_refused(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        with pytest.raises(UnknownUniverse):
            store.record_membership("never-defined", D1, [btc])
        with pytest.raises(UnknownUniverse):
            store.members_at("never-defined", D1)
        with pytest.raises(UnknownUniverse):
            store.load_evaluation_runs("never-defined")


def test_an_unstored_instrument_is_refused_and_nothing_is_written(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band())
        with pytest.raises(UnknownInstrument):
            store.record_membership("top", D1, [btc, InstrumentId("crypto.native.nosuch")])
        assert store.load_evaluation_runs("top") == []
        with pytest.raises(NotYetStarted):
            store.members_at("top", D1)


def test_an_unknown_revision_is_refused(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        store.save_universe(band())
        with pytest.raises(UnknownRevision):
            store.record_membership("top", D1, [btc], revision=2)


def test_membership_and_runs_survive_a_reopen(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth], run_at=RUN_AT)
        runs = store.load_evaluation_runs("top")
    with Store(db_path) as store:
        assert store.members_at("top", D2) == Universe(sorted([btc, eth]))
        assert store.load_evaluation_runs("top") == runs


def test_membership_is_read_in_one_statement(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc, eth])
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        store.members_at("top", D2)
        store.conn.set_trace_callback(None)
        assert len(statements) == 1
        plan = store.conn.execute(f"EXPLAIN QUERY PLAN {statements[0]}").fetchall()
    assert any("UniverseMembership_by_date" in str(step) for step in plan)


def test_intervals_are_durable_and_runs_rebuildable():
    assert TABLES[UniverseMembershipRow] is Persistence.DURABLE
    assert TABLES[UniverseEntryRow] is Persistence.DURABLE
    assert TABLES[EvaluationRunRow] is Persistence.REBUILDABLE


def test_the_store_agrees_with_the_value_type_fold(db_path: Path):
    """One sequence through `record_membership` and through `append`, read back at every date."""
    with Store(db_path) as store:
        btc, eth, sol = coins(store, "BTC", "ETH", "SOL")
        store.save_universe(band())
        sequence: list[tuple[date, list[InstrumentId]]] = [
            (D1, []),
            (D1 + timedelta(days=3), [btc, eth]),
            (D1 + timedelta(days=5), [btc, eth]),
            (D1 + timedelta(days=9), [btc, sol]),
            (D1 + timedelta(days=12), []),
            (D1 + timedelta(days=14), [eth]),
        ]
        series = UniverseSeries(name="top")
        for when, members in sequence:
            store.record_membership("top", when, members)
            series = series.append(when, sorted(members))
        assert len(series.entries) == 5
        for offset in range(20):
            when = D1 + timedelta(days=offset)
            assert store.members_at("top", when) == series.as_of(when)


def test_a_raw_insert_of_a_dangling_interval_is_rejected(db_path: Path):
    with Store(db_path) as store:
        store.save_universe(band())
        with pytest.raises(sqlite3.IntegrityError):
            UniverseMembershipRow(definition=1, instrument=999, valid_from=D1).save(store.conn)


def test_a_write_that_fails_part_way_leaves_nothing_behind(db_path: Path):
    """Two Runs of one Definition cannot share a run time; the intervals written first go too."""
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        store.save_universe(band())
        store.record_membership("top", D1, [btc], run_at=RUN_AT)
        with pytest.raises(sqlite3.IntegrityError):
            store.record_membership("top", D2, [eth], run_at=RUN_AT)
    with Store(db_path) as store:
        assert store.members_at("top", D3) == Universe([btc])
        assert store.revision_at("top", D3) == 1
