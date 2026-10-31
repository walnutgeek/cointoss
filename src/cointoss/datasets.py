"""Datasets: named, typed, long-form tables read through the `frame` node (ADR-0010).

A `Dataset` is a definition in code: a name, a description, a pydantic params model, its
columns, and a reader that turns the store and validated params into rows. `DATASETS` is the
registry the `datasets` and `frame` nodes on `cointoss.app.CointossApp` serve, so adding a
Dataset means adding one definition here, plus a store read if no existing one fits. Nothing
here touches SQL: rows come through `cointoss.store` (ADR-0008).

Every Dataset is long, one row per fact with a fixed column list, and every column declares a
type and a role so a client can pick axes and series without guessing from names. Rows about
an Instrument carry its survivor's `instrument_id` and current `symbol`; the rest of its
attributes are in the `instruments` Dataset.

woodglue serializes a node's pydantic result with `model_dump(mode="json")`, so a date reaches
a JSON-RPC client as an ISO string. `load` reads the column types `datasets` publishes and puts
them back on a pandas DataFrame. pandas is imported only there, from the `ui` extra.

A response holds at most `ROW_CAP` rows. The cap is read when a frame is read, so a test can
patch it. Over it is `TooManyRows` naming the matching count; there is no paging.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Generic, Self, TypeVar, cast

from lythonic.frame import FrameData
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from cointoss.instrument import InstrumentId, Source
from cointoss.store import Store, TooManyRows, UnknownInstrument

if TYPE_CHECKING:
    import pandas as pd
    from woodglue.client import WoodglueClient

__all__ = [
    "DATASETS",
    "ROW_CAP",
    "Column",
    "ColumnRole",
    "ColumnType",
    "Dataset",
    "DatasetView",
    "InvalidParams",
    "UnknownDataset",
    "describe",
    "load",
    "lookup",
]

ROW_CAP = 250_000
"""The most rows one `frame` call returns."""

DEFAULT_NAMESPACE = "cointoss.data"
"""The JSON-RPC method prefix `cointoss init` mounts the fragment under."""


class UnknownDataset(LookupError):
    """No Dataset by that name."""


class InvalidParams(ValueError):
    """Params a Dataset's model refused, with the first error on one line."""


class ColumnType(StrEnum):
    DATE = "date"
    DATETIME = "datetime"
    STR = "str"
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"


class ColumnRole(StrEnum):
    """What a column is for: the time axis, the thing a row is about, a value, or a label."""

    TIME = "time"
    ENTITY = "entity"
    MEASURE = "measure"
    ATTRIBUTE = "attribute"


class Column(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    type: ColumnType
    role: ColumnRole
    description: str


class DatasetView(BaseModel):
    """A Dataset as `datasets` lists it: its columns and the JSON Schema of its params."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str
    description: str
    columns: list[Column]
    params: dict[str, Any]


P = TypeVar("P", bound=BaseModel)


@dataclass(frozen=True)
class Dataset(Generic[P]):
    """One Dataset's definition. `rows` returns each row's values in `columns` order."""

    name: str
    description: str
    params: type[P]
    columns: tuple[Column, ...]
    rows: Callable[[Store, P], Sequence[Sequence[Any]]]

    def parse(self, params: dict[str, Any] | None) -> P:
        """Validate raw params, raising `InvalidParams` with the first error on one line."""
        try:
            return self.params.model_validate(params or {})
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            message = first["msg"] if not where else f"{where}: {first['msg']}"
            raise InvalidParams(f"{self.name} params: {message}") from None

    def read(self, store: Store, params: P) -> FrameData:
        """The Dataset as a frame, raising `TooManyRows` past `ROW_CAP`."""
        data = [list(row) for row in self.rows(store, params)]
        if len(data) > ROW_CAP:
            raise TooManyRows(len(data), ROW_CAP)
        return FrameData(columns=[c.name for c in self.columns], data=data)

    def view(self) -> DatasetView:
        return DatasetView(
            name=self.name,
            description=self.description,
            columns=list(self.columns),
            params=self.params.model_json_schema(),
        )


def _iso_date(value: Any) -> Any:
    """Accept a date as `YYYY-MM-DD` text only, as JSON carries it, or a date already parsed."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("expected a date as YYYY-MM-DD text")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a date as YYYY-MM-DD") from None


IsoDate = Annotated[
    dt.date, BeforeValidator(_iso_date), Field(json_schema_extra={"format": "date"})
]


class _Params(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class InstrumentParams(_Params):
    instruments: list[str] | None = Field(
        default=None, description="Instrument Ids to list; every stored Instrument if omitted"
    )


class _RangeParams(_Params):
    start: IsoDate = Field(description="First date of the range, inclusive (YYYY-MM-DD)")
    end: IsoDate = Field(description="Last date of the range, inclusive (YYYY-MM-DD)")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"end {self.end} precedes start {self.start}")
        return self


_INSTRUMENTS = "Instrument Ids to narrow to, each read as its survivor; all if omitted"


class PriceParams(_RangeParams):
    instruments: list[str] | None = Field(default=None, description=_INSTRUMENTS)
    source: Source | None = Field(
        default=None,
        description="Read only this source's bars, under their stamped dates, instead of the "
        "Price Sources",
    )
    universe: str | None = Field(
        default=None, description="Keep a row only if its Instrument was a member on its date"
    )
    ever: bool = Field(
        default=False,
        description="With `universe`, keep members on any date of the range instead",
    )

    @model_validator(mode="after")
    def _ever_needs_universe(self) -> Self:
        if self.ever and self.universe is None:
            raise ValueError("ever applies only with a universe")
        return self


class MembershipParams(_RangeParams):
    universe: str | None = Field(default=None, description="One universe; all if omitted")
    instruments: list[str] | None = Field(default=None, description=_INSTRUMENTS)


class RunParams(_RangeParams):
    universe: str | None = Field(default=None, description="One universe; all if omitted")


def _ids(instruments: list[str] | None) -> list[InstrumentId] | None:
    return None if instruments is None else [InstrumentId(i) for i in instruments]


def _col(name: str, type_: ColumnType, role: ColumnRole, description: str) -> Column:
    return Column(name=name, type=type_, role=role, description=description)


T, E, M, A = ColumnRole.TIME, ColumnRole.ENTITY, ColumnRole.MEASURE, ColumnRole.ATTRIBUTE
DATE, DATETIME, STR, FLOAT, INT = (
    ColumnType.DATE,
    ColumnType.DATETIME,
    ColumnType.STR,
    ColumnType.FLOAT,
    ColumnType.INT,
)
INSTRUMENT_ID = _col("instrument_id", STR, E, "Instrument Id, of the survivor where superseded")
SYMBOL = _col("symbol", STR, A, "The Instrument's current symbol")


def _instrument_rows(store: Store, params: InstrumentParams) -> list[tuple[Any, ...]]:
    instruments = store.load_instruments()
    if params.instruments is not None:
        wanted = set(params.instruments)
        missing = wanted - {str(i.id) for i in instruments}
        if missing:
            raise UnknownInstrument(f"{', '.join(sorted(missing))} not stored")
        instruments = [i for i in instruments if str(i.id) in wanted]
    return [
        (
            str(i.id),
            i.symbol,
            i.name,
            i.type.value,
            str(i.scope),
            i.issuer_name,
            i.identity_source.value,
            ",".join(s.value for s in i.price_sources),
            i.figi_resolution.value,
            i.minted_at,
            None if i.superseded_by is None else str(i.superseded_by),
        )
        for i in sorted(instruments, key=lambda i: str(i.id))
    ]


def _bar_rows(store: Store, params: PriceParams) -> list[tuple[Any, ...]]:
    return [
        (
            b.bar_date,
            str(b.instrument_id),
            b.symbol,
            b.source.value,
            b.open,
            b.high,
            b.low,
            b.close,
            b.adj_close,
            b.volume,
            b.market_cap,
            b.fetched_at,
        )
        for b in store.resolved_bars(
            params.start,
            params.end,
            instruments=_ids(params.instruments),
            source=params.source,
            universe=params.universe,
            ever=params.ever,
            limit=ROW_CAP,
        )
    ]


def _return_rows(store: Store, params: PriceParams) -> list[tuple[Any, ...]]:
    return [
        (
            p.bar_date,
            str(p.instrument_id),
            p.symbol,
            p.source.value,
            p.prev_date,
            (p.bar_date - p.prev_date).days,
            p.close,
            # A zero close cannot start a return; None says so where inf would mislead.
            None if p.prev_close == 0 else p.close / p.prev_close - 1.0,
        )
        for p in store.close_pairs(
            params.start,
            params.end,
            instruments=_ids(params.instruments),
            source=params.source,
            universe=params.universe,
            ever=params.ever,
            limit=ROW_CAP,
        )
    ]


def _membership_rows(store: Store, params: MembershipParams) -> list[tuple[Any, ...]]:
    return [
        (m.universe, str(m.instrument_id), m.symbol, m.valid_from, m.valid_to)
        for m in store.membership_intervals(
            params.start, params.end, universe=params.universe, instruments=_ids(params.instruments)
        )
        # An interval closed on `start` ended before the range; it reaches `changes` only.
        if m.valid_to is None or m.valid_to > params.start
    ]


def _change_rows(store: Store, params: MembershipParams) -> list[tuple[Any, ...]]:
    changes: list[tuple[Any, ...]] = []
    for m in store.membership_intervals(
        params.start, params.end, universe=params.universe, instruments=_ids(params.instruments)
    ):
        if params.start <= m.valid_from:
            changes.append((m.valid_from, m.universe, str(m.instrument_id), m.symbol, "joined"))
        if m.valid_to is not None and m.valid_to <= params.end:
            changes.append((m.valid_to, m.universe, str(m.instrument_id), m.symbol, "left"))
    return sorted(changes)


def _run_rows(store: Store, params: RunParams) -> list[tuple[Any, ...]]:
    names = store.universe_names() if params.universe is None else [params.universe]
    return sorted(
        (
            r.source_asof,
            r.definition,
            r.run_at,
            r.revision,
            r.outcome.value,
            r.n_admitted,
            r.n_dropped,
            r.n_unresolved,
        )
        for name in names
        for r in store.load_evaluation_runs(name)
        if params.start <= r.source_asof <= params.end
    )


DATASETS: dict[str, Dataset[Any]] = {
    d.name: d
    for d in (
        Dataset(
            name="instruments",
            description="Every stored Instrument, superseded ones included, by Instrument Id.",
            params=InstrumentParams,
            columns=(
                _col("instrument_id", STR, E, "Instrument Id, as minted"),
                SYMBOL,
                _col("name", STR, A, "Display name"),
                _col("type", STR, A, "Instrument type: crypto or stock"),
                _col("scope", STR, A, "Authority under which the symbol is unique"),
                _col("issuer_name", STR, A, "Issuer, where a source reports one"),
                _col("identity_source", STR, A, "Source whose name and symbol are taken"),
                _col("price_sources", STR, A, "Price Sources in preference order, comma separated"),
                _col("figi_resolution", STR, A, "Outcome of anchoring to a FIGI"),
                _col("minted_at", DATE, A, "Observation date the Instrument was minted from"),
                _col("superseded_by", STR, A, "Instrument Id this one was found to be, if any"),
            ),
            rows=_instrument_rows,
        ),
        Dataset(
            name="bars",
            description=(
                "One Bar per Instrument and date, resolved through its Price Sources unless "
                "`source` names one. Resolved Bars are dated by the session they close, so "
                "CoinGecko's Bar stamped D+1 is D's (Session Offset); with `source`, Bars carry "
                "the date that source stamped."
            ),
            params=PriceParams,
            columns=(
                _col("date", DATE, T, "Session closed; the stamped date with `source`"),
                INSTRUMENT_ID,
                SYMBOL,
                _col("source", STR, A, "Source the Bar came from"),
                _col("open", FLOAT, M, "Open, null for a close-only source"),
                _col("high", FLOAT, M, "High, null for a close-only source"),
                _col("low", FLOAT, M, "Low, null for a close-only source"),
                _col("close", FLOAT, M, "Close"),
                _col("adj_close", FLOAT, M, "Dividend-adjusted close, where reported"),
                _col("volume", FLOAT, M, "Traded volume"),
                _col("market_cap", FLOAT, M, "Market cap, where reported"),
                _col("fetched_at", DATETIME, A, "When the stored vintage was fetched"),
            ),
            rows=_bar_rows,
        ),
        Dataset(
            name="returns",
            description=(
                "Simple close-to-close returns, derived on read, between the Bars `bars` gives "
                "for a date and for the Instrument's previous date with one. A date whose Bar "
                "comes from another source than the previous one has no return: sources are "
                "never spliced. A gap gives one return over the whole gap, and the first date "
                "of the range has one from the Bar before it."
            ),
            params=PriceParams,
            columns=(
                _col("date", DATE, T, "Date of the later close, dated as in `bars`"),
                INSTRUMENT_ID,
                SYMBOL,
                _col("source", STR, A, "Source both closes came from"),
                _col("prev_date", DATE, A, "Date of the earlier close, dated as in `bars`"),
                _col("gap_days", INT, A, "Days between the two closes; 1 when consecutive"),
                _col("close", FLOAT, M, "The later close"),
                _col("simple_return", FLOAT, M, "close / previous close - 1"),
            ),
            rows=_return_rows,
        ),
        Dataset(
            name="membership",
            description="One row per stretch an Instrument spent in a universe, in force in the range.",
            params=MembershipParams,
            columns=(
                _col("universe", STR, A, "Universe Definition name"),
                INSTRUMENT_ID,
                SYMBOL,
                _col("valid_from", DATE, T, "First date a member"),
                _col("valid_to", DATE, A, "First date no longer a member; null while a member"),
            ),
            rows=_membership_rows,
        ),
        Dataset(
            name="changes",
            description="One row per date an Instrument joined or left a universe in the range.",
            params=MembershipParams,
            columns=(
                _col("date", DATE, T, "Date of the change; a leaver is absent from this date"),
                _col("universe", STR, A, "Universe Definition name"),
                INSTRUMENT_ID,
                SYMBOL,
                _col("change", STR, A, "joined or left"),
            ),
            rows=_change_rows,
        ),
        Dataset(
            name="runs",
            description="Evaluation Runs whose source data is dated in the range.",
            params=RunParams,
            columns=(
                _col("source_asof", DATE, T, "Date of the source data evaluated"),
                _col("universe", STR, E, "Universe Definition name"),
                _col("run_at", DATETIME, A, "When the job executed"),
                _col("revision", INT, A, "Definition revision it ran under"),
                _col("outcome", STR, A, "changed or unchanged"),
                _col("n_admitted", INT, M, "Instruments that joined"),
                _col("n_dropped", INT, M, "Instruments that left"),
                _col("n_unresolved", INT, M, "Standing members with no Instrument"),
            ),
            rows=_run_rows,
        ),
    )
}


def lookup(name: str) -> Dataset[Any]:
    try:
        return DATASETS[name]
    except KeyError:
        raise UnknownDataset(f"no Dataset {name!r}; known: {', '.join(DATASETS)}") from None


def describe() -> list[DatasetView]:
    return [d.view() for d in DATASETS.values()]


_PANDAS_DTYPES = {
    ColumnType.STR: "string",
    ColumnType.FLOAT: "float64",
    ColumnType.INT: "Int64",
    ColumnType.BOOL: "boolean",
}


async def load(
    client: WoodglueClient,
    name: str,
    /,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    **params: Any,
) -> pd.DataFrame:
    """Read a Dataset over JSON-RPC into a pandas DataFrame with its column types applied.

    Async, because `WoodglueClient.call` is; a sync caller such as Streamlit wraps it in
    `asyncio.run`. Calls `datasets` for the column types, then
    `frame`. A `date` column becomes naive `datetime64` at midnight and a `datetime` column
    UTC-aware `datetime64`, so both chart and resample directly; a nullable `int` or `bool`
    column uses pandas' nullable dtype. `namespace` is the method prefix the fragment is
    mounted under. A JSON-RPC error raises `woodglue.client.WoodglueRpcError`.

    Needs pandas, from the `ui` extra.
    """
    import pandas as pd

    described = await client.call(f"{namespace}:datasets")
    types = {
        c["name"]: ColumnType(c["type"])
        for d in described
        if d["name"] == name
        for c in d["columns"]
    }
    frame = FrameData.model_validate(
        await client.call(f"{namespace}:frame", dataset=name, params=params)
    )
    df = frame.pd.frame()
    for column, type_ in types.items():
        if column not in df.columns:
            continue
        values = cast("pd.Series", df[column])
        if type_ is ColumnType.DATE:
            df[column] = pd.to_datetime(values, format="ISO8601")
        elif type_ is ColumnType.DATETIME:
            df[column] = pd.to_datetime(values, format="ISO8601", utc=True)
        else:
            df[column] = values.astype(_PANDAS_DTYPES[type_])
    return df
