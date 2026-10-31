"""The `cointoss` command: set up an instance, sweep by hand, check on it, and back it up.

    cointoss init     [--data-dir D] [--systemd [--unit NAME]] [--port N] [--schedule CRON]
                                                   create the instance, idempotently
    cointoss sweep    [--data-dir D] [--force]
    cointoss backfill [--data-dir D] [--days N]    fill dates with no stored bar
    cointoss status   [--data-dir D]
    cointoss token    [--data-dir D]               print the JSON-RPC bearer token
    cointoss backup   [--data-dir D] DEST          copy the database and configs into DEST
    cointoss restore  [--data-dir D] SRC           copy a backup into a stopped instance
    cointoss portfolio create NAME [--kind K] [--custodian C]       [--url U] [--token=T]
    cointoss trade add PORTFOLIO INSTRUMENT QUANTITY [--cost USD] [--at TIME] [--kind K]
                       [--note N] [--source S] [--coingecko-id ID]  [--url U] [--token=T]
    cointoss trade void TRADE                                       [--url U] [--token=T]

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
history backfill: `backfill` calls the fragment's `backfill` node, waiting for the sweep lock
before each write as the scheduled one does.

`backup` and `restore` are described in `cointoss.backup`. `backup` runs beside a serving
instance and waits for a sweep in progress; `restore` refuses while the server for its data
directory runs, and checks the backup's configs as `init` checks an instance's before writing
anything. It leaves `auth.db` alone: `cointoss init` afterwards creates a token if there is none.

`portfolio` and `trade` are JSON-RPC clients of a running server rather than writers of the
local store, so they work the same against int from another machine on the tailnet, and run as
whoever the token names: that User owns what they create. They call the fragment's
`create_portfolio`, `add_trade` and `void_trade` (see `cointoss.app` for the Role they need).
The server is `--url`, else `$COINTOSS_URL`, else the local instance's own `woodglue.yaml`; the
token is `--token`, else `$COINTOSS_TOKEN`, else the local instance's admin token when calling
it, else the one `wgl login URL` stored. `init` writes the `portfolio` Role into a new
`woodglue.yaml`, and reports a kept one that lacks it.

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
from typing import Any, NamedTuple

import yaml
from croniter import CroniterError, croniter
from lythonic.compose.engine import resolve_file
from pydantic import ValidationError
from ruamel.yaml import YAMLError as WoodglueYAMLError
from tornado.httpclient import HTTPClientError
from woodglue.auth_store import AuthStore
from woodglue.client import WoodglueClient, WoodglueRpcError
from woodglue.config import CONFIG_FILENAME as WOODGLUE_CONFIG
from woodglue.config import WoodglueConfig, load_config

from cointoss.app import (
    BACKFILL_TRIGGER,
    DEFAULT_BACKFILL_SCHEDULE,
    DEFAULT_SWEEP_SCHEDULE,
    NAMESPACE,
    PORTFOLIO_GRANTS,
    PORTFOLIO_ROLE,
    SWEEP_TRIGGER,
    CointossApp,
    NotInitialized,
    SweepInProgress,
    fragment_entry,
    fragment_schedule,
    utc_today,
)
from cointoss.backfill import DEFAULT_DAYS as BACKFILL_DAYS
from cointoss.backup import CONFIGS, BackupError, read_manifest, restore, take
from cointoss.config import (
    CONFIG_FILENAME,
    ConfigError,
    Settings,
    ensure_universes,
    resolve_data_dir,
)
from cointoss.instrument import Source
from cointoss.portfolio import PORTFOLIO_HELD, PortfolioKind, TradeKind
from cointoss.prices import as_utc
from cointoss.store import Store, UnknownUniverse
from cointoss.universe import UniverseError

__all__ = ["main", "render_unit", "user_unit_dir"]

UNIT_TEMPLATE = "cointoss.service"
DEFAULT_UNIT = "cointoss"
DEFAULT_PORT = 5321
URL_ENV = "COINTOSS_URL"
TOKEN_ENV = "COINTOSS_TOKEN"
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
    the sweep is not callable, and firing its trigger needs a `trigger` Grant. The portfolio
    writes are `api` nodes but also check `call` on `cointoss.app.WRITE_TARGET`, which this
    Grant does not cover.
    """
    return {
        "kind": "streamlit",
        "app": str(EXPLORER_SCRIPT),
        "label": "Explore",
        "grants": [f"call {NAMESPACE}"],
    }


def auth_roles() -> dict[str, list[str]]:
    """The Roles a new `woodglue.yaml` defines: `portfolio`, for Users who record their trades.

    The built-in `admin` Role, whose token `cointoss token` prints, needs no definition. A Role
    added by hand that grants `call *` or `call cointoss*` can write portfolios too, since the
    write check matches `call cointoss.portfolio` against Grant patterns; one meant only to read
    names `call cointoss` exactly.
    """
    return {PORTFOLIO_ROLE: list(PORTFOLIO_GRANTS)}


def woodglue_config(
    *, port: int = DEFAULT_PORT, schedule: str = DEFAULT_SWEEP_SCHEDULE, explorer: bool = False
) -> dict[str, Any]:
    """The `woodglue.yaml` a new instance starts from: the cointoss namespace, local, with auth.

    With `explorer`, it also serves the Dataset explorer at `/ui/explore/`.
    """
    config: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "auth": {"enabled": True, "roles": auth_roles()},
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


def _backfill_scheduled(config: WoodglueConfig) -> bool:
    """Whether a loaded `woodglue.yaml` gives the cointoss fragment its `daily_backfill` trigger."""
    namespace = config.namespaces.get(NAMESPACE)
    return any(
        fragment_schedule(entry, "backfill", BACKFILL_TRIGGER) is not None
        for entry in ((namespace.entries or []) if namespace else [])
    )


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
    _report_portfolio_role(config)

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
    if not _backfill_scheduled(config):
        trigger = {"name": BACKFILL_TRIGGER, "schedule": DEFAULT_BACKFILL_SCHEDULE}
        snippet = yaml.safe_dump({"backfill": {"triggers": [trigger]}}, sort_keys=False)
        print(
            f"{WOODGLUE_CONFIG}: no {BACKFILL_TRIGGER} trigger; to backfill daily, add to the "
            "cointoss fragment entry's `configs`:"
        )
        print("".join(f"  {line}" for line in snippet.splitlines(keepends=True)), end="")


def _report_portfolio_role(config: WoodglueConfig) -> None:
    """Say what to add when a kept `woodglue.yaml` has no `portfolio` Role to grant Users.

    Without it only the admin token, or a wildcard `call` Grant (see `auth_roles`), can write
    portfolios, so it is reported rather than refused.
    """
    if not config.auth.enabled or PORTFOLIO_ROLE in config.auth.roles:
        return
    snippet = yaml.safe_dump({"auth": {"roles": auth_roles()}}, sort_keys=False)
    print(f"{WOODGLUE_CONFIG}: no `{PORTFOLIO_ROLE}` Role; for Users who record trades, add:")
    print("".join(f"  {line}" for line in snippet.splitlines(keepends=True)), end="")


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


def cmd_backfill(data_dir: Path, days: int) -> int:
    with _loading_settings(data_dir):
        app = CointossApp(str(data_dir))
    try:
        report = asyncio.run(app.backfill(days=days))
    except (NotInitialized, ValueError) as exc:
        raise CliError(str(exc)) from exc
    except SweepInProgress as exc:
        raise CliError(f"{exc}; backfill stopped") from exc
    print(
        f"backfill {report.at.date()} over {report.days} days: {report.targets} Instruments, "
        f"{report.coingecko_bars} CoinGecko bars, {report.yahoo_bars} Yahoo bars"
    )
    for instrument_id in report.linked:
        print(f"  {instrument_id}: linked to Yahoo")
    for label, why_by_id in (
        ("rejected", report.rejected),
        ("FAILED", report.failed),
        ("skipped", report.skipped),
    ):
        for instrument_id, why in sorted(why_by_id.items()):
            print(f"  {instrument_id}: {label} {why}")
    return 0


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
        if store.load_universe(PORTFOLIO_HELD) is not None:
            print(_universe_status(store, PORTFOLIO_HELD, today))
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
    # What the run's own sweep wrote: CoinGecko bars stamped with its date, which close the
    # session before (Session Offset), so a resolved read of that date finds none yet.
    bar_by_member = store.member_bars(name, run.source_asof, source=Source.COINGECKO)
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


class Endpoint(NamedTuple):
    """A server's base URL, and the bearer token to send it; None for what `wgl login` stored."""

    url: str
    token: str | None


def _endpoint(data_dir: Path, url: str | None, token: str | None) -> Endpoint:
    """Where the portfolio commands send their calls, and as whom.

    The URL is `--url`, else `$COINTOSS_URL`, else the local instance's `woodglue.yaml`. The
    token is `--token`, else `$COINTOSS_TOKEN`, else the local instance's admin token when
    calling that instance, else whatever `wgl login` stored for the URL.
    """
    url = url or os.environ.get(URL_ENV)
    token = token or os.environ.get(TOKEN_ENV)
    if url:
        return Endpoint(url, token)
    try:
        config = load_config(data_dir)
    except FileNotFoundError:
        raise CliError(
            f"no instance at {data_dir}; give --url (or ${URL_ENV}) to reach one elsewhere"
        ) from None
    except (ValidationError, WoodglueYAMLError) as exc:
        raise CliError(f"{data_dir / WOODGLUE_CONFIG}: {_one_line(exc)}") from exc
    # A server listening on every interface is reached locally on loopback.
    host = "127.0.0.1" if config.host in ("0.0.0.0", "::", "") else config.host
    if token is None and (auth := _auth_store(data_dir, config)) is not None:
        tokens = auth.admin_tokens()
        token = tokens[0] if len(tokens) == 1 else None
    return Endpoint(f"http://{host}:{config.port}", token)


def _call(endpoint: Endpoint, method: str, **params: Any) -> dict[str, Any]:
    """Call one of the fragment's JSON-RPC methods; a failure of any kind is a `CliError`."""

    async def call() -> Any:
        # Built inside the loop `asyncio.run` makes: tornado binds a client to its loop.
        client = WoodglueClient(endpoint.url, token=endpoint.token)
        sent = {name: value for name, value in params.items() if value is not None}
        return await client.call(f"{NAMESPACE}.data:{method}", **sent)

    try:
        return asyncio.run(call())
    except WoodglueRpcError as exc:
        hint = "; give --token=TOKEN, or `wgl login` to the server" if exc.code == -32000 else ""
        raise CliError(f"{exc.message}{hint}") from None
    except (HTTPClientError, OSError) as exc:
        raise CliError(f"{endpoint.url}: {exc}") from None


def cmd_portfolio_create(endpoint: Endpoint, name: str, kind: str, custodian: str | None) -> None:
    portfolio = _call(endpoint, "create_portfolio", name=name, kind=kind, custodian=custodian)
    print(
        f"portfolio {portfolio['id']}: {portfolio['name']} ({portfolio['kind']}), "
        f"owned by {portfolio['owner']}"
    )


def cmd_trade_add(endpoint: Endpoint, args: argparse.Namespace) -> None:
    trade = _call(
        endpoint,
        "add_trade",
        portfolio_id=args.portfolio_id,
        instrument=args.instrument,
        quantity=args.quantity,
        cost=args.cost,
        traded_at=args.at,
        kind=args.kind,
        note=args.note,
        source=args.source,
        coingecko_id=args.coingecko_id,
    )
    cost = "" if trade["cost"] is None else f", cost {trade['cost']:g} USD"
    print(
        f"trade {trade['id']}: {trade['quantity']:+g} {trade['instrument_id']} "
        f"at {trade['traded_at']}{cost}"
    )


def cmd_trade_void(endpoint: Endpoint, trade_id: int) -> None:
    trade = _call(endpoint, "void_trade", trade_id=trade_id)
    print(f"trade {trade['id']}: voided at {trade['voided_at']}")


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
    backfill = commands.add_parser(
        "backfill", parents=[data], help="fill dates with no stored bar from CoinGecko and Yahoo"
    )
    backfill.add_argument(
        "--days",
        type=int,
        default=BACKFILL_DAYS,
        help=f"how many days of CoinGecko history to fill, 90 to 365 (default: {BACKFILL_DAYS})",
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

    server = argparse.ArgumentParser(add_help=False, parents=[data])
    server.add_argument(
        "--url",
        help=f"the server's base URL (default: ${URL_ENV}, else the local instance's)",
    )
    server.add_argument(
        "--token",
        help=f"bearer token, given as --token=TOKEN since one may start with '-'; prefer "
        f"${TOKEN_ENV}, which keeps it out of the process list (default: ${TOKEN_ENV}, else the "
        "local instance's admin token, else what `wgl login` stored for the URL)",
    )
    portfolio = commands.add_parser("portfolio", help="create a portfolio on a server")
    portfolio_actions = portfolio.add_subparsers(dest="action", required=True)
    create = portfolio_actions.add_parser(
        "create", parents=[server], help="create a portfolio you own"
    )
    create.add_argument("name")
    create.add_argument(
        "--kind", choices=[k.value for k in PortfolioKind], default=PortfolioKind.REAL.value
    )
    create.add_argument("--custodian", help="who holds the assets, such as an exchange")

    trade = commands.add_parser("trade", help="record or void a trade on a server")
    trade_actions = trade.add_subparsers(dest="action", required=True)
    add = trade_actions.add_parser(
        "add", parents=[server], help="record a trade in a portfolio you own"
    )
    add.add_argument("portfolio_id", type=int, metavar="PORTFOLIO")
    add.add_argument(
        "instrument", metavar="INSTRUMENT", help="an Instrument Id or a symbol, such as btc"
    )
    add.add_argument("quantity", type=float, metavar="QUANTITY", help="negative for a sale")
    add.add_argument("--cost", type=float, metavar="USD", help="the trade's total in USD")
    add.add_argument(
        "--at", metavar="TIME", help="when it traded, ISO, UTC unless an offset is given (now)"
    )
    add.add_argument("--kind", choices=[k.value for k in TradeKind], default=TradeKind.REAL.value)
    add.add_argument("--note")
    add.add_argument(
        "--source",
        choices=[s.value for s in Source],
        default=Source.COINGECKO.value,
        help="what a symbol is typed against (default: coingecko)",
    )
    add.add_argument("--coingecko-id", help="the CoinGecko coin, when several share a symbol")
    void = trade_actions.add_parser(
        "void", parents=[server], help="void a trade; it stays recorded and counts for nothing"
    )
    void.add_argument("trade_id", type=int, metavar="TRADE")
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
            case "backfill":
                return cmd_backfill(data_dir, args.days)
            case "status":
                cmd_status(data_dir)
            case "token":
                cmd_token(data_dir)
            case "backup":
                cmd_backup(data_dir, args.dest.expanduser().absolute())
            case "restore":
                cmd_restore(data_dir, args.src.expanduser().absolute())
            case "portfolio":
                endpoint = _endpoint(data_dir, args.url, args.token)
                cmd_portfolio_create(endpoint, args.name, args.kind, args.custodian)
            case "trade":
                endpoint = _endpoint(data_dir, args.url, args.token)
                if args.action == "add":
                    cmd_trade_add(endpoint, args)
                else:
                    cmd_trade_void(endpoint, args.trade_id)
            case _:
                raise AssertionError(args.command)
    except CliError as exc:
        print(f"cointoss {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
