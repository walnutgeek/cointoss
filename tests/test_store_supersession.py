"""Supersession folded on read, ADR-0004 through `cointoss.store`.

A late resolution that finds two Instruments are one records `superseded_by` on the later one
and rewrites nothing. Every read that returns or aggregates by Instrument then answers in terms
of the survivor: membership reads map each member to its survivor and deduplicate, the
cross-cutting read treats an Instrument and everything superseded into it as one, and bars
stored under a predecessor are read as the survivor's. The evaluation diff folds too, so a
membership re-expressed under the survivor is not a change.

The fixture mints BTC first, so it is the one that survives; WBTC is the late duplicate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    SupersessionCycle,
)
from cointoss.prices import Bar
from cointoss.series import DatedUniverse, UniverseSeries
from cointoss.store import Store, UniverseMembershipRow
from cointoss.universe import RunOutcome, UniverseDefinition, UniverseParameters

D1, D2, D3, D4 = (date(2024, m, 1) for m in range(1, 5))
FETCHED = datetime(2024, 6, 1, tzinfo=UTC)


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


def supersede(store: Store, merged: InstrumentId, survivor: InstrumentId) -> None:
    instrument = store.load_instrument(merged)
    assert instrument is not None
    instrument.superseded_by = survivor
    store.save_instrument(instrument)


def define(store: Store, name: str = "top") -> None:
    store.save_universe(UniverseDefinition.create(name, UniverseParameters(), D1))


def bar(instrument: InstrumentId, source: Source, day: date, close: float) -> Bar:
    return Bar(
        instrument_id=instrument, source=source, bar_date=day, close=close, fetched_at=FETCHED
    )


# -- members_at and load_universe_series --


def test_a_member_recorded_before_the_merge_reads_as_its_survivor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, eth = coins(store, "BTC", "WBTC", "ETH")
        define(store)
        store.record_membership("top", D1, [wbtc, eth])
        supersede(store, wbtc, btc)
        assert store.members_at("top", D2) == Universe(sorted([btc, eth]))


def test_two_members_that_turn_out_to_be_one_are_listed_once(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        store.record_membership("top", D1, [btc, wbtc])
        supersede(store, wbtc, btc)
        assert store.members_at("top", D1) == Universe([btc])


def test_a_chain_is_chased_to_its_end(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, xbtc = coins(store, "BTC", "WBTC", "XBTC")
        define(store)
        store.record_membership("top", D1, [xbtc])
        supersede(store, xbtc, wbtc)
        supersede(store, wbtc, btc)
        assert store.members_at("top", D1) == Universe([btc])


def test_a_cycle_is_refused_rather_than_answered(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        store.record_membership("top", D1, [wbtc])
        supersede(store, wbtc, btc)
        supersede(store, btc, wbtc)
        with pytest.raises(SupersessionCycle):
            store.members_at("top", D1)


def test_the_folded_point_read_is_still_one_indexed_statement(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        store.record_membership("top", D1, [wbtc])
        supersede(store, wbtc, btc)
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        assert store.members_at("top", D2) == Universe([btc])
        store.conn.set_trace_callback(None)
        assert len(statements) == 1
        plan = store.conn.execute(f"EXPLAIN QUERY PLAN {statements[0]}").fetchall()
    assert any("UniverseMembership_by_date" in str(step) for step in plan)


def test_a_loaded_series_is_folded_like_the_point_read(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, eth = coins(store, "BTC", "WBTC", "ETH")
        define(store)
        store.record_membership("top", D1, [btc, wbtc])
        store.record_membership("top", D2, [wbtc, eth])
        supersede(store, wbtc, btc)
        series = store.load_universe_series("top")
        assert series == UniverseSeries(
            name="top",
            entries=(
                DatedUniverse(as_of=D1, universe=Universe([btc]), revision=1),
                DatedUniverse(as_of=D2, universe=Universe(sorted([btc, eth])), revision=1),
            ),
        )
        for day in (D1, D2, D3):
            assert store.members_at("top", day) == series.as_of(day)


# -- record_membership --


def test_re_expressing_a_member_under_its_survivor_is_not_a_change(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        store.record_membership("top", D1, [wbtc])
        supersede(store, wbtc, btc)
        run = store.record_membership("top", D2, [btc])
        assert (run.outcome, run.n_admitted, run.n_dropped) == (RunOutcome.UNCHANGED, 0, 0)
        assert store.load_universe_series("top").dates() == (D1,)


def test_dropping_a_survivor_closes_the_interval_held_under_its_predecessor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, eth = coins(store, "BTC", "WBTC", "ETH")
        define(store)
        store.record_membership("top", D1, [wbtc, eth])
        supersede(store, wbtc, btc)
        run = store.record_membership("top", D2, [eth])
        assert (run.n_admitted, run.n_dropped) == (0, 1)
        assert store.members_at("top", D2) == Universe([eth])
        open_rows = UniverseMembershipRow.select(store.conn, valid_to=None)
        assert len(open_rows) == 1


def test_a_predecessor_admitted_after_the_merge_is_recorded_as_its_survivor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        supersede(store, wbtc, btc)
        run = store.record_membership("top", D1, [wbtc, btc])
        assert run.n_admitted == 1
        assert store.members_at("top", D1) == Universe([btc])


# -- universes_containing --


def test_a_survivor_is_found_where_its_predecessor_was_recorded(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, eth = coins(store, "BTC", "WBTC", "ETH")
        define(store, "alpha")
        define(store, "beta")
        store.record_membership("alpha", D1, [wbtc])
        store.record_membership("beta", D1, [eth])
        supersede(store, wbtc, btc)
        assert store.universes_containing(btc, D1, D4) == ("alpha",)


def test_asking_with_a_predecessor_answers_for_its_survivor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store, "alpha")
        define(store, "beta")
        store.record_membership("alpha", D1, [btc])
        store.record_membership("beta", D1, [wbtc])
        supersede(store, wbtc, btc)
        assert store.universes_containing(wbtc, D1, D4) == ("alpha", "beta")
        assert store.universes_containing(btc, D1, D4) == ("alpha", "beta")


def test_the_folded_cross_cutting_read_is_still_served_by_the_index(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        define(store)
        store.record_membership("top", D1, [wbtc])
        supersede(store, wbtc, btc)
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        store.universes_containing(btc, D1, D4)
        store.conn.set_trace_callback(None)
        membership = [s for s in statements if "UniverseMembership" in s]
        assert len(membership) == 1
        plan = store.conn.execute(f"EXPLAIN QUERY PLAN {membership[0]}").fetchall()
    assert any("UniverseMembership_by_instrument" in str(step) for step in plan)


# -- bars_for and member_bars --


def test_bars_stored_under_a_predecessor_are_read_as_the_survivors(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        store.upsert_bars(
            [bar(wbtc, Source.COINGECKO, D1, 1.0), bar(btc, Source.COINGECKO, D2, 2.0)]
        )
        supersede(store, wbtc, btc)
        bars = store.bars_for(btc, D1, D4)
        assert [(b.instrument_id, b.bar_date, b.close) for b in bars] == [
            (btc, D1, 1.0),
            (btc, D2, 2.0),
        ]
        assert store.bars_for(wbtc, D1, D4) == bars


def test_on_one_date_and_source_the_survivors_own_bar_wins(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        store.upsert_bars(
            [bar(wbtc, Source.COINGECKO, D1, 1.5), bar(btc, Source.COINGECKO, D1, 1.0)]
        )
        supersede(store, wbtc, btc)
        assert [b.close for b in store.bars_for(btc, D1, D1)] == [1.0]


def test_price_source_order_decides_before_which_instrument_held_the_bar(db_path: Path):
    """A predecessor's Yahoo bar beats the survivor's CoinGecko bar: the survivor prefers Yahoo."""
    with Store(db_path) as store:
        btc, wbtc = coins(store, "BTC", "WBTC")
        store.upsert_bars([bar(wbtc, Source.YAHOO, D1, 1.5), bar(btc, Source.COINGECKO, D1, 1.0)])
        supersede(store, wbtc, btc)
        (chosen,) = store.bars_for(btc, D1, D1)
        assert (chosen.instrument_id, chosen.source, chosen.close) == (btc, Source.YAHOO, 1.5)


def test_member_bars_are_keyed_by_survivor(db_path: Path):
    with Store(db_path) as store:
        btc, wbtc, eth = coins(store, "BTC", "WBTC", "ETH")
        define(store)
        store.record_membership("top", D1, [wbtc, eth])
        store.upsert_bars([bar(wbtc, Source.COINGECKO, D2, 1.0)])
        supersede(store, wbtc, btc)
        held = store.member_bars("top", D2)
        assert set(held) == {btc, eth}
        assert held[eth] is None
        found = held[btc]
        assert found is not None
        assert (found.instrument_id, found.close) == (btc, 1.0)


def test_a_survivor_not_yet_stored_is_still_the_answer(db_path: Path):
    """`superseded_by` may name an Instrument saved later; reads fold to it all the same."""
    with Store(db_path) as store:
        (wbtc,) = coins(store, "WBTC")
        define(store)
        store.record_membership("top", D1, [wbtc])
        later = InstrumentId("crypto.native.btc")
        supersede(store, wbtc, later)
        assert store.members_at("top", D1) == Universe([later])
        assert store.universes_containing(wbtc, D1, D4) == ("top",)
