"""`cointoss backup` and `cointoss restore`, run through `cointoss.cli.main`.

Each test builds an instance in a temporary directory with `init` and one sweep, CoinGecko
answered from the recorded `markets` listing and the clock fixed, as `tests/test_cli.py` does.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from woodglue.config import load_config

from cointoss.app import sweep_lock
from cointoss.backup import BackupError, take
from cointoss.cli import main
from cointoss.config import HOME_ENV, UniverseDeclaration, ensure_universes
from cointoss.store import SCHEMA_VERSION, Store

FIXTURE = Path(__file__).parent / "data" / "coingecko_markets_top250.json"
AT = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)


@contextmanager
def market(at: datetime = AT) -> Iterator[MagicMock]:
    pages: list[list[dict[str, Any]]] = json.loads(FIXTURE.read_text())["pages"]

    async def fetch(url: str) -> MagicMock:
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        response = MagicMock()
        response.body = json.dumps(pages[page - 1] if page <= len(pages) else []).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=client),
        patch("cointoss.app.utcnow", return_value=at),
        patch("cointoss.cli.utc_today", return_value=at.date()),
    ):
        yield client


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture
def home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A swept instance."""
    monkeypatch.delenv(HOME_ENV, raising=False)
    home = tmp_path / "instance"
    with market():
        assert run(capsys, "init", "--data-dir", str(home))[0] == 0
        assert run(capsys, "sweep", "--data-dir", str(home))[0] == 0
    return home


def status_lines(capsys: pytest.CaptureFixture[str], data_dir: Path) -> list[str]:
    """What `status` says about the store, without the lines naming the directory or server."""
    with market():
        code, out = run(capsys, "status", "--data-dir", str(data_dir))
    assert code == 0, out
    return [ln for ln in out.splitlines() if not ln.startswith(("data dir:", "server:"))]


def backup(capsys: pytest.CaptureFixture[str], home: Path, dest: Path) -> None:
    code, out = run(capsys, "backup", "--data-dir", str(home), str(dest))
    assert code == 0, out


def test_a_backup_beside_a_writer_is_consistent_and_restores_into_a_fresh_dir(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    with Store(home / "cointoss.db") as writer:
        # A committed write left in the WAL, and one still open when the backup is taken.
        writer.conn.execute("PRAGMA wal_autocheckpoint = 0")
        declared = UniverseDeclaration(name="cg-top-50", enter_rank=50, exit_rank=60)
        ensure_universes(writer, [declared], AT.date())
        assert (home / "cointoss.db-wal").stat().st_size > 0
        writer.conn.execute(
            "INSERT INTO SchemaVersion (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, AT.isoformat()),
        )
        backup(capsys, home, dest)
        writer.conn.rollback()

    assert sorted(p.name for p in dest.iterdir()) == [
        "cointoss.db",
        "cointoss.yaml",
        "manifest.json",
        "woodglue.yaml",
    ]
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert set(manifest) == {"cointoss_version", "schema_version", "taken_at"}
    with closing(sqlite3.connect(dest / "cointoss.db")) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with Store(dest / "cointoss.db") as store:
        assert store.universe_names() == ["cg-top-100", "cg-top-50"]

    fresh = tmp_path / "fresh"
    code, out = run(capsys, "restore", "--data-dir", str(fresh), str(dest))
    assert code == 0, out
    assert "run `cointoss init`" in out
    assert not (fresh / "auth.db").exists()
    assert (fresh / "woodglue.yaml").read_text() == (home / "woodglue.yaml").read_text()

    assert run(capsys, "init", "--data-dir", str(fresh))[0] == 0
    assert (fresh / "auth.db").exists()
    restored = status_lines(capsys, fresh)
    assert restored == status_lines(capsys, home)
    assert any("last run 2026-09-30" in ln for ln in restored)


def test_backup_waits_for_the_sweep_lock(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cointoss.app.LOCK_POLL", 0.01)
    holder = ExitStack()
    holder.enter_context(sweep_lock(home))
    threading.Timer(0.1, holder.close).start()
    backup(capsys, home, tmp_path / "bak")
    assert (tmp_path / "bak" / "manifest.json").exists()


def test_backup_gives_up_when_the_lock_stays_held(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cointoss.app.LOCK_POLL", 0.01)
    dest = tmp_path / "bak"
    with sweep_lock(home), pytest.raises(BackupError, match="still held"):
        take(home, dest, wait=0.05)
    assert not (dest / "manifest.json").exists()


def test_backup_refuses_a_non_empty_dest_and_a_dir_without_a_database(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    dest.mkdir()
    (dest / "other").write_text("")
    code, out = run(capsys, "backup", "--data-dir", str(home), str(dest))
    assert code == 1
    assert out.strip().splitlines() == [
        f"cointoss backup: {dest} exists and is not an empty directory"
    ]

    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = run(capsys, "backup", "--data-dir", str(empty), str(tmp_path / "bak2"))
    assert code == 1
    (line,) = out.strip().splitlines()
    assert line == f"cointoss backup: no database at {empty / 'cointoss.db'}; nothing to back up"


def test_restore_refuses_a_newer_schema_with_one_line_and_writes_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    backup(capsys, home, dest)
    manifest = json.loads((dest / "manifest.json").read_text())
    manifest["schema_version"] = SCHEMA_VERSION + 1
    (dest / "manifest.json").write_text(json.dumps(manifest))

    fresh = tmp_path / "fresh"
    code, out = run(capsys, "restore", "--data-dir", str(fresh), str(dest))
    assert code == 1
    (line,) = out.strip().splitlines()
    assert line.startswith(f"cointoss restore: backup is at schema version {SCHEMA_VERSION + 1}")
    assert not fresh.exists()


def test_restore_removes_init_data_dir_from_an_old_woodglue_yaml(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    backup(capsys, home, dest)
    config: dict[str, Any] = yaml.safe_load((dest / "woodglue.yaml").read_text())
    config["namespaces"]["cointoss"]["entries"][0]["init"] = {"data_dir": str(home)}
    (dest / "woodglue.yaml").write_text("# kept\n" + yaml.safe_dump(config, sort_keys=False))

    fresh = tmp_path / "fresh"
    code, out = run(capsys, "restore", "--data-dir", str(fresh), str(dest))
    assert code == 0, out
    assert "woodglue.yaml: restored; removed init.data_dir" in out
    text = (fresh / "woodglue.yaml").read_text()
    assert text.startswith("# kept\n")
    entries = load_config(fresh).namespaces["cointoss"].entries
    assert entries is not None
    assert "init" not in entries[0]


def test_restore_refuses_a_running_server_an_existing_database_and_a_non_backup(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    backup(capsys, home, dest)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "wgl.pid").write_text(f"{os.getpid()}\n")
    code, out = run(capsys, "restore", "--data-dir", str(fresh), str(dest))
    assert code == 1
    assert out.strip().splitlines() == [
        f"cointoss restore: the server for {fresh} is running (pid {os.getpid()}); stop it first"
    ]
    assert not (fresh / "cointoss.db").exists()

    code, out = run(capsys, "restore", "--data-dir", str(home), str(dest))
    assert code == 1
    assert out.strip().splitlines() == [
        f"cointoss restore: {home / 'cointoss.db'} exists; move it aside to restore over it"
    ]

    code, out = run(capsys, "restore", "--data-dir", str(tmp_path / "other"), str(home))
    assert code == 1
    assert out.strip().splitlines() == [
        f"cointoss restore: no manifest.json in {home}; not a cointoss backup"
    ]


def test_restore_ignores_a_stale_pid_file(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    dest = tmp_path / "bak"
    backup(capsys, home, dest)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    # Past any pid the kernel hands out by default (pid_max is at most 2**22).
    (fresh / "wgl.pid").write_text(f"{2**22 + 1}\n")
    code, out = run(capsys, "restore", "--data-dir", str(fresh), str(dest))
    assert code == 0, out
    with Store(fresh / "cointoss.db") as store:
        assert store.runs_on("cg-top-100", date(2026, 9, 30))
