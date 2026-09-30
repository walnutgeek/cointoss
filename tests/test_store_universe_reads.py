"""Universe reads and the append-equivalence guard, ADR-0008, through `cointoss.store`.

`universes_containing` is the cross-cutting read that decided the storage shape; it is tested
for its interval-overlap semantics and for being served by an index. `load_universe_series` is
the expensive projection for callers typed on `UniverseSeries`.

The guard: the store's diff-and-close in `record_membership` and `UniverseSeries.append` are two
implementations of one rule. `test_the_store_is_the_fold` replays seeded random sequences through
both and asserts they agree -- on the loaded Series, on `members_at` at every date, on which
writes are refused, on the Run each write reports, and on `universes_containing` over random
ranges. Members are handed to the store in random order and to the fold sorted, because the
store keeps no order and projects members sorted by Instrument Id; the fold keeps caller order,
so only a sorted feed can be compared with `==`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
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
from cointoss.series import DatedUniverse, EntryOrder, NotYetStarted, SeriesError, UniverseSeries
from cointoss.store import Store, UnknownInstrument, UnknownUniverse
from cointoss.universe import RunOutcome, UniverseDefinition, UniverseParameters

D1, D2, D3, D4, D5 = (date(2024, m, 1) for m in range(1, 6))


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


def define(store: Store, name: str) -> None:
    store.save_universe(UniverseDefinition.create(name, UniverseParameters(), D1))


# -- universes_containing --


def test_a_range_finds_every_universe_that_held_the_instrument(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        for name in ("alpha", "beta", "gamma"):
            define(store, name)
        store.record_membership("alpha", D1, [btc, eth])
        store.record_membership("beta", D1, [eth])
        store.record_membership("gamma", D1, [btc])
        assert store.universes_containing(btc, D1, D5) == ("alpha", "gamma")
        assert store.universes_containing(eth, D1, D5) == ("alpha", "beta")


def test_both_ends_of_the_range_are_inclusive(db_path: Path):
    """Membership is `[valid_from, valid_to)`; the question is asked over whole days."""
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        define(store, "top")
        store.record_membership("top", D2, [btc])
        store.record_membership("top", D4, [eth])
        # Joined on D2: a range ending on D2 sees it, one ending the day before does not.
        assert store.universes_containing(btc, D1, D2) == ("top",)
        assert store.universes_containing(btc, D1, D2 - timedelta(days=1)) == ()
        # Left on D4: absent that day, present the day before.
        assert store.universes_containing(btc, D4, D5) == ()
        assert store.universes_containing(btc, D4 - timedelta(days=1), D5) == ("top",)
        # A single-day range is a point query.
        assert store.universes_containing(btc, D3, D3) == ("top",)


def test_an_open_interval_extends_to_any_later_date(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        define(store, "top")
        store.record_membership("top", D1, [btc])
        assert store.universes_containing(btc, date(2030, 1, 1), date(2030, 1, 1)) == ("top",)


def test_a_universe_is_named_once_however_often_the_instrument_rejoined(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        define(store, "top")
        store.record_membership("top", D1, [btc])
        store.record_membership("top", D2, [eth])
        store.record_membership("top", D3, [btc])
        assert store.universes_containing(btc, D1, D5) == ("top",)
        assert store.universes_containing(btc, D2, D3 - timedelta(days=1)) == ()


def test_an_instrument_in_no_universe_is_in_none(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        define(store, "top")
        store.record_membership("top", D1, [])
        assert store.universes_containing(btc, D1, D5) == ()


def test_an_unstored_instrument_is_refused(db_path: Path):
    with Store(db_path) as store:
        with pytest.raises(UnknownInstrument):
            store.universes_containing(InstrumentId("crypto.native.nosuch"), D1, D5)


def test_a_backwards_range_is_refused(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        with pytest.raises(ValueError, match="precedes"):
            store.universes_containing(btc, D5, D1)


def test_the_cross_cutting_read_is_served_by_an_index_on_the_instrument(db_path: Path):
    with Store(db_path) as store:
        (btc,) = coins(store, "BTC")
        define(store, "top")
        store.record_membership("top", D1, [btc])
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        store.universes_containing(btc, D1, D5)
        store.conn.set_trace_callback(None)
        membership = [s for s in statements if "UniverseMembership" in s]
        assert len(membership) == 1
        plan = store.conn.execute(f"EXPLAIN QUERY PLAN {membership[0]}").fetchall()
    assert any("UniverseMembership_by_instrument" in str(step) for step in plan)


# -- load_universe_series --


def test_a_stored_series_loads_as_the_value_object(db_path: Path):
    with Store(db_path) as store:
        btc, eth, sol = coins(store, "BTC", "ETH", "SOL")
        define(store, "top")
        store.record_membership("top", D1, [eth, btc])
        store.record_membership("top", D2, [btc, eth])
        store.record_membership("top", D3, [sol, btc])
        store.record_membership("top", D4, [])
        series = store.load_universe_series("top")
    assert series == UniverseSeries(
        name="top",
        entries=(
            DatedUniverse(as_of=D1, universe=Universe(sorted([btc, eth]))),
            DatedUniverse(as_of=D3, universe=Universe(sorted([btc, sol]))),
            DatedUniverse(as_of=D4, universe=Universe([])),
        ),
    )


def test_a_definition_never_evaluated_loads_as_an_empty_series(db_path: Path):
    with Store(db_path) as store:
        define(store, "top")
        series = store.load_universe_series("top")
    assert series == UniverseSeries(name="top")
    with pytest.raises(NotYetStarted):
        series.as_of(D1)


def test_loading_an_unknown_universe_is_refused(db_path: Path):
    with Store(db_path) as store, pytest.raises(UnknownUniverse):
        store.load_universe_series("never-defined")


def test_series_of_different_definitions_do_not_leak(db_path: Path):
    with Store(db_path) as store:
        btc, eth = coins(store, "BTC", "ETH")
        define(store, "a")
        define(store, "b")
        store.record_membership("a", D1, [btc])
        store.record_membership("b", D2, [eth])
        assert store.load_universe_series("a").dates() == (D1,)
        assert list(store.load_universe_series("b").as_of(D3)) == [eth]


# -- The append-equivalence guard --


@dataclass(frozen=True)
class Step:
    when: date
    members: tuple[InstrumentId, ...]


def generate(rng: random.Random, pool: list[InstrumentId], length: int) -> list[Step]:
    """A dated sequence of evaluations mixing every case the two implementations must agree on.

    Mostly moves forward by a few days, but also repeats a date (same-day reruns, both with the
    same and a different result), repeats the previous membership (no-change evaluations),
    produces empty memberships, lets members leave and rejoin, and occasionally steps backwards
    so the refusals are exercised too. Members come in random order.
    """
    steps: list[Step] = []
    when = D1
    members: list[InstrumentId] = []
    for _ in range(length):
        move = rng.random()
        if steps and move < 0.15:
            pass
        elif steps and move < 0.22:
            when -= timedelta(days=rng.randint(1, 4))
        else:
            when += timedelta(days=rng.randint(1, 4))
        shape = rng.random()
        if steps and shape < 0.3:
            pass
        elif shape < 0.4:
            members = []
        elif shape < 0.7 and members:
            flipped = rng.choice(pool)
            members = (
                [m for m in members if m != flipped]
                if flipped in members
                else [
                    *members,
                    flipped,
                ]
            )
        else:
            members = rng.sample(pool, rng.randint(0, len(pool)))
        rng.shuffle(members)
        steps.append(Step(when, tuple(members)))
    return steps


def fold_step(series: UniverseSeries, step: Step) -> UniverseSeries | None:
    """The fold's answer to one step, or None when `append` refuses it."""
    try:
        return series.append(step.when, sorted(step.members))
    except SeriesError:
        return None


def store_step(store: Store, name: str, step: Step) -> RunOutcome | None:
    try:
        return store.record_membership(name, step.when, step.members).outcome
    except EntryOrder:
        return None


def contained(series: UniverseSeries, instrument: InstrumentId, start: date, end: date) -> bool:
    """The oracle for `universes_containing`: a member on at least one day of `[start, end]`."""
    day = start
    while day <= end:
        try:
            if instrument in series.as_of(day):
                return True
        except NotYetStarted:
            pass
        day += timedelta(days=1)
    return False


SEEDS = range(40)


def test_the_store_is_the_fold(db_path: Path):
    """Every seeded sequence, through `record_membership` and through `append`, agrees."""
    with Store(db_path) as store:
        pool = coins(store, "BTC", "ETH", "SOL", "ADA", "DOT")
        series_by_name: dict[str, UniverseSeries] = {}
        latest_run: dict[str, date] = {}
        kinds = dict.fromkeys(
            ("heartbeat", "empty", "rejoin", "same_day", "same_day_conflict", "stricter"), 0
        )
        for seed in SEEDS:
            rng = random.Random(seed)
            name = f"u{seed}"
            define(store, name)
            series = UniverseSeries(name=name)
            ever: set[str] = set()
            for step in generate(rng, pool, rng.randint(10, 30)):
                before = series
                folded = fold_step(series, step)
                outcome = store_step(store, name, step)
                run_date = latest_run.get(name)
                if folded is not None and run_date is not None and step.when < run_date:
                    # The one documented divergence: the store refuses a date behind its latest
                    # Run even where no entry sits in between, since hysteresis made that run
                    # depend on the membership before it. The fold has no Runs to know about.
                    assert outcome is None, (seed, step)
                    kinds["stricter"] += 1
                    continue
                assert (folded is None) == (outcome is None), (seed, step)
                if folded is None:
                    if before.entries and step.when == before.entries[-1].as_of:
                        kinds["same_day_conflict"] += 1
                    continue
                latest_run[name] = step.when
                assert (outcome is RunOutcome.CHANGED) == (folded is not before), (seed, step)
                if outcome is RunOutcome.UNCHANGED:
                    kinds["heartbeat"] += 1
                if not step.members:
                    kinds["empty"] += 1
                if before.entries and step.when == before.entries[-1].as_of:
                    kinds["same_day"] += 1
                previous: set[str] = set(before.entries[-1].universe) if before.entries else set()
                if any(m not in previous and m in ever for m in step.members):
                    kinds["rejoin"] += 1
                ever |= set(step.members)
                series = folded
                assert store.load_universe_series(name) == series, (seed, step)
            series_by_name[name] = series

        # Every case the generator claims to cover actually occurred.
        assert all(count > 0 for count in kinds.values()), kinds

        for name, series in series_by_name.items():
            assert store.load_universe_series(name) == series
            if not series.entries:
                continue
            first, last = series.entries[0].as_of, series.entries[-1].as_of
            day = first - timedelta(days=2)
            while day <= last + timedelta(days=2):
                if day < first:
                    with pytest.raises(NotYetStarted):
                        store.members_at(name, day)
                else:
                    assert store.members_at(name, day) == series.as_of(day), (name, day)
                day += timedelta(days=1)

        rng = random.Random(-1)
        span = max(s.entries[-1].as_of for s in series_by_name.values() if s.entries) - D1
        for _ in range(200):
            instrument = rng.choice(pool)
            start = D1 - timedelta(days=3) + timedelta(days=rng.randint(0, span.days + 6))
            end = start + timedelta(days=rng.randint(0, 20))
            expected = tuple(
                sorted(n for n, s in series_by_name.items() if contained(s, instrument, start, end))
            )
            assert store.universes_containing(instrument, start, end) == expected, (
                instrument,
                start,
                end,
            )


def test_a_reordered_feed_folds_to_the_same_dates_and_sets(db_path: Path):
    """The fold keeps caller order, so only sets are comparable against a shuffled feed."""
    with Store(db_path) as store:
        pool = coins(store, "BTC", "ETH", "SOL", "ADA")
        for seed in range(10):
            rng = random.Random(seed)
            name = f"u{seed}"
            define(store, name)
            series = UniverseSeries(name=name)
            last_run: date | None = None
            for step in generate(rng, pool, 20):
                if last_run is not None and step.when < last_run:
                    continue
                try:
                    series = series.append(step.when, list(step.members))
                except SeriesError:
                    continue
                store.record_membership(name, step.when, step.members)
                last_run = step.when
            loaded = store.load_universe_series(name)
            assert loaded.dates() == series.dates()
            assert [set(e.universe) for e in loaded.entries] == [
                set(e.universe) for e in series.entries
            ]
