"""Reconciling declared Universe Definitions into the store, through `cointoss.config`.

Every test opens a real `Store` on a temporary file. Hand-pinned members go in through
`pin_member`, with OpenFIGI answered by patching `AsyncHTTPClient` exactly as
`tests/test_ingest.py` does, so the members reconciliation must leave alone are the ones a
researcher would actually have created.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from cointoss.config import (
    CONFIG_FILENAME,
    DEFAULT_UNIVERSES,
    HOME_ENV,
    DuplicateUniverse,
    Reconciliation,
    Settings,
    UniverseDeclaration,
    UniverseReconciled,
    ensure_universes,
    resolve_data_dir,
)
from cointoss.ingest import DEFAULT_BAR_WINDOW, DEFAULT_TOP_N, pin_member
from cointoss.instrument import InstrumentId, Source
from cointoss.sources.openfigi import API_KEY_ENV
from cointoss.store import Store
from cointoss.universe import (
    MemberRole,
    RaggedRule,
    UniverseDefinition,
    UniverseParameters,
)

DAY1 = date(2026, 10, 1)
DAY2 = date(2026, 10, 2)
DAY3 = date(2026, 10, 3)


@pytest.fixture(autouse=True)
def anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)


@pytest.fixture
def store() -> Iterator[Store]:
    with TemporaryDirectory() as tmp, Store(Path(tmp) / "cointoss.db") as opened:
        yield opened


def openfigi(body: Any) -> MagicMock:
    response = MagicMock()
    response.body = json.dumps([body]).encode()
    client = MagicMock()
    client.fetch = AsyncMock(return_value=response)
    return client


async def pin_coin(store: Store, role: MemberRole, symbol: str, figi: str, at: date) -> None:
    client = openfigi({"data": [{"figi": figi}]})
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        await pin_member(store, "cg-top-100", role, Source.COINGECKO, symbol, at)


def band(store: Store, name: str) -> tuple[int | None, int | None]:
    parameters = store.require_universe(name).parameters
    return parameters.enter_rank, parameters.exit_rank


def test_an_empty_store_gets_the_default_universe_at_revision_1(store: Store):
    assert ensure_universes(store, DEFAULT_UNIVERSES, DAY1) == [
        UniverseReconciled(name="cg-top-100", outcome=Reconciliation.CREATED, revision=1)
    ]
    definition = store.require_universe("cg-top-100")
    assert definition.revision == 1
    assert definition.revisions[0].changed_at == DAY1
    assert band(store, "cg-top-100") == (100, 120)


def test_the_same_declaration_again_adds_no_revision(store: Store):
    ensure_universes(store, DEFAULT_UNIVERSES, DAY1)
    assert ensure_universes(store, DEFAULT_UNIVERSES, DAY2) == [
        UniverseReconciled(name="cg-top-100", outcome=Reconciliation.UNCHANGED, revision=1)
    ]
    assert store.require_universe("cg-top-100").revision == 1


@pytest.mark.parametrize(
    ("enter", "exit", "changed"),
    [
        (100, 150, ("exit_rank",)),
        (50, 120, ("enter_rank",)),
        (50, 60, ("enter_rank", "exit_rank")),
    ],
)
def test_a_changed_band_adds_exactly_one_revision_dated_at(
    store: Store, enter: int, exit: int, changed: tuple[str, ...]
):
    ensure_universes(store, DEFAULT_UNIVERSES, DAY1)
    declared = UniverseDeclaration(name="cg-top-100", enter_rank=enter, exit_rank=exit)
    assert ensure_universes(store, [declared], DAY2) == [
        UniverseReconciled(name="cg-top-100", outcome=Reconciliation.REVISED, revision=2)
    ]
    definition = store.require_universe("cg-top-100")
    assert definition.revision == 2
    assert definition.revisions[-1].changed_at == DAY2
    assert definition.revisions[-1].changed == changed
    assert band(store, "cg-top-100") == (enter, exit)
    # The earlier revision is kept as it was, never rewritten.
    assert definition.parameters_at(1) == UniverseParameters(enter_rank=100, exit_rank=120)
    assert ensure_universes(store, [declared], DAY3)[0].outcome is Reconciliation.UNCHANGED


async def test_pinned_members_survive_a_band_change_untouched(store: Store):
    ensure_universes(store, DEFAULT_UNIVERSES, DAY1)
    await pin_coin(store, MemberRole.INCLUSION, "BTC", "KKG000000M81", DAY1)
    await pin_coin(store, MemberRole.EXCLUSION, "USDT", "KKG00000DV14", DAY1)
    members = store.load_universe_members("cg-top-100")
    pinned_at = store.require_universe("cg-top-100").revision
    assert pinned_at == 3

    declared = UniverseDeclaration(name="cg-top-100", enter_rank=50, exit_rank=60)
    assert ensure_universes(store, [declared], DAY2) == [
        UniverseReconciled(name="cg-top-100", outcome=Reconciliation.REVISED, revision=4)
    ]

    assert store.load_universe_members("cg-top-100") == members
    parameters = store.require_universe("cg-top-100").parameters
    assert parameters.inclusions == frozenset({InstrumentId("crypto.native.btc")})
    assert parameters.exclusions == frozenset({InstrumentId("crypto.native.usdt")})
    assert (parameters.enter_rank, parameters.exit_rank) == (50, 60)


async def test_an_unchanged_band_leaves_pinned_members_and_revision_alone(store: Store):
    ensure_universes(store, DEFAULT_UNIVERSES, DAY1)
    await pin_coin(store, MemberRole.EXCLUSION, "USDT", "KKG00000DV14", DAY1)
    before = store.require_universe("cg-top-100")
    members = store.load_universe_members("cg-top-100")

    assert ensure_universes(store, DEFAULT_UNIVERSES, DAY2)[0] == UniverseReconciled(
        name="cg-top-100", outcome=Reconciliation.UNCHANGED, revision=2
    )
    assert store.require_universe("cg-top-100") == before
    assert store.load_universe_members("cg-top-100") == members


@pytest.mark.parametrize(("enter", "exit"), [(120, 100), (0, 20), (-5, 10)])
def test_an_invalid_band_is_refused_by_universe_parameters(enter: int, exit: int):
    with pytest.raises(RaggedRule):
        UniverseDeclaration(name="bad", enter_rank=enter, exit_rank=exit)


def test_a_config_naming_an_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        UniverseDeclaration.model_validate(
            {"name": "cg-top-100", "enter_rank": 100, "exit_rank": 120, "source": "coingecko"}
        )


def test_a_definition_dropped_from_config_stays_stored_and_unchanged(store: Store):
    other = UniverseDeclaration(name="cg-top-10", enter_rank=10, exit_rank=12)
    ensure_universes(store, [*DEFAULT_UNIVERSES, other], DAY1)
    before = store.require_universe("cg-top-100")

    assert ensure_universes(store, [other], DAY2) == [
        UniverseReconciled(name="cg-top-10", outcome=Reconciliation.UNCHANGED, revision=1)
    ]
    assert ensure_universes(store, [], DAY3) == []
    assert store.require_universe("cg-top-100") == before


def test_each_declaration_is_reconciled_on_its_own(store: Store):
    ensure_universes(store, DEFAULT_UNIVERSES, DAY1)
    declarations = [
        UniverseDeclaration(name="cg-top-100", enter_rank=100, exit_rank=130),
        UniverseDeclaration(name="cg-top-10", enter_rank=10, exit_rank=12),
    ]
    assert ensure_universes(store, declarations, DAY2) == [
        UniverseReconciled(name="cg-top-100", outcome=Reconciliation.REVISED, revision=2),
        UniverseReconciled(name="cg-top-10", outcome=Reconciliation.CREATED, revision=1),
    ]


def test_a_rule_is_added_to_a_stored_definition_that_had_none(store: Store):
    store.save_universe(UniverseDefinition.create("cg-top-100", UniverseParameters(), DAY1))
    assert ensure_universes(store, DEFAULT_UNIVERSES, DAY2)[0].outcome is Reconciliation.REVISED
    definition = store.require_universe("cg-top-100")
    assert definition.revisions[-1].changed == ("enter_rank", "exit_rank")


def test_a_name_declared_twice_is_refused_before_anything_is_written(store: Store):
    declarations = [
        UniverseDeclaration(name="cg-top-10", enter_rank=10, exit_rank=12),
        UniverseDeclaration(name="cg-top-10", enter_rank=10, exit_rank=15),
    ]
    with pytest.raises(DuplicateUniverse, match="cg-top-10"):
        ensure_universes(store, declarations, DAY1)
    assert store.load_universe("cg-top-10") is None
    # Loading settings refuses it by the same check, reported through pydantic.
    with pytest.raises(ValidationError, match="declared more than once: cg-top-10"):
        Settings(data_dir=Path("."), universes=tuple(declarations))


def test_the_bar_window_is_read_from_the_config(tmp_path: Path):
    (tmp_path / CONFIG_FILENAME).write_text("bar_window_hours: 1.5\n")
    assert Settings.load(tmp_path).bar_window == timedelta(minutes=90)


def test_settings_default_when_the_data_directory_has_no_config(tmp_path: Path):
    settings = Settings.load(tmp_path)
    assert settings.data_dir == tmp_path
    assert settings.db_path == tmp_path / "cointoss.db"
    assert settings.universes == DEFAULT_UNIVERSES
    assert settings.top_n == DEFAULT_TOP_N
    assert settings.bar_window == DEFAULT_BAR_WINDOW == timedelta(hours=3)


def test_settings_read_the_config_file_in_the_data_directory(tmp_path: Path):
    (tmp_path / CONFIG_FILENAME).write_text(
        "top_n: 50\nuniverses:\n  - {name: cg-top-10, enter_rank: 10, exit_rank: 12}\n"
    )
    settings = Settings.load(tmp_path)
    assert settings.top_n == 50
    assert settings.universes == (
        UniverseDeclaration(name="cg-top-10", enter_rank=10, exit_rank=12),
    )


def test_the_data_directory_comes_from_the_environment_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert resolve_data_dir() == tmp_path
    assert resolve_data_dir(tmp_path / "other") == tmp_path / "other"
    monkeypatch.delenv(HOME_ENV)
    assert resolve_data_dir() == Path.home() / ".local" / "share" / "cointoss"


@pytest.mark.parametrize(
    "text",
    [
        "top_n: 100\nuniverses:\n  - {name: cg-top-100, enter_rank: 100, exit_rank: 120}\n",
        "universes:\n  - {name: a, enter_rank: 1, exit_rank: 2}\n"
        "  - {name: a, enter_rank: 1, exit_rank: 3}\n",
        "source: coingecko\n",
        "bar_window_hours: 0\n",
        "bar_window_hours: 25\n",
    ],
)
def test_a_config_that_could_not_sweep_is_refused_when_loaded(tmp_path: Path, text: str):
    (tmp_path / CONFIG_FILENAME).write_text(text)
    with pytest.raises(ValidationError):
        Settings.load(tmp_path)
