"""The Dataset explorer: a Streamlit app woodglue runs and serves at `/ui/explore/`.

`cointoss init` configures it as `uis.explore` in `woodglue.yaml`, with `app` naming this file
in the installed package; woodglue runs it as `streamlit run` under its own Python, so the
`ui` extra must be installed beside woodglue. It reads only through the `datasets` and `frame`
nodes (ADR-0010), as the viewer, with the Delegated Token woodglue forwards; it never opens
`cointoss.db`. What it decides is in `cointoss.explorer`; this script only draws it.

`render` takes the viewer and a client factory, so a test can drive it with a fake client.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast

import pandas as pd
import streamlit as st
from tornado.httpclient import HTTPClientError
from woodglue.client import WoodglueClient, WoodglueRpcError
from woodglue.ui_support import TOKEN_HEADER, ViewingUser, current_client, current_user

from cointoss.datasets import DEFAULT_NAMESPACE, load
from cointoss.explorer import (
    ChartSpec,
    ParamWidget,
    WidgetKind,
    build_params,
    chart_frame,
    chart_spec,
    header,
    param_widgets,
    pivot,
    viewer_key,
)

CACHE_TTL = dt.timedelta(minutes=10)
CHART_SERIES = 10
"""How many entities the chart draws until the viewer picks others."""


API_ERRORS = (WoodglueRpcError, HTTPClientError)
"""What a refused read raises: a JSON-RPC error, or an HTTP one such as an expired Session."""


def _message(exc: Exception) -> str:
    return exc.message if isinstance(exc, WoodglueRpcError) else str(exc)


class Client(Protocol):
    async def call(self, method: str, /, **params: Any) -> Any: ...


ClientFactory = Callable[[], Client]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _call(viewer: str, method: str, _client: ClientFactory) -> Any:
    """A parameterless read node's answer, cached per viewer."""
    del viewer  # Only part of the cache key.

    async def call() -> Any:
        return await _client().call(f"{DEFAULT_NAMESPACE}:{method}")

    return asyncio.run(call())


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading...")
def _frame(viewer: str, dataset: str, params_json: str, _client: ClientFactory) -> pd.DataFrame:
    """A Dataset as a typed DataFrame, cached per viewer, Dataset and params."""
    del viewer  # Only part of the cache key.

    async def read() -> pd.DataFrame:
        # `load` takes a `WoodglueClient`; anything with its `call` will do.
        client: Any = _client()
        return await load(client, dataset, **json.loads(params_json))

    return asyncio.run(read())


def _instrument_labels(viewer: str, client: ClientFactory) -> dict[str, str]:
    """Instrument Id to `SYMBOL - name`, or the symbol alone, for every Instrument not superseded."""
    df: Any = _frame(viewer, "instruments", "{}", client)
    if "superseded_by" in df.columns:
        df = df[df["superseded_by"].isna()]
    rows: list[dict[str, Any]] = df.to_dict("records")
    labels: dict[str, str] = {}
    for row in rows:
        name = row.get("name")
        labels[str(row["instrument_id"])] = (
            str(row["symbol"]) if name is None or name is pd.NA else f"{row['symbol']} - {name}"
        )
    return labels


def _widget(widget: ParamWidget, key: str, viewer: str, client: ClientFactory) -> Any:
    label = widget.label if widget.required else f"{widget.label} (optional)"
    match widget.kind:
        case WidgetKind.DATE:
            default = cast(dt.date, widget.default)
            picked: Any = st.date_input(label, value=default, help=widget.help, key=key)
            return picked
        case WidgetKind.BOOL:
            return st.checkbox(label, value=bool(widget.default), help=widget.help, key=key)
        case WidgetKind.CHOICE | WidgetKind.UNIVERSE:
            if widget.kind is WidgetKind.UNIVERSE:
                options = [u["name"] for u in _call(viewer, "universes", client)]
            else:
                options = list(widget.options)
            choices = options if widget.required else ["", *options]
            return st.selectbox(
                label,
                choices,
                format_func=lambda v: v or "(all)",
                help=widget.help,
                key=key,
            )
        case WidgetKind.INSTRUMENTS:
            labels = _instrument_labels(viewer, client)

            def instrument(instrument_id: str) -> str:
                return labels.get(instrument_id, instrument_id)

            return st.multiselect(
                label,
                list(labels),
                format_func=instrument,
                placeholder="all",
                help=widget.help,
                key=key,
            )
        case WidgetKind.TEXT_LIST:
            text = st.text_input(label, help=f"{widget.help} (comma separated)", key=key)
            return [part.strip() for part in text.split(",") if part.strip()]
        case WidgetKind.INT:
            value = widget.default if widget.default is not None else 0
            return int(st.number_input(label, value=value, step=1, help=widget.help, key=key))
        case WidgetKind.FLOAT:
            value = widget.default if widget.default is not None else 0.0
            return float(st.number_input(label, value=value, help=widget.help, key=key))
        case WidgetKind.TEXT:
            return st.text_input(label, value=widget.default or "", help=widget.help, key=key)


def _params_form(
    dataset: Mapping[str, Any], viewer: str, client: ClientFactory
) -> dict[str, Any] | None:
    """The params the viewer last submitted for this Dataset, or None before they load it."""
    name = dataset["name"]
    widgets = param_widgets(dataset["params"], dt.date.today())
    with st.form(f"params:{name}"):
        values = {w.name: _widget(w, f"{name}.{w.name}", viewer, client) for w in widgets}
        submitted = st.form_submit_button("Load", type="primary")
    if submitted:
        st.session_state[f"request:{name}"] = build_params(widgets, values)
    return st.session_state.get(f"request:{name}")


def _chart(df: pd.DataFrame, spec: ChartSpec, name: str) -> None:
    measure = st.selectbox("Measure", spec.measures, key=f"chart.measure:{name}")
    cumulative = measure in spec.compounding and st.toggle(
        "Cumulative", key=f"chart.cumulative:{name}"
    )
    entities: Sequence[str] | None = None
    if spec.entity is not None:
        values: list[Any] = cast(Any, df[spec.entity]).dropna().unique().tolist()
        every = sorted(str(e) for e in values)
        entities = st.multiselect(
            f"Lines ({spec.entity})",
            every,
            default=every[:CHART_SERIES],
            key=f"chart.entities:{name}",
        )
    wide = chart_frame(df, spec, measure, entities=entities, cumulative=cumulative)
    st.line_chart(wide)


def _pivot(df: pd.DataFrame, spec: ChartSpec, name: str) -> None:
    measure = st.selectbox("Measure", spec.measures, key=f"pivot.measure:{name}")
    wide = pivot(df, spec, measure)
    st.dataframe(wide)
    st.download_button(
        "Download pivot CSV",
        wide.to_csv(),
        file_name=f"{name}-{measure}-pivot.csv",
        mime="text/csv",
        key=f"pivot.csv:{name}",
    )


def render(user: ViewingUser | None, token: str | None, client: ClientFactory) -> None:
    st.set_page_config(page_title="cointoss explorer", layout="wide")
    if user is None:
        st.warning("Open the explorer through woodglue, at /ui/explore/.")
        st.stop()
        return
    viewer = viewer_key(user, token)

    try:
        datasets: list[dict[str, Any]] = _call(viewer, "datasets", client)
    except API_ERRORS as exc:
        st.error(_message(exc))
        st.stop()
        return
    by_name: dict[str, dict[str, Any]] = {d["name"]: d for d in datasets}

    with st.sidebar:
        st.caption(f"Signed in as {user.name}")
        name = st.selectbox("Dataset", list(by_name), key="dataset")
        dataset = by_name[name]
        st.caption(dataset["description"])
        try:
            params = _params_form(dataset, viewer, client)
        except API_ERRORS as exc:
            st.error(_message(exc))
            st.stop()
            return

    st.title(name)
    if params is None:
        st.info("Set the parameters and press Load.")
        return
    try:
        df = _frame(viewer, name, json.dumps(params, sort_keys=True), client)
    except API_ERRORS as exc:
        st.error(_message(exc))
        return

    st.caption(f"{len(df):,} rows; params {json.dumps(params)}")
    if df.empty:
        st.info("No rows match.")
        return
    st.download_button(
        "Download CSV",
        df.to_csv(index=False),
        file_name=f"{name}.csv",
        mime="text/csv",
        key=f"csv:{name}",
    )
    spec = chart_spec(dataset["columns"])
    tabs = st.tabs(["Table", "Chart", "Pivot"] if spec else ["Table"])
    with tabs[0]:
        st.dataframe(df, hide_index=True)
    if spec is not None:
        with tabs[1]:
            _chart(df, spec, name)
        with tabs[2]:
            _pivot(df, spec, name)


def main() -> None:
    headers = dict(st.context.headers)

    def client() -> WoodglueClient:
        return current_client(headers)

    render(current_user(headers), header(headers, TOKEN_HEADER), client)


if __name__ == "__main__":
    main()
