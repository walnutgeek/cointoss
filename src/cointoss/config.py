"""cointoss's own configuration, and reconciling what it declares into the store.

A Universe Definition is revision-logged (ADR-0007), so configuration cannot simply be the
Definition: editing a YAML file would otherwise either rewrite history or be ignored. A
`UniverseSpec` declares the rank band a Definition should have now, and `ensure_universes`
brings the store up to it by the only move the log allows, appending a revision.

What reconciliation owns is deliberately narrow: whether a Definition exists, and its rank band.
Inclusions and Exclusions are hand-pinned through `cointoss.ingest.pin_member`, carry
provenance that a config file has no way to express, and are never touched here.

A Definition removed from the config is simply not reconciled. There is no "retired" flag: the
stored Definition, its revisions and its members stay exactly as they were, and anything that
decides which Definitions to evaluate reads the declared specs rather than the store.

A spec names no source. Every declared universe is a CoinGecko rank band while the instance is
crypto-only, and the Definition itself stores no source (ADR-0007's recipe has none), so a
source here would be a field nothing reads. Unknown fields are refused, so a config that does
name one fails loudly rather than being silently ignored.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cointoss.store import Store
from cointoss.universe import UniverseDefinition, UniverseParameters

__all__ = [
    "DEFAULT_UNIVERSES",
    "ConfigError",
    "DuplicateUniverse",
    "Reconciliation",
    "UniverseReconciled",
    "UniverseSpec",
    "ensure_universes",
]

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Base class for configuration errors."""


class DuplicateUniverse(ConfigError):
    """Two specs declare the same Universe Definition name."""


class UniverseSpec(BaseModel):
    """A Universe Definition as declared in configuration: its name and its rank band.

    The band is checked by building the `UniverseParameters` it stands for, so a bad band is
    refused when the config is read, by the same rule that refuses it anywhere else, and raises
    that rule's `RaggedRule` rather than a pydantic `ValidationError`.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    enter_rank: int
    exit_rank: int

    @model_validator(mode="after")
    def _validate(self) -> UniverseSpec:
        _ = self.parameters
        return self

    @property
    def parameters(self) -> UniverseParameters:
        """The parameters of a newly created Definition: the band and no pinned members."""
        return UniverseParameters(enter_rank=self.enter_rank, exit_rank=self.exit_rank)


DEFAULT_UNIVERSES: tuple[UniverseSpec, ...] = (
    UniverseSpec(name="cg-top-100", enter_rank=100, exit_rank=120),
)


class Reconciliation(StrEnum):
    """What `ensure_universes` did to one declared Definition."""

    CREATED = "created"
    REVISED = "revised"
    UNCHANGED = "unchanged"


class UniverseReconciled(BaseModel):
    """The outcome for one spec, and the Definition's latest revision afterwards."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    outcome: Reconciliation
    revision: int


def ensure_universes(
    store: Store, specs: Iterable[UniverseSpec], at: date
) -> list[UniverseReconciled]:
    """Bring each declared Definition's rank band in the store up to its spec.

    A missing Definition is created at revision 1 dated `at`. One whose band differs gets one
    more revision dated `at`, carrying its Inclusions and Exclusions forward unchanged; one whose
    band matches is left alone. Nothing is deleted or rewritten, and stored Definitions absent
    from `specs` are not read. Results are in spec order.

    A name declared twice is refused before anything is written, since which band would win
    would otherwise depend on list order.
    """
    specs = list(specs)
    duplicated = sorted(name for name, n in Counter(s.name for s in specs).items() if n > 1)
    if duplicated:
        raise DuplicateUniverse(f"declared more than once: {', '.join(duplicated)}")
    return [_ensure_universe(store, spec, at) for spec in specs]


def _ensure_universe(store: Store, spec: UniverseSpec, at: date) -> UniverseReconciled:
    stored = store.load_universe(spec.name)
    if stored is None:
        definition = UniverseDefinition.create(spec.name, spec.parameters, at)
        outcome = Reconciliation.CREATED
    else:
        # `edited` is a no-op when the band already matches, and touches only the two ranks, so
        # the Inclusions and Exclusions projected from member rows ride along as they are.
        definition = stored.edited(at, enter_rank=spec.enter_rank, exit_rank=spec.exit_rank)
        outcome = (
            Reconciliation.UNCHANGED
            if definition.revision == stored.revision
            else Reconciliation.REVISED
        )
    if outcome is not Reconciliation.UNCHANGED:
        # No member records are passed: the member rows already stored are the system of record
        # for Inclusions and Exclusions, and a new revision is covered by every standing member.
        store.save_universe(definition)
        log.info("%s %s at revision %d", spec.name, outcome, definition.revision)
    return UniverseReconciled(name=spec.name, outcome=outcome, revision=definition.revision)
