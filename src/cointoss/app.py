"""The cointoss namespace: the scheduled daily sweep and backfill, and the read API.

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
          backfill:
            triggers: [{name: daily_backfill, schedule: "0 1 * * *"}]
```

The read nodes are tagged `api`, so woodglue serves them over JSON-RPC as `cointoss.data:members`
and so on. `sweep` and `backfill` are not: they write and call CoinGecko, so they run from
their triggers, from woodglue's `system.fire_trigger`, or from the CLI, never from an ordinary
API call.

`backfill` (`cointoss.backfill`) has its own trigger rather than running inside the sweep, so a
slow or failing backfill neither delays the day's sweep nor marks it failed, and each firing is
logged on its own. It fires an hour after the default sweep; the order is not load-bearing,
since neither overwrites a bar the other stored. It takes the sweep lock for each write rather
than for its whole run, which is long, so a sweep or backup waiting for the lock gets it within
one coin's write.

The portfolio writes, `create_portfolio`, `add_trade` and `void_trade`, are `api` nodes too, but
a woodglue Grant names a whole namespace, so `call cointoss` alone would reach them, and that is
the explorer's Grant. Each write therefore checks a second Grant itself: `call` on
`WRITE_TARGET`, `cointoss.portfolio`. No namespace can be called that, since woodglue splits a
method name at its first dot, so the Grant means only "may write portfolios". The `portfolio`
Role (`PORTFOLIO_ROLE`, `PORTFOLIO_GRANTS`) holds it beside `call cointoss`, and the built-in
`admin` Role holds every Grant. A Delegated Token is narrowed to its UI's `grants`, so the
explorer's token cannot write whoever views it. A Grant's target is an fnmatch pattern, so a
wildcard that also matches `cointoss.portfolio`, such as `call *` or `call cointoss*`, grants
the writes too: a Role meant only to read names `call cointoss` exactly. A refused write raises
woodglue's `AccessDenied` before anything is read or written, which a JSON-RPC client receives
as -32003.

The caller is the owner: `create_portfolio` records the Caller's User id, and `add_trade` and
`void_trade` act only on that User's Portfolios. Another User's Portfolio or Trade is reported
exactly as a missing one is, `NotFound`, so ids do not reveal what others hold. With auth
disabled every request is woodglue's anonymous Caller, and owns as `anonymous`.

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
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import IO, Any, ClassVar, TypeVar

from lythonic.compose.namespace import NamespaceFragment, nsnode
from lythonic.frame import FrameData
from pydantic import BaseModel, ConfigDict, ValidationError
from woodglue.access import ANONYMOUS, Action, check, current_caller
from woodglue.apps.rpc import RpcError
from woodglue.mount import current_mount

from cointoss import ingest
from cointoss.backfill import DEFAULT_DAYS as BACKFILL_DAYS
from cointoss.backfill import BackfillReport
from cointoss.backfill import backfill as run_backfill
from cointoss.config import Settings, UniverseReconciled, ensure_universes
from cointoss.datasets import (
    DatasetView,
    InvalidParams,
    OwnedDataset,
    UnknownDataset,
    describe,
    lookup,
)
from cointoss.instrument import InstrumentError, InstrumentId, Source
from cointoss.portfolio import (
    PORTFOLIO_HELD,
    Portfolio,
    PortfolioKind,
    Trade,
    TradeKind,
    UserId,
    check_cost,
    check_quantity,
)
from cointoss.prices import Bar, as_utc
from cointoss.series import NotYetStarted
from cointoss.store import (
    DuplicatePortfolio,
    Store,
    TooManyRows,
    TradeAlreadyVoided,
    UnknownInstrument,
    UnknownPortfolio,
    UnknownTrade,
    UnknownUniverse,
)
from cointoss.universe import EvaluationRun

__all__ = [
    "BACKFILL_NODE",
    "BACKFILL_TRIGGER",
    "DEFAULT_BACKFILL_SCHEDULE",
    "DEFAULT_SWEEP_SCHEDULE",
    "FRAGMENT_GREF",
    "NAMESPACE",
    "PORTFOLIO_GRANTS",
    "PORTFOLIO_ROLE",
    "SWEEP_LOCK_WAIT",
    "SWEEP_NODE",
    "SWEEP_TRIGGER",
    "WRITE_TARGET",
    "ApiError",
    "BadRequest",
    "ChangeView",
    "CointossApp",
    "Conflict",
    "MemberView",
    "MembersView",
    "NotFound",
    "NotInitialized",
    "SweepInProgress",
    "SweepOutcome",
    "Unauthorized",
    "UniverseView",
    "fragment_entry",
    "fragment_schedule",
    "one_line",
    "sweep_lock",
    "sweep_lock_async",
    "utc_today",
    "utcnow",
]

log = logging.getLogger(__name__)

E = TypeVar("E", bound=StrEnum)

DEFAULT_SWEEP_SCHEDULE = "5 0 * * *"
"""Five past midnight UTC: after CoinGecko's 00:00 point exists, and early enough to be it."""

SWEEP_NODE = "sweep"
"""The fragment's sweep node, as named in a namespace entry's `configs`."""
SWEEP_TRIGGER = "daily_sweep"
DEFAULT_BACKFILL_SCHEDULE = "0 1 * * *"
"""01:00 UTC: after the default sweep has written the day's bars and released the lock."""
BACKFILL_NODE = "backfill"
BACKFILL_TRIGGER = "daily_backfill"
FRAGMENT_GREF = "cointoss.app:CointossApp"
NAMESPACE = "cointoss"
"""The woodglue namespace `cointoss init` mounts the fragment under."""
WRITE_TARGET = f"{NAMESPACE}.portfolio"
"""The target of the `call` Grant a portfolio write checks for; never a namespace's name.

Matched as an fnmatch pattern, so `call *` and `call cointoss*` grant the writes as well."""
PORTFOLIO_ROLE = "portfolio"
PORTFOLIO_GRANTS = (f"call {NAMESPACE}", f"call {WRITE_TARGET}")
"""The Grants of `PORTFOLIO_ROLE`: the read API and the portfolio writes."""
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
    backfill_schedule: str = DEFAULT_BACKFILL_SCHEDULE,
) -> dict[str, Any]:
    """The `woodglue.yaml` namespace entry mounting the fragment with its daily triggers.

    lythonic reads triggers only from configuration, so the default schedule lives here and in
    whatever `cointoss init` writes from it. Without `data_dir` the fragment resolves the data
    directory itself when it is built, from woodglue's mount when there is one.
    """
    entry: dict[str, Any] = {
        "type": "fragment",
        "gref": FRAGMENT_GREF,
        "nsref": "data:",
        "configs": {
            SWEEP_NODE: {"triggers": [{"name": SWEEP_TRIGGER, "schedule": schedule}]},
            BACKFILL_NODE: {
                "triggers": [{"name": BACKFILL_TRIGGER, "schedule": backfill_schedule}]
            },
        },
    }
    if data_dir is not None:
        entry["init"] = {"data_dir": str(data_dir)}
    return entry


def fragment_schedule(
    entry: dict[str, Any], node: str = SWEEP_NODE, trigger: str = SWEEP_TRIGGER
) -> str | None:
    """A node's daily schedule in a namespace entry shaped like `fragment_entry`'s, if any.

    >>> fragment_schedule(fragment_entry(schedule="35 0 * * *"))
    '35 0 * * *'
    >>> fragment_schedule(fragment_entry(), BACKFILL_NODE, BACKFILL_TRIGGER)
    '0 1 * * *'
    >>> fragment_schedule({"type": "fragment"}) is None
    True
    """
    for configured in entry.get("configs", {}).get(node, {}).get("triggers", []):
        if configured.get("name") == trigger:
            return configured.get("schedule")
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


class Unauthorized(ApiError):
    """A write called in-process, with no Caller to own what it writes."""

    code: int = -32000


class Conflict(ApiError):
    """The write contradicts what is stored: a Portfolio name taken, a Trade already voided."""

    code: int = -32004


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
    """The daily sweep and backfill, and the read API over one instance's store."""

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
        except (
            UnknownUniverse,
            UnknownInstrument,
            UnknownPortfolio,
            UnknownTrade,
            NotYetStarted,
        ) as exc:
            raise NotFound(str(exc)) from exc
        except (DuplicatePortfolio, TradeAlreadyVoided) as exc:
            raise Conflict(str(exc)) from exc

    @nsnode(tags=["scheduled"])
    async def sweep(self, force: bool = False, wait: float = SWEEP_LOCK_WAIT) -> SweepOutcome:
        """Reconcile the declared universes, then sweep CoinGecko `markets` for today (UTC).

        Idempotent within a UTC day. Once every declared universe has a Run for today, a further
        firing fetches nothing and reports `already_swept`. Otherwise -- the first firing, a
        retry after a failure, a universe newly declared, or `force` -- it sweeps at the current
        time. `ingest.sweep` returns a universe's Run already recorded for the day rather than
        evaluating it again, and keeps any bar already stored for the day, so a re-sweep only
        fills in what is missing and never files a Restatement against the day's own bars.

        The universes swept are the declared ones and, once a trade has created it,
        `portfolio-held`.

        Creates the data directory and store if absent. Waits up to `wait` seconds for the sweep
        lock, since the trigger fires the node with no arguments, and raises `SweepInProgress` if
        another sweep or a backup still holds it then.
        """
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        async with sweep_lock_async(self.settings.data_dir, wait):
            # Read the clock once the lock is held: the wait may have taken minutes.
            now = as_utc(utcnow())
            day = now.date()
            with Store(self.settings.db_path) as store:
                reconciled = ensure_universes(store, self.settings.universes, day)
                names = [declared.name for declared in self.settings.universes]
                if store.load_universe(PORTFOLIO_HELD) is not None:
                    names.append(PORTFOLIO_HELD)
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

    @nsnode(tags=["scheduled"])
    async def backfill(
        self, days: int = BACKFILL_DAYS, wait: float = SWEEP_LOCK_WAIT
    ) -> BackfillReport:
        """Fill every date with no stored bar, from CoinGecko's last `days` days and from Yahoo.

        See `cointoss.backfill`. Each write waits up to `wait` seconds for the sweep lock and
        raises `SweepInProgress` if it is still held then. Refuses a data directory with no
        store, since only a sweep has universes whose members it could fill.
        """
        if not self.settings.db_path.exists():
            raise NotInitialized(
                f"no database at {self.settings.db_path}; run `cointoss init` first"
            )
        data_dir = self.settings.data_dir
        with Store(self.settings.db_path) as store:
            return await run_backfill(
                store, utcnow(), days=days, lock=lambda: sweep_lock_async(data_dir, wait)
            )

    @nsnode(tags=["api"])
    def members(self, universe: str, date: str) -> MembersView:
        """The members of a universe on a date, each with that date's close, volume and market cap.

        Bars are dated by the session they close (Session Offset), so on the latest swept date a
        CoinGecko-priced member has no close yet: its close is the next day's 00:00 price, and
        arrives with the next sweep.
        """
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
        """One Instrument's bars from `start` to `end` inclusive, through its Price Sources.

        Each is dated by the session it closes, as `Store.bars_for` resolves them.
        """
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

    @nsnode(tags=["api"])
    def datasets(self) -> list[DatasetView]:
        """Every Dataset `frame` reads, with its columns and the JSON Schema of its params."""
        return describe()

    @nsnode(tags=["api"])
    def frame(self, dataset: str, params: dict[str, Any] | None = None) -> FrameData:
        """One Dataset as a long table, `params` validated against its model (see `datasets`).

        An unknown Dataset is `NotFound`; params its model refuses, or a match over
        `cointoss.datasets.ROW_CAP` rows, are `BadRequest`. Nothing matching is an empty frame.

        An owned Dataset (`trades`, `positions`, `portfolio_value`) holds only the caller's own
        Portfolios, and another User's Portfolio named in its params is `NotFound`, as one that
        does not exist. Read in-process, with no Caller, it is `Unauthorized`, as a write is.
        """
        try:
            found = lookup(dataset)
            parsed = found.parse(params)
        except UnknownDataset as exc:
            raise NotFound(str(exc)) from None
        except InvalidParams as exc:
            raise BadRequest(str(exc)) from None
        if isinstance(found, OwnedDataset):
            owner = _caller_user(f"{dataset} reads a User's portfolios")
            read = partial(found.read, params=parsed, owner=owner)
        else:
            read = partial(found.read, params=parsed)
        with self._store() as store:
            try:
                return read(store)
            except TooManyRows as exc:
                raise BadRequest(
                    f"{dataset}: {exc.count} rows match, over the cap of {exc.limit}; "
                    "narrow the range, the instruments or the universe"
                ) from None

    @nsnode(tags=["api"])
    def create_portfolio(
        self, name: str, kind: str = PortfolioKind.REAL, custodian: str | None = None
    ) -> Portfolio:
        """Create a Portfolio owned by the caller. `kind` is `real`, `paper` or `what_if`.

        A name is unique among the caller's own Portfolios; reusing one is a `Conflict`.
        """
        owner = _writer()
        portfolio_kind = _choice(PortfolioKind, kind, "kind")
        if not name.strip():
            raise BadRequest("name: a portfolio needs a name")
        with self._store() as store:
            return store.create_portfolio(owner, name.strip(), portfolio_kind, custodian)

    @nsnode(tags=["api"])
    async def add_trade(
        self,
        portfolio_id: int,
        instrument: str,
        quantity: float,
        cost: float | None = None,
        traded_at: str | None = None,
        kind: str = TradeKind.REAL,
        note: str | None = None,
        source: str = Source.COINGECKO,
        coingecko_id: str | None = None,
    ) -> Trade:
        """Record a Trade in one of the caller's Portfolios, and hold its Instrument for pricing.

        `instrument` is a stored Instrument Id (`type.scope.symbol`) or a symbol typed against
        `source`, pinned as `cointoss.ingest.resolve_typed_symbol` pins one, with its mandatory
        FIGI attempt; `coingecko_id` names the coin when a CoinGecko symbol is shared. A positive
        `quantity` is a buy, a negative one a sale; `cost` is the Trade's total in USD.
        `traded_at` is an ISO time, read as UTC when it has no offset, and now by default.

        The Instrument is included in `portfolio-held`, created on first use, so the next
        sweep writes its bar even when it ranks outside every declared universe.

        The other arguments are checked before the Instrument is named, since pinning a symbol
        stores its Instrument. The Inclusion is written before the Trade, each in its own commit:
        should the Trade then fail, an Instrument held with no Trade only costs the sweep a
        bar, while a Trade whose Instrument is not held would go unpriced.
        """
        owner = _writer()
        now = as_utc(utcnow())
        when = now if traded_at is None else _moment(traded_at, "traded_at")
        trade_kind = _choice(TradeKind, kind, "kind")
        typed_source = _choice(Source, source, "source")
        checked_quantity = _checked(check_quantity, quantity, "quantity")
        checked_cost = None if cost is None else _checked(check_cost, cost, "cost")
        with self._store() as store:
            _require_owned(store, owner, portfolio_id)
            named, by_source, by_symbol = await _named_instrument(
                store, instrument, typed_source, now.date(), coingecko_id
            )
            ingest.hold(store, named, by_source, by_symbol, now.date())
            try:
                return store.record_trade(
                    portfolio_id,
                    named,
                    when,
                    checked_quantity,
                    kind=trade_kind,
                    cost=checked_cost,
                    note=note,
                )
            except ValidationError as exc:
                raise BadRequest(one_line(exc)) from None

    @nsnode(tags=["api"])
    def void_trade(self, trade_id: int) -> Trade:
        """Void one of the caller's Trades now. It stays recorded and counts toward nothing.

        A Trade is voided once: voiding it again is a `Conflict`, and the first Void stands.
        """
        owner = _writer()
        with self._store() as store:
            trade = store.load_trade(trade_id)
            if trade is None or not _owns(store, owner, trade.portfolio_id):
                raise NotFound(f"no trade {trade_id} of yours")
            return store.void_trade(trade_id, at=as_utc(utcnow()))


def _writer() -> UserId:
    """The User a portfolio write runs as, once the Caller is shown to hold the write Grant."""
    check(Action.call, WRITE_TARGET)
    return _caller_user("a portfolio write runs as a User")


def _caller_user(why: str) -> UserId:
    """The Caller's User id, `anonymous` with auth disabled; `Unauthorized` with no Caller.

    An owned Dataset reads as this User with no Grant beyond the namespace's, so the explorer
    reads as its viewer; a write checks its Grant first.
    """
    caller = current_caller.get()
    if caller is None:
        raise Unauthorized(f"{why}; call it over JSON-RPC")
    return caller.user_id or ANONYMOUS.name


def _owns(store: Store, owner: UserId, portfolio_id: int) -> bool:
    portfolio = store.load_portfolio(portfolio_id)
    return portfolio is not None and portfolio.owner == owner


def _require_owned(store: Store, owner: UserId, portfolio_id: int) -> None:
    if not _owns(store, owner, portfolio_id):
        raise NotFound(f"no portfolio {portfolio_id} of yours")


async def _named_instrument(
    store: Store, instrument: str, source: Source, at: dt.date, coingecko_id: str | None
) -> tuple[InstrumentId, Source, str]:
    """The stored Instrument a trade names, with the source and symbol it was named by.

    Text with the two dots of `type.scope.symbol` is an Id, which must be stored, and is
    recorded by its Identity Source and display symbol. Anything else is a symbol, pinned
    against `source`.
    """
    if instrument.count(".") == 2:
        try:
            stored = store.load_instrument(InstrumentId(instrument))
        except (ValueError, InstrumentError) as exc:
            raise BadRequest(f"instrument: {exc}") from None
        if stored is None:
            raise NotFound(f"{instrument} is not stored")
        return stored.id, stored.identity_source, stored.symbol
    try:
        pinned = await ingest.resolve_typed_symbol(
            store, source, instrument, at, coingecko_id=coingecko_id
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if pinned is None:
        raise NotFound(f"{instrument!r} typed against {source} names no single Instrument")
    return pinned, source, instrument


def _choice(enum: type[E], value: str, name: str) -> E:
    """`value` as a member of `enum`, or `BadRequest` naming the parameter and the choices.

    >>> _choice(PortfolioKind, "paper", "kind")
    <PortfolioKind.PAPER: 'paper'>
    """
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise BadRequest(f"{name}: {value!r} is not one of {allowed}") from None


def one_line(exc: Exception, *, whole: str = "") -> str:
    """An error's message on one line: each validation error's field and reason, or the text.

    A validation error of the whole model, with no field, is labelled `whole` when given.

    >>> one_line(ValueError("not   here"))
    'not here'
    """
    if not isinstance(exc, ValidationError):
        return " ".join(str(exc).split())
    reasons: list[str] = []
    for error in exc.errors():
        label = ".".join(str(part) for part in error["loc"]) or whole
        reasons.append(f"{label}: {error['msg']}" if label else error["msg"])
    return "; ".join(reasons)


def _checked(check: Callable[[float], float], value: float, name: str) -> float:
    """`value` passed through `check`, or `BadRequest` naming the parameter.

    >>> _checked(check_quantity, 2, "quantity")
    2.0
    """
    try:
        return check(float(value))
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{name}: {exc}") from None


def _moment(value: str, name: str) -> dt.datetime:
    try:
        return as_utc(dt.datetime.fromisoformat(value))
    except (TypeError, ValueError):
        raise BadRequest(f"{name}: {value!r} is not an ISO time (YYYY-MM-DDTHH:MM)") from None


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
