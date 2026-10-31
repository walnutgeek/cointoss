"""The portfolio Datasets, `trades`, `positions` and `portfolio_value`, read through `frame`.

March 2024, days 1 to 5. The store is seeded directly, as in `tests/test_datasets.py`:

- BTC has a CoinGecko bar closing each of days 1, 2, 3 and 5, none for day 4, and a Yahoo bar
  for day 5, which wins since Yahoo is first in its Price Sources. CoinGecko stamps the bar
  closing day n with n + 1 (Session Offset).
- ETH has a CoinGecko bar closing each of days 1 to 5.
- Alice holds `main` (2 BTC for 200 on day 1; 10 ETH for 110 on day 2; a voided buy of 100
  ETH) and `paper` (1 ETH on day 1). Bob holds his own `main` (1 BTC on day 1).

Each read runs as a woodglue `Caller` set on `current_caller`, as woodglue's JSON-RPC handler
sets it; `TestExplorerReads` reads over HTTP with each User's Delegated Token, as the explorer
does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pandas as pd
import pytest
import tornado.testing
import yaml
from lythonic.compose.namespace import Namespace
from typing_extensions import override
from woodglue.access import ANONYMOUS, Caller, Grant, current_caller, resolve_roles
from woodglue.apps.rpc import _serialize_result  # pyright: ignore[reportPrivateUsage]
from woodglue.apps.server import create_app
from woodglue.auth_store import AuthStore
from woodglue.cli import load_namespaces
from woodglue.client import WoodglueClient
from woodglue.config import AuthConfig, NamespaceEntry, WoodglueConfig

from cointoss.app import NotFound, Unauthorized, fragment_entry
from cointoss.datasets import load
from cointoss.explorer import chart_frame, chart_spec, param_widgets
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.portfolio import PortfolioKind
from cointoss.prices import Bar
from cointoss.store import Store

CG, YH = Source.COINGECKO, Source.YAHOO
BTC, ETH = InstrumentId("crypto.native.btc"), InstrumentId("crypto.native.eth")
READ = (tuple(map(Grant.parse, ["call cointoss"])),)
ALICE = Caller(user_id="usr_alice", grant_sets=READ)
BOB = Caller(user_id="usr_bob", grant_sets=READ)


def day(n: int) -> date:
    return date(2024, 3, n)


def iso(n: int) -> str:
    return day(n).isoformat()


def noon(n: int) -> datetime:
    return datetime(2024, 3, n, 12, tzinfo=UTC)


def coin(store: Store, symbol: str) -> InstrumentId:
    outcome = InstrumentRegistry().observe(
        Observation(
            type=InstrumentType.CRYPTO,
            symbol=symbol,
            scope="native",
            observed_at=day(1),
            source=CG,
            references=(ExternalReference.provider_id("coingecko", symbol.lower()),),
            figi_resolution=FigiResolution.NOT_FOUND,
        )
    )
    assert outcome.instrument is not None
    store.save_instrument(outcome.instrument)
    return outcome.instrument.id


def bar(instrument: InstrumentId, source: Source, n: int, close: float) -> Bar:
    """The Bar closing day `n`, under the date `source` stamps it with."""
    stamped = day(n) + timedelta(days=source.session_offset)
    fetched = datetime.combine(stamped + timedelta(days=1), datetime.min.time(), UTC)
    return Bar(
        instrument_id=instrument, source=source, bar_date=stamped, close=close, fetched_at=fetched
    )


def seed(
    db: Path, alice: str = "usr_alice", bob: str = "usr_bob", anonymous: str | None = None
) -> None:
    with Store(db) as store:
        btc, eth = coin(store, "BTC"), coin(store, "ETH")
        store.upsert_bars(
            [
                *(bar(btc, CG, n, 100.0 + n) for n in (1, 2, 3, 5)),
                *(bar(eth, CG, n, 10.0 + n) for n in range(1, 6)),
                bar(btc, YH, 5, 200.0),
            ]
        )
        main = store.create_portfolio(alice, "main", PortfolioKind.REAL, "Kraken").id
        store.record_trade(main, btc, noon(1), 2.0, cost=200.0)
        store.record_trade(main, eth, noon(2), 10.0, cost=110.0, note="dip")
        mistake = store.record_trade(main, eth, noon(2), 100.0, cost=1_100.0)
        store.void_trade(mistake.id, at=noon(3))
        paper = store.create_portfolio(alice, "paper", PortfolioKind.PAPER).id
        store.record_trade(paper, eth, noon(1), 1.0)
        bobs = store.create_portfolio(bob, "main", PortfolioKind.REAL).id
        store.record_trade(bobs, btc, noon(1), 1.0, cost=101.0)
        if anonymous is not None:
            unowned = store.create_portfolio(anonymous, "open", PortfolioKind.WHAT_IF).id
            store.record_trade(unowned, eth, noon(1), 3.0)


@pytest.fixture
def ns() -> Iterator[Namespace]:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "cointoss.yaml").write_text(yaml.safe_dump({"universes": []}))
        seed(home / "cointoss.db", anonymous=ANONYMOUS.name)
        yield Namespace.from_dict([fragment_entry(data_dir=home)])


@contextmanager
def acting_as(caller: Caller | None) -> Iterator[None]:
    token = current_caller.set(caller)
    try:
        yield
    finally:
        current_caller.reset(token)


def frame(
    ns: Namespace, caller: Caller | None, dataset: str, **params: Any
) -> list[dict[str, Any]]:
    """A Dataset as woodglue would put it on the wire for `caller`, parsed back into records."""
    with acting_as(caller):
        result = ns.get("data:frame")(dataset=dataset, params=params)
    answer = json.loads(json.dumps(_serialize_result(result)))
    return [dict(zip(answer["columns"], row, strict=True)) for row in answer["data"]]


def pick(rows: list[dict[str, Any]], *names: str) -> list[tuple[Any, ...]]:
    return [tuple(r[n] for n in names) for r in rows]


def test_trades_lists_only_the_callers_trades_with_voided_ones_flagged(ns: Namespace):
    alices = frame(ns, ALICE, "trades")
    assert pick(
        alices, "portfolio", "instrument_id", "symbol", "quantity", "cost", "voided_at"
    ) == [
        ("main", BTC, "BTC", 2.0, 200.0, None),
        ("main", ETH, "ETH", 10.0, 110.0, None),
        ("main", ETH, "ETH", 100.0, 1_100.0, "2024-03-03T12:00:00Z"),
        ("paper", ETH, "ETH", 1.0, None, None),
    ]
    assert alices[1]["traded_at"] == "2024-03-02T12:00:00Z"
    assert (alices[1]["kind"], alices[1]["note"]) == ("real", "dip")

    bobs = frame(ns, BOB, "trades")
    assert pick(bobs, "portfolio", "instrument_id", "quantity") == [("main", BTC, 1.0)]
    assert pick(frame(ns, ALICE, "trades", portfolio="paper"), "portfolio") == [("paper",)]


def test_another_users_portfolio_reads_as_one_that_does_not_exist(ns: Namespace):
    for dataset, params in (
        ("trades", {}),
        ("positions", {"date": iso(3)}),
        ("portfolio_value", {"start": iso(1), "end": iso(2)}),
    ):
        with pytest.raises(NotFound) as theirs:
            frame(ns, BOB, dataset, portfolio="paper", **params)
        with pytest.raises(NotFound) as nobodys:
            frame(ns, BOB, dataset, portfolio="nowhere", **params)
        assert str(theirs.value) == str(nobodys.value).replace("nowhere", "paper")


def test_a_position_is_valued_at_the_close_of_the_session_it_is_held_on(ns: Namespace):
    rows = frame(ns, ALICE, "positions", date=iso(3), portfolio="main")
    assert pick(
        rows, "date", "instrument_id", "quantity", "average_cost", "close", "value", "source"
    ) == [
        # CoinGecko's Bar stamped day 4 closes day 3.
        (iso(3), BTC, 2.0, 100.0, 103.0, 206.0, "coingecko"),
        (iso(3), ETH, 10.0, 11.0, 13.0, 130.0, "coingecko"),
    ]
    yahoo = frame(ns, ALICE, "positions", date=iso(5), portfolio="main")
    assert pick(yahoo, "instrument_id", "close", "source") == [
        (BTC, 200.0, "yahoo"),
        (ETH, 15.0, "coingecko"),
    ]


def test_a_position_without_a_bar_on_its_date_has_no_close_or_value(ns: Namespace):
    rows = frame(ns, ALICE, "positions", date=iso(4))
    assert pick(rows, "portfolio", "instrument_id", "quantity", "close", "value", "source") == [
        ("main", BTC, 2.0, None, None, None),
        ("main", ETH, 10.0, 14.0, 140.0, "coingecko"),
        ("paper", ETH, 1.0, 14.0, 14.0, "coingecko"),
    ]
    assert frame(ns, BOB, "positions", date=iso(4)) == [
        {
            "date": iso(4),
            "portfolio": "main",
            "instrument_id": BTC,
            "symbol": "BTC",
            "quantity": 1.0,
            "average_cost": 101.0,
            "close": None,
            "value": None,
            "source": None,
        }
    ]


def test_portfolio_value_is_null_on_a_date_any_position_is_unpriced(ns: Namespace):
    rows = frame(ns, ALICE, "portfolio_value", start=iso(1), end=iso(5))
    assert pick(rows, "date", "portfolio", "value", "positions", "unpriced") == [
        (iso(1), "main", 202.0, 1, 0),
        (iso(1), "paper", 11.0, 1, 0),
        (iso(2), "main", 2 * 102.0 + 10 * 12.0, 2, 0),
        (iso(2), "paper", 12.0, 1, 0),
        (iso(3), "main", 206.0 + 130.0, 2, 0),
        (iso(3), "paper", 13.0, 1, 0),
        (iso(4), "main", None, 2, 1),
        (iso(4), "paper", 14.0, 1, 0),
        (iso(5), "main", 400.0 + 150.0, 2, 0),
        (iso(5), "paper", 15.0, 1, 0),
    ]
    before = frame(ns, BOB, "portfolio_value", start="2024-02-28", end=iso(1))
    assert pick(before, "date", "value", "positions", "unpriced") == [
        ("2024-02-28", 0.0, 0, 0),
        ("2024-02-29", 0.0, 0, 0),
        (iso(1), 101.0, 1, 0),
    ]


def test_a_read_needs_a_user_and_auth_disabled_reads_as_anonymous(ns: Namespace):
    for dataset, params in (
        ("trades", {}),
        ("positions", {"date": iso(3)}),
        ("portfolio_value", {"start": iso(1), "end": iso(1)}),
    ):
        with pytest.raises(Unauthorized):
            frame(ns, None, dataset, **params)
    assert pick(frame(ns, ANONYMOUS, "trades"), "portfolio", "quantity") == [("open", 3.0)]
    assert len(frame(ns, None, "bars", start=iso(1), end=iso(1))) == 2


class TestExplorerReads(tornado.testing.AsyncHTTPTestCase):
    """`portfolio_value` read and charted as the explorer does, with each viewer's token."""

    home: TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    auth: AuthStore  # pyright: ignore[reportUninitializedInstanceVariable]
    users: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def get_app(self) -> Any:
        self.home = TemporaryDirectory()
        data_dir = Path(self.home.name)
        (data_dir / "cointoss.yaml").write_text(yaml.safe_dump({"universes": []}))
        roles: dict[str, list[str]] = {"viewer": ["call cointoss"]}
        self.auth = AuthStore(data_dir / "auth.db", resolve_roles(roles))
        self.users = {name: self.auth.create_service_user(name).id for name in ("alice", "bob")}
        for user_id in self.users.values():
            self.auth.assign_role(user_id, "viewer")
        seed(data_dir / "cointoss.db", alice=self.users["alice"], bob=self.users["bob"])
        config = WoodglueConfig(namespaces={}, auth=AuthConfig(enabled=True, roles=roles))
        entry = NamespaceEntry(entries=[fragment_entry()], run_engine=False)
        namespaces = load_namespaces({"cointoss": entry}, data_dir)
        return create_app(namespaces=namespaces, config=config, auth_store=self.auth)

    @override
    def tearDown(self) -> None:
        super().tearDown()
        self.home.cleanup()

    def explorer(self, name: str) -> WoodglueClient:
        """A client holding the Delegated Token the explorer gets for viewer `name`."""
        session = self.auth.create_session(self.users[name])
        token = self.auth.delegate(session, "explore", ["call cointoss"])
        return WoodglueClient(self.get_url(""), token=token)

    def chart(self, name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """`portfolio_value` for days 3 to 5 as viewer `name` loads it, and the chart drawn."""
        client = self.explorer(name)
        described = self.io_loop.run_sync(lambda: client.call("cointoss.data:datasets"))
        value = next(d for d in described if d["name"] == "portfolio_value")
        spec = chart_spec(value["columns"])
        assert spec is not None and (spec.time, spec.entity) == ("date", "portfolio")
        assert spec.measures[0] == "value"
        widgets = param_widgets(value["params"], day(5))
        assert [w.name for w in widgets if w.required] == ["start", "end"]
        df = self.io_loop.run_sync(
            lambda: load(client, "portfolio_value", start=iso(3), end=iso(5))
        )
        return df, chart_frame(df, spec, "value")

    def test_each_viewer_charts_only_their_own_portfolio_value(self) -> None:
        alices, chart = self.chart("alice")
        assert (
            sorted(cast("list[str]", alices["portfolio"].tolist())) == ["main"] * 3 + ["paper"] * 3
        )
        assert list(chart.index) == [pd.Timestamp(day(n)) for n in (3, 4, 5)]
        assert chart["main"].isna().tolist() == [False, True, False]
        assert chart["paper"].tolist() == [13.0, 14.0, 15.0]

        bobs, _ = self.chart("bob")
        assert cast("list[str]", bobs["portfolio"].tolist()) == ["main"] * 3
