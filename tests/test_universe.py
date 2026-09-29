"""Recipe and evaluation rules of ADR-0007, driven through the public seam of
`cointoss.universe`.

Every test builds a Definition, edits it, and evaluates it against a ranked list, asserting on
returned memberships, resolved parameters and refusals. Nothing here reaches into the revision
log's storage, the counter mechanics or the rank bookkeeping inside `evaluate`.

The refusals carry as much weight as the successes: a half-written rank rule and an exit rank
narrower than the entry rank are only observable as failures. An empty membership is
deliberately not among them -- an empty universe is a fact, and is asserted as a value.
"""

from __future__ import annotations

from datetime import date

import pytest

from cointoss.instrument import InstrumentId
from cointoss.universe import (
    MemberRole,
    RaggedRule,
    UniverseDefinition,
    UniverseError,
    UniverseMemberRecord,
    UniverseParameters,
    UnknownRevision,
    evaluate,
    resolved_members,
)

BORN, Q1, Q2 = date(2024, 1, 1), date(2024, 3, 31), date(2024, 6, 30)

BTC = InstrumentId("crypto.native.btc")
ETH = InstrumentId("crypto.eth.eth")
SOL = InstrumentId("crypto.native.sol")
XRP = InstrumentId("crypto.native.xrp")
WBTC = InstrumentId("crypto.eth.wbtc")
AAPL = InstrumentId("stock.us.aapl")
MSFT = InstrumentId("stock.us.msft")

TOP_THREE = UniverseParameters(enter_rank=3, exit_rank=5)
RANKED = (BTC, ETH, SOL, XRP, WBTC)


def definition(
    parameters: UniverseParameters = TOP_THREE, name: str = "crypto"
) -> UniverseDefinition:
    return UniverseDefinition.create(name, parameters, BORN)


# -- Recipes and revisions --


def test_a_definition_starts_at_revision_one_recording_what_it_was_given() -> None:
    made = definition()
    assert made.revision == 1
    assert made.revisions[0].changed_at == BORN
    assert made.revisions[0].changed == ("enter_rank", "exit_rank")
    assert made.parameters == TOP_THREE


def test_an_empty_definition_is_a_legitimate_starting_point() -> None:
    """Somewhere to put a list before it is known what goes in it."""
    empty = definition(UniverseParameters(), name="watchlist")
    assert empty.revisions[0].changed == ()
    assert empty.parameters.has_rule is False
    assert evaluate(empty.parameters, ()) == frozenset()


def test_every_edit_appends_a_revision_recording_what_changed() -> None:
    edited = (
        definition()
        .edited(Q1, enter_rank=10, exit_rank=12)
        .edited(Q2, inclusions=frozenset({AAPL}))
    )
    assert [r.revision for r in edited.revisions] == [1, 2, 3]
    assert edited.revisions[1].changed == ("enter_rank", "exit_rank")
    assert edited.revisions[1].changed_at == Q1
    assert edited.revisions[2].changed == ("inclusions",)


def test_the_parameters_at_a_past_revision_stay_recoverable() -> None:
    edited = definition().edited(Q1, enter_rank=10, exit_rank=12).edited(Q2, exit_rank=20)
    assert [edited.parameters_at(n).exit_rank for n in (1, 2, 3)] == [5, 12, 20]
    assert edited.parameters_at(1).enter_rank == 3
    with pytest.raises(UnknownRevision):
        edited.parameters_at(4)


def test_an_edit_that_changes_nothing_is_not_recorded() -> None:
    """The log records real changes rather than save-button noise."""
    made = definition()
    assert made.edited(Q1, enter_rank=3) is made
    assert made.edited(Q1, enter_rank=3, exit_rank=5).revision == 1


def test_an_edit_naming_no_such_parameter_is_refused() -> None:
    made = definition()
    with pytest.raises(RaggedRule, match="entre_rank"):
        made.edited(Q1, entre_rank=10)
    assert made.parameters.enter_rank == 3


def test_an_edit_stores_the_validated_recipe() -> None:
    """model_copy skips validation, so storing its result would keep the raw value uncoerced."""
    edited = definition().edited(Q1, inclusions={"stock.us.aapl"})
    assert edited.parameters.inclusions == frozenset({AAPL})
    assert isinstance(next(iter(edited.parameters.inclusions)), InstrumentId)


def test_a_definition_with_no_revisions_is_refused() -> None:
    with pytest.raises(UniverseError):
        UniverseDefinition(name="crypto", revisions=())


def test_a_revision_log_with_a_gap_is_refused() -> None:
    """Only reachable by hand-built input, which is exactly the case that must not read back as
    though `parameters_at` could answer for the missing revision."""
    made = definition().edited(Q1, exit_rank=6)
    with pytest.raises(UniverseError, match="1..n"):
        UniverseDefinition(name="crypto", revisions=(made.revisions[1],))


def test_a_definition_round_trips_through_serialization() -> None:
    made = definition().edited(Q1, inclusions=frozenset({AAPL, MSFT}), exclusions=frozenset({WBTC}))
    back = UniverseDefinition.model_validate_json(made.model_dump_json())
    assert back == made
    assert back.parameters_at(1).inclusions == frozenset()
    assert back.parameters.exclusions == frozenset({WBTC})


# -- Refusals on the recipe itself --


def test_a_rank_rule_is_present_in_whole_or_not_at_all() -> None:
    with pytest.raises(RaggedRule, match="exit_rank"):
        UniverseParameters(enter_rank=100)
    with pytest.raises(RaggedRule, match="enter_rank"):
        UniverseParameters(exit_rank=120)
    assert UniverseParameters().has_rule is False


def test_an_exit_rank_narrower_than_the_entry_rank_is_refused() -> None:
    """A narrower exit than entry is a band that ejects what it just admitted."""
    with pytest.raises(RaggedRule, match="narrower"):
        UniverseParameters(enter_rank=120, exit_rank=100)
    assert UniverseParameters(enter_rank=100, exit_rank=100).has_rule is True


def test_ranks_are_one_based_so_a_zero_entry_rank_is_refused() -> None:
    with pytest.raises(RaggedRule, match="1-based"):
        UniverseParameters(enter_rank=0, exit_rank=10)


def test_an_edit_that_half_removes_a_rule_is_refused() -> None:
    with pytest.raises(RaggedRule):
        definition().edited(Q1, exit_rank=None)


# -- Composition --


def test_inclusions_are_added_to_the_rule_output() -> None:
    """Forcing in something the rule would not admit."""
    forced = UniverseParameters(enter_rank=2, exit_rank=2, inclusions=frozenset({XRP}))
    assert evaluate(forced, RANKED) == frozenset({BTC, ETH, XRP})


def test_exclusions_are_removed_from_the_rule_output() -> None:
    """Forcing out a wrapped, bridged or otherwise redundant asset."""
    filtered = UniverseParameters(enter_rank=5, exit_rank=5, exclusions=frozenset({WBTC}))
    assert evaluate(filtered, RANKED) == frozenset({BTC, ETH, SOL, XRP})


def test_an_instrument_both_included_and_excluded_resolves_to_excluded() -> None:
    """Exclusions are applied last, so composition has one defined answer rather than an
    ordering accident."""
    both = UniverseParameters(
        enter_rank=1, exit_rank=1, inclusions=frozenset({WBTC}), exclusions=frozenset({WBTC})
    )
    assert evaluate(both, RANKED) == frozenset({BTC})


def test_an_exclusion_holds_while_the_rule_would_not_have_admitted_it_anyway() -> None:
    """A standing exclusion still applies when the coin later crosses back into the band."""
    policy = UniverseParameters(enter_rank=2, exit_rank=2, exclusions=frozenset({WBTC}))
    assert evaluate(policy, (BTC, ETH, WBTC)) == frozenset({BTC, ETH})
    assert evaluate(policy, (WBTC, BTC, ETH)) == frozenset({BTC})


def test_an_exclusion_beats_prior_membership() -> None:
    policy = UniverseParameters(enter_rank=3, exit_rank=5, exclusions=frozenset({ETH}))
    assert evaluate(policy, RANKED, previous={BTC, ETH, SOL}) == frozenset({BTC, SOL})


# -- Hysteresis --


def test_a_coin_inside_the_entry_rank_is_admitted() -> None:
    assert evaluate(TOP_THREE, RANKED) == frozenset({BTC, ETH, SOL})


def test_a_coin_between_the_two_ranks_is_retained_but_not_admitted() -> None:
    """The one rule that makes a boundary coin an event rather than daily noise."""
    slipped = (BTC, ETH, SOL, XRP, WBTC)
    assert evaluate(TOP_THREE, slipped, previous={WBTC}) == frozenset({BTC, ETH, SOL, WBTC})
    assert evaluate(TOP_THREE, slipped, previous=()) == frozenset({BTC, ETH, SOL})


def test_a_coin_past_the_exit_rank_is_dropped_however_long_it_was_a_member() -> None:
    fallen = (BTC, ETH, SOL, XRP, WBTC, MSFT)
    assert evaluate(TOP_THREE, fallen, previous={MSFT}) == frozenset({BTC, ETH, SOL})


def test_a_coin_absent_from_the_ranking_is_dropped_regardless_of_prior_membership() -> None:
    """A coin no source ranks has no rank to be inside a band."""
    assert evaluate(TOP_THREE, (BTC, ETH), previous={SOL}) == frozenset({BTC, ETH})


def test_an_absent_coin_survives_only_when_it_is_a_standing_inclusion() -> None:
    pinned = UniverseParameters(enter_rank=3, exit_rank=5, inclusions=frozenset({SOL}))
    assert evaluate(pinned, (BTC, ETH), previous={SOL}) == frozenset({BTC, ETH, SOL})


def test_hysteresis_reads_across_a_sequence_of_evaluations() -> None:
    """A coin enters, holds between the two ranks, then leaves -- three evaluations, two
    events."""
    day_one = evaluate(TOP_THREE, (BTC, ETH, XRP, SOL))
    assert day_one == frozenset({BTC, ETH, XRP})
    day_two = evaluate(TOP_THREE, (BTC, ETH, SOL, XRP), previous=day_one)
    assert day_two == frozenset({BTC, ETH, SOL, XRP})
    day_three = evaluate(TOP_THREE, (BTC, ETH, SOL, WBTC, MSFT, XRP), previous=day_two)
    assert day_three == frozenset({BTC, ETH, SOL})


# -- The stock universe, and emptiness --


def test_a_definition_with_no_rule_evaluates_to_its_inclusions_alone() -> None:
    stocks = UniverseParameters(inclusions=frozenset({AAPL, MSFT}))
    assert evaluate(stocks, ()) == frozenset({AAPL, MSFT})
    assert evaluate(stocks, RANKED, previous={BTC}) == frozenset({AAPL, MSFT})


def test_an_empty_result_is_returned_rather_than_raised() -> None:
    assert evaluate(TOP_THREE, ()) == frozenset()
    assert evaluate(UniverseParameters(), (), previous={AAPL}) == frozenset()
    everything_out = UniverseParameters(enter_rank=3, exit_rank=3, exclusions=frozenset(RANKED))
    assert evaluate(everything_out, RANKED) == frozenset()


def test_a_duplicated_ranking_entry_counts_at_its_best_rank() -> None:
    """Defensive: a source that lists one coin twice must not decide membership by which copy
    was read last."""
    assert evaluate(TOP_THREE, (BTC, ETH, SOL, XRP, BTC)) == frozenset({BTC, ETH, SOL})


# -- Member records and their projection --


def test_a_member_record_keeps_the_symbol_as_typed_alongside_what_it_pinned_to() -> None:
    record = UniverseMemberRecord(
        role=MemberRole.INCLUSION,
        source="yahoo",
        symbol_as_typed="BRK-B",
        instrument_id=InstrumentId("stock.us.brk_b"),
    )
    assert record.symbol_as_typed == "BRK-B"
    assert record.source == "yahoo"
    assert record.is_resolved


def test_an_unresolved_member_is_visibly_unresolved_and_never_reaches_the_parameters() -> None:
    """ "Not yet identified" stays distinguishable from "not in the universe"."""
    records = [
        UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source="yahoo",
            symbol_as_typed="AAPL",
            instrument_id=AAPL,
        ),
        UniverseMemberRecord(role=MemberRole.INCLUSION, source="yahoo", symbol_as_typed="NOSUCH"),
    ]
    assert records[1].is_resolved is False
    resolved = resolved_members(records, MemberRole.INCLUSION, 1)
    assert resolved == frozenset({AAPL})
    assert evaluate(UniverseParameters(inclusions=resolved), ()) == frozenset({AAPL})


def test_removing_and_re_adding_one_ticker_stays_two_recorded_events() -> None:
    records = [
        UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source="yahoo",
            symbol_as_typed="AAPL",
            instrument_id=AAPL,
            added_in_revision=1,
            removed_in_revision=2,
        ),
        UniverseMemberRecord(
            role=MemberRole.INCLUSION,
            source="yahoo",
            symbol_as_typed="AAPL",
            instrument_id=AAPL,
            added_in_revision=3,
        ),
    ]
    at = [resolved_members(records, MemberRole.INCLUSION, n) for n in (1, 2, 3)]
    assert at == [frozenset({AAPL}), frozenset(), frozenset({AAPL})]
    assert len(records) == 2


def test_the_projection_separates_the_two_roles() -> None:
    records = [
        UniverseMemberRecord(
            role=MemberRole.INCLUSION, source="cg", symbol_as_typed="xrp", instrument_id=XRP
        ),
        UniverseMemberRecord(
            role=MemberRole.EXCLUSION, source="cg", symbol_as_typed="wbtc", instrument_id=WBTC
        ),
    ]
    parameters = UniverseParameters(
        enter_rank=5,
        exit_rank=5,
        inclusions=resolved_members(records, MemberRole.INCLUSION, 1),
        exclusions=resolved_members(records, MemberRole.EXCLUSION, 1),
    )
    assert evaluate(parameters, RANKED) == frozenset({BTC, ETH, SOL, XRP})
