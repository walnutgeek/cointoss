"""Universe Definitions and the pure evaluation that turns one into membership.

Implements ADR-0007. A Universe Definition is the recipe; the membership it produces is a
separate, dated record. The recipe says "the top hundred coins, minus the wrapped ones"; the
membership says who that was on a Tuesday. Keeping them apart is what lets "I changed my mind
about the band" stay distinguishable from "the market moved".

A Definition is a named, editable recipe, and editing appends a revision rather than
overwriting, exactly as a `RiskModel` does -- the two modules are deliberate analogues and
should read alike. A stock universe is a set of pinned Inclusions and no rule at all; a crypto
universe is a market-cap rank band with Inclusions and Exclusions layered over it as standing
policy rather than as one-off corrections that the next evaluation would undo.

`evaluate` is a pure function of the parameters, a ranked list and the previous membership. It
makes no source call, reads no database and knows no date, so an evaluation for any past day is
ordinary rather than special. The band is expressed as two ranks, an entry and a wider exit, so
a coin sitting on the boundary does not join and leave every day; rank rather than an absolute
market cap, so a broad drawdown does not empty the universe and record a mass exodus that is
really one macro event.

Pure: no network, no clock, no database. Edit dates and ranked input are supplied by the
caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from cointoss.instrument import InstrumentId, Source

__all__ = [
    "EvaluationRun",
    "MemberRole",
    "RaggedRule",
    "RunOutcome",
    "UniverseDefinition",
    "UniverseDefinitionRevision",
    "UniverseError",
    "UniverseMemberRecord",
    "UniverseParameters",
    "UnknownRevision",
    "evaluate",
    "resolved_members",
]

# `InstrumentId` is a validating `str` subclass from a module that knows nothing about pydantic.
# Annotating it here keeps the parsing rules on the type itself rather than restating them.


class UniverseError(Exception):
    """Base class for universe errors."""


class UnknownRevision(UniverseError):
    """A revision number is not in the owning Definition's log."""


class RaggedRule(UniverseError):
    """A rank rule is missing part of itself, or its two ranks contradict each other."""


class MemberRole(StrEnum):
    """Why an instrument was pinned to a Definition by hand."""

    INCLUSION = "inclusion"
    EXCLUSION = "exclusion"


class RunOutcome(StrEnum):
    """Whether an Evaluation Run wrote a Universe Series entry.

    The first run of a Definition always does, even when its membership is empty, because it is
    what turns "not yet started" into "nothing qualified".
    """

    CHANGED = "changed"
    UNCHANGED = "unchanged"


class EvaluationRun(BaseModel):
    """One execution of a Universe Definition: evidence the job ran, not a record of truth.

    `source_asof` is the date the membership is recorded for and `run_at` is when the job
    executed, kept apart so a late or backfilled run is not mistaken for a stale market. The
    counts are against the membership in force before the run; `n_unresolved` counts the
    members standing at `revision` that have no Instrument and so could not take part.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    definition: str
    revision: int
    run_at: datetime
    source_asof: date
    outcome: RunOutcome
    n_admitted: int
    n_dropped: int
    n_unresolved: int


class UniverseParameters(BaseModel):
    """A Universe Definition's recipe at one revision.

    The rank rule is optional in whole: a stock universe has none and is nothing but its
    Inclusions. When it is present both ranks are required, because one rank alone is not a band
    and silently defaulting the other would invent a policy nobody wrote.

    Ranks are 1-based, matching how a ranked list is read aloud: the largest coin is rank 1, and
    `enter_rank=100` means "the top hundred".
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    enter_rank: int | None = None
    exit_rank: int | None = None
    inclusions: frozenset[InstrumentId] = frozenset()
    exclusions: frozenset[InstrumentId] = frozenset()

    @model_validator(mode="after")
    def _validate(self) -> UniverseParameters:
        enter, leave = self.enter_rank, self.exit_rank
        if enter is None and leave is None:
            return self
        if enter is None or leave is None:
            missing = "enter_rank" if enter is None else "exit_rank"
            raise RaggedRule(f"a rank rule is present in whole or not at all; {missing} is missing")
        if enter <= 0:
            raise RaggedRule(f"ranks are 1-based, got enter_rank {enter}")
        if leave < enter:
            raise RaggedRule(
                f"exit_rank {leave} is narrower than enter_rank {enter}; the exit rank must be "
                f"at least as wide as the entry rank"
            )
        return self

    @property
    def has_rule(self) -> bool:
        """Whether membership is driven by a rank band at all, or by Inclusions alone."""
        return self.enter_rank is not None

    def pinned(self, role: MemberRole, instrument_id: InstrumentId | None) -> UniverseParameters:
        """These parameters with `instrument_id` added to the set `role` names.

        An unresolved member (None) leaves the sets unchanged, since only resolved members are
        projected into them.
        """
        if instrument_id is None:
            return self
        match role:
            case MemberRole.INCLUSION:
                return self.model_copy(update={"inclusions": self.inclusions | {instrument_id}})
            case MemberRole.EXCLUSION:
                return self.model_copy(update={"exclusions": self.exclusions | {instrument_id}})


class UniverseMemberRecord(BaseModel):
    """The provenance of one hand-pinned member, in the words it was typed in.

    A member is resolved to an Instrument once, when it is added, so a ticker later reassigned to
    an unrelated issuer cannot silently change what the list means. `instrument_id` is None while
    resolution has not succeeded, which is how "not yet identified" stays distinguishable from
    "not in the universe" -- an unresolved member never reaches `UniverseParameters` and so can
    never reach `evaluate`.

    Removal sets `removed_in_revision` rather than deleting the record, so removing and re-adding
    one ticker stays two recorded events instead of flattening into "it is there now".
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    role: MemberRole
    source: Source
    symbol_as_typed: str
    instrument_id: InstrumentId | None = None
    added_in_revision: int = 1
    removed_in_revision: int | None = None

    @property
    def is_resolved(self) -> bool:
        return self.instrument_id is not None

    def covers(self, revision: int) -> bool:
        """Whether this member was standing at `revision`."""
        return self.added_in_revision <= revision and (
            self.removed_in_revision is None or revision < self.removed_in_revision
        )


def resolved_members(
    records: Iterable[UniverseMemberRecord], role: MemberRole, revision: int
) -> frozenset[InstrumentId]:
    """The Instrument Ids a set of member records projects to at one revision.

    This is the projection that fills `UniverseParameters`: standing members of the given role
    that resolved to an Instrument. Unresolved members are dropped here rather than downstream,
    which is what keeps a null Instrument out of every evaluated membership.

    >>> records = [
    ...     UniverseMemberRecord(
    ...         role=MemberRole.INCLUSION, source="yahoo", symbol_as_typed="AAPL",
    ...         instrument_id="stock.us.aapl",
    ...     ),
    ...     UniverseMemberRecord(
    ...         role=MemberRole.INCLUSION, source="yahoo", symbol_as_typed="NOSUCH",
    ...     ),
    ... ]
    >>> sorted(resolved_members(records, MemberRole.INCLUSION, 1))
    [InstrumentId('stock.us.aapl')]
    """
    return frozenset(
        r.instrument_id
        for r in records
        if r.role is role and r.covers(revision) and r.instrument_id is not None
    )


class UniverseDefinitionRevision(BaseModel):
    """One recorded edit to a Universe Definition.

    Holds the whole parameter set in force after the edit, not a diff, for the same reason
    Series entries do: a diff makes every read a fold, and one bad early diff poisons the rest.
    `changed` names the fields the edit touched, so the log still reads as a history of edits.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    revision: int
    changed_at: date
    changed: tuple[str, ...]
    parameters: UniverseParameters


class UniverseDefinition(BaseModel):
    """A named, editable recipe for producing membership.

    Editing keeps the Definition's identity and appends to its log rather than minting a new
    one, so there is no versioning lifecycle to operate. The value itself is immutable: an edit
    returns a new `UniverseDefinition` carrying the longer log, exactly as a Series append does.

    >>> d = UniverseDefinition.create(
    ...     "top-100", UniverseParameters(enter_rank=100, exit_rank=120), date(2024, 1, 1)
    ... )
    >>> d.revision
    1
    >>> d = d.edited(date(2024, 6, 1), enter_rank=50, exit_rank=75)
    >>> d.revision, d.parameters_at(1).enter_rank, d.parameters.enter_rank
    (2, 100, 50)
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    revisions: tuple[UniverseDefinitionRevision, ...]

    @model_validator(mode="after")
    def _validate(self) -> UniverseDefinition:
        if not self.revisions:
            raise UniverseError(f"{self.name}: a Universe Definition has at least one revision")
        expected = list(range(1, len(self.revisions) + 1))
        if [r.revision for r in self.revisions] != expected:
            raise UniverseError(f"{self.name}: revisions must run 1..n without gaps or reordering")
        return self

    @classmethod
    def create(cls, name: str, parameters: UniverseParameters, at: date) -> UniverseDefinition:
        """A Definition at revision 1, whose every populated field counts as changed.

        Derived from the parameters themselves rather than from `model_fields_set`, which
        records how an object was built: a Definition read back from storage would otherwise log
        every unset field as having changed. An empty Inclusion set is not a change, which is
        what makes "somewhere to put a list before I know what goes in it" a revision about
        nothing.
        """
        first = UniverseDefinitionRevision(
            revision=1,
            changed_at=at,
            changed=tuple(sorted(_populated(parameters))),
            parameters=parameters,
        )
        return cls(name=name, revisions=(first,))

    @property
    def revision(self) -> int:
        """The latest revision number."""
        return self.revisions[-1].revision

    @property
    def parameters(self) -> UniverseParameters:
        """The recipe in force now."""
        return self.revisions[-1].parameters

    def parameters_at(self, revision: int) -> UniverseParameters:
        """The recipe in force at a past revision, so an old Series entry stays interpretable."""
        for entry in self.revisions:
            if entry.revision == revision:
                return entry.parameters
        raise UnknownRevision(f"{self.name} has no revision {revision}")

    def edited(self, at: date, **changes: Any) -> UniverseDefinition:
        """A Definition with edited parameters and one more revision.

        An edit that changes nothing is a no-op: no bump and no log entry, so the log records
        real changes rather than save-button noise.
        """
        current = self.parameters
        unknown = sorted(set(changes) - set(UniverseParameters.model_fields))
        if unknown:
            raise RaggedRule(f"{self.name}: no such parameter: {', '.join(unknown)}")
        # Validate and keep the validated object. `model_copy(update=...)` skips validation, so
        # storing its result would keep an unchecked recipe and lose any coercion.
        updated = UniverseParameters.model_validate({**current.model_dump(), **changes})
        touched = tuple(sorted(k for k in changes if getattr(current, k) != getattr(updated, k)))
        if not touched:
            return self
        entry = UniverseDefinitionRevision(
            revision=self.revision + 1,
            changed_at=at,
            changed=touched,
            parameters=updated,
        )
        return UniverseDefinition(name=self.name, revisions=(*self.revisions, entry))


def _populated(parameters: UniverseParameters) -> list[str]:
    """Parameter names carrying a value, treating an empty override set as absent."""
    populated: list[str] = []
    for name in UniverseParameters.model_fields:
        value = getattr(parameters, name)
        if value is None or value == frozenset():
            continue
        populated.append(name)
    return populated


def evaluate(
    parameters: UniverseParameters,
    ranked: Sequence[InstrumentId] | Mapping[InstrumentId, int],
    previous: Iterable[InstrumentId] = (),
) -> frozenset[InstrumentId]:
    """The membership a recipe produces from one ranked list and the membership before it.

    `ranked` is ordered by descending market cap, so position 1 is the largest; where an id
    appears twice its best rank is the one that counts. A mapping gives each id its rank outright,
    for a source whose ranks are not its list positions: an id skipped or held back upstream then
    leaves a gap rather than promoting everything below it. `previous` is the membership from the
    last Series entry, which is what makes the band hysteretic: an id already in the universe
    survives while its rank is better than `exit_rank`, and an id not in it is admitted only at
    `enter_rank` or better. An id absent from `ranked` entirely is out of the rule's reach
    regardless of what it was before -- a coin no source ranks has no rank to be inside a band.

    Composition is `(rule output | inclusions) - exclusions`, exclusions applied last, so an
    instrument both included and excluded resolves to excluded and the answer does not depend on
    an ordering accident. A Definition with no rule is its Inclusions alone, which is the stock
    universe. An empty result is a result: nothing here raises on one.

    >>> btc, eth, sol = (InstrumentId(f"crypto.native.{s}") for s in ("btc", "eth", "sol"))
    >>> band = UniverseParameters(enter_rank=1, exit_rank=2)
    >>> sorted(evaluate(band, [btc, eth, sol]))
    [InstrumentId('crypto.native.btc')]
    >>> sorted(evaluate(band, [btc, eth, sol], previous={eth}))
    [InstrumentId('crypto.native.btc'), InstrumentId('crypto.native.eth')]
    >>> evaluate(band, [btc, sol], previous={eth})
    frozenset({InstrumentId('crypto.native.btc')})
    >>> evaluate(band, {eth: 2, sol: 3})
    frozenset()
    """
    rank: dict[InstrumentId, int] = {}
    if isinstance(ranked, Mapping):
        rank.update(ranked)
    else:
        for position, instrument_id in enumerate(ranked, start=1):
            rank.setdefault(instrument_id, position)

    members: set[InstrumentId] = set()
    enter, leave = parameters.enter_rank, parameters.exit_rank
    if enter is not None and leave is not None:
        standing = frozenset(previous)
        for instrument_id, position in rank.items():
            if position <= (leave if instrument_id in standing else enter):
                members.add(instrument_id)

    return frozenset((members | parameters.inclusions) - parameters.exclusions)
