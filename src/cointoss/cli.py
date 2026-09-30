"""The `cointoss` command: set up an instance, sweep by hand, and check on it.

    cointoss init   [--data-dir D] [--systemd]   create the instance, idempotently
    cointoss sweep  [--data-dir D] [--force]
    cointoss status [--data-dir D]
    cointoss token  [--data-dir D]               print the JSON-RPC bearer token

The data directory is `--data-dir`, else `$COINTOSS_HOME`, else `~/.local/share/cointoss`
(`cointoss.config.resolve_data_dir`). It holds `cointoss.db`, `cointoss.yaml`, `woodglue.yaml`
and woodglue's own stores (`auth.db`, `mounts/`, `wgl.log`).

`init` writes each config only when it is missing, so an edited one is never overwritten, then
validates both, so a re-run doubles as a config check. The `woodglue.yaml` it writes names the
data directory in the fragment's `init.data_dir`, because woodglue does not pass its own data
directory to a fragment; the file then describes the instance on its own, and `wgl --data D
start` from a shell serves the same store the unit does. The unit also sets `COINTOSS_HOME`, for
the `cointoss` commands it runs.

`sweep` calls the fragment's own `sweep` node, so a manual run follows exactly the scheduled
run's idempotency rule and takes the same lock. It records CoinGecko's current listing for
today (UTC), so it has no date to choose; a missed past day is history backfill.

Expected failures -- a config that does not load, a sweep already running, a missing store --
are turned into `CliError` where they arise and reported as one line. Anything else is a bug
and keeps its traceback.

The CLI uses argparse rather than lythonic's `ActionTree`, which derives option names from
Python parameter names (`--data_dir`) and takes root options only before the subcommand.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

import yaml
from lythonic.compose.engine import resolve_file
from pydantic import ValidationError
from ruamel.yaml import YAMLError as WoodglueYAMLError
from woodglue.config import CONFIG_FILENAME as WOODGLUE_CONFIG
from woodglue.config import load_config
from woodglue.token_store import ensure_token, get_single_token

from cointoss.app import CointossApp, SweepInProgress, fragment_entry, utc_today
from cointoss.config import (
    CONFIG_FILENAME,
    ConfigError,
    Settings,
    ensure_universes,
    resolve_data_dir,
)
from cointoss.prices import as_utc
from cointoss.store import Store, UnknownUniverse
from cointoss.universe import UniverseError

__all__ = ["main", "render_unit", "user_unit_dir"]

UNIT_NAME = "cointoss.service"
NAMESPACE = "cointoss"


class CliError(Exception):
    """A failure reported as one line on stderr, with a non-zero exit."""


def user_unit_dir() -> Path:
    """Where `systemd --user` looks for units the user installed."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "systemd" / "user"


def render_unit(*, bin_dir: Path, data_dir: Path, timeout: Path | None = None) -> str:
    """The shipped `cointoss.service` template with this install's paths filled in.

    `timeout` is coreutils' `timeout`, found on `PATH` when not given; it bounds the catch-up
    sweep, since systemd's own `TimeoutStartSec` would fail the whole unit rather than let the
    server start without it. Raises `ValueError` when it cannot be found.

    systemd splits `Exec*=` lines on whitespace, so a path containing any is refused rather than
    quoted into a unit that might still be misread. A `%` is doubled, since systemd would read
    it as the start of a specifier.
    """
    if timeout is None:
        found = shutil.which("timeout")
        if found is None:
            raise ValueError("`timeout` (coreutils) not found on PATH")
        timeout = Path(found)
    paths = {"bin_dir": bin_dir, "data_dir": data_dir, "timeout": timeout}
    for path in paths.values():
        if any(c.isspace() for c in str(path)):
            raise ValueError(f"path contains whitespace, not supported in the unit: {path}")
    template = files("cointoss").joinpath(UNIT_NAME).read_text()
    # The template's header explains the placeholders, which are gone once filled in.
    body = template[template.index("\n[Unit]") + 1 :]
    header = (
        "# Written by `cointoss init --systemd` from the template cointoss/cointoss.service.\n\n"
    )
    escaped = {name: str(path).replace("%", "%%") for name, path in paths.items()}
    return header + Template(body).substitute(escaped)


def woodglue_config(data_dir: Path) -> dict[str, Any]:
    """The `woodglue.yaml` a new instance starts from: the cointoss namespace, local, with auth."""
    return {
        "host": "127.0.0.1",
        "port": 5321,
        "auth": {"enabled": True},
        "namespaces": {
            NAMESPACE: {
                "expose_api": True,
                "run_engine": True,
                "entries": [fragment_entry(data_dir=data_dir)],
            }
        },
    }


def cointoss_config() -> dict[str, Any]:
    """The `cointoss.yaml` a new instance starts from: the default universes and listing width."""
    defaults = Settings(data_dir=Path("."))
    return {
        "top_n": defaults.top_n,
        "bar_window_hours": defaults.bar_window_hours,
        "universes": [declared.model_dump() for declared in defaults.universes],
    }


def _write_new(path: Path, text: str) -> bool:
    """Write `text` to `path` unless it exists; whether it was written."""
    try:
        with path.open("x") as f:
            f.write(text)
    except FileExistsError:
        return False
    return True


def _yaml(header: str, content: dict[str, Any]) -> str:
    return header + yaml.safe_dump(content, sort_keys=False)


def _auth_db(data_dir: Path) -> Path | None:
    """The token database woodglue will use, or None when auth is off."""
    try:
        config = load_config(data_dir)
    except FileNotFoundError as exc:
        raise CliError(f"{exc}; run `cointoss init` first") from exc
    except (ValidationError, WoodglueYAMLError) as exc:
        raise CliError(f"{data_dir / WOODGLUE_CONFIG}: {_one_line(exc)}") from exc
    if not config.auth.enabled:
        return None
    return resolve_file(data_dir, config.storage.auth_db, "auth.db")


@contextmanager
def _loading_settings(data_dir: Path) -> Iterator[None]:
    """Report a `cointoss.yaml` that does not load as a `CliError`."""
    try:
        yield
    except (ValidationError, ConfigError, UniverseError, yaml.YAMLError) as exc:
        raise CliError(f"{data_dir / CONFIG_FILENAME}: {_one_line(exc)}") from exc


def _one_line(exc: Exception) -> str:
    """An error's message on one line: each validation error's field and reason, or the text."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
    return " ".join(str(exc).split())


def _settings(data_dir: Path) -> Settings:
    with _loading_settings(data_dir):
        return Settings.load(data_dir)


def _bin_dir() -> Path:
    """The bin directory of the environment running this command, holding `wgl` and `cointoss`."""
    bin_dir = Path(sys.executable).parent
    missing = [name for name in ("wgl", "cointoss") if not (bin_dir / name).exists()]
    if missing:
        raise CliError(f"{', '.join(missing)} not found in {bin_dir}; run from the project venv")
    return bin_dir


def cmd_init(data_dir: Path, systemd: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"data dir: {data_dir}")

    written = _write_new(
        data_dir / CONFIG_FILENAME,
        _yaml(
            "# cointoss: declared universes and how wide a listing each sweep fetches.\n",
            cointoss_config(),
        ),
    )
    print(f"{CONFIG_FILENAME}: {'created' if written else 'kept'}")
    settings = _settings(data_dir)

    written = _write_new(
        data_dir / WOODGLUE_CONFIG,
        _yaml(
            "# woodglue: serves the cointoss namespace and runs its daily sweep trigger.\n",
            woodglue_config(data_dir),
        ),
    )
    print(f"{WOODGLUE_CONFIG}: {'created' if written else 'kept'}")
    auth_db = _auth_db(data_dir)

    with Store(settings.db_path) as store:
        for reconciled in ensure_universes(store, settings.universes, utc_today()):
            print(f"universe {reconciled.name}: {reconciled.outcome} (rev {reconciled.revision})")
    print(f"database: {settings.db_path}")

    if auth_db is None:
        print("auth: disabled in woodglue.yaml")
    else:
        created = ensure_token(auth_db) is not None
        print(f"auth token: {'created' if created else 'exists'}; `cointoss token` prints it")

    if systemd:
        _install_unit(data_dir)


def _install_unit(data_dir: Path) -> None:
    try:
        unit = render_unit(bin_dir=_bin_dir(), data_dir=data_dir)
    except ValueError as exc:
        raise CliError(f"unit not written: {exc}") from exc
    unit_dir = user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / UNIT_NAME
    if _write_new(path, unit):
        print(f"unit: created {path}")
    elif path.read_text() == unit:
        print(f"unit: kept {path}")
    else:
        print(f"unit: kept {path}, which differs from this install's; remove it to regenerate")
    print("to start it now and at every login:")
    print("  systemctl --user daemon-reload && systemctl --user enable --now cointoss")
    print("to keep it running while logged out:")
    print("  loginctl enable-linger $USER")


def cmd_sweep(data_dir: Path, force: bool) -> int:
    with _loading_settings(data_dir):
        app = CointossApp(str(data_dir))
    try:
        outcome = asyncio.run(app.sweep(force=force))
    except SweepInProgress as exc:
        raise CliError(f"{exc}; not sweeping") from exc
    if outcome.report is None:
        print(f"sweep {outcome.day}: already swept")
        return 0
    report = outcome.report
    print(
        f"sweep {outcome.day} at {report.at.isoformat()}: listed {report.listed}, "
        f"{report.bars} bars, {report.bars_kept} kept, {report.restatements} restatements, "
        f"{len(report.skipped)} skipped"
    )
    if report.bars_skipped_late:
        print("  no bars: swept after the bar window; the day's bars are left for a backfill")
    for run in report.runs:
        print(f"  {run.definition}: {run.outcome}, +{run.n_admitted} -{run.n_dropped}")
    for name, why in sorted(report.failed.items()):
        print(f"  {name}: FAILED {why}")
    return 1 if report.failed else 0


def _server_state(data_dir: Path) -> str:
    """Whether the `wgl start` named in its pid file is alive.

    The file alone is not enough: `wgl start` removes it only on a clean exit, and a SIGTERM from
    `systemctl stop` kills it before its cleanup runs.
    """
    pid_file = data_dir / "wgl.pid"
    if not pid_file.exists():
        return "not running"
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return "not running (unreadable wgl.pid)"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "not running (stale wgl.pid)"
    except PermissionError:
        pass
    return f"running (pid {pid})"


def cmd_status(data_dir: Path) -> None:
    settings = _settings(data_dir)
    if not settings.db_path.exists():
        raise CliError(f"no database at {settings.db_path}; run `cointoss init` first")
    today = utc_today()
    print(f"data dir: {data_dir}")
    print(f"server: {_server_state(data_dir)}")
    with Store(settings.db_path) as store:
        for declared in settings.universes:
            print(_universe_status(store, declared.name, today))
        latest = store.latest_bar_date()
    print(f"last bar {latest}" if latest else "last bar: none yet")


def _universe_status(store: Store, name: str, today: dt.date) -> str:
    try:
        runs = store.load_evaluation_runs(name)
    except UnknownUniverse:
        return f"{name}: not stored yet (the next sweep creates it)"
    if not runs:
        return f"{name}: no runs yet"
    run = max(runs, key=lambda r: (r.source_asof, r.run_at))
    bar_by_member = store.member_bars(name, run.source_asof)
    priced = sum(bar is not None for bar in bar_by_member.values())
    behind = "" if run.source_asof >= today else f"; BEHIND, today is {today}"
    return (
        f"{name}: last run {run.source_asof} at {as_utc(run.run_at).isoformat()} {run.outcome}, "
        f"+{run.n_admitted} -{run.n_dropped}; {len(bar_by_member)} members, "
        f"{priced}/{len(bar_by_member)} bars{behind}"
    )


def cmd_token(data_dir: Path) -> None:
    auth_db = _auth_db(data_dir)
    if auth_db is None:
        raise CliError("auth is disabled in woodglue.yaml; no token is needed")
    ensure_token(auth_db)
    token = get_single_token(auth_db)
    if token is None:
        raise CliError(f"{auth_db} holds several tokens; read them from its `tokens` table")
    print(token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cointoss", description="Set up, sweep and check a cointoss instance."
    )
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument(
        "--data-dir",
        type=Path,
        help="instance directory (default: $COINTOSS_HOME, else ~/.local/share/cointoss)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", parents=[data], help="create or check the instance")
    init.add_argument(
        "--systemd",
        action="store_true",
        help=f"also install the systemd --user unit as {user_unit_dir() / UNIT_NAME}",
    )
    sweep = commands.add_parser("sweep", parents=[data], help="run today's sweep now")
    sweep.add_argument(
        "--force", action="store_true", help="re-fetch even if today is already swept"
    )
    commands.add_parser("status", parents=[data], help="last Evaluation Run per universe")
    commands.add_parser("token", parents=[data], help="print the JSON-RPC bearer token")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data_dir = resolve_data_dir(args.data_dir).expanduser().absolute()
    try:
        match args.command:
            case "init":
                cmd_init(data_dir, args.systemd)
            case "sweep":
                return cmd_sweep(data_dir, args.force)
            case "status":
                cmd_status(data_dir)
            case "token":
                cmd_token(data_dir)
            case _:
                raise AssertionError(args.command)
    except CliError as exc:
        print(f"cointoss {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
