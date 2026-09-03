"""Declared covariance and the Risk Models that produce it.

Implements ADR-0005. A covariance matrix is *declared*, not computed on demand: once an entry
exists for a date, that is the matrix for that date, permanently. Adjusted price history is
rewritten by corporate actions, so recomputing one date twice legitimately gives two answers;
keeping the declared one is what lets a past research result reproduce exactly.

That makes `CovarianceSeries` a system of record holding a *derived* quantity, which is a
deliberate exception to the Series/Snapshot rule in ADR-0003 -- everywhere else, derived things
are caches. The reason is reproducibility rather than provenance.

A `RiskModel` is the recipe: a universe reference, a lookback, a return frequency, an
estimator. It records how a covariance came to be; the declared entry defines what it is.
Editing a model keeps its identity and appends to its revision log, and every declared entry
stamps the revision it was produced under, so an entry declared four edits ago can still say
what lookback made it.

This module is the ledger, not the calculator. A matrix arrives already computed; turning price
history into returns and returns into a matrix is separate work. A vendor import therefore
works end to end here, and a computed matrix does not until an estimator exists.

Pure: no network, no clock, no database. Edit and declaration dates are supplied by the caller,
which keeps the module testable and makes historical backfill ordinary rather than special.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from enum import StrEnum
from typing import Any, ClassVar

from lythonic.symmetric import SymmetricMatrix
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "CorrectionRefused",
    "CovarianceSeries",
    "DateOccupied",
    "DeclaredCovariance",
    "EntryOrder",
    "Estimator",
    "NoEntry",
    "RaggedRecipe",
    "ReturnFrequency",
    "RiskError",
    "RiskModel",
    "RiskModelRevision",
    "RiskParameters",
    "UnknownRevision",
]


class RiskError(Exception):
    """Base class for risk errors."""


class NoEntry(RiskError):
    """No covariance was declared for the requested date."""


class EntryOrder(RiskError):
    """Entries are out of date order, or two of them claim one date."""


class DateOccupied(EntryOrder):
    """A covariance is already declared for that date, and a declaration is final."""


class UnknownRevision(RiskError):
    """A stamp names a revision that is not in the owning model's log."""


class CorrectionRefused(RiskError):
    """Correcting a declared covariance is deliberately not possible. See ADR-0005."""


class RaggedRecipe(RiskError):
    """A computable estimator is missing part of its recipe."""


class Estimator(StrEnum):
    """How a covariance was produced. `EXTERNAL` is a vendor import with no computable recipe.

    Deliberately short: only a full covariance over a universe is expressible here, so there is
    no factor variant to invite the factor model that ADR-0005 puts out of scope.
    """

    SAMPLE = "sample"
    EXTERNAL = "external"


class ReturnFrequency(StrEnum):
    """The sampling interval of the returns a covariance was estimated from."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RiskParameters(BaseModel):
    """A Risk Model's recipe at one revision.

    A computable estimator needs a universe, a lookback and a frequency. An external one is a
    vendor import, so a missing lookback is expected rather than an error, and it carries a
    source instead.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    estimator: Estimator
    universe: str | None = None
    lookback: int | None = None
    frequency: ReturnFrequency | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> RiskParameters:
        if self.estimator is Estimator.EXTERNAL:
            if not self.source:
                raise RaggedRecipe("an external estimator must name its source")
            return self
        missing = [
            name for name in ("universe", "lookback", "frequency") if getattr(self, name) is None
        ]
        if missing:
            raise RaggedRecipe(f"{self.estimator} is missing {', '.join(missing)}")
        if self.lookback is not None and self.lookback <= 0:
            raise RaggedRecipe(f"lookback must be positive, got {self.lookback}")
        return self


class RiskModelRevision(BaseModel):
    """One recorded edit to a Risk Model.

    Holds the whole parameter set in force after the edit, not a diff, for the same reason
    Series entries do: a diff makes every read a fold, and one bad early diff poisons the rest.
    `changed` names the fields the edit touched, so the log still reads as a history of edits.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    revision: int
    changed_at: date
    changed: tuple[str, ...]
    parameters: RiskParameters


class RiskModel(BaseModel):
    """A named, editable recipe for producing a covariance.

    Editing keeps the model's identity and appends to its log rather than minting a new one, so
    there is no versioning lifecycle to operate. The value itself is immutable: an edit returns
    a new `RiskModel` carrying the longer log, exactly as a Series append does.

    >>> m = RiskModel.create(
    ...     "eq-60d",
    ...     RiskParameters(
    ...         estimator=Estimator.SAMPLE,
    ...         universe="midcap",
    ...         lookback=60,
    ...         frequency=ReturnFrequency.DAILY,
    ...     ),
    ...     date(2024, 1, 1),
    ... )
    >>> m.revision
    1
    >>> m = m.edited(date(2024, 6, 1), lookback=250)
    >>> m.revision, m.parameters_at(1).lookback, m.parameters.lookback
    (2, 60, 250)
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    revisions: tuple[RiskModelRevision, ...]

    @model_validator(mode="after")
    def _validate(self) -> RiskModel:
        if not self.revisions:
            raise RiskError(f"{self.name}: a Risk Model has at least one revision")
        expected = list(range(1, len(self.revisions) + 1))
        if [r.revision for r in self.revisions] != expected:
            raise RiskError(f"{self.name}: revisions must run 1..n without gaps or reordering")
        return self

    @classmethod
    def create(cls, name: str, parameters: RiskParameters, at: date) -> RiskModel:
        """A model at revision 1, whose every populated field counts as changed.

        Derived from the parameters themselves rather than from `model_fields_set`, which
        records how an object was built: a model read back from storage would otherwise log
        every unset field as having changed.
        """
        first = RiskModelRevision(
            revision=1,
            changed_at=at,
            changed=tuple(
                sorted(k for k in RiskParameters.model_fields if getattr(parameters, k) is not None)
            ),
            parameters=parameters,
        )
        return cls(name=name, revisions=(first,))

    @property
    def revision(self) -> int:
        """The latest revision number."""
        return self.revisions[-1].revision

    @property
    def parameters(self) -> RiskParameters:
        """The recipe in force now."""
        return self.revisions[-1].parameters

    def parameters_at(self, revision: int) -> RiskParameters:
        """The recipe in force at a past revision, so an old entry stays interpretable."""
        for entry in self.revisions:
            if entry.revision == revision:
                return entry.parameters
        raise UnknownRevision(f"{self.name} has no revision {revision}")

    def edited(self, at: date, **changes: Any) -> RiskModel:
        """A model with edited parameters and one more revision.

        An edit that changes nothing is a no-op: no bump and no log entry, so the log records
        real changes rather than save-button noise.
        """
        current = self.parameters
        unknown = sorted(set(changes) - set(RiskParameters.model_fields))
        if unknown:
            raise RaggedRecipe(f"{self.name}: no such parameter: {', '.join(unknown)}")
        # Validate and keep the validated object. `model_copy(update=...)` skips validation, so
        # storing its result would keep an unchecked recipe and lose any coercion.
        updated = RiskParameters.model_validate({**current.model_dump(), **changes})
        touched = tuple(sorted(k for k in changes if getattr(current, k) != getattr(updated, k)))
        if not touched:
            return self
        entry = RiskModelRevision(
            revision=self.revision + 1,
            changed_at=at,
            changed=touched,
            parameters=updated,
        )
        return RiskModel(name=self.name, revisions=(*self.revisions, entry))


class DeclaredCovariance(BaseModel):
    """One covariance matrix, declared for a date under a stated Risk Model revision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    as_of: date
    revision: int
    matrix: SymmetricMatrix


class CovarianceSeries(BaseModel):
    """A Series of declared covariance matrices belonging to exactly one Risk Model.

    Unlike the Series in `cointoss.series`, lookup here is by **exact date**. A step function
    would answer a June query with March's matrix, presenting a stale risk estimate as current
    -- and because ADR-0005 forbids recomputation, that answer would never self-correct.
    `nearest_earlier` offers the approximation explicitly, and says which date it gave.

    >>> from lythonic.symmetric import SymmetricMatrixBuilder
    >>> b = SymmetricMatrixBuilder()
    >>> b.set_diagonal({"AAPL": 0.04, "MSFT": 0.09})
    >>> b.set_value("AAPL", "MSFT", 0.02)
    >>> params = RiskParameters(estimator=Estimator.EXTERNAL, source="vendor")
    >>> s = CovarianceSeries(model=RiskModel.create("v", params, date(2024, 1, 1)))
    >>> s = s.declare(date(2024, 3, 31), b.build())
    >>> s.at(date(2024, 3, 31)).value("MSFT", "AAPL")
    0.02
    >>> s.parameters_at(date(2024, 3, 31)).source
    'vendor'
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    model: RiskModel
    entries: tuple[DeclaredCovariance, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> CovarianceSeries:
        dates = [e.as_of for e in self.entries]
        for earlier, later in zip(dates, dates[1:], strict=False):
            if later < earlier:
                raise EntryOrder(f"{self.model.name}: entries are out of date order at {later}")
            if later == earlier:
                raise DateOccupied(f"{self.model.name}: two entries claim {later}")
        for entry in self.entries:
            self.model.parameters_at(entry.revision)
        return self

    def dates(self) -> tuple[date, ...]:
        """The grid on which risk is actually available, in order."""
        return tuple(e.as_of for e in self.entries)

    def at(self, when: date) -> SymmetricMatrix:
        """The covariance declared for exactly `when`.

        Deliberately not the step lookup used by `cointoss.series`: a stale covariance is a
        wrong risk number that recomputation is forbidden from ever fixing.
        """
        for entry in self.entries:
            if entry.as_of == when:
                return entry.matrix
        raise NoEntry(f"{self.model.name} has no covariance declared for {when}")

    def nearest_earlier(self, when: date) -> tuple[date, SymmetricMatrix]:
        """The latest covariance declared at or before `when`, and the date it came from.

        The date is returned rather than implied, so an approximation is always visible as one.
        """
        index = bisect_right(self.dates(), when) - 1
        if index < 0:
            raise NoEntry(f"{self.model.name} declares nothing at or before {when}")
        entry = self.entries[index]
        return entry.as_of, entry.matrix

    def revision_at(self, when: date) -> int:
        """The Risk Model revision the covariance for `when` was produced under."""
        for entry in self.entries:
            if entry.as_of == when:
                return entry.revision
        raise NoEntry(f"{self.model.name} has no covariance declared for {when}")

    def parameters_at(self, when: date) -> RiskParameters:
        """The recipe that produced the covariance for `when`, not the recipe in force now."""
        return self.model.parameters_at(self.revision_at(when))

    def declare(self, when: date, matrix: SymmetricMatrix) -> CovarianceSeries:
        """Declare a covariance for a date. Final: there is no replace and no upsert.

        The stamp is the model's revision as it stands now, taken here rather than supplied, so
        it cannot drift from the recipe that actually produced the matrix.
        """
        stamp = self.model.revision
        standing = next((e for e in self.entries if e.as_of == when), None)
        if standing is not None:
            raise DateOccupied(
                f"{self.model.name} already declares a covariance for {when} under revision "
                f"{standing.revision}; a declaration is final and there is no correction "
                f"mechanism -- see ADR-0005"
            )
        entry = DeclaredCovariance(as_of=when, revision=stamp, matrix=matrix)
        ordered = tuple(sorted((*self.entries, entry), key=lambda e: e.as_of))
        return CovarianceSeries(model=self.model, entries=ordered)

    def correct(
        self,
        when: date,
        matrix: SymmetricMatrix,  # pyright: ignore[reportUnusedParameter] - see docstring
    ) -> CovarianceSeries:
        """Always refuses. ADR-0005 defers how a wrong covariance is corrected.

        The signature mirrors `declare` on purpose: someone reaching for a correction should
        find this method and its refusal, rather than find nothing and invent a way around.

        Failing loudly is the point: it makes the deferred decision surface the first time
        someone needs it, rather than being quietly routed around.
        """
        raise CorrectionRefused(
            f"{self.model.name}: a covariance declared for {when} stands. ADR-0005 defers how "
            f"a wrong covariance is corrected; revisit it rather than working around this."
        )

    def edit_model(self, at: date, **changes: Any) -> CovarianceSeries:
        """A Series whose Risk Model has been edited. Declared entries are untouched.

        An edit affects only what is declared afterwards; every existing entry keeps the
        revision it was stamped with.
        """
        return CovarianceSeries(model=self.model.edited(at, **changes), entries=self.entries)
