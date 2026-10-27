"""The Dataset explorer's Streamlit script, driven by `streamlit.testing.v1.AppTest`.

The script runs against a fake client that calls the fragment's nodes in process and puts
their answers through woodglue's JSON serialization, so the explorer meets the same data, and
the same JSON-RPC errors, it would over HTTP. The store is `tests/test_datasets.py`'s.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from tests.test_datasets import CONFIG, day, seed


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        seed(home / "cointoss.db")
        yield home


def explorer(data_dir: str, user_id: str) -> None:
    """The script AppTest runs: `render` with a fake client over the fragment's nodes."""
    import json
    from pathlib import Path

    from lythonic.compose.namespace import Namespace
    from woodglue.apps.rpc import _serialize_result  # pyright: ignore[reportPrivateUsage]
    from woodglue.client import WoodglueRpcError
    from woodglue.ui_support import ViewingUser

    from cointoss.app import ApiError, fragment_entry
    from cointoss.explorer_app import render

    ns = Namespace.from_dict([fragment_entry(data_dir=Path(data_dir))])

    class FakeClient:
        async def call(self, method: str, /, **params: object) -> object:
            prefix, node = method.split(":")
            assert prefix == "cointoss.data"
            try:
                result = ns.get(f"data:{node}")(**params)
            except ApiError as exc:
                raise WoodglueRpcError(exc.code, str(exc)) from None
            return json.loads(json.dumps(_serialize_result(result)))

    render(ViewingUser(id=user_id, name="Ann", roles=("admin",)), "token", FakeClient)


def app(data_dir: Path, user_id: str = "u1") -> AppTest:
    return AppTest.from_function(explorer, args=(str(data_dir), user_id), default_timeout=30).run()


def test_bars_load_chart_and_pivot_through_the_api(data_dir: Path):
    at = app(data_dir)
    assert not at.exception
    assert at.selectbox(key="dataset").options == [
        "instruments",
        "bars",
        "returns",
        "membership",
        "changes",
        "runs",
    ]
    at.selectbox(key="dataset").set_value("bars").run()
    assert at.multiselect(key="bars.instruments").options == [
        "BTC",
        "ETH",
        "SOL",
    ]
    assert at.selectbox(key="bars.universe").options == ["(all)", "top"]
    at.date_input(key="bars.start").set_value(day(1))
    at.date_input(key="bars.end").set_value(day(7))
    at.multiselect(key="bars.instruments").select("crypto.native.eth")
    at.button[0].click().run()

    assert not at.exception and not at.error
    table: Any = at.dataframe[0].value
    symbols: list[str] = table["symbol"].tolist()
    assert len(table) == 7 and set(symbols) == {"ETH"}
    assert at.selectbox(key="chart.measure:bars").options[:4] == ["open", "high", "low", "close"]
    assert len(at.get("vega_lite_chart")) == 1
    at.selectbox(key="pivot.measure:bars").set_value("close").run()
    pivoted = at.dataframe[1].value
    assert list(pivoted.columns) == ["crypto.native.eth"]
    assert pivoted["crypto.native.eth"].tolist() == [10.0 + n for n in range(1, 8)]


def test_returns_offer_a_cumulative_chart(data_dir: Path):
    at = app(data_dir)
    at.selectbox(key="dataset").set_value("returns").run()
    at.date_input(key="returns.start").set_value(day(2))
    at.date_input(key="returns.end").set_value(day(5))
    at.button[0].click().run()
    assert not at.exception and not at.error
    at.selectbox(key="chart.measure:returns").set_value("simple_return").run()
    at.toggle(key="chart.cumulative:returns").set_value(True).run()
    assert not at.exception


def test_an_api_refusal_is_shown_as_its_message(data_dir: Path):
    at = app(data_dir)
    at.selectbox(key="dataset").set_value("bars").run()
    at.date_input(key="bars.start").set_value(day(1))
    at.date_input(key="bars.end").set_value(day(7))
    at.checkbox(key="bars.ever").check()
    at.button[0].click().run()
    assert not at.exception
    assert [e.value for e in at.error] == [
        "bars params: Value error, ever applies only with a universe"
    ]


def test_membership_charts_its_row_count(data_dir: Path):
    at = app(data_dir)
    at.selectbox(key="dataset").set_value("membership").run()
    at.date_input(key="membership.start").set_value(day(1))
    at.date_input(key="membership.end").set_value(day(7))
    at.selectbox(key="membership.universe").set_value("top")
    at.button[0].click().run()
    assert not at.exception and not at.error
    assert len(at.dataframe[0].value) == 3
    assert at.selectbox(key="chart.measure:membership").options == ["(row count)"]
