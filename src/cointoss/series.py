"""Time-versioned Series over lythonic value types.

Implements ADR-0003. lythonic owns the values -- `Universe`, `ExposureMatrix`, `KeyedVector` --
and cointoss owns time. A `Series` is a system of record: the truth about what a scope or a
classification was on a date. That is the distinction from a `Snapshot`, which is a
materialized cache of something derivable and is never truth.

Pure: no network, no clock, no database. Axes are plain strings; in production they hold
Instrument Ids, but nothing here imports or validates them.

Two rules are worth stating before the code, because both are deliberate irritants:

`as_of` is a step function and a date before the first entry raises. Returning an empty value
would make an un-started Series indistinguishable from an empty one, and every aggregate
downstream would then compute over nothing and report zero.

Entries hold whole values, never diffs. A diff is derivable, whereas storing diffs makes every
read a fold from the beginning and lets one corrupted early diff poison every later date.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from enum import StrEnum
from typing import ClassVar

from lythonic.exposure import ExposureMatrix
from lythonic.universe import Universe
from lythonic.vector import KeyedVector
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "DatedMatrix",
    "DatedUniverse",
    "EntryOrder",
    "ExposureSemantics",
    "ExposureSeries",
    "MatrixChange",
    "MissingPrice",
    "NotYetStarted",
    "RowSumViolation",
    "SemanticsMismatch",
    "SeriesError",
    "TargetAxisMismatch",
    "TargetOutsideAxis",
    "UniverseChange",
    "UniverseSeries",
    "series_covering",
]

SUM_TOLERANCE = 1e-9
"""How far a declared sum-to-one row may drift before it is a violation."""


class SeriesError(Exception):
    """Base class for Series errors."""


class NotYetStarted(SeriesError):
    """A date precedes the first entry, so the Series has nothing to say about it."""


class EntryOrder(SeriesError):
    """Entries are out of date order, or two of them claim one date."""


class TargetOutsideAxis(SeriesError):
    """An entry carries a Target the Series did not declare."""


class TargetAxisMismatch(SeriesError):
    """Two matrices or Series do not share a Target axis."""


class RowSumViolation(SeriesError):
    """A row breaks a declared expectation that Exposures sum to one."""


class SemanticsMismatch(SeriesError):
    """Two Series measure different things and must not be combined."""


class MissingPrice(SeriesError):
    """A position was held in a classified subject with no price to value it at."""


class ExposureSemantics(StrEnum):
    """What the numbers in an Exposure Series mean.

    lythonic's matrix claims nothing about its values, so the meaning lives here.
    """

    PERCENT = "percent"
    VALUE = "value"
    SCORE = "score"


class DatedUniverse(BaseModel):
    """One whole Universe, in force from its date until the next entry."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    as_of: date
    universe: Universe


class DatedMatrix(BaseModel):
    """One whole ExposureMatrix, in force from its date until the next entry."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    as_of: date
    matrix: ExposureMatrix


class UniverseChange(BaseModel):
    """What joined and what left a Universe Series between two dates."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    admitted: tuple[str, ...]
    dropped: tuple[str, ...]


class MatrixChange(BaseModel):
    """One cell whose Exposure differs between two dates. `before`/`after` are cell values."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    subject: str
    target: str
    before: float
    after: float


def _check_dates(dates: Sequence[date], name: str) -> None:
    """Entries are date-ordered and no two claim one date."""
    for earlier, later in zip(dates, dates[1:], strict=False):
        if later < earlier:
            raise EntryOrder(f"{name}: entries are out of date order at {later}")
        if later == earlier:
            raise EntryOrder(f"{name}: two entries claim {later}")


def _cell(matrix: ExposureMatrix, subject: str, target: str) -> float:
    """One cell, reading an absent subject as the matrix's own fill rather than raising."""
    if subject not in matrix.subjects:
        return matrix.cell_fill
    return matrix.exposure(subject, target)


def _position(dates: Sequence[date], when: date, name: str) -> int:
    """Index of the entry in force at `when`. The step is backwards, never forwards."""
    if not dates:
        raise NotYetStarted(f"{name} has no entries")
    if when < dates[0]:
        raise NotYetStarted(f"{name} begins {dates[0]}, which is after {when}")
    return bisect_right(dates, when) - 1


class UniverseSeries(BaseModel):
    """A Series of instrument lists: the scope of data gathering and research over time.

    Construction may be rule-based or hand-curated; the Series records the outcome either way,
    so nothing downstream branches on provenance.

    >>> s = UniverseSeries(name="midcap").append(date(2024, 1, 1), ["AAPL", "MSFT"])
    >>> s = s.append(date(2024, 4, 1), ["AAPL", "NVDA"])
    >>> list(s.as_of(date(2024, 2, 15)))
    ['AAPL', 'MSFT']
    >>> s.change(date(2024, 1, 1), date(2024, 4, 1))
    UniverseChange(admitted=('NVDA',), dropped=('MSFT',))
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    entries: tuple[DatedUniverse, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> UniverseSeries:
        _check_dates([e.as_of for e in self.entries], self.name)
        return self

    def dates(self) -> tuple[date, ...]:
        """The dates on which the scope actually changed, in order."""
        return tuple(e.as_of for e in self.entries)

    def as_of(self, when: date) -> Universe:
        """The Universe in force on `when`. A date before the first entry raises."""
        return self.entries[_position(self.dates(), when, self.name)].universe

    def append(self, when: date, universe: Universe | Iterable[str]) -> UniverseSeries:
        """A new Series with one more entry. Series are immutable."""
        entry = DatedUniverse(as_of=when, universe=Universe(universe))
        return UniverseSeries(name=self.name, entries=(*self.entries, entry))

    def change(self, start: date, end: date) -> UniverseChange:
        """What joined and what left between two dates. Derived, never stored."""
        before, after = self.as_of(start), self.as_of(end)
        return UniverseChange(
            admitted=tuple(k for k in after if k not in before),
            dropped=tuple(k for k in before if k not in after),
        )


class ExposureSeries(BaseModel):
    """A named Series of exposure matrices over a fixed Target axis.

    The Target axis is declared once and fixed for the life of the Series, so a Target means
    the same thing at every date and a cross-time comparison compares like with like. Where a
    genuinely new Target appears -- a revised sector taxonomy, say -- the right move is a new
    Series, because that is a different classification rather than a new date in the old one.

    The Subject axis is free to grow, since instruments enter and leave scope constantly.

    >>> from lythonic.exposure import ExposureMatrixBuilder
    >>> b = ExposureMatrixBuilder(targets=["tech", "energy"])
    >>> b.set_exposure("AAPL", "tech", 1.0)
    >>> s = ExposureSeries(
    ...     name="sector", targets=["tech", "energy"], semantics=ExposureSemantics.PERCENT
    ... ).append(date(2024, 1, 1), b.build())
    >>> s.as_of(date(2024, 6, 1)).exposure("AAPL", "tech")
    1.0
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    targets: Universe
    semantics: ExposureSemantics
    rows_sum_to_one: bool = False
    entries: tuple[DatedMatrix, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> ExposureSeries:
        _check_dates([e.as_of for e in self.entries], self.name)
        for entry in self.entries:
            self._check_entry(entry)
        return self

    def _check_entry(self, entry: DatedMatrix) -> None:
        outside = [t for t in entry.matrix.targets if t not in self.targets]
        if outside:
            raise TargetOutsideAxis(
                f"{self.name}: entry {entry.as_of} carries targets outside the declared axis: "
                f"{sorted(outside)}"
            )
        if entry.matrix.targets != self.targets:
            raise TargetAxisMismatch(
                f"{self.name}: entry {entry.as_of} was not cast onto the declared target axis"
            )
        if not self.rows_sum_to_one:
            return
        for subject in entry.matrix.subjects:
            total = sum(entry.matrix.exposures_of(subject).values())
            if abs(total - 1.0) > SUM_TOLERANCE:
                raise RowSumViolation(
                    f"{self.name}: {subject} sums to {total} on {entry.as_of}, "
                    f"but rows are declared to sum to one"
                )

    def dates(self) -> tuple[date, ...]:
        """The dates on which the classification actually changed, in order."""
        return tuple(e.as_of for e in self.entries)

    def as_of(self, when: date) -> ExposureMatrix:
        """The ExposureMatrix in force on `when`. A date before the first entry raises."""
        return self.entries[_position(self.dates(), when, self.name)].matrix

    def append(self, when: date, matrix: ExposureMatrix) -> ExposureSeries:
        """A new Series with one more entry, cast onto the declared Target axis.

        Casting rather than merely checking is what makes every entry share one axis, so two
        dates can be compared cell by cell without realigning them first.
        """
        outside = [t for t in matrix.targets if t not in self.targets]
        if outside:
            raise TargetOutsideAxis(
                f"{self.name}: entry {when} carries targets outside the declared axis: "
                f"{sorted(outside)}"
            )
        entry = DatedMatrix(as_of=when, matrix=matrix.cast(targets=self.targets))
        # Rebuilt through the constructor, not model_copy: model_copy skips validators, so a
        # target or sum violation would reach a reader unchecked.
        return ExposureSeries(
            name=self.name,
            targets=self.targets,
            semantics=self.semantics,
            rows_sum_to_one=self.rows_sum_to_one,
            entries=(*self.entries, entry),
        )

    def trace(self, subject: str, target: str) -> dict[date, float]:
        """One subject's Exposure to one Target at every date the Series holds.

        The Subject axis grows, so a subject is absent from the entries that predate it. Those
        dates read as the matrix's own cell fill rather than raising, because a subject outside
        the classification has no exposure, which is a value rather than an error.
        """
        return {e.as_of: _cell(e.matrix, subject, target) for e in self.entries}

    def change(self, start: date, end: date) -> tuple[MatrixChange, ...]:
        """Every cell whose Exposure differs between two dates. Derived, never stored.

        A cell absent from either side reads as that matrix's own fill, so a Series whose fill
        is not zero does not report a phantom change against zero.
        """
        before, after = self.as_of(start), self.as_of(end)
        subjects = list(before.subjects) + [s for s in after.subjects if s not in before.subjects]
        changes: list[MatrixChange] = []
        for subject in subjects:
            for target in self.targets:
                was = _cell(before, subject, target)
                now = _cell(after, subject, target)
                if was != now:
                    changes.append(
                        MatrixChange(subject=subject, target=target, before=was, after=now)
                    )
        return tuple(changes)

    def require_comparable(self, other: ExposureSeries) -> None:
        """Refuse two Series that cannot legitimately be combined.

        Exposure Semantics exists to stop a currency value being added to a percentage, and a
        shared Target axis is what makes two cells the same cell. Every operation that puts two
        Series together must call this first; none exists yet, and this is the check the first
        one will owe.
        """
        if self.semantics is not other.semantics:
            raise SemanticsMismatch(
                f"{self.name} is {self.semantics} but {other.name} is {other.semantics}"
            )
        if self.targets != other.targets:
            raise TargetAxisMismatch(f"{self.name} and {other.name} do not share a Target axis")

    def cast_to(self, scope: UniverseSeries, when: date) -> ExposureMatrix:
        """The matrix in force on `when`, restricted to the scope in force on the same date.

        Both sides are read at one date, which is the point: aligning a classification to a
        scope from another date would compare different worlds.
        """
        return self.as_of(when).cast(subjects=scope.as_of(when))

    def portfolio_exposure(
        self,
        positions: Mapping[str, float],
        prices: Mapping[str, float],
        when: date,
    ) -> KeyedVector:
        """Valued exposure per Target, computed from positions at `when`.

        Never stored on a position. The classification used is the one in force at the date
        being valued, not the latest one -- a historical valuation uses its own era's view.
        """
        matrix = self.as_of(when)
        totals = dict.fromkeys(self.targets, 0.0)
        for subject, quantity in positions.items():
            if subject not in matrix.subjects:
                continue
            if subject not in prices:
                raise MissingPrice(f"{self.name}: no price for {subject} on {when}")
            valued = quantity * prices[subject]
            for target, exposure in matrix.exposures_of(subject).items():
                totals[target] += valued * exposure
        return KeyedVector(universe=self.targets, values=[totals[t] for t in self.targets])


def series_covering(subject: str, series: Iterable[ExposureSeries], when: date) -> tuple[str, ...]:
    """Names of the Series that classify `subject` on `when`, in the order given.

    An instrument advertises which classifications exist for it; it never owns them. This is a
    lookup across Series rather than a field on the instrument, so changing a classification
    touches no instrument.
    """
    names: list[str] = []
    for one in series:
        try:
            matrix = one.as_of(when)
        except NotYetStarted:
            continue
        if subject in matrix.subjects and matrix.exposures_of(subject):
            names.append(one.name)
    return tuple(names)
