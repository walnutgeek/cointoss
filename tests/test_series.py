"""Time-versioning rules of ADR-0003, driven through the public seam of `cointoss.series`.

Every test builds a Series from a handful of dated entries and queries it at dates between, on
and outside them. Nothing here reaches into entry storage, ordering internals or the sparse
record layout -- that last one belongs to lythonic and is tested there.
"""

from __future__ import annotations

from datetime import date

import pytest
from lythonic.exposure import ExposureMatrix, ExposureMatrixBuilder
from lythonic.universe import Universe

from cointoss.series import (
    DatedUniverse,
    EntryOrder,
    ExposureSemantics,
    ExposureSeries,
    MissingPrice,
    NotYetStarted,
    RowSumViolation,
    SemanticsMismatch,
    TargetAxisMismatch,
    TargetOutsideAxis,
    UniverseSeries,
    series_covering,
)

Q1, Q2, Q3 = date(2024, 1, 1), date(2024, 4, 1), date(2024, 7, 1)
SECTORS = ["tech", "energy", "finance"]


def matrix(rows: dict[str, dict[str, float]], targets: list[str] = SECTORS) -> ExposureMatrix:
    """Build a matrix from subject-to-target-to-exposure rows."""
    builder = ExposureMatrixBuilder(targets=targets)
    for subject, exposures in rows.items():
        builder.set_exposures(subject, exposures)
    return builder.build()


def sectors(*, name: str = "sector", rows_sum_to_one: bool = False) -> ExposureSeries:
    """A percent sector Series over the standard target axis."""
    return ExposureSeries(
        name=name,
        targets=Universe(SECTORS),
        semantics=ExposureSemantics.PERCENT,
        rows_sum_to_one=rows_sum_to_one,
    )


# -- Step lookup --


def test_a_date_selects_the_entry_in_force() -> None:
    """On an entry, between two, and after the last: the step is backwards, never forwards."""
    series = UniverseSeries(name="midcap").append(Q1, ["AAPL"]).append(Q2, ["AAPL", "NVDA"])
    assert list(series.as_of(Q1)) == ["AAPL"]
    assert list(series.as_of(date(2024, 2, 15))) == ["AAPL"]
    assert list(series.as_of(Q2)) == ["AAPL", "NVDA"]
    assert list(series.as_of(date(2030, 1, 1))) == ["AAPL", "NVDA"]


def test_a_date_before_the_beginning_is_not_an_empty_scope() -> None:
    """An un-started Series and an empty one are different facts, and conflating them would
    make every downstream aggregate compute over nothing and report zero."""
    started_empty = UniverseSeries(name="midcap").append(Q1, [])
    assert list(started_empty.as_of(Q1)) == []
    with pytest.raises(NotYetStarted):
        started_empty.as_of(date(2023, 12, 31))
    with pytest.raises(NotYetStarted):
        UniverseSeries(name="never").as_of(Q1)


# -- Universe evolution --


def test_universe_admissions_and_drops_are_derived_not_stored() -> None:
    series = (
        UniverseSeries(name="midcap")
        .append(Q1, ["AAPL", "MSFT"])
        .append(Q2, ["AAPL", "NVDA", "TSLA"])
    )
    change = series.change(Q1, Q2)
    assert change.admitted == ("NVDA", "TSLA")
    assert change.dropped == ("MSFT",)
    assert series.dates() == (Q1, Q2)


# -- Exposure evolution --


def test_an_exposure_can_be_traced_across_dates() -> None:
    series = (
        sectors()
        .append(Q1, matrix({"AAPL": {"tech": 1.0}}))
        .append(Q2, matrix({"AAPL": {"tech": 0.7, "finance": 0.3}}))
    )
    assert series.as_of(Q1).exposure("AAPL", "finance") == 0.0
    assert series.trace("AAPL", "finance") == {Q1: 0.0, Q2: 0.3}
    changed = series.change(Q1, Q2)
    assert {(c.target, c.before, c.after) for c in changed} == {
        ("tech", 1.0, 0.7),
        ("finance", 0.0, 0.3),
    }


# -- Axis rules --


def test_a_target_outside_the_declared_axis_is_refused() -> None:
    """A Target must mean the same thing at every date; a new one is a new Series."""
    with pytest.raises(TargetOutsideAxis):
        sectors().append(Q1, matrix({"AAPL": {"crypto": 1.0}}, targets=["crypto"]))


def test_the_subject_axis_grows_without_disturbing_earlier_dates() -> None:
    series = (
        sectors()
        .append(Q1, matrix({"AAPL": {"tech": 1.0}}))
        .append(Q2, matrix({"AAPL": {"tech": 1.0}, "XOM": {"energy": 1.0}}))
    )
    assert "XOM" not in series.as_of(Q1).subjects
    assert series.as_of(Q2).exposure("XOM", "energy") == 1.0


def test_an_entry_is_cast_onto_the_declared_axis_so_dates_stay_comparable() -> None:
    """Every entry shares one Target axis, so two dates compare cell by cell without
    realigning them first."""
    series = sectors().append(Q1, matrix({"AAPL": {"tech": 1.0}}, targets=["tech"]))
    assert list(series.as_of(Q1).targets) == SECTORS


# -- Exposure Semantics --


def test_series_measuring_different_things_are_not_combined() -> None:
    percent = sectors().append(Q1, matrix({"AAPL": {"tech": 1.0}}))
    dollars = ExposureSeries(
        name="supplier", targets=Universe(SECTORS), semantics=ExposureSemantics.VALUE
    ).append(Q1, matrix({"AAPL": {"tech": 5.0}}))
    with pytest.raises(SemanticsMismatch):
        percent.require_comparable(dollars)
    other_axis = ExposureSeries(
        name="narrow", targets=Universe(["tech"]), semantics=ExposureSemantics.PERCENT
    )
    with pytest.raises(TargetAxisMismatch):
        percent.require_comparable(other_axis)
    percent.require_comparable(sectors())


def test_a_declared_sum_expectation_surfaces_a_violation() -> None:
    """Thematic tagging and dollar supplier exposure legitimately do not sum, so the check is
    opt-in and only the Series that declares it is held to it."""
    bad = matrix({"AAPL": {"tech": 0.7}})
    with pytest.raises(RowSumViolation):
        sectors(rows_sum_to_one=True).append(Q1, bad)
    assert sectors().append(Q1, bad).as_of(Q1).exposure("AAPL", "tech") == 0.7
    assert sectors(rows_sum_to_one=True).append(Q1, matrix({"AAPL": {"tech": 1.0}}))


# -- Instrument-valued targets --


def test_a_target_axis_of_instruments_behaves_like_any_other() -> None:
    """Supplier exposure puts instruments on the target axis; nothing about that is special."""
    suppliers = ExposureSeries(
        name="supplier",
        targets=Universe(["TSM", "QCOM"]),
        semantics=ExposureSemantics.VALUE,
    ).append(Q1, matrix({"AAPL": {"TSM": 4.2e10}}, targets=["TSM", "QCOM"]))
    assert suppliers.as_of(Q1).exposure("AAPL", "TSM") == 4.2e10
    assert suppliers.as_of(Q1).exposure("AAPL", "QCOM") == 0.0


# -- Entries --


def test_two_entries_cannot_claim_one_date() -> None:
    with pytest.raises(EntryOrder):
        UniverseSeries(
            name="midcap",
            entries=(
                DatedUniverse(as_of=Q1, universe=Universe(["AAPL"])),
                DatedUniverse(as_of=Q1, universe=Universe(["MSFT"])),
            ),
        )
    with pytest.raises(EntryOrder):
        UniverseSeries(name="midcap").append(Q2, ["AAPL"]).append(Q1, ["MSFT"])


def test_appending_leaves_the_original_readable_and_unchanged() -> None:
    first = UniverseSeries(name="midcap").append(Q1, ["AAPL"])
    second = first.append(Q2, ["AAPL", "NVDA"])
    assert first.dates() == (Q1,)
    assert second.dates() == (Q1, Q2)
    assert list(first.as_of(Q2)) == ["AAPL"]


def test_a_series_round_trips_through_serialization() -> None:
    """Both types serialize on their own, so persistence stays a later choice."""
    scope = UniverseSeries(name="midcap").append(Q1, ["AAPL"]).append(Q2, ["AAPL", "NVDA"])
    restored = UniverseSeries.model_validate_json(scope.model_dump_json())
    assert restored == scope
    assert list(restored.as_of(Q2)) == ["AAPL", "NVDA"]

    series = sectors().append(Q1, matrix({"AAPL": {"tech": 0.6, "finance": 0.4}}))
    back = ExposureSeries.model_validate_json(series.model_dump_json())
    assert back == series
    assert back.as_of(Q1).exposures_of("AAPL") == {"tech": 0.6, "finance": 0.4}


# -- Casting and portfolio exposure --


def test_casting_aligns_a_classification_to_the_scope_of_the_same_date() -> None:
    """Aligning to a scope from another date would compare different worlds."""
    scope = UniverseSeries(name="midcap").append(Q1, ["AAPL", "XOM"]).append(Q2, ["AAPL"])
    series = sectors().append(Q1, matrix({"AAPL": {"tech": 1.0}, "XOM": {"energy": 1.0}}))
    assert list(series.cast_to(scope, Q1).subjects) == ["AAPL", "XOM"]
    assert list(series.cast_to(scope, Q2).subjects) == ["AAPL"]


def test_portfolio_exposure_uses_the_classification_of_the_valuation_date() -> None:
    """A historical valuation uses its own era's view, not the latest one."""
    series = (
        sectors()
        .append(Q1, matrix({"AAPL": {"tech": 1.0}}))
        .append(Q2, matrix({"AAPL": {"tech": 0.5, "finance": 0.5}}))
    )
    positions, prices = {"AAPL": 10.0, "UNCLASSIFIED": 99.0}, {"AAPL": 20.0, "UNCLASSIFIED": 1.0}
    at_q1 = series.portfolio_exposure(positions, prices, Q1).to_dict()
    assert at_q1 == {"tech": 200.0, "energy": 0.0, "finance": 0.0}
    at_q3 = series.portfolio_exposure(positions, prices, Q3).to_dict()
    assert at_q3 == {"tech": 100.0, "energy": 0.0, "finance": 100.0}


def test_an_instrument_advertises_its_classifications_without_owning_them() -> None:
    covered = sectors().append(Q1, matrix({"AAPL": {"tech": 1.0}}))
    later = sectors(name="late").append(Q3, matrix({"AAPL": {"tech": 1.0}}))
    empty = sectors(name="empty").append(Q1, matrix({"XOM": {"energy": 1.0}}))
    assert series_covering("AAPL", [covered, later, empty], Q1) == ("sector",)
    assert series_covering("AAPL", [covered, later, empty], Q3) == ("sector", "late")


# -- Regressions the review surfaced --


def test_a_subject_absent_from_an_early_entry_still_traces() -> None:
    """The Subject axis grows, so a subject is absent from the entries that predate it. Those
    dates are a value, not an error."""
    series = (
        sectors()
        .append(Q1, matrix({"AAPL": {"tech": 1.0}}))
        .append(Q2, matrix({"AAPL": {"tech": 1.0}, "XOM": {"energy": 1.0}}))
    )
    assert series.trace("XOM", "energy") == {Q1: 0.0, Q2: 1.0}


def test_a_non_zero_cell_fill_does_not_produce_a_phantom_change() -> None:
    """Absent cells read as the matrix's own fill, so a Series filled with anything other than
    zero does not report every unmodelled subject as having changed."""
    filled = ExposureMatrixBuilder(targets=SECTORS, cell_fill=0.5)
    filled.set_exposures("AAPL", {"tech": 0.5, "energy": 0.5, "finance": 0.5})
    grown = ExposureMatrixBuilder(targets=SECTORS, cell_fill=0.5)
    grown.set_exposures("AAPL", {"tech": 0.5, "energy": 0.5, "finance": 0.5})
    grown.set_exposures("XOM", {"tech": 0.5, "energy": 0.5, "finance": 0.5})
    series = sectors().append(Q1, filled.build()).append(Q2, grown.build())
    assert series.change(Q1, Q2) == ()
    assert series.trace("XOM", "tech") == {Q1: 0.5, Q2: 0.5}


def test_a_classified_position_without_a_price_is_a_domain_error() -> None:
    series = sectors().append(Q1, matrix({"AAPL": {"tech": 1.0}}))
    with pytest.raises(MissingPrice):
        series.portfolio_exposure({"AAPL": 10.0}, {}, Q1)
