"""The cointoss namespace: the scheduled daily sweep and the read API.

`CointossApp` is a lythonic `NamespaceFragment`. woodglue mounts it from `woodglue.yaml` as one
entry of a namespace's `entries`, and `fragment_entry` builds that entry:

```yaml
namespaces:
  cointoss:
    run_engine: true
    entries:
      - type: fragment
        gref: "cointoss.app:CointossApp"
        nsref: "data:"
        configs:
          sweep:
            triggers: [{name: daily_sweep, schedule: "5 0 * * *"}]
```

The read nodes are tagged `api`, so woodglue serves them over JSON-RPC as `cointoss.data:members`
and so on. `sweep` is not: it writes and calls CoinGecko, so it runs from its trigger, from
woodglue's `system.fire_trigger`, or from the CLI, never from an ordinary API call.

Runtime settings come from the data directory (`cointoss.config.Settings`): `init.data_dir` if
given, else woodglue's instance data directory (`current_mount`), else `$COINTOSS_HOME`, else
`~/.local/share/cointoss`. A `woodglue.yaml` written by `cointoss init` carries no
`init.data_dir`, so a copied instance serves its own store; older files that carry one still
work. Settings are read once, when the fragment is built, so a bad `cointoss.yaml` stops
woodglue at startup rather than failing the next sweep silently. A config edit takes effect on
restart.

Each call opens the store and closes it before returning, rather than holding one connection
for the fragment's life. woodglue calls a sync node on the IOLoop thread while lythonic's
`DagRunner` runs one in an executor thread, and a `sqlite3` connection refuses use from a thread
other than its creator's; the store's WAL journal lets these short readers run beside a sweep's
writes; and nothing is left holding a transaction or a replaced file. Opening costs the schema
and version check, a few small queries, which a local API at human rates does not notice.

A read never creates anything: against a data directory with no `cointoss.db` it raises
`NotInitialized`, naming `cointoss init`. Only `init` and the sweep create the store.

One sweep runs at a time per data directory. The sweep node takes an exclusive `flock` on
`sweep.lock` there, which `cointoss backup` also holds while it copies. A scheduled firing
waits for the lock, up to `SWEEP_LOCK_WAIT`: lythonic records a firing as the trigger's last run
even when it fails, so a firing skipped because a backup held the lock would not be retried
until the next day, losing that day's bars. It waits by retrying a non-blocking `flock` between
`asyncio.sleep`s, never with a blocking `flock`, which would stall woodglue's IOLoop and the
JSON-RPC API with it. A manual `cointoss sweep` passes `wait=0`, so one started while another
runs raises `SweepInProgress` at once and does nothing.

A request the store cannot answer -- an unknown universe or Instrument, a date before a
universe's first entry, a malformed date -- raises an `ApiError` carrying a JSON-RPC error code
and a message naming what was not found. `ApiError` is a woodglue `RpcError`, so a JSON-RPC
client receives that code and message, and woodglue logs it without a traceback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import IO, Any, ClassVar

from lythonic.compose.namespace import NamespaceFragment, nsnode
from pydantic import BaseModel, ConfigDict
from woodglue.apps.rpc import RpcError
from woodglue.mount import current_mount

from cointoss import ingest
from cointoss.config import Settings, UniverseReconciled, ensure_universes
from cointoss.instrument import InstrumentId, Source
from cointoss.prices import Bar, as_utc
from cointoss.series import NotYetStarted
from cointoss.store import Store, UnknownInstrument, UnknownUniverse
from cointoss.universe import EvaluationRun

__all__ = [
    "DEFAULT_SWEEP_SCHEDULE",
    "FRAGMENT_GREF",
    "SWEEP_LOCK_WAIT",
    "SWEEP_TRIGGER",
    "ApiError",
    "BadRequest",
    "ChangeView",
    "CointossApp",
    "MemberView",
    "MembersView",
    "NotFound",
    "NotInitialized",
    "SweepInProgress",
    "SweepOutcome",
    "UniverseView",
    "fragment_entry",
    "fragment_schedule",
    "sweep_lock",
    "sweep_lock_async",
    "utc_today",
    "utcnow",
]

log = logging.getLogger(__name__)

DEFAULT_SWEEP_SCHEDULE = "5 0 * * *"
"""Five past midnight UTC: after CoinGecko's 00:00 point exists, and early enough to be it."""

SWEEP_TRIGGER = "daily_sweep"
FRAGMENT_GREF = "cointoss.app:CointossApp"
SWEEP_LOCK = "sweep.lock"
SWEEP_LOCK_WAIT = 600.0
"""Seconds a scheduled sweep or a backup waits for the sweep lock before giving up."""
LOCK_POLL = 1.0
"""Seconds between attempts to take a held sweep lock."""


def utcnow() -> dt.datetime:
    """The clock the sweep reads; a module function so a test can fix it."""
    return dt.datetime.now(dt.UTC)


def utc_today() -> dt.date:
    """Today's date on the UTC calendar, by `utcnow`."""
    return as_utc(utcnow()).date()


def fragment_entry(
    *,
    data_dir: Path | None = None,
    schedule: str = DEFAULT_SWEEP_SCHEDULE,
) -> dict[str, Any]:
    """The `woodglue.yaml` namespace entry mounting the fragment with its daily sweep trigger.

    lythonic reads triggers only from configuration, so the default schedule lives here and in
    whatever `cointoss init` writes from it. Without `data_dir` the fragment resolves the data
    directory itself when it is built, from woodglue's mount when there is one.
    """
    entry: dict[str, Any] = {
        "type": "fragment",
        "gref": FRAGMENT_GREF,
        "nsref": "data:",
        "configs": {"sweep": {"triggers": [{"name": SWEEP_TRIGGER, "schedule": schedule}]}},
    }
    if data_dir is not None:
        entry["init"] = {"data_dir": str(data_dir)}
    return entry


def fragment_schedule(entry: dict[str, Any]) -> str | None:
    """The daily sweep schedule in a namespace entry shaped like `fragment_entry`'s, if any.

    >>> fragment_schedule(fragment_entry(schedule="35 0 * * *"))
    '35 0 * * *'
    >>> fragment_schedule({"type": "fragment"}) is None
    True
    """
    for trigger in entry.get("configs", {}).get("sweep", {}).get("triggers", []):
        if trigger.get("name") == SWEEP_TRIGGER:
            return trigger.get("schedule")
    return None


class ApiError(RpcError):
    """A request the store cannot answer, with the JSON-RPC error code it stands for."""

    code: int = -32000


class NotFound(ApiError):
    """The universe or Instrument named is not stored, or holds nothing on the date asked."""

    code: int = -32001


class NotInitialized(ApiError):
    """The data directory holds no database yet."""

    code: int = -32002


class BadRequest(ApiError):
    """A parameter is malformed: not an ISO date, a range ending before it starts."""

    code: int = -32602


class SweepInProgress(Exception):
    """Another sweep holds the data directory's sweep lock."""


def _try_lock(f: IO[str]) -> bool:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _lock_held(path: Path, wait: float) -> SweepInProgress:
    if wait <= 0:
        return SweepInProgress(f"another sweep holds {path}")
    return SweepInProgress(f"{path} still held after waiting {wait:g}s")


@contextmanager
def sweep_lock(data_dir: Path, wait: float = 0.0) -> Iterator[None]:
    """Hold the data directory's sweep lock, waiting up to `wait` seconds for it.

    Raises `SweepInProgress` if it is still held then; with no `wait`, at once. The wait sleeps
    the calling thread, so the event loop uses `sweep_lock_async`. An `flock` is released by the
    kernel when its holder exits, so a killed sweep leaves no stale lock behind.
    """
    path = data_dir / SWEEP_LOCK
    deadline = time.monotonic() + wait
    with path.open("a") as f:
        while not _try_lock(f):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _lock_held(path, wait) from None
            time.sleep(min(LOCK_POLL, remaining))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@asynccontextmanager
async def sweep_lock_async(data_dir: Path, wait: float = 0.0) -> AsyncIterator[None]:
    """`sweep_lock`, waiting with `asyncio.sleep` so the event loop keeps running meanwhile."""
    path = data_dir / SWEEP_LOCK
    deadline = time.monotonic() + wait
    with path.open("a") as f:
        while not _try_lock(f):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _lock_held(path, wait) from None
            await asyncio.sleep(min(LOCK_POLL, remaining))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class MemberView(BaseModel):
    """One member on a date and its bar for that date, if one is stored.

    Close, volume and market cap are what a swept CoinGecko bar holds. Rank is not persisted,
    so it is not here.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    instrument_id: str
    symbol: str | None
    name: str | None
    close: float | None
    volume: float | None
    market_cap: float | None
    source: Source | None
    fetched_at: dt.datetime | None


class MembersView(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    universe: str
    date: dt.date
    members: list[MemberView]


class ChangeView(BaseModel):
    """What joined and what left a universe on one date."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    date: dt.date
    admitted: list[str]
    dropped: list[str]


class UniverseView(BaseModel):
    """A stored Universe Definition at its latest revision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    revision: int
    revised_at: dt.date
    enter_rank: int | None
    exit_rank: int | None
    inclusions: list[str]
    exclusions: list[str]


class SweepOutcome(BaseModel):
    """What one firing of the sweep did for the UTC `day`.

    `already_swept` means every declared universe already had a Run for `day`, so nothing was
    fetched and `report` is None.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    day: dt.date
    already_swept: bool
    reconciled: list[UniverseReconciled]
    report: ingest.SweepReport | None


class CointossApp(NamespaceFragment):
    """The daily sweep and the read API over one instance's store."""

    settings: Settings

    def __init__(self, data_dir: str | None = None) -> None:
        if data_dir is not None:
            home = Path(data_dir)
        else:
            mount = current_mount.get(None)
            home = mount.data_dir if mount is not None else None
        self.settings = Settings.load(home)

    @contextmanager
    def _store(self) -> Iterator[Store]:
        """The store for a read, which must already exist; opening never creates one here."""
        if not self.settings.db_path.exists():
            raise NotInitialized(
                f"no database at {self.settings.db_path}; run `cointoss init` first"
            )
        try:
            with Store(self.settings.db_path) as store:
                yield store
        except (UnknownUniverse, UnknownInstrument, NotYetStarted) as exc:
            raise NotFound(str(exc)) from exc

    @nsnode(tags=["scheduled"])
    async def sweep(self, force: bool = False, wait: float = SWEEP_LOCK_WAIT) -> SweepOutcome:
        """Reconcile the declared universes, then sweep CoinGecko `markets` for today (UTC).

        Idempotent within a UTC day. Once every declared universe has a Run for today, a further
        firing fetches nothing and reports `already_swept`. Otherwise -- the first firing, a
        retry after a failure, a universe newly declared, or `force` -- it sweeps at the current
        time. `ingest.sweep` returns a universe's Run already recorded for the day rather than
        evaluating it again, and keeps any bar already stored for the day, so a re-sweep only
        fills in what is missing and never files a Restatement against the day's own bars.

        Creates the data directory and store if absent. Waits up to `wait` seconds for the sweep
        lock, since the trigger fires the node with no arguments, and raises `SweepInProgress` if
        another sweep or a backup still holds it then.
        """
        names = [declared.name for declared in self.settings.universes]
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        async with sweep_lock_async(self.settings.data_dir, wait):
            # Read the clock once the lock is held: the wait may have taken minutes.
            now = as_utc(utcnow())
            day = now.date()
            with Store(self.settings.db_path) as store:
                reconciled = ensure_universes(store, self.settings.universes, day)
                swept_today = {name for name in names if store.runs_on(name, day)}
                if not force and names and swept_today >= set(names):
                    log.info("sweep %s: already swept", day)
                    return SweepOutcome(
                        day=day, already_swept=True, reconciled=reconciled, report=None
                    )
                report = await ingest.sweep(
                    store,
                    now,
                    names,
                    top_n=self.settings.top_n,
                    bar_window=self.settings.bar_window,
                )
        log.info(
            "sweep %s: listed %d, %d bars, %d kept, %d restatements%s, failed %s",
            day,
            report.listed,
            report.bars,
            report.bars_kept,
            report.restatements,
            ", late: no bars" if report.bars_skipped_late else "",
            sorted(report.failed) or "none",
        )
        return SweepOutcome(day=day, already_swept=False, reconciled=reconciled, report=report)

    @nsnode(tags=["api"])
    def members(self, universe: str, date: str) -> MembersView:
        """The members of a universe on a date, each with that date's close, volume and market cap."""
        on = _date(date, "date")
        with self._store() as store:
            bar_by_member = store.member_bars(universe, on)
            instruments = {i: store.load_instrument(i) for i in bar_by_member}
        views: list[MemberView] = []
        for instrument_id, bar in bar_by_member.items():
            instrument = instruments[instrument_id]
            views.append(
                MemberView(
                    instrument_id=str(instrument_id),
                    symbol=instrument.symbol if instrument else None,
                    name=instrument.name if instrument else None,
                    close=bar.close if bar else None,
                    volume=bar.volume if bar else None,
                    market_cap=bar.market_cap if bar else None,
                    source=bar.source if bar else None,
                    fetched_at=bar.fetched_at if bar else None,
                )
            )
        return MembersView(universe=universe, date=on, members=views)

    @nsnode(tags=["api"])
    def changes(self, universe: str, start: str, end: str) -> list[ChangeView]:
        """Joins and leaves by date, for each date in `[start, end]` the membership changed.

        A universe's first entry reports every member as joining.
        """
        first, last = _range(start, end)
        with self._store() as store:
            series = store.load_universe_series(universe)
        views: list[ChangeView] = []
        previous: dt.date | None = None
        for entry in series.entries:
            if first <= entry.as_of <= last:
                if previous is None:
                    admitted, dropped = tuple(entry.universe), ()
                else:
                    change = series.change(previous, entry.as_of)
                    admitted, dropped = change.admitted, change.dropped
                views.append(
                    ChangeView(date=entry.as_of, admitted=list(admitted), dropped=list(dropped))
                )
            previous = entry.as_of
        return views

    @nsnode(tags=["api"])
    def bars(self, instrument_id: str, start: str, end: str) -> list[Bar]:
        """One Instrument's bars from `start` to `end` inclusive, through its Price Sources."""
        first, last = _range(start, end)
        with self._store() as store:
            return store.bars_for(InstrumentId(instrument_id), first, last)

    @nsnode(tags=["api"])
    def universes_of(self, instrument_id: str, start: str, end: str) -> list[str]:
        """The universes an Instrument was a member of on any day of `[start, end]`."""
        first, last = _range(start, end)
        with self._store() as store:
            return list(store.universes_containing(InstrumentId(instrument_id), first, last))

    @nsnode(tags=["api"])
    def runs(self, universe: str, limit: int = 20) -> list[EvaluationRun]:
        """A universe's most recent Evaluation Runs, newest first: whether the sweep is alive."""
        if limit < 1:
            raise BadRequest(f"limit must be at least 1, got {limit}")
        with self._store() as store:
            return store.load_evaluation_runs(universe)[::-1][:limit]

    @nsnode(tags=["api"])
    def universes(self) -> list[UniverseView]:
        """Every stored Universe Definition with its current band, declared in config or not."""
        with self._store() as store:
            definitions = [store.require_universe(n) for n in store.universe_names()]
        return [
            UniverseView(
                name=d.name,
                revision=d.revision,
                revised_at=d.revisions[-1].changed_at,
                enter_rank=d.parameters.enter_rank,
                exit_rank=d.parameters.exit_rank,
                inclusions=sorted(d.parameters.inclusions),
                exclusions=sorted(d.parameters.exclusions),
            )
            for d in definitions
        ]


def _date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name}: {value!r} is not an ISO date (YYYY-MM-DD)") from None


def _range(start: str, end: str) -> tuple[dt.date, dt.date]:
    first, last = _date(start, "start"), _date(end, "end")
    if last < first:
        raise BadRequest(f"range end {last} precedes its start {first}")
    return first, last
