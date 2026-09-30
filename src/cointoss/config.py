"""cointoss's own configuration, and reconciling what it declares into the store.

A Universe Definition is revision-logged (ADR-0007), so configuration cannot simply be the
Definition: editing a YAML file would otherwise either rewrite history or be ignored. A
`UniverseDeclaration` declares the rank band a Definition should have now, and `ensure_universes`
brings the store up to it by the only move the log allows, appending a revision.

What reconciliation owns is deliberately narrow: whether a Definition exists, and its rank band.
Inclusions and Exclusions are hand-pinned through `cointoss.ingest.pin_member`, carry
provenance that a config file has no way to express, and are never touched here.

A Definition removed from the config is simply not reconciled. There is no "retired" flag: the
stored Definition, its revisions and its members stay exactly as they were, and anything that
decides which Definitions to evaluate reads the declarations rather than the store.

A declaration names no source. Every declared universe is a CoinGecko rank band while the instance is
crypto-only, and the Definition itself stores no source (ADR-0007's recipe has none), so a
source here would be a field nothing reads. Unknown fields are refused, so a config that does
name one fails loudly rather than being silently ignored.

`Settings` is where a running instance finds all of this. An instance lives in one data
directory, named by `--data-dir`, else `COINTOSS_HOME`, else `~/.local/share/cointoss`; it holds
`cointoss.db` and an optional `cointoss.yaml` declaring the universes and how wide a listing each
sweep fetches, and the bar window: how long after 00:00 UTC a sweep may still record the day's
bars. A missing file means the defaults. The file is validated when loaded, so a config that
could not sweep is refused at startup rather than at the next scheduled run.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cointoss.ingest import DEFAULT_BAR_WINDOW, DEFAULT_TOP_N, check_listing_width
from cointoss.store import Store
from cointoss.universe import UniverseDefinition, UniverseParameters

__all__ = [
    "CONFIG_FILENAME",
    "DB_FILENAME",
    "DEFAULT_UNIVERSES",
    "HOME_ENV",
    "ConfigError",
    "DuplicateUniverse",
    "Reconciliation",
    "Settings",
    "UniverseReconciled",
    "UniverseDeclaration",
    "check_unique_names",
    "ensure_universes",
    "resolve_data_dir",
]

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Base class for configuration errors."""


class DuplicateUniverse(ConfigError, ValueError):
    """Two declarations name the same Universe Definition.

    A `ValueError` too, so pydantic reports it as a `ValidationError` when `Settings` loads.
    """


def check_unique_names(declarations: Iterable[UniverseDeclaration]) -> None:
    """Refuse a name declared twice, since which band would win would depend on list order."""
    counts = Counter(d.name for d in declarations)
    duplicated = sorted(name for name, n in counts.items() if n > 1)
    if duplicated:
        raise DuplicateUniverse(f"declared more than once: {', '.join(duplicated)}")


class UniverseDeclaration(BaseModel):
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
    def _validate(self) -> UniverseDeclaration:
        _ = self.parameters
        return self

    @property
    def parameters(self) -> UniverseParameters:
        """The parameters of a newly created Definition: the band and no pinned members."""
        return UniverseParameters(enter_rank=self.enter_rank, exit_rank=self.exit_rank)


DEFAULT_UNIVERSES: tuple[UniverseDeclaration, ...] = (
    UniverseDeclaration(name="cg-top-100", enter_rank=100, exit_rank=120),
)


HOME_ENV = "COINTOSS_HOME"
CONFIG_FILENAME = "cointoss.yaml"
DB_FILENAME = "cointoss.db"


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    """The instance's data directory: the one given, else `$COINTOSS_HOME`, else the XDG default."""
    if data_dir is not None:
        return data_dir
    home = os.environ.get(HOME_ENV)
    if home:
        return Path(home)
    return Path.home() / ".local" / "share" / "cointoss"


class Settings(BaseModel):
    """What a running instance needs to know: where its store is, and what to sweep.

    `universes` are the declared Definitions, reconciled and evaluated by every sweep; `top_n` is
    how many coins the sweep lists, and must reach every declared exit rank. `bar_window_hours`
    is how long after 00:00 UTC a sweep still writes the day's bars; a later sweep records
    membership only, since its price is no longer the day's 00:00 price.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    data_dir: Path
    universes: tuple[UniverseDeclaration, ...] = DEFAULT_UNIVERSES
    top_n: int = Field(default=DEFAULT_TOP_N, gt=0)
    bar_window_hours: float = Field(default=DEFAULT_BAR_WINDOW.total_seconds() / 3600, gt=0, le=24)

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        check_unique_names(self.universes)
        check_listing_width(self.top_n, (d.exit_rank for d in self.universes))
        return self

    @property
    def bar_window(self) -> timedelta:
        return timedelta(hours=self.bar_window_hours)

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @classmethod
    def load(cls, data_dir: Path | None = None) -> Settings:
        """The settings of the instance in `data_dir`, resolved by `resolve_data_dir`."""
        home = resolve_data_dir(data_dir)
        config = home / CONFIG_FILENAME
        declared: dict[str, Any] = {}
        if config.exists():
            declared = yaml.safe_load(config.read_text()) or {}
        return cls.model_validate({**declared, "data_dir": home})


class Reconciliation(StrEnum):
    """What `ensure_universes` did to one declared Definition."""

    CREATED = "created"
    REVISED = "revised"
    UNCHANGED = "unchanged"


class UniverseReconciled(BaseModel):
    """The outcome for one declaration, and the Definition's latest revision afterwards."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    outcome: Reconciliation
    revision: int


def ensure_universes(
    store: Store, declarations: Iterable[UniverseDeclaration], at: date
) -> list[UniverseReconciled]:
    """Bring each declared Definition's rank band in the store up to its declaration.

    A missing Definition is created at revision 1 dated `at`. One whose band differs gets one
    more revision dated `at`, carrying its Inclusions and Exclusions forward unchanged; one whose
    band matches is left alone. Nothing is deleted or rewritten, and stored Definitions absent
    from `declarations` are not read. Results are in declaration order.

    A name declared twice raises `DuplicateUniverse` before anything is written.
    """
    declarations = list(declarations)
    check_unique_names(declarations)
    return [_ensure_universe(store, declared, at) for declared in declarations]


def _ensure_universe(store: Store, declared: UniverseDeclaration, at: date) -> UniverseReconciled:
    stored = store.load_universe(declared.name)
    if stored is None:
        definition = UniverseDefinition.create(declared.name, declared.parameters, at)
        outcome = Reconciliation.CREATED
    else:
        # `edited` is a no-op when the band already matches, and touches only the two ranks, so
        # the Inclusions and Exclusions projected from member rows ride along as they are.
        definition = stored.edited(at, enter_rank=declared.enter_rank, exit_rank=declared.exit_rank)
        outcome = (
            Reconciliation.UNCHANGED
            if definition.revision == stored.revision
            else Reconciliation.REVISED
        )
    if outcome is not Reconciliation.UNCHANGED:
        # No member records are passed: the member rows already stored are the system of record
        # for Inclusions and Exclusions, and a new revision is covered by every standing member.
        store.save_universe(definition)
        log.info("%s %s at revision %d", declared.name, outcome, definition.revision)
    return UniverseReconciled(name=declared.name, outcome=outcome, revision=definition.revision)
