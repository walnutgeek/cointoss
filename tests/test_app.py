"""The cointoss namespace, mounted the way woodglue mounts it.

The fragment is registered through `Namespace.from_dict` with the entry `fragment_entry` builds,
which is what a `woodglue.yaml` carries, over a temporary data directory holding a
`cointoss.yaml`. CoinGecko is answered from the recorded `markets` listing by patching
`AsyncHTTPClient`, as `tests/test_sweep.py` does, and the clock by patching `cointoss.app.utcnow`,
so nothing touches the network and "today" is fixed.

Day 1 is the recording as fetched. Day 2 swaps two coins' ranks (BNB from 4 to 150, Gnosis the
other way) and moves Bitcoin's price, so both universes have a join and a leave to report.
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
from lythonic.compose.engine import StorageConfig
from lythonic.compose.namespace import Namespace
from lythonic.compose.trigger import TriggerManager, TriggerStore
from pydantic import BaseModel
from typing_extensions import override
from woodglue.apps.rpc import _serialize_result  # pyright: ignore[reportPrivateUsage]
from woodglue.apps.server import create_app
from woodglue.cli import load_namespaces
from woodglue.config import NamespaceEntry

from cointoss.app import (
    DEFAULT_SWEEP_SCHEDULE,
    SWEEP_TRIGGER,
    BadRequest,
    NotFound,
    NotInitialized,
    SweepInProgress,
    fragment_entry,
    sweep_lock,
)
from cointoss.store import Store

FIXTURE = Path(__file__).parent / "data" / "coingecko_markets_top250.json"
DAY1_AT = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
DAY2_AT = DAY1_AT + timedelta(days=1)
BTC = "crypto.native.btc"
BNB = "crypto.native.bnb"
GNO = "crypto.native.gno"
CONFIG = {
    "top_n": 250,
    "universes": [
        {"name": "cg-top-100", "enter_rank": 100, "exit_rank": 120},
        {"name": "cg-top-10", "enter_rank": 10, "exit_rank": 12},
    ],
}
READ_NODES = {"members", "changes", "bars", "universes_of", "runs", "universes"}

Page = list[dict[str, Any]]


def day1_pages() -> list[Page]:
    return json.loads(FIXTURE.read_text())["pages"]


def day2_pages() -> list[Page]:
    pages = day1_pages()
    for coin in pages[0]:
        if coin["id"] == "binancecoin":
            coin["market_cap_rank"] = 150
        elif coin["id"] == "gnosis":
            coin["market_cap_rank"] = 4
        elif coin["id"] == "bitcoin":
            coin["current_price"] = 84000.0
    return pages


def http(pages: list[Page]) -> MagicMock:
    """An `AsyncHTTPClient` answering `/coins/markets` with the page its query names."""

    async def fetch(url: str) -> MagicMock:
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        response = MagicMock()
        response.body = json.dumps(pages[page - 1] if page <= len(pages) else []).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    return client


@contextmanager
def market(at: datetime, pages: list[Page]) -> Iterator[MagicMock]:
    client = http(pages)
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=client),
        patch("cointoss.app.utcnow", return_value=at),
    ):
        yield client


def as_json(value: Any) -> Any:
    """What woodglue's JSON-RPC handler would put on the wire, parsed back."""
    return json.loads(json.dumps(_serialize_result(value)))


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        yield home


@pytest.fixture
def ns(data_dir: Path) -> Namespace:
    return Namespace.from_dict([fragment_entry(data_dir=data_dir)])


async def sweep(ns: Namespace, at: datetime, pages: list[Page], **kwargs: Any) -> Any:
    with market(at, pages):
        return await ns.get("data:sweep")(**kwargs)


@pytest.fixture
async def seeded(ns: Namespace) -> AsyncIterator[Namespace]:
    await sweep(ns, DAY1_AT, day1_pages())
    await sweep(ns, DAY2_AT, day2_pages())
    yield ns


def call(ns: Namespace, name: str, **kwargs: Any) -> Any:
    return as_json(ns.get(f"data:{name}")(**kwargs))


def test_the_fragment_registers_read_nodes_for_the_api_and_a_scheduled_sweep(ns: Namespace):
    api = {node.nsref.name for node in ns.query("api")}
    assert api == READ_NODES
    sweep_node = ns.get("data:sweep")
    assert "api" not in sweep_node.tags
    node, trigger = ns.get_trigger(SWEEP_TRIGGER)
    assert node is sweep_node
    assert trigger.schedule == DEFAULT_SWEEP_SCHEDULE == "5 0 * * *"


def test_the_schedule_is_configurable(data_dir: Path):
    ns = Namespace.from_dict([fragment_entry(data_dir=data_dir, schedule="30 0 * * *")])
    _, trigger = ns.get_trigger(SWEEP_TRIGGER)
    assert trigger.schedule == "30 0 * * *"


async def test_firing_the_trigger_runs_one_sweep_end_to_end(ns: Namespace, data_dir: Path):
    with TemporaryDirectory() as state:
        storage = StorageConfig(log_file=None)
        storage.resolve_paths(Path(state))
        storage.log_file = None
        ns.mount(storage)
        assert storage.triggers_db is not None
        manager = TriggerManager(ns, TriggerStore(storage.triggers_db))
        manager.activate(SWEEP_TRIGGER)
        with market(DAY1_AT, day1_pages()) as client:
            result = await manager.fire(SWEEP_TRIGGER)

    assert result.status == "completed", result.error
    assert client.fetch.call_count == 1
    with Store(data_dir / "cointoss.db") as store:
        for name, top in (("cg-top-100", 100), ("cg-top-10", 10)):
            (run,) = store.load_evaluation_runs(name)
            assert run.source_asof == DAY1_AT.date()
            assert run.run_at == DAY1_AT
            assert len(store.members_at(name, DAY1_AT.date())) == top


async def test_a_second_fire_on_the_same_day_is_a_no_op(ns: Namespace, data_dir: Path):
    first = await sweep(ns, DAY1_AT, day1_pages())
    later = DAY1_AT + timedelta(hours=9)
    with market(later, day2_pages()) as client:
        again = await ns.get("data:sweep")()

    assert client.fetch.call_count == 0
    assert again.already_swept
    assert again.day == first.day == DAY1_AT.date()
    assert again.report is None
    with Store(data_dir / "cointoss.db") as store:
        assert len(store.load_evaluation_runs("cg-top-100")) == 1
        (bar,) = store.bars_for(BTC, DAY1_AT.date(), DAY1_AT.date())
        assert bar.close == 83227


async def test_a_forced_resweep_keeps_the_days_bars_and_runs(ns: Namespace, data_dir: Path):
    await sweep(ns, DAY1_AT, day1_pages())
    moved = day1_pages()
    for coin in moved[0]:
        if coin["id"] == "bitcoin":
            coin["current_price"] = 90000.0
    later = DAY1_AT + timedelta(hours=2)

    retried = await sweep(ns, later, moved, force=True)

    assert not retried.already_swept
    assert retried.report is not None
    assert retried.report.at == later
    assert retried.report.failed == {}
    assert retried.report.restatements == 0
    assert retried.report.bars == 0
    with Store(data_dir / "cointoss.db") as store:
        assert len(store.load_evaluation_runs("cg-top-100")) == 1
        assert store.restatements_for(BTC) == []
        (bar,) = store.bars_for(BTC, DAY1_AT.date(), DAY1_AT.date())
        assert (bar.close, bar.fetched_at) == (83227, DAY1_AT)


async def test_a_late_sweep_writes_no_bars_unless_the_window_allows(ns: Namespace, data_dir: Path):
    late = DAY1_AT.replace(hour=5)
    swept = await sweep(ns, late, day1_pages())
    assert swept.report is not None
    assert swept.report.bars_skipped_late
    assert len(swept.report.runs) == 2

    (data_dir / "cointoss.yaml").write_text(yaml.safe_dump({**CONFIG, "bar_window_hours": 6}))
    widened = Namespace.from_dict([fragment_entry(data_dir=data_dir)])
    swept = await sweep(widened, late + timedelta(days=1), day1_pages())
    assert swept.report is not None
    assert not swept.report.bars_skipped_late
    assert swept.report.bars == 249


async def test_a_sweep_while_another_holds_the_lock_exits_without_fetching(
    ns: Namespace, data_dir: Path
):
    with sweep_lock(data_dir), market(DAY1_AT, day1_pages()) as client:
        with pytest.raises(SweepInProgress):
            await ns.get("data:sweep")()
    assert client.fetch.call_count == 0
    assert (await sweep(ns, DAY1_AT, day1_pages())).report is not None


async def test_the_sweep_reconciles_declared_universes_first(ns: Namespace):
    swept = await sweep(ns, DAY1_AT, day1_pages())
    assert [(r.name, r.outcome) for r in swept.reconciled] == [
        ("cg-top-100", "created"),
        ("cg-top-10", "created"),
    ]
    assert as_json(swept)["day"] == "2026-09-30"


async def test_members_returns_each_member_with_its_bar(seeded: Namespace):
    view = call(seeded, "members", universe="cg-top-10", date="2026-10-01")
    assert view["universe"] == "cg-top-10"
    assert view["date"] == "2026-10-01"
    members = {m["instrument_id"]: m for m in view["members"]}
    assert len(members) == 10
    assert GNO in members and BNB not in members
    btc = members[BTC]
    assert btc["symbol"] == "BTC"
    assert btc["name"] == "Bitcoin"
    assert btc["close"] == 84000.0
    assert btc["volume"] == 28071563826
    assert btc["market_cap"] == 1672100896326
    assert btc["source"] == "coingecko"


async def test_changes_reports_joins_and_leaves_by_date(seeded: Namespace):
    changes = call(seeded, "changes", universe="cg-top-10", start="2026-09-01", end="2026-12-31")
    assert [c["date"] for c in changes] == ["2026-09-30", "2026-10-01"]
    assert len(changes[0]["admitted"]) == 10 and changes[0]["dropped"] == []
    assert changes[1] == {"date": "2026-10-01", "admitted": [GNO], "dropped": [BNB]}

    only_day2 = call(seeded, "changes", universe="cg-top-10", start="2026-10-01", end="2026-10-01")
    assert only_day2 == changes[1:]


async def test_bars_returns_the_stored_history(seeded: Namespace):
    bars = call(seeded, "bars", instrument_id=BTC, start="2026-09-30", end="2026-10-01")
    assert [(b["bar_date"], b["close"]) for b in bars] == [
        ("2026-09-30", 83227),
        ("2026-10-01", 84000.0),
    ]
    assert bars[0]["fetched_at"] == "2026-09-30T00:05:00Z"


async def test_universes_of_names_the_universes_an_instrument_was_in(seeded: Namespace):
    assert call(
        seeded, "universes_of", instrument_id=BTC, start="2026-09-30", end="2026-10-01"
    ) == [
        "cg-top-10",
        "cg-top-100",
    ]
    assert (
        call(seeded, "universes_of", instrument_id=GNO, start="2026-09-30", end="2026-09-30") == []
    )
    assert (
        call(seeded, "universes_of", instrument_id=BNB, start="2026-10-01", end="2026-10-01") == []
    )


async def test_runs_lists_recent_evaluation_runs_newest_first(seeded: Namespace):
    runs = call(seeded, "runs", universe="cg-top-100")
    assert [r["source_asof"] for r in runs] == ["2026-10-01", "2026-09-30"]
    assert runs[0]["n_admitted"] == 1 and runs[0]["n_dropped"] == 1
    assert call(seeded, "runs", universe="cg-top-100", limit=1) == runs[:1]


async def test_universes_lists_stored_definitions_with_their_band(seeded: Namespace):
    assert call(seeded, "universes") == [
        {
            "name": "cg-top-10",
            "revision": 1,
            "revised_at": "2026-09-30",
            "enter_rank": 10,
            "exit_rank": 12,
            "inclusions": [],
            "exclusions": [],
        },
        {
            "name": "cg-top-100",
            "revision": 1,
            "revised_at": "2026-09-30",
            "enter_rank": 100,
            "exit_rank": 120,
            "inclusions": [],
            "exclusions": [],
        },
    ]


def test_universes_is_empty_before_anything_is_stored(ns: Namespace, data_dir: Path):
    Store(data_dir / "cointoss.db").close()
    assert call(ns, "universes") == []


@pytest.mark.parametrize("missing", ["database", "data_dir"])
def test_a_read_without_a_database_is_refused_and_creates_nothing(
    data_dir: Path, tmp_path: Path, missing: str
):
    home = data_dir if missing == "database" else tmp_path / "absent"
    ns = Namespace.from_dict([fragment_entry(data_dir=home)])
    with pytest.raises(NotInitialized, match="cointoss init"):
        ns.get("data:universes")()
    assert not (home / "cointoss.db").exists()
    assert home.exists() == (missing == "database")


@pytest.mark.parametrize(
    ("name", "kwargs", "error", "message"),
    [
        ("members", {"universe": "nope", "date": "2026-10-01"}, NotFound, "nope"),
        ("members", {"universe": "cg-top-10", "date": "2026-01-01"}, NotFound, "cg-top-10"),
        (
            "changes",
            {"universe": "nope", "start": "2026-10-01", "end": "2026-10-01"},
            NotFound,
            "nope",
        ),
        ("runs", {"universe": "nope"}, NotFound, "nope"),
        (
            "bars",
            {"instrument_id": "crypto.native.nope", "start": "2026-10-01", "end": "2026-10-01"},
            NotFound,
            "crypto.native.nope",
        ),
        (
            "universes_of",
            {"instrument_id": "crypto.native.nope", "start": "2026-10-01", "end": "2026-10-01"},
            NotFound,
            "crypto.native.nope",
        ),
        ("members", {"universe": "cg-top-10", "date": "yesterday"}, BadRequest, "yesterday"),
        (
            "bars",
            {"instrument_id": BTC, "start": "2026-10-02", "end": "2026-10-01"},
            BadRequest,
            "precedes",
        ),
        ("runs", {"universe": "cg-top-10", "limit": 0}, BadRequest, "limit"),
    ],
)
async def test_a_bad_request_raises_a_clear_api_error(
    seeded: Namespace, name: str, kwargs: dict[str, Any], error: type[Exception], message: str
):
    with pytest.raises(error, match=message):
        seeded.get(f"data:{name}")(**kwargs)


class WoodglueRpc(BaseModel):
    """One JSON-RPC 2.0 request body."""

    method: str
    params: dict[str, Any] = {}

    def body(self) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": 1, **self.model_dump()})


class TestThroughWoodglue(tornado.testing.AsyncHTTPTestCase):
    """The namespace as `woodglue start` loads it from `woodglue.yaml`, answering over HTTP."""

    # Set in `get_app`, which tornado calls from `setUp`.
    home: TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    ns: Namespace  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def get_app(self) -> Any:
        self.home = TemporaryDirectory()
        data_dir = Path(self.home.name)
        (data_dir / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        entry = NamespaceEntry(entries=[fragment_entry(data_dir=data_dir)], run_engine=True)
        namespaces = load_namespaces({"cointoss": entry}, data_dir)
        self.ns = namespaces["cointoss"][0]
        return create_app(namespaces=namespaces)

    @override
    def tearDown(self) -> None:
        super().tearDown()
        self.home.cleanup()

    def rpc(self, method: str, **params: Any) -> dict[str, Any]:
        response = self.fetch(
            "/rpc", method="POST", body=WoodglueRpc(method=method, params=params).body()
        )
        assert response.code == 200
        return json.loads(response.body)

    def test_read_nodes_answer_json_rpc(self) -> None:
        with market(DAY1_AT, day1_pages()):
            self.io_loop.run_sync(self.ns.get("data:sweep"))
        universes = self.rpc("cointoss.data:universes")["result"]
        assert [u["name"] for u in universes] == ["cg-top-10", "cg-top-100"]
        members = self.rpc("cointoss.data:members", universe="cg-top-10", date="2026-09-30")
        assert len(members["result"]["members"]) == 10

    def test_an_unknown_universe_is_an_error_response_not_a_traceback(self) -> None:
        answer = self.rpc("cointoss.data:runs", universe="nope")
        assert "result" not in answer
        assert answer["error"]["code"] < 0
        assert "Traceback" not in json.dumps(answer)
