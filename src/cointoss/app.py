"""The cointoss namespace: the scheduled daily sweep and the read API, issue #21.

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
        init: {data_dir: /home/me/.local/share/cointoss}   # optional
        configs:
          sweep:
            triggers: [{name: daily_sweep, schedule: "5 0 * * *"}]
```

The read nodes are tagged `api`, so woodglue serves them over JSON-RPC as `cointoss.data:members`
and so on. `sweep` is not: it writes and calls CoinGecko, so it runs from its trigger, from
woodglue's `system.fire_trigger`, or from the CLI, never from an ordinary API call.

Runtime settings come from the data directory (`cointoss.config.Settings`): `init.data_dir` if
given, else `$COINTOSS_HOME`, else `~/.local/share/cointoss`. They are read once, when the
fragment is built, so a bad `cointoss.yaml` stops woodglue at startup rather than failing the
next sweep silently. A config edit takes effect on restart.

Each call opens the store and closes it before returning, rather than holding one connection
for the fragment's life. woodglue calls a sync node on the IOLoop thread while lythonic's
`DagRunner` runs one in an executor thread, and a `sqlite3` connection refuses use from a thread
other than its creator's; the store's WAL journal lets these short readers run beside a sweep's
writes; and nothing is left holding a transaction or a replaced file. Opening costs the schema
and version check, a few small queries, which a local API at human rates does not notice.

A request the store cannot answer -- an unknown universe or Instrument, a date before a
universe's first entry, a malformed date -- raises an `ApiError` carrying a JSON-RPC error code
and a message naming what was not found. woodglue 0.0.6 answers any exception from a node with
`-32603 Internal error` and logs the traceback server-side, so the message reaches a Python
caller but not yet a JSON-RPC client.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

from lythonic.compose.namespace import NamespaceFragment, nsnode
from pydantic import BaseModel, ConfigDict

from cointoss import ingest
from cointoss.config import Settings, UniverseReconciled, ensure_universes
from cointoss.instrument import InstrumentId, Source
from cointoss.prices import Bar, as_utc
from cointoss.series import NotYetStarted
from cointoss.store import Store, UnknownInstrument, UnknownUniverse
from cointoss.universe import EvaluationRun

__all__ = [
    "DEFAULT_SWEEP_SCHEDULE",
    "SWEEP_TRIGGER",
    "ApiError",
    "BadRequest",
    "ChangeView",
    "CointossApp",
    "MemberView",
    "MembersView",
    "NotFound",
    "SweepOutcome",
    "UniverseView",
    "fragment_entry",
    "utcnow",
]

log = logging.getLogger(__name__)

DEFAULT_SWEEP_SCHEDULE = "5 0 * * *"
"""Five past midnight UTC: after CoinGecko's 00:00 point exists, and early enough to be it."""

SWEEP_TRIGGER = "daily_sweep"


def utcnow() -> dt.datetime:
    """The clock the sweep reads; a module function so a test can fix it."""
    return dt.datetime.now(dt.UTC)


def fragment_entry(
    *,
    data_dir: Path | None = None,
    schedule: str = DEFAULT_SWEEP_SCHEDULE,
    nsref: str = "data:",
) -> dict[str, Any]:
    """The `woodglue.yaml` namespace entry mounting the fragment with its daily sweep trigger.

    lythonic reads triggers only from configuration, so the default schedule lives here and in
    whatever `cointoss init` writes from it. Without `data_dir` the fragment resolves the data
    directory itself when it is built.
    """
    entry: dict[str, Any] = {
        "type": "fragment",
        "gref": "cointoss.app:CointossApp",
        "nsref": nsref,
        "configs": {"sweep": {"triggers": [{"name": SWEEP_TRIGGER, "schedule": schedule}]}},
    }
    if data_dir is not None:
        entry["init"] = {"data_dir": str(data_dir)}
    return entry


class ApiError(Exception):
    """A request the store cannot answer, with the JSON-RPC error code it stands for."""

    code: ClassVar[int] = -32000


class NotFound(ApiError):
    """The universe or Instrument named is not stored, or holds nothing on the date asked."""

    code: ClassVar[int] = -32001


class BadRequest(ApiError):
    """A parameter is malformed: not an ISO date, a range ending before it starts."""

    code: ClassVar[int] = -32602


class MemberView(BaseModel):
    """One member on a date and its bar for that date, if one is stored.

    Close and volume are what a CoinGecko snapshot bar holds. Market cap and rank are not
    persisted, so they are not here.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    instrument_id: str
    symbol: str | None
    name: str | None
    close: float | None
    volume: float | None
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
    """What one firing of the sweep did.

    `at` is the moment the day's data is stamped with. `already_swept` means every declared
    universe already had a Run for `date`, so nothing was fetched and `report` is None.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    date: dt.date
    at: dt.datetime
    already_swept: bool
    reconciled: list[UniverseReconciled]
    report: ingest.SweepReport | None


class CointossApp(NamespaceFragment):
    """The daily sweep and the read API over one instance's store."""

    settings: Settings

    def __init__(self, data_dir: str | None = None) -> None:
        self.settings = Settings.load(Path(data_dir) if data_dir is not None else None)

    @contextmanager
    def _store(self) -> Iterator[Store]:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Store(self.settings.db_path) as store:
                yield store
        except (UnknownUniverse, UnknownInstrument, NotYetStarted) as exc:
            raise NotFound(str(exc)) from exc

    @nsnode(tags=["scheduled"])
    async def sweep(self, force: bool = False) -> SweepOutcome:
        """Reconcile the declared universes, then sweep CoinGecko `markets` for today (UTC).

        Idempotent within a UTC day. Once every declared universe has a Run for today, a further
        firing fetches nothing and reports `already_swept`. Otherwise -- the first firing, a
        retry after a failure, a universe newly declared, or `force` -- the sweep is stamped with
        the `run_at` of the day's earliest Run if there is one, else the current time. Reusing
        the day's moment is what lets a retry return the Runs already recorded instead of being
        refused for a second membership on the same date. A forced re-sweep still re-fetches
        prices, so a price that has moved since is filed as a Restatement.
        """
        now = utcnow()
        day = as_utc(now).date()
        names = [spec.name for spec in self.settings.universes]
        with self._store() as store:
            reconciled = ensure_universes(store, self.settings.universes, day)
            held = {
                run.definition: as_utc(run.run_at)
                for name in names
                for run in store.load_evaluation_runs(name)
                if run.source_asof == day
            }
            at = min(held.values(), default=as_utc(now))
            if not force and names and held.keys() >= set(names):
                log.info("sweep %s: already swept at %s", day, at.isoformat())
                return SweepOutcome(
                    date=day, at=at, already_swept=True, reconciled=reconciled, report=None
                )
            report = await ingest.sweep(store, at, names, top_n=self.settings.top_n)
        log.info(
            "sweep %s: listed %d, %d bars, %d restatements, failed %s",
            day,
            report.listed,
            report.bars,
            report.restatements,
            sorted(report.failed) or "none",
        )
        return SweepOutcome(
            date=day, at=at, already_swept=False, reconciled=reconciled, report=report
        )

    @nsnode(tags=["api"])
    def members(self, universe: str, date: str) -> MembersView:
        """The members of a universe on a date, each with that date's close and volume."""
        on = _date(date, "date")
        with self._store() as store:
            held = store.member_bars(universe, on)
            instruments = {i: store.load_instrument(i) for i in held}
        views: list[MemberView] = []
        for instrument_id, bar in held.items():
            instrument = instruments[instrument_id]
            views.append(
                MemberView(
                    instrument_id=str(instrument_id),
                    symbol=instrument.symbol if instrument else None,
                    name=instrument.name if instrument else None,
                    close=bar.close if bar else None,
                    volume=bar.volume if bar else None,
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
