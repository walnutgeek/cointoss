"""Declaration rules of ADR-0005, driven through the public seam of `cointoss.risk`.

Every test declares entries, edits models, and reads back, asserting on returned matrices,
resolved parameters and refusals. Nothing here reaches into log storage, counter mechanics or
the triangle layout -- that last one belongs to lythonic and is tested there.

The refusals carry as much weight as the successes: three of this module's guarantees -- no
replacement, no unresolvable stamp, no silent correction -- are only observable as failures.
"""

from __future__ import annotations

from datetime import date

import pytest
from lythonic.symmetric import SymmetricMatrix, SymmetricMatrixBuilder

from cointoss.risk import (
    CorrectionRefused,
    CovarianceSeries,
    DateOccupied,
    DeclaredCovariance,
    EntryOrder,
    Estimator,
    NoEntry,
    RaggedRecipe,
    ReturnFrequency,
    RiskModel,
    RiskParameters,
    UnknownRevision,
)

BORN, Q1, Q2, Q3 = date(2024, 1, 1), date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30)


def covariance(
    variances: dict[str, float], pairs: dict[tuple[str, str], float] | None = None
) -> SymmetricMatrix:
    builder = SymmetricMatrixBuilder()
    builder.set_diagonal(variances)
    for (a, b), value in (pairs or {}).items():
        builder.set_value(a, b, value)
    return builder.build()


SAMPLE = RiskParameters(
    estimator=Estimator.SAMPLE, universe="midcap", lookback=60, frequency=ReturnFrequency.DAILY
)
VENDOR = RiskParameters(estimator=Estimator.EXTERNAL, source="acme-risk")


def series(parameters: RiskParameters = SAMPLE, name: str = "eq") -> CovarianceSeries:
    return CovarianceSeries(model=RiskModel.create(name, parameters, BORN))


# -- Declaring and reading --


def test_a_declared_covariance_reads_back_exactly() -> None:
    matrix = covariance({"AAPL": 0.04, "MSFT": 0.09}, {("AAPL", "MSFT"): 0.02})
    declared = series().declare(Q1, matrix).at(Q1)
    assert declared.diagonal() == {"AAPL": 0.04, "MSFT": 0.09}
    assert declared.value("MSFT", "AAPL") == 0.02
    assert declared == matrix


def test_lookup_is_exact_and_never_answers_from_an_earlier_date() -> None:
    """A step lookup would present a stale risk estimate as current, and recomputation is
    forbidden from ever fixing it."""
    risk = series().declare(Q1, covariance({"AAPL": 0.04}))
    assert risk.at(Q1).value("AAPL", "AAPL") == 0.04
    with pytest.raises(NoEntry):
        risk.at(Q2)
    assert risk.dates() == (Q1,)


def test_nearest_earlier_says_which_date_it_gave() -> None:
    """An approximation is always visible as one, because the date comes back with it."""
    risk = series().declare(Q1, covariance({"AAPL": 0.04}))
    when, matrix = risk.nearest_earlier(Q2)
    assert when == Q1
    assert matrix.value("AAPL", "AAPL") == 0.04
    with pytest.raises(NoEntry):
        risk.nearest_earlier(date(2023, 12, 31))


# -- Model edits and attribution --


def test_editing_a_model_leaves_declared_entries_untouched() -> None:
    risk = series().declare(Q1, covariance({"AAPL": 0.04}))
    edited = risk.edit_model(Q2, lookback=250)
    assert edited.at(Q1).value("AAPL", "AAPL") == 0.04
    assert edited.revision_at(Q1) == 1
    assert edited.parameters_at(Q1).lookback == 60
    assert edited.model.parameters.lookback == 250


def test_each_entry_resolves_to_the_recipe_in_force_when_it_was_declared() -> None:
    risk = (
        series()
        .declare(Q1, covariance({"AAPL": 0.04}))
        .edit_model(Q2, lookback=250)
        .declare(Q2, covariance({"AAPL": 0.05}))
        .edit_model(Q3, frequency=ReturnFrequency.WEEKLY)
        .declare(Q3, covariance({"AAPL": 0.06}))
    )
    assert [risk.revision_at(d) for d in (Q1, Q2, Q3)] == [1, 2, 3]
    assert [risk.parameters_at(d).lookback for d in (Q1, Q2, Q3)] == [60, 250, 250]
    assert risk.parameters_at(Q2).frequency is ReturnFrequency.DAILY
    assert risk.parameters_at(Q3).frequency is ReturnFrequency.WEEKLY


def test_revisions_increase_and_the_log_records_what_changed() -> None:
    model = (
        RiskModel.create("eq", SAMPLE, BORN)
        .edited(Q1, lookback=250)
        .edited(Q2, frequency=ReturnFrequency.WEEKLY, universe="largecap")
    )
    assert [r.revision for r in model.revisions] == [1, 2, 3]
    assert model.revisions[1].changed == ("lookback",)
    assert model.revisions[2].changed == ("frequency", "universe")
    assert model.revisions[1].changed_at == Q1


def test_an_edit_that_changes_nothing_is_not_recorded() -> None:
    """The log records real changes rather than save-button noise."""
    model = RiskModel.create("eq", SAMPLE, BORN)
    assert model.edited(Q1, lookback=60) is model
    assert model.edited(Q1, lookback=60, universe="midcap").revision == 1


def test_every_revision_stays_readable_after_later_edits() -> None:
    model = RiskModel.create("eq", SAMPLE, BORN).edited(Q1, lookback=250).edited(Q2, lookback=500)
    assert [model.parameters_at(n).lookback for n in (1, 2, 3)] == [60, 250, 500]
    with pytest.raises(UnknownRevision):
        model.parameters_at(4)


# -- Refusals --


def test_a_second_declaration_for_one_date_is_refused_and_names_the_date() -> None:
    risk = series().declare(Q1, covariance({"AAPL": 0.04}))
    with pytest.raises(DateOccupied, match=f"{Q1}.*revision 1"):
        risk.declare(Q1, covariance({"AAPL": 0.99}))
    assert risk.at(Q1).value("AAPL", "AAPL") == 0.04


def test_a_stamp_that_names_no_revision_is_refused() -> None:
    """A stamp is taken at declaration, so this is only reachable by hand-built input -- which
    is exactly the case that must not read back as though it were attributable."""
    with pytest.raises(UnknownRevision):
        CovarianceSeries(
            model=RiskModel.create("eq", SAMPLE, BORN),
            entries=(DeclaredCovariance(as_of=Q1, revision=7, matrix=covariance({"AAPL": 0.04})),),
        )


def test_out_of_order_entries_are_refused() -> None:
    with pytest.raises(EntryOrder):
        CovarianceSeries(
            model=RiskModel.create("eq", SAMPLE, BORN),
            entries=(
                DeclaredCovariance(as_of=Q2, revision=1, matrix=covariance({"AAPL": 0.05})),
                DeclaredCovariance(as_of=Q1, revision=1, matrix=covariance({"AAPL": 0.04})),
            ),
        )


def test_correcting_a_declared_covariance_fails_loudly() -> None:
    """The deferred decision must surface, not be quietly routed around."""
    risk = series().declare(Q1, covariance({"AAPL": 0.04}))
    with pytest.raises(CorrectionRefused, match="ADR-0005"):
        risk.correct(Q1, covariance({"AAPL": 0.05}))
    assert risk.at(Q1).value("AAPL", "AAPL") == 0.04


def test_an_edit_naming_no_such_parameter_is_refused() -> None:
    """A typo would otherwise reach a stored recipe, or fail with an unhelpful AttributeError."""
    model = RiskModel.create("eq", SAMPLE, BORN)
    with pytest.raises(RaggedRecipe, match="lookbcak"):
        model.edited(Q1, lookbcak=250)
    with pytest.raises(RaggedRecipe):
        model.edited(Q1, lookback=-5)
    assert model.parameters.lookback == 60


def test_an_edit_stores_the_validated_recipe() -> None:
    """model_copy skips validation, so storing its result would keep the raw value uncoerced."""
    edited = RiskModel.create("eq", SAMPLE, BORN).edited(Q1, frequency="weekly")
    assert edited.parameters.frequency is ReturnFrequency.WEEKLY


def test_a_computable_estimator_needs_its_whole_recipe() -> None:
    with pytest.raises(RaggedRecipe):
        RiskParameters(estimator=Estimator.SAMPLE, universe="midcap", lookback=60)
    with pytest.raises(RaggedRecipe):
        RiskParameters(estimator=Estimator.EXTERNAL)


# -- Series conventions --


def test_declaring_leaves_the_original_readable_and_unchanged() -> None:
    first = series().declare(Q1, covariance({"AAPL": 0.04}))
    second = first.declare(Q2, covariance({"AAPL": 0.05}))
    assert first.dates() == (Q1,)
    assert second.dates() == (Q1, Q2)
    with pytest.raises(NoEntry):
        first.at(Q2)


def test_a_series_round_trips_through_serialization() -> None:
    risk = (
        series()
        .declare(Q1, covariance({"AAPL": 0.04, "MSFT": 0.09}, {("AAPL", "MSFT"): 0.02}))
        .edit_model(Q2, lookback=250)
        .declare(Q2, covariance({"AAPL": 0.05, "MSFT": 0.08}))
    )
    back = CovarianceSeries.model_validate_json(risk.model_dump_json())
    assert back == risk
    assert back.at(Q1).value("AAPL", "MSFT") == 0.02
    assert back.parameters_at(Q1).lookback == 60
    assert back.parameters_at(Q2).lookback == 250


def test_a_declaration_out_of_date_order_still_lands_in_order() -> None:
    risk = series().declare(Q2, covariance({"AAPL": 0.05})).declare(Q1, covariance({"AAPL": 0.04}))
    assert risk.dates() == (Q1, Q2)
    assert risk.nearest_earlier(Q3)[0] == Q2


# -- Delegated to lythonic --


def test_a_matrix_that_is_not_positive_semi_definite_is_still_stored() -> None:
    """Definiteness is a query, never a construction check: losing the record of a bad matrix
    is worse than holding one."""
    bad = covariance({"A": 1.0, "B": 1.0}, {("A", "B"): 5.0})
    risk = series().declare(Q1, bad)
    assert risk.at(Q1).value("A", "B") == 5.0
    assert not risk.at(Q1).np.is_psd()


def test_an_entry_casts_onto_a_narrower_universe() -> None:
    matrix = covariance({"AAPL": 0.04, "MSFT": 0.09, "XOM": 0.16}, {("AAPL", "MSFT"): 0.02})
    narrowed = series().declare(Q1, matrix).at(Q1).cast(["AAPL", "MSFT"])
    assert list(narrowed.universe) == ["AAPL", "MSFT"]
    assert narrowed.value("AAPL", "MSFT") == 0.02


# -- Vendor imports --


def test_a_vendor_model_declares_and_attributes_like_any_other() -> None:
    """A missing lookback is expected for an external estimator, not an error."""
    risk = series(VENDOR, name="acme").declare(Q1, covariance({"AAPL": 0.04}))
    assert risk.parameters_at(Q1).source == "acme-risk"
    assert risk.parameters_at(Q1).lookback is None
    moved = risk.edit_model(Q2, source="acme-risk-v2").declare(Q2, covariance({"AAPL": 0.05}))
    assert moved.parameters_at(Q1).source == "acme-risk"
    assert moved.parameters_at(Q2).source == "acme-risk-v2"


def test_two_entries_can_be_compared_on_the_recipe_that_made_them() -> None:
    """Knowing two matrices came from different parameters is what says whether they are
    comparable at all."""
    risk = (
        series()
        .declare(Q1, covariance({"AAPL": 0.04}))
        .edit_model(Q2, lookback=250)
        .declare(Q2, covariance({"AAPL": 0.05}))
        .declare(Q3, covariance({"AAPL": 0.06}))
    )
    assert risk.parameters_at(Q1) != risk.parameters_at(Q2)
    assert risk.parameters_at(Q2) == risk.parameters_at(Q3)


def test_the_first_revision_records_the_fields_the_recipe_actually_has() -> None:
    """`changed` describes the value, not how the object was built, so a model read back from
    storage does not log every unset field as having changed."""
    computed = RiskModel.create("eq", SAMPLE, BORN)
    assert computed.revisions[0].changed == ("estimator", "frequency", "lookback", "universe")
    vendor = RiskModel.create("acme", VENDOR, BORN)
    assert vendor.revisions[0].changed == ("estimator", "source")
    restored = RiskModel.model_validate_json(vendor.model_dump_json())
    assert restored.revisions[0].changed == vendor.revisions[0].changed
