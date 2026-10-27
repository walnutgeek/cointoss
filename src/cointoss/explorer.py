"""What the Dataset explorer decides, kept out of its Streamlit script (`cointoss.explorer_app`).

The explorer knows only what `datasets()` describes (ADR-0010). From a Dataset's params JSON
Schema it derives one input widget per parameter; from its column roles it derives a chart
(the `time` column on x, a chosen `measure` on y, one line per `entity`) and a pivot (time by
entity for one measure). The functions here are plain, so they are tested without Streamlit.

Three parameter names get richer widgets than their schema type gives: `universe` picks from
the stored universes, `instruments` from the `instruments` Dataset, and a date named `start`
defaults to `DEFAULT_LOOKBACK` before today. A measure whose name ends in `_return` is a simple
return, so the chart can compound it.

Cached reads are keyed on the viewer as well as the request (`viewer_key`), so one viewer's
data is never served to another.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import pandas as pd
from woodglue.ui_support import ViewingUser

from cointoss.datasets import ColumnRole, ColumnType

__all__ = [
    "DEFAULT_LOOKBACK",
    "ROW_COUNT",
    "ChartSpec",
    "ParamWidget",
    "WidgetKind",
    "build_params",
    "chart_frame",
    "chart_spec",
    "header",
    "param_widgets",
    "pivot",
    "viewer_key",
]

DEFAULT_LOOKBACK = dt.timedelta(days=90)

ROW_COUNT = "(row count)"
"""The measure charted for a Dataset with a time column but no measure: rows per time."""

RETURN_SUFFIX = "_return"


class WidgetKind(StrEnum):
    DATE = "date"
    BOOL = "bool"
    CHOICE = "choice"
    UNIVERSE = "universe"
    INSTRUMENTS = "instruments"
    TEXT_LIST = "text_list"
    TEXT = "text"
    INT = "int"
    FLOAT = "float"


@dataclass(frozen=True)
class ParamWidget:
    """One parameter's input. An optional parameter left empty is not sent."""

    name: str
    kind: WidgetKind
    label: str
    help: str
    required: bool
    default: Any
    options: tuple[str, ...] = ()


def _resolve(prop: Mapping[str, Any], defs: Mapping[str, Any]) -> Mapping[str, Any]:
    """A property's schema with `X | None` unwrapped and a `$ref` followed."""
    variants = [v for v in prop.get("anyOf", [prop]) if v.get("type") != "null"]
    schema = variants[0] if len(variants) == 1 else prop
    ref = schema.get("$ref")
    if isinstance(ref, str):
        schema = defs[ref.rsplit("/", 1)[-1]]
    return schema


def _kind(name: str, schema: Mapping[str, Any]) -> WidgetKind:
    type_ = schema.get("type")
    if "enum" in schema:
        return WidgetKind.CHOICE
    if type_ == "string" and schema.get("format") == "date":
        return WidgetKind.DATE
    if type_ == "string":
        return WidgetKind.UNIVERSE if name == "universe" else WidgetKind.TEXT
    if type_ == "array":
        return WidgetKind.INSTRUMENTS if name == "instruments" else WidgetKind.TEXT_LIST
    if type_ == "boolean":
        return WidgetKind.BOOL
    if type_ == "integer":
        return WidgetKind.INT
    if type_ == "number":
        return WidgetKind.FLOAT
    return WidgetKind.TEXT


def param_widgets(schema: Mapping[str, Any], today: dt.date) -> list[ParamWidget]:
    """The widgets for a params JSON Schema, in property order.

    >>> schema = {
    ...     "properties": {
    ...         "start": {"type": "string", "format": "date", "title": "Start"},
    ...         "end": {"type": "string", "format": "date"},
    ...         "source": {"anyOf": [{"$ref": "#/$defs/S"}, {"type": "null"}], "default": None},
    ...         "universe": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    ...         "ever": {"type": "boolean", "default": False},
    ...     },
    ...     "required": ["start", "end"],
    ...     "$defs": {"S": {"enum": ["yahoo", "coingecko"], "type": "string"}},
    ... }
    >>> for w in param_widgets(schema, dt.date(2026, 10, 23)):
    ...     print(w.name, w.kind, w.required, w.default, w.options)
    start date True 2026-07-25 ()
    end date True 2026-10-23 ()
    source choice False None ('yahoo', 'coingecko')
    universe universe False None ()
    ever bool False False ()
    """
    defs: Mapping[str, Any] = schema.get("$defs", {})
    required = set(schema.get("required", ()))
    widgets: list[ParamWidget] = []
    for name, prop in schema.get("properties", {}).items():
        resolved = _resolve(prop, defs)
        kind = _kind(name, resolved)
        default = prop.get("default")
        if kind is WidgetKind.DATE:
            default = today - DEFAULT_LOOKBACK if name == "start" else today
        widgets.append(
            ParamWidget(
                name=name,
                kind=kind,
                label=prop.get("title") or name.replace("_", " ").capitalize(),
                help=prop.get("description", ""),
                required=name in required,
                default=default,
                options=tuple(str(o) for o in resolved.get("enum", ())),
            )
        )
    return widgets


def build_params(widgets: Sequence[ParamWidget], values: Mapping[str, Any]) -> dict[str, Any]:
    """The `frame` params for widget values: dates as ISO text, empty optional values left out.

    A boolean is sent only when it differs from its default, so an unticked flag is not sent.

    >>> ws = param_widgets(
    ...     {
    ...         "properties": {
    ...             "start": {"type": "string", "format": "date"},
    ...             "instruments": {"type": "array", "items": {"type": "string"}},
    ...             "universe": {"type": "string"},
    ...             "ever": {"type": "boolean", "default": False},
    ...         },
    ...         "required": ["start"],
    ...     },
    ...     dt.date(2026, 1, 1),
    ... )
    >>> build_params(ws, {"start": dt.date(2026, 1, 2), "instruments": [], "universe": "", "ever": False})
    {'start': '2026-01-02'}
    >>> build_params(ws, {"start": dt.date(2026, 1, 2), "instruments": ["a"], "ever": True})
    {'start': '2026-01-02', 'instruments': ['a'], 'ever': True}
    """
    params: dict[str, Any] = {}
    for widget in widgets:
        value = values.get(widget.name)
        if value is None or value == "" or value == []:
            continue
        if widget.kind is WidgetKind.BOOL and value == widget.default:
            continue
        if isinstance(value, dt.date):
            value = value.isoformat()
        params[widget.name] = value
    return params


@dataclass(frozen=True)
class ChartSpec:
    """How a Dataset charts: `time` on x, one of `measures` on y, a line per `entity` value.

    `compounding` are the measures that are simple returns, which the chart can show
    cumulatively. Without an `entity` column, there is one line.
    """

    time: str
    entity: str | None
    measures: tuple[str, ...]
    compounding: frozenset[str]


_NUMERIC = {ColumnType.FLOAT, ColumnType.INT}


def chart_spec(columns: Sequence[Mapping[str, Any]]) -> ChartSpec | None:
    """The chart for a Dataset's columns as `datasets()` lists them; None without a time column.

    A Dataset with no numeric measure charts its row count per time instead.

    >>> def col(name, type_, role):
    ...     return {"name": name, "type": type_, "role": role}
    >>> chart_spec([
    ...     col("date", "date", "time"), col("instrument_id", "str", "entity"),
    ...     col("close", "float", "measure"), col("simple_return", "float", "measure"),
    ... ])
    ChartSpec(time='date', entity='instrument_id', measures=('close', 'simple_return'), compounding=frozenset({'simple_return'}))
    >>> chart_spec([col("valid_from", "date", "time"), col("universe", "str", "attribute")])
    ChartSpec(time='valid_from', entity=None, measures=('(row count)',), compounding=frozenset())
    >>> chart_spec([col("instrument_id", "str", "entity")]) is None
    True
    """

    def named(role: ColumnRole) -> list[Mapping[str, Any]]:
        return [c for c in columns if c["role"] == role]

    times = named(ColumnRole.TIME)
    if not times:
        return None
    entities = named(ColumnRole.ENTITY)
    measures = tuple(c["name"] for c in named(ColumnRole.MEASURE) if c["type"] in _NUMERIC)
    return ChartSpec(
        time=times[0]["name"],
        entity=entities[0]["name"] if entities else None,
        measures=measures or (ROW_COUNT,),
        compounding=frozenset(m for m in measures if m.endswith(RETURN_SUFFIX)),
    )


def pivot(df: pd.DataFrame, spec: ChartSpec, measure: str) -> pd.DataFrame:
    """`measure` as a time by entity table, sorted by time; the row count per time for `ROW_COUNT`.

    >>> df = pd.DataFrame({
    ...     "date": pd.to_datetime(["2024-03-02", "2024-03-01", "2024-03-01"]),
    ...     "id": ["a", "a", "b"],
    ...     "close": [2.0, 1.0, 10.0],
    ... })
    >>> spec = ChartSpec("date", "id", ("close",), frozenset())
    >>> wide = pivot(df, spec, "close")
    >>> wide.index.strftime("%d").tolist(), wide.to_dict("list")
    (['01', '02'], {'a': [1.0, 2.0], 'b': [10.0, nan]})
    >>> pivot(df, spec, ROW_COUNT)["rows"].tolist()
    [2, 1]
    """
    if measure == ROW_COUNT:
        counts = cast("pd.Series", df.groupby(spec.time).size())
        return counts.to_frame("rows").sort_index()
    keys = [spec.time] if spec.entity is None else [spec.time, spec.entity]
    # Long Datasets hold one row per time and entity; this only guards a duplicate. Unlike an
    # aggregation it keeps a row whose value is null, such as a date with no return.
    unique = df.drop_duplicates(keys, keep="last")
    if spec.entity is None:
        return cast(pd.DataFrame, unique.set_index(spec.time)[[measure]]).sort_index()
    return unique.pivot(index=spec.time, columns=spec.entity, values=measure).sort_index()


def chart_frame(
    df: pd.DataFrame,
    spec: ChartSpec,
    measure: str,
    *,
    entities: Sequence[str] | None = None,
    cumulative: bool = False,
) -> pd.DataFrame:
    """The wide frame a line chart draws: `pivot`, narrowed to `entities`, maybe compounded.

    Compounding treats a missing return as zero, so a line carries on across a date with no
    return, and stays blank before an entity's first return.

    >>> df = pd.DataFrame({
    ...     "date": pd.to_datetime(["2024-03-01", "2024-03-02", "2024-03-02", "2024-03-03"]),
    ...     "id": ["a", "a", "b", "b"],
    ...     "simple_return": [0.1, 0.1, 0.5, None],
    ... })
    >>> spec = chart_spec([
    ...     {"name": "date", "type": "date", "role": "time"},
    ...     {"name": "id", "type": "str", "role": "entity"},
    ...     {"name": "simple_return", "type": "float", "role": "measure"},
    ... ])
    >>> chart_frame(df, spec, "simple_return", cumulative=True).round(4).to_dict("list")
    {'a': [0.1, 0.21, 0.21], 'b': [nan, 0.5, 0.5]}
    >>> list(chart_frame(df, spec, "simple_return", entities=["b"]).columns)
    ['b']
    """
    wide = pivot(df, spec, measure)
    if entities is not None and spec.entity is not None:
        wanted = set(entities)
        columns: list[Any] = list(wide.columns)
        wide = cast(pd.DataFrame, wide[[c for c in columns if c in wanted]])
    if cumulative and measure in spec.compounding:
        started: pd.DataFrame = wide.notna().cummax()
        compounded: pd.DataFrame = (1 + wide.fillna(0)).cumprod() - 1
        wide = compounded.where(started)
    return wide


def viewer_key(user: ViewingUser | None, token: str | None) -> str:
    """Who a cached read belongs to: the User and a digest of their Delegated Token.

    The token changes with each Session, so a read cached in one Session is not served in
    another, even to the same User; the digest keeps the token itself out of cache keys.

    >>> ann = ViewingUser(id="u1", name="Ann")
    >>> viewer_key(ann, "t1") == viewer_key(ann, "t1")
    True
    >>> len({viewer_key(ann, "t1"), viewer_key(ann, "t2"), viewer_key(ViewingUser(id="u2", name="Bo"), "t1")})
    3
    >>> viewer_key(None, None)
    'anonymous:'
    """
    who = "anonymous" if user is None else json.dumps([user.id, user.name])
    digest = "" if token is None else hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"{who}:{digest}"


def header(headers: Mapping[str, str], name: str) -> str | None:
    """A request header by case-insensitive name.

    >>> header({"x-woodglue-token": "t"}, "X-Woodglue-Token")
    't'
    """
    lower = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lower), None)
