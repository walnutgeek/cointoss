"""Portfolio writes through the cointoss namespace: `create_portfolio`, `add_trade`, `void_trade`.

The fragment is mounted as `tests/test_app.py` mounts it, over a data directory whose sweep lists
only the top 120 coins, so NEO (rank 198 in the recorded listing) is a coin outside
`cg-top-100` and outside the listing. Each call runs as a woodglue `Caller` set on
`current_caller`, as woodglue's JSON-RPC handler sets it; `TestOverHttp` drives the same nodes
with real tokens from an `AuthStore`. CoinGecko and OpenFIGI are answered by patching
`AsyncHTTPClient` in their adapters, and the clock by patching `cointoss.app.utcnow`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import tornado.testing
import yaml
from lythonic.compose.namespace import Namespace
from typing_extensions import override
from woodglue.access import AccessDenied, Caller, Grant, current_caller, resolve_roles
from woodglue.apps.server import create_app
from woodglue.auth_store import AuthStore
from woodglue.cli import load_namespaces
from woodglue.config import AuthConfig, NamespaceEntry, WoodglueConfig

from cointoss.app import (
    PORTFOLIO_GRANTS,
    PORTFOLIO_ROLE,
    BadRequest,
    Conflict,
    NotFound,
    fragment_entry,
)
from cointoss.instrument import InstrumentId, Source
from cointoss.portfolio import PORTFOLIO_HELD
from cointoss.sources.openfigi import API_KEY_ENV
from cointoss.store import Store

FIXTURE = Path(__file__).parent / "data" / "coingecko_markets_top250.json"
DAY1_AT = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
DAY2_AT = DAY1_AT + timedelta(days=1)
CONFIG = {"top_n": 120, "universes": [{"name": "cg-top-100", "enter_rank": 100, "exit_rank": 120}]}
BTC = "crypto.native.btc"
NEO = "crypto.native.neo"
NOT_FOUND = {"warning": "No identifier found."}

ALICE = Caller(user_id="usr_alice", grant_sets=(tuple(map(Grant.parse, PORTFOLIO_GRANTS)),))
BOB = Caller(user_id="usr_bob", grant_sets=(tuple(map(Grant.parse, PORTFOLIO_GRANTS)),))
READER = Caller(user_id="usr_reader", grant_sets=((Grant.parse("call cointoss"),),))
EXPLORER = Caller(
    user_id="admin",
    grant_sets=(resolve_roles({})["admin"], (Grant.parse("call cointoss"),)),
    credential_kind="delegated",
)


def coins() -> list[dict[str, Any]]:
    return [coin for page in json.loads(FIXTURE.read_text())["pages"] for coin in page]


def coingecko() -> MagicMock:
    """An `AsyncHTTPClient` answering `/coins/markets` by page, by `ids` or by `symbols`."""
    listing = coins()

    async def fetch(url: str) -> MagicMock:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "ids" in query:
            answer = [c for c in listing if c["id"] in query["ids"].split(",")]
        elif "symbols" in query:
            answer = [c for c in listing if c["symbol"] in query["symbols"].split(",")]
        else:
            per_page, page = int(query.get("per_page", "100")), int(query.get("page", "1"))
            answer = listing[(page - 1) * per_page : page * per_page]
        response = MagicMock()
        response.body = json.dumps(answer).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    return client


def openfigi() -> MagicMock:
    """An `AsyncHTTPClient` that finds nothing, as OpenFIGI answers most crypto tickers."""

    async def fetch(*_: Any, **_kwargs: Any) -> MagicMock:
        response = MagicMock()
        response.body = json.dumps([NOT_FOUND]).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    return client


@contextmanager
def market(at: datetime) -> Iterator[MagicMock]:
    gecko = coingecko()
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=gecko),
        patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=openfigi()),
        patch("cointoss.app.utcnow", return_value=at),
    ):
        yield gecko


@contextmanager
def acting_as(caller: Caller | None) -> Iterator[None]:
    token = current_caller.set(caller)
    try:
        yield
    finally:
        current_caller.reset(token)


@pytest.fixture(autouse=True)
def anonymous_openfigi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        yield home


@pytest.fixture
async def ns(data_dir: Path) -> AsyncIterator[Namespace]:
    """The namespace after the day-1 sweep, which lists the top 120 coins."""
    namespace = Namespace.from_dict([fragment_entry(data_dir=data_dir)])
    with market(DAY1_AT):
        await namespace.get("data:sweep")()
    yield namespace


def stored(data_dir: Path) -> Store:
    return Store(data_dir / "cointoss.db")


def create(ns: Namespace, caller: Caller, name: str = "main", **kwargs: Any) -> Any:
    with acting_as(caller):
        return ns.get("data:create_portfolio")(name=name, **kwargs)


async def add(ns: Namespace, caller: Caller, portfolio_id: int, instrument: str, **kwargs: Any):
    with market(DAY1_AT + timedelta(hours=1)), acting_as(caller):
        return await ns.get("data:add_trade")(
            portfolio_id=portfolio_id, instrument=instrument, **kwargs
        )


def void(ns: Namespace, caller: Caller, trade_id: int) -> Any:
    with acting_as(caller):
        return ns.get("data:void_trade")(trade_id=trade_id)


@pytest.mark.parametrize("caller", [READER, EXPLORER], ids=["reader", "explorer"])
async def test_a_caller_without_the_role_is_refused_and_nothing_is_written(
    ns: Namespace, data_dir: Path, caller: Caller
):
    portfolio = create(ns, ALICE)
    trade = await add(ns, ALICE, portfolio.id, BTC, quantity=1.0)

    with pytest.raises(AccessDenied):
        create(ns, caller, name="mine")
    with pytest.raises(AccessDenied):
        await add(ns, caller, portfolio.id, "neo", quantity=1.0)
    with pytest.raises(AccessDenied):
        void(ns, caller, trade.id)

    with stored(data_dir) as store:
        assert [p.name for p in store.portfolios_of(caller.user_id or "")] == []
        assert [t.voided for t in store.trades_of(portfolio.id)] == [False]
        assert store.load_instrument(NEO) is None
        assert InstrumentId(NEO) not in store.require_universe(PORTFOLIO_HELD).parameters.inclusions


async def test_the_caller_owns_the_portfolio_it_creates(ns: Namespace, data_dir: Path):
    portfolio = create(ns, ALICE, kind="paper", custodian="Kraken")

    assert (portfolio.owner, portfolio.name, portfolio.kind) == ("usr_alice", "main", "paper")
    assert create(ns, BOB).owner == "usr_bob"
    with pytest.raises(Conflict, match="main"):
        create(ns, ALICE)
    with pytest.raises(BadRequest, match="kind"):
        create(ns, ALICE, name="other", kind="imaginary")
    with stored(data_dir) as store:
        assert store.portfolios_of("usr_alice") == [portfolio]


async def test_a_user_cannot_trade_in_or_void_from_another_users_portfolio(
    ns: Namespace, data_dir: Path
):
    alices = create(ns, ALICE)
    trade = await add(ns, ALICE, alices.id, BTC, quantity=1.0)

    with pytest.raises(NotFound, match=f"no portfolio {alices.id} of yours"):
        await add(ns, BOB, alices.id, BTC, quantity=5.0)
    with pytest.raises(NotFound, match=f"no trade {trade.id} of yours"):
        void(ns, BOB, trade.id)

    with stored(data_dir) as store:
        assert store.trades_of(alices.id) == [trade]


async def test_trading_a_coin_outside_the_listing_makes_it_held_and_the_next_sweep_prices_it(
    ns: Namespace, data_dir: Path
):
    portfolio = create(ns, ALICE)
    with stored(data_dir) as store:
        assert store.load_instrument(NEO) is None

    trade = await add(
        ns, ALICE, portfolio.id, "neo", quantity=10.0, cost=25.0, traded_at="2026-09-29T15:00:00"
    )

    assert (trade.instrument_id, trade.quantity, trade.cost) == (NEO, 10.0, 25.0)
    assert trade.traded_at == datetime(2026, 9, 29, 15, tzinfo=UTC)
    with stored(data_dir) as store:
        held = store.require_universe(PORTFOLIO_HELD)
        assert held.parameters.inclusions == {InstrumentId(NEO)}
        assert not held.parameters.has_rule
        (member,) = store.load_universe_members(PORTFOLIO_HELD)
        assert (member.source, member.symbol_as_typed) == (Source.COINGECKO, "neo")

    with market(DAY2_AT):
        outcome = await ns.get("data:sweep")()

    assert outcome.report.unlisted == 1
    assert PORTFOLIO_HELD in {run.definition for run in outcome.report.runs}
    with stored(data_dir) as store:
        assert NEO in store.members_at(PORTFOLIO_HELD, DAY2_AT.date())
        assert NEO not in store.members_at("cg-top-100", DAY2_AT.date())
        (bar,) = store.bars_for(NEO, DAY2_AT.date(), DAY2_AT.date(), source=Source.COINGECKO)
        assert bar.close == 2.56


async def test_a_trade_by_instrument_id_is_held_once_however_often_it_is_traded(
    ns: Namespace, data_dir: Path
):
    portfolio = create(ns, ALICE)
    await add(ns, ALICE, portfolio.id, BTC, quantity=1.0)
    await add(ns, ALICE, portfolio.id, "btc", quantity=-0.5)

    with stored(data_dir) as store:
        held = store.require_universe(PORTFOLIO_HELD)
        assert held.parameters.inclusions == {InstrumentId(BTC)}
        assert held.revision == 1
        (position,) = store.positions_at(portfolio.id, DAY1_AT.date() + timedelta(days=1))
        assert position.quantity == 0.5


async def test_a_trade_that_cannot_be_recorded_writes_nothing(ns: Namespace, data_dir: Path):
    portfolio = create(ns, ALICE)

    with pytest.raises(NotFound, match="crypto.native.nosuch"):
        await add(ns, ALICE, portfolio.id, "crypto.native.nosuch", quantity=1.0)
    with pytest.raises(BadRequest, match="quantity"):
        await add(ns, ALICE, portfolio.id, BTC, quantity=0.0)
    with pytest.raises(BadRequest, match="traded_at"):
        await add(ns, ALICE, portfolio.id, "neo", quantity=1.0, traded_at="last week")

    with stored(data_dir) as store:
        assert store.trades_of(portfolio.id) == []
        assert store.load_universe(PORTFOLIO_HELD) is None
        assert store.load_instrument(NEO) is None


async def test_voiding_twice_is_an_error_and_keeps_the_first_void(ns: Namespace, data_dir: Path):
    portfolio = create(ns, ALICE)
    trade = await add(ns, ALICE, portfolio.id, BTC, quantity=1.0)

    with patch("cointoss.app.utcnow", return_value=DAY1_AT + timedelta(hours=2)):
        voided = void(ns, ALICE, trade.id)
    assert voided.voided_at == DAY1_AT + timedelta(hours=2)

    with pytest.raises(Conflict, match="voided"):
        void(ns, ALICE, trade.id)
    with pytest.raises(NotFound):
        void(ns, ALICE, 999)
    with stored(data_dir) as store:
        assert store.trades_of(portfolio.id) == [voided]


class TestOverHttp(tornado.testing.AsyncHTTPTestCase):
    """The write methods over JSON-RPC with auth on, each request carrying a bearer token."""

    # Set in `get_app`, which tornado calls from `setUp`.
    home: TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    auth: AuthStore  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def get_app(self) -> Any:
        self.home = TemporaryDirectory()
        data_dir = Path(self.home.name)
        (data_dir / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        Store(data_dir / "cointoss.db").close()
        roles: dict[str, list[str]] = {
            PORTFOLIO_ROLE: list(PORTFOLIO_GRANTS),
            "viewer": ["call cointoss"],
        }
        self.auth = AuthStore(data_dir / "auth.db", resolve_roles(roles))
        config = WoodglueConfig(namespaces={}, auth=AuthConfig(enabled=True, roles=roles))
        entry = NamespaceEntry(entries=[fragment_entry()], run_engine=True)
        namespaces = load_namespaces({"cointoss": entry}, data_dir)
        return create_app(namespaces=namespaces, config=config, auth_store=self.auth)

    @override
    def tearDown(self) -> None:
        super().tearDown()
        self.home.cleanup()

    def token(self, name: str, role: str) -> str:
        user = self.auth.create_service_user(name)
        self.auth.assign_role(user.id, role)
        return self.auth.create_token(user.id, "test").secret

    def rpc(self, token: str, method: str, **params: Any) -> dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = self.fetch("/rpc", method="POST", body=body, headers=headers)
        assert response.code == 200
        return json.loads(response.body)

    def test_the_role_reaches_the_writes_and_a_reader_or_the_explorer_does_not(self) -> None:
        trader = self.token("trader", PORTFOLIO_ROLE)
        reader = self.token("reader", "viewer")
        self.auth.ensure_admin_token()
        session = self.auth.create_session("admin")
        explorer = self.auth.delegate(session, "explore", ["call cointoss"])

        created = self.rpc(trader, "cointoss.data:create_portfolio", name="main")["result"]
        assert created["owner"].startswith("svc_")
        for token in (reader, explorer):
            answer = self.rpc(token, "cointoss.data:create_portfolio", name="theirs")
            assert answer["error"]["code"] == -32003
            assert self.rpc(token, "cointoss.data:universes")["result"] == []
        with Store(Path(self.home.name) / "cointoss.db") as store:
            assert store.portfolios_of("admin") == []

        admin = self.auth.admin_tokens()[0]
        missing = self.rpc(admin, "cointoss.data:void_trade", trade_id=1)
        assert missing["error"]["code"] == NotFound.code
