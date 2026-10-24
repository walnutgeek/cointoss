"""The `cointoss` command: set up an instance, sweep by hand, check on it, and back it up.

    cointoss init     [--data-dir D] [--systemd [--unit NAME]] [--port N] [--schedule CRON]
                                                   create the instance, idempotently
    cointoss sweep    [--data-dir D] [--force]
    cointoss status   [--data-dir D]
    cointoss token    [--data-dir D]               print the JSON-RPC bearer token
    cointoss backup   [--data-dir D] DEST          copy the database and configs into DEST
    cointoss restore  [--data-dir D] SRC           copy a backup into a stopped instance

The data directory is `--data-dir`, else `$COINTOSS_HOME`, else `~/.local/share/cointoss`
(`cointoss.config.resolve_data_dir`). It holds `cointoss.db`, `cointoss.yaml`, `woodglue.yaml`
and woodglue's own stores (`auth.db`, `mounts/`, and `wgl.log` when not under systemd).

`init` writes each config only when it is missing, so an edited one is never overwritten, then
validates both, so a re-run doubles as a config check. The `woodglue.yaml` it writes does not
name the data directory: the fragment takes woodglue's own (`wgl --data D`), so `wgl --data D
start` from a shell serves the same store the unit does, and a copied instance serves its own.
The unit also sets `COINTOSS_HOME`, for any `cointoss` command run under it.

When Streamlit is installed (the `ui` extra), a new `woodglue.yaml` also serves the Dataset
explorer as `uis.explore`, naming the absolute path of `cointoss/explorer_app.py` in this
install, since woodglue resolves a relative `app` against the data directory. woodglue runs it
with its own Python, which is this environment's. A kept `woodglue.yaml` without it is reported
with the entry to add, as a differing port is.

A second instance on the same host, such as a dev checkout beside an installed one, takes its
own `--data-dir`, `--unit` and `--port`. Each unit names its own virtualenv's `bin`, which is why
it is a plain unit per instance rather than a `cointoss@.service` template.

A sweep missed while the server was down is caught up by lythonic when the server starts: a
schedule trigger that missed its last firing fires once on start, so the unit needs no catch-up
step of its own.

`sweep` calls the fragment's own `sweep` node, so a manual run follows exactly the scheduled
run's idempotency rule and takes the same lock, but without waiting for it. It records
CoinGecko's current listing for today (UTC), so it has no date to choose; a missed past day is
history backfill.

`backup` and `restore` are described in `cointoss.backup`. `backup` runs beside a serving
instance and waits for a sweep in progress; `restore` refuses while the server for its data
directory runs, and checks the backup's configs as `init` checks an instance's before writing
anything. It leaves `auth.db` alone: `cointoss init` afterwards creates a token if there is none.

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
import importlib.util
import logging
import os
import string
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

import yaml
from croniter import CroniterError, croniter
from lythonic.compose.engine import resolve_file
from pydantic import ValidationError
from ruamel.yaml import YAMLError as WoodglueYAMLError
from woodglue.auth_store import AuthStore
from woodglue.config import CONFIG_FILENAME as WOODGLUE_CONFIG
from woodglue.config import WoodglueConfig, load_config

from cointoss.app import (
    DEFAULT_SWEEP_SCHEDULE,
    SWEEP_TRIGGER,
    CointossApp,
    SweepInProgress,
    fragment_entry,
    fragment_schedule,
    utc_today,
)
from cointoss.backup import CONFIGS, BackupError, read_manifest, restore, take
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

UNIT_TEMPLATE = "cointoss.service"
DEFAULT_UNIT = "cointoss"
DEFAULT_PORT = 5321
NAMESPACE = "cointoss"
EXPLORE_UI = "explore"
EXPLORER_SCRIPT = Path(__file__).with_name("explorer_app.py")
UNIT_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")


class CliError(Exception):
    """A failure reported as one line on stderr, with a non-zero exit."""


def user_unit_dir() -> Path:
    """Where `systemd --user` looks for units the user installed."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "systemd" / "user"


def render_unit(*, bin_dir: Path, data_dir: Path, unit: str = DEFAULT_UNIT) -> str:
    """The shipped `cointoss.service` template with this install's paths and unit name filled in.

    systemd splits `Exec*=` lines on whitespace, so a path containing any is refused rather than
    quoted into a unit that might still be misread. A `%` is doubled, since systemd would read
    it as the start of a specifier.
    """
    paths = {"bin_dir": bin_dir, "data_dir": data_dir}
    for path in paths.values():
        if any(c.isspace() for c in str(path)):
            raise ValueError(f"path contains whitespace, not supported in the unit: {path}")
    template = files("cointoss").joinpath(UNIT_TEMPLATE).read_text()
    # The template's header explains the placeholders, which are gone once filled in.
    body = template[template.index("\n[Unit]") + 1 :]
    header = (
        "# Written by `cointoss init --systemd` from the template cointoss/cointoss.service.\n\n"
    )
    escaped = {name: str(path).replace("%", "%%") for name, path in paths.items()}
    return header + Template(body).substitute(escaped, unit=unit)


def check_unit_name(name: str) -> None:
    """Refuse a unit name that is not a plain one, installed as `NAME.service`.

    >>> check_unit_name("cointoss-dev")
    >>> check_unit_name("x.service")
    Traceback (most recent call last):
    ...
    cointoss.cli.CliError: --unit 'x.service': give the name without .service
    """
    if not name or not set(name) <= UNIT_NAME_CHARS:
        raise CliError(f"--unit {name!r}: use only letters, digits, '_', '.' and '-'")
    if name.endswith(".service"):
        raise CliError(f"--unit {name!r}: give the name without .service")


def check_schedule(schedule: str) -> None:
    """Refuse anything but a 5-field cron expression, read as UTC by lythonic's trigger, that fires.

    >>> check_schedule("0 0 30 2 *")
    Traceback (most recent call last):
    ...
    cointoss.cli.CliError: --schedule '0 0 30 2 *': never fires
    """
    if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
        raise CliError(f"--schedule {schedule!r}: not a 5-field cron expression")
    # A valid expression can still name no real day, such as 30 February.
    try:
        first_daily_firing(schedule)
    except CroniterError as exc:
        raise CliError(f"--schedule {schedule!r}: never fires") from exc


def first_daily_firing(schedule: str) -> dt.timedelta:
    """How long after 00:00 UTC a cron schedule first fires on a day it fires at all.

    The hour and minute fields do not depend on the day, so any day's first firing will do.

    >>> first_daily_firing("5 0 * * *")
    datetime.timedelta(seconds=300)
    >>> first_daily_firing("30 1,12 * * 1")
    datetime.timedelta(seconds=5400)
    """
    fired = croniter(schedule, -1).get_next(float)
    return dt.timedelta(seconds=fired % 86400)


def streamlit_installed() -> bool:
    """Whether this environment, the one woodglue runs a Streamlit UI with, has Streamlit."""
    return importlib.util.find_spec("streamlit") is not None


def explorer_ui() -> dict[str, Any]:
    """The `uis.explore` entry serving this install's Dataset explorer.

    Its only Grant is `call` on the cointoss namespace, so whoever views it, the app can do no
    more than call cointoss's read nodes: woodglue exposes only `api` nodes over JSON-RPC, so
    the sweep is not callable, and firing its trigger needs a `trigger` Grant.
    """
    return {
        "kind": "streamlit",
        "app": str(EXPLORER_SCRIPT),
        "label": "Explore",
        "grants": [f"call {NAMESPACE}"],
    }


def woodglue_config(
    *, port: int = DEFAULT_PORT, schedule: str = DEFAULT_SWEEP_SCHEDULE, explorer: bool = False
) -> dict[str, Any]:
    """The `woodglue.yaml` a new instance starts from: the cointoss namespace, local, with auth.

    With `explorer`, it also serves the Dataset explorer at `/ui/explore/`.
    """
    config: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "auth": {"enabled": True},
        "namespaces": {
            NAMESPACE: {
                "expose_api": True,
                "run_engine": True,
                "entries": [fragment_entry(schedule=schedule)],
            }
        },
    }
    if explorer:
        config["uis"] = {EXPLORE_UI: explorer_ui()}
    return config


def _sweep_schedule(config: WoodglueConfig) -> str | None:
    """The `daily_sweep` schedule a loaded `woodglue.yaml` gives the cointoss fragment, if any."""
    namespace = config.namespaces.get(NAMESPACE)
    for entry in (namespace.entries or []) if namespace else []:
        if (schedule := fragment_schedule(entry)) is not None:
            return schedule
    return None


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


def _load_woodglue(data_dir: Path) -> WoodglueConfig:
    try:
        return load_config(data_dir)
    except FileNotFoundError as exc:
        raise CliError(f"{exc}; run `cointoss init` first") from exc
    except (ValidationError, WoodglueYAMLError) as exc:
        raise CliError(f"{data_dir / WOODGLUE_CONFIG}: {_one_line(exc)}") from exc


def _auth_store(data_dir: Path, config: WoodglueConfig) -> AuthStore | None:
    """The token database woodglue will use, or None when auth is off."""
    if not config.auth.enabled:
        return None
    auth_db = resolve_file(data_dir, config.storage.auth_db, "auth.db")
    return AuthStore(auth_db, config.auth.resolved_roles())


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


def cmd_init(
    data_dir: Path,
    systemd: bool,
    *,
    unit: str | None = None,
    port: int | None = None,
    schedule: str | None = None,
) -> None:
    """Create or check the instance; `port` and `schedule` apply to a new `woodglue.yaml` only.

    Every option is checked before anything is written.
    """
    if unit is not None:
        if not systemd:
            raise CliError("--unit names the systemd unit; give it with --systemd")
        check_unit_name(unit)
    if port is not None and not 0 < port < 65536:
        raise CliError(f"--port {port}: not a TCP port")
    if schedule is not None:
        check_schedule(schedule)

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

    explorer = streamlit_installed()
    written = _write_new(
        data_dir / WOODGLUE_CONFIG,
        _yaml(
            "# woodglue: serves the cointoss namespace and runs its daily sweep trigger.\n",
            woodglue_config(
                port=DEFAULT_PORT if port is None else port,
                schedule=schedule or DEFAULT_SWEEP_SCHEDULE,
                explorer=explorer,
            ),
        ),
    )
    print(f"{WOODGLUE_CONFIG}: {'created' if written else 'kept'}")
    config = _load_woodglue(data_dir)
    auth = _auth_store(data_dir, config)
    if not written:
        _report_differences(config, port=port, schedule=schedule)
    elif schedule is not None:
        _warn_after_bar_window(schedule, settings.bar_window)
    _report_explorer(data_dir, config, installed=explorer)

    with Store(settings.db_path) as store:
        for reconciled in ensure_universes(store, settings.universes, utc_today()):
            print(f"universe {reconciled.name}: {reconciled.outcome} (rev {reconciled.revision})")
    print(f"database: {settings.db_path}")

    if auth is None:
        print("auth: disabled in woodglue.yaml")
    else:
        created = not auth.admin_tokens()
        auth.ensure_admin_token()
        print(f"auth token: {'created' if created else 'exists'}; `cointoss token` prints it")

    if systemd:
        _install_unit(data_dir, unit or DEFAULT_UNIT)


def _report_differences(config: WoodglueConfig, *, port: int | None, schedule: str | None) -> None:
    """Say which requested values a kept `woodglue.yaml` does not have; it is never rewritten."""
    fix = "edit it, or remove it to regenerate"
    if port is not None and config.port != port:
        print(f"{WOODGLUE_CONFIG}: port {config.port} differs from --port {port}; {fix}")
    configured = _sweep_schedule(config)
    if schedule is not None and (configured or "").split() != schedule.split():
        print(
            f"{WOODGLUE_CONFIG}: {SWEEP_TRIGGER} schedule {configured!r} differs from "
            f"--schedule {schedule!r}; {fix}"
        )


def _report_explorer(data_dir: Path, config: WoodglueConfig, *, installed: bool) -> None:
    """Say whether `woodglue.yaml` serves the explorer, and what to change if it cannot."""
    entry = config.uis.get(EXPLORE_UI)
    if entry is None:
        if not installed:
            print("explorer: not served; install the `ui` extra (Streamlit) to have it")
            return
        snippet = yaml.safe_dump({"uis": {EXPLORE_UI: explorer_ui()}}, sort_keys=False)
        print(f"{WOODGLUE_CONFIG}: no `{EXPLORE_UI}` UI; to serve the explorer, add:")
        print("".join(f"  {line}" for line in snippet.splitlines(keepends=True)), end="")
    elif not installed:
        print(
            f"{WOODGLUE_CONFIG}: uis.{EXPLORE_UI} is configured but Streamlit is not installed "
            "here; install the `ui` extra or remove the UI"
        )
    elif entry.app is not None and not (data_dir / entry.app).exists():
        print(
            f"{WOODGLUE_CONFIG}: uis.{EXPLORE_UI} app {entry.app} does not exist; "
            f"this install's is {EXPLORER_SCRIPT}; edit it"
        )
    else:
        print(f"explorer: /ui/{EXPLORE_UI}/")


def _warn_after_bar_window(schedule: str, bar_window: dt.timedelta) -> None:
    """Warn when the sweep would first fire too late in the day to write the day's bars."""
    first = first_daily_firing(schedule)
    if first > bar_window:
        at = (dt.datetime.min + first).strftime("%H:%M")
        print(
            f"warning: --schedule {schedule!r} first fires at {at} UTC, after the "
            f"{bar_window} bar window; sweeps then record membership but no bars",
            file=sys.stderr,
        )


def _install_unit(data_dir: Path, name: str) -> None:
    try:
        unit = render_unit(bin_dir=_bin_dir(), data_dir=data_dir, unit=name)
    except ValueError as exc:
        raise CliError(f"unit not written: {exc}") from exc
    unit_dir = user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / f"{name}.service"
    if _write_new(path, unit):
        print(f"unit: created {path}")
    elif path.read_text() == unit:
        print(f"unit: kept {path}")
    else:
        print(f"unit: kept {path}, which differs from this install's; remove it to regenerate")
    print("to start it now and at every login:")
    print(f"  systemctl --user daemon-reload && systemctl --user enable --now {name}")
    print("to keep it running while logged out:")
    print("  loginctl enable-linger $USER")


def cmd_sweep(data_dir: Path, force: bool) -> int:
    with _loading_settings(data_dir):
        app = CointossApp(str(data_dir))
    try:
        outcome = asyncio.run(app.sweep(force=force, wait=0))
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


def _server(data_dir: Path) -> tuple[int | None, str]:
    """The pid of the `wgl start` named in its pid file if it is alive, and a word on it.

    The file alone is not enough: a server killed outright, such as by `kill -9`, leaves it
    behind.
    """
    pid_file = data_dir / "wgl.pid"
    if not pid_file.exists():
        return None, "not running"
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None, "not running (unreadable wgl.pid)"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None, "not running (stale wgl.pid)"
    except PermissionError:
        pass
    return pid, f"running (pid {pid})"


def cmd_status(data_dir: Path) -> None:
    settings = _settings(data_dir)
    if not settings.db_path.exists():
        raise CliError(f"no database at {settings.db_path}; run `cointoss init` first")
    today = utc_today()
    print(f"data dir: {data_dir}")
    print(f"server: {_server(data_dir)[1]}")
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
    auth = _auth_store(data_dir, _load_woodglue(data_dir))
    if auth is None:
        raise CliError("auth is disabled in woodglue.yaml; no token is needed")
    auth.ensure_admin_token()
    tokens = auth.admin_tokens()
    if len(tokens) > 1:
        raise CliError(f"{auth.db_path} holds several admin tokens; `wgl token` lists them")
    print(tokens[0])


def cmd_backup(data_dir: Path, dest: Path) -> None:
    try:
        manifest = take(data_dir, dest)
    except BackupError as exc:
        raise CliError(str(exc)) from exc
    copied = [name for name in CONFIGS if (dest / name).exists()]
    print(f"backup: {dest}")
    print(
        f"cointoss {manifest.cointoss_version}, schema version {manifest.schema_version}, "
        f"taken {manifest.taken_at.isoformat(timespec='seconds')}"
    )
    print(f"copied: cointoss.db{''.join(f', {name}' for name in copied)}")


def cmd_restore(data_dir: Path, src: Path) -> None:
    pid, _ = _server(data_dir)
    if pid is not None:
        raise CliError(f"the server for {data_dir} is running (pid {pid}); stop it first")
    try:
        read_manifest(src)
        _settings(src)
        if (src / WOODGLUE_CONFIG).exists():
            _load_woodglue(src)
        written = restore(src, data_dir)
    except BackupError as exc:
        raise CliError(str(exc)) from exc
    print(f"data dir: {data_dir}")
    for line in written:
        print(line)
    if not (data_dir / "auth.db").exists():
        print("no auth.db: run `cointoss init` to create a token")


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
        help=f"also install the systemd --user unit as {user_unit_dir()}/NAME.service",
    )
    init.add_argument(
        "--unit",
        metavar="NAME",
        help=f"the unit's name, for a second instance on this host (default: {DEFAULT_UNIT})",
    )
    init.add_argument(
        "--port",
        type=int,
        help=f"the server port in a new woodglue.yaml (default: {DEFAULT_PORT})",
    )
    init.add_argument(
        "--schedule",
        metavar="CRON",
        help=f"the daily sweep's UTC cron schedule in a new woodglue.yaml "
        f"(default: {DEFAULT_SWEEP_SCHEDULE!r})",
    )
    sweep = commands.add_parser("sweep", parents=[data], help="run today's sweep now")
    sweep.add_argument(
        "--force", action="store_true", help="re-fetch even if today is already swept"
    )
    commands.add_parser("status", parents=[data], help="last Evaluation Run per universe")
    commands.add_parser("token", parents=[data], help="print the JSON-RPC bearer token")
    backup = commands.add_parser(
        "backup", parents=[data], help="copy the database and configs into a new directory"
    )
    backup.add_argument("dest", type=Path, help="directory to write; must not exist or be empty")
    restore = commands.add_parser(
        "restore", parents=[data], help="copy a backup into an instance with no database"
    )
    restore.add_argument("src", type=Path, help="a directory `cointoss backup` wrote")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data_dir = resolve_data_dir(args.data_dir).expanduser().absolute()
    try:
        match args.command:
            case "init":
                cmd_init(
                    data_dir,
                    args.systemd,
                    unit=args.unit,
                    port=args.port,
                    schedule=args.schedule,
                )
            case "sweep":
                return cmd_sweep(data_dir, args.force)
            case "status":
                cmd_status(data_dir)
            case "token":
                cmd_token(data_dir)
            case "backup":
                cmd_backup(data_dir, args.dest.expanduser().absolute())
            case "restore":
                cmd_restore(data_dir, args.src.expanduser().absolute())
            case _:
                raise AssertionError(args.command)
    except CliError as exc:
        print(f"cointoss {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
