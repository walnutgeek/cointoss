"""Datasets, ADR-0010: the `datasets` and `frame` nodes, the store's bulk reads, and `load`.

The store is seeded directly rather than through a sweep, so each case is a small fact table:
March 2024, days 1 to 7.

- BTC, ETH and SOL have a CoinGecko bar every day, except BTC on day 4. CoinGecko stamps the
  bar closing day n with n + 1 (Session Offset), so `bars` shows it on n and `source` on n + 1.
- BTC also has Yahoo bars on days 6 and 7, and Yahoo is first in a coin's Price Sources.
- Universe `top` holds BTC and ETH from day 1; SOL joins on day 3 and leaves on day 5.

Errors and `load` are exercised over HTTP, the way a client meets them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
import tornado.testing
import yaml
from lythonic.compose.namespace import Namespace
from typing_extensions import override
from woodglue.apps.rpc import _serialize_result  # pyright: ignore[reportPrivateUsage]
from woodglue.apps.server import create_app
from woodglue.cli import load_namespaces
from woodglue.client import WoodglueClient, WoodglueRpcError
from woodglue.config import NamespaceEntry

from cointoss.app import BadRequest, NotFound, fragment_entry
from cointoss.datasets import (
    DATASETS,
    Column,
    ColumnRole,
    ColumnType,
    Dataset,
    InstrumentParams,
    load,
)
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
    SupersessionCycle,
)
from cointoss.prices import Bar
from cointoss.store import Store, TooManyRows
from cointoss.universe import UniverseDefinition, UniverseParameters

CG, YH = Source.COINGECKO, Source.YAHOO
CONFIG: dict[str, Any] = {"universes": []}


def day(n: int) -> date:
    return date(2024, 3, n)


def iso(n: int) -> str:
    return day(n).isoformat()


def coins(store: Store, *symbols: str) -> list[InstrumentId]:
    registry = InstrumentRegistry()
    ids: list[InstrumentId] = []
    for symbol in symbols:
        outcome = registry.observe(
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
        ids.append(outcome.instrument.id)
    return ids


def bar(instrument: InstrumentId, source: Source, n: int, close: float) -> Bar:
    """The Bar closing day `n`, under the date `source` stamps it with."""
    stamped = day(n) + timedelta(days=source.session_offset)
    fetched = datetime.combine(stamped + timedelta(days=1), datetime.min.time(), UTC)
    return Bar(
        instrument_id=instrument, source=source, bar_date=stamped, close=close, fetched_at=fetched
    )


def seed(db: Path) -> None:
    with Store(db) as store:
        btc, eth, sol = coins(store, "BTC", "ETH", "SOL")
        store.upsert_bars(
            [
                *(bar(btc, CG, n, 100.0 + n) for n in (1, 2, 3, 5, 6, 7)),
                *(bar(eth, CG, n, 10.0 + n) for n in range(1, 8)),
                *(bar(sol, CG, n, 1.0 + n) for n in range(1, 8)),
                bar(btc, YH, 6, 200.0),
                bar(btc, YH, 7, 220.0),
            ]
        )
        store.save_universe(UniverseDefinition.create("top", UniverseParameters(), day(1)))
        store.record_membership("top", day(1), [btc, eth])
        store.record_membership("top", day(3), [btc, eth, sol])
        store.record_membership("top", day(5), [btc, eth])


BTC, ETH, SOL = (InstrumentId(f"crypto.native.{s}") for s in ("btc", "eth", "sol"))


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        seed(home / "cointoss.db")
        yield home


@pytest.fixture
def ns(data_dir: Path) -> Namespace:
    return Namespace.from_dict([fragment_entry(data_dir=data_dir)])


def frame(ns: Namespace, dataset: str, **params: Any) -> list[dict[str, Any]]:
    """A Dataset as woodglue would put it on the wire, parsed back into records."""
    answer = json.loads(
        json.dumps(_serialize_result(ns.get("data:frame")(dataset=dataset, params=params)))
    )
    return [dict(zip(answer["columns"], row, strict=True)) for row in answer["data"]]


def pick(rows: list[dict[str, Any]], *names: str) -> list[tuple[Any, ...]]:
    return [tuple(r[n] for n in names) for r in rows]


# -- datasets and frame --


def test_datasets_lists_each_with_typed_columns_and_a_params_schema(ns: Namespace):
    listed = _serialize_result(ns.get("data:datasets")())
    assert [d["name"] for d in listed] == [
        "instruments",
        "bars",
        "returns",
        "membership",
        "changes",
        "runs",
        "trades",
        "positions",
        "portfolio_value",
    ]
    for d in listed:
        assert d["description"]
        assert d["columns"] and all(
            c["type"] in set(ColumnType) and c["role"] in set(ColumnRole) for c in d["columns"]
        )
        assert d["params"]["type"] == "object"
    by_name = {d["name"]: d for d in listed}
    assert set(by_name["bars"]["params"]["required"]) == {"start", "end"}
    assert set(by_name["bars"]["params"]["properties"]) == {
        "start",
        "end",
        "instruments",
        "source",
        "universe",
        "ever",
    }
    assert "required" not in by_name["instruments"]["params"]
    assert [c["name"] for c in by_name["returns"]["columns"]][:3] == [
        "date",
        "instrument_id",
        "symbol",
    ]


def test_instruments_lists_every_instrument(ns: Namespace):
    rows = frame(ns, "instruments")
    assert pick(rows, "instrument_id", "symbol", "price_sources", "minted_at") == [
        (BTC, "BTC", "yahoo,coingecko", "2024-03-01"),
        (ETH, "ETH", "yahoo,coingecko", "2024-03-01"),
        (SOL, "SOL", "yahoo,coingecko", "2024-03-01"),
    ]
    assert pick(frame(ns, "instruments", instruments=[ETH]), "symbol") == [("ETH",)]


def test_bars_resolve_through_price_sources_unless_a_source_is_named(ns: Namespace):
    rows = frame(ns, "bars", start=iso(5), end=iso(7), instruments=[BTC])
    assert pick(rows, "date", "symbol", "source", "close") == [
        (iso(5), "BTC", "coingecko", 105.0),
        (iso(6), "BTC", "yahoo", 200.0),
        (iso(7), "BTC", "yahoo", 220.0),
    ]
    assert rows[0]["fetched_at"] == "2024-03-07T00:00:00Z"
    named = frame(ns, "bars", start=iso(6), end=iso(8), instruments=[BTC], source="coingecko")
    assert pick(named, "date", "source", "close") == [
        (iso(6), "coingecko", 105.0),
        (iso(7), "coingecko", 106.0),
        (iso(8), "coingecko", 107.0),
    ]


def test_bars_cover_every_instrument_in_long_form(ns: Namespace):
    rows = frame(ns, "bars", start=iso(1), end=iso(7))
    assert len(rows) == 6 + 7 + 7
    assert [r["instrument_id"] for r in rows] == sorted(r["instrument_id"] for r in rows)


def test_returns_pair_the_bars_that_bars_shows_and_span_a_gap_once(ns: Namespace):
    rows = frame(ns, "returns", start=iso(2), end=iso(5))
    assert len({(r["date"], r["instrument_id"]) for r in rows}) == len(rows)
    btc = [r for r in rows if r["instrument_id"] == BTC]
    assert pick(btc, "source", "prev_date", "date", "gap_days") == [
        ("coingecko", iso(1), iso(2), 1),
        ("coingecko", iso(2), iso(3), 1),
        ("coingecko", iso(3), iso(5), 2),
    ]
    gap = btc[2]
    assert gap["close"] == 105.0
    assert gap["simple_return"] == pytest.approx(105.0 / 103.0 - 1)


def test_no_return_across_a_switch_of_the_resolved_source(ns: Namespace):
    """`bars` shows BTC from CoinGecko on day 5 and from Yahoo on days 6 and 7."""
    rows = frame(ns, "returns", start=iso(5), end=iso(7), instruments=[BTC])
    assert pick(rows, "source", "prev_date", "date", "close") == [
        ("coingecko", iso(3), iso(5), 105.0),
        ("yahoo", iso(6), iso(7), 220.0),
    ]
    assert rows[-1]["simple_return"] == pytest.approx(0.1)
    named = frame(ns, "returns", start=iso(6), end=iso(8), instruments=[BTC], source="coingecko")
    assert pick(named, "prev_date", "date") == [
        (iso(4), iso(6)),
        (iso(6), iso(7)),
        (iso(7), iso(8)),
    ]


def test_returns_reach_back_one_bar_before_start(ns: Namespace):
    rows = frame(ns, "returns", start=iso(6), end=iso(6), instruments=[BTC], source="coingecko")
    assert pick(rows, "prev_date", "date", "gap_days") == [(iso(4), iso(6), 2)]
    assert frame(ns, "returns", start=iso(1), end=iso(1), instruments=[ETH]) == []


def test_universe_is_point_in_time_and_ever_widens_it(ns: Namespace):
    def sol_dates(**params: Any) -> list[str]:
        rows = frame(ns, "bars", start=iso(1), end=iso(7), **params)
        return [r["date"] for r in rows if r["instrument_id"] == SOL]

    assert sol_dates(universe="top") == [iso(3), iso(4)]
    assert sol_dates(universe="top", ever=True) == [iso(n) for n in range(1, 8)]
    eth = [
        r
        for r in frame(ns, "bars", start=iso(1), end=iso(7), universe="top")
        if r["symbol"] == "ETH"
    ]
    assert len(eth) == 7

    returns = frame(ns, "returns", start=iso(1), end=iso(7), universe="top", instruments=[SOL])
    assert pick(returns, "prev_date", "date") == [(iso(2), iso(3)), (iso(3), iso(4))]
    outside = frame(ns, "bars", start=iso(5), end=iso(7), universe="top", ever=True)
    assert SOL not in {r["instrument_id"] for r in outside}


def test_membership_lists_intervals_and_changes_lists_joins_and_leaves(ns: Namespace):
    intervals = frame(ns, "membership", start=iso(1), end=iso(7), universe="top")
    assert pick(intervals, "instrument_id", "valid_from", "valid_to") == [
        (BTC, iso(1), None),
        (ETH, iso(1), None),
        (SOL, iso(3), iso(5)),
    ]
    assert frame(ns, "membership", start=iso(5), end=iso(7), instruments=[SOL]) == []

    changes = frame(ns, "changes", start=iso(2), end=iso(7))
    assert pick(changes, "date", "universe", "symbol", "change") == [
        (iso(3), "top", "SOL", "joined"),
        (iso(5), "top", "SOL", "left"),
    ]
    assert pick(frame(ns, "changes", start=iso(5), end=iso(5)), "symbol", "change") == [
        ("SOL", "left")
    ]


def test_runs_lists_evaluation_runs_in_the_range(ns: Namespace):
    rows = frame(ns, "runs", start=iso(2), end=iso(7), universe="top")
    assert pick(rows, "source_asof", "universe", "outcome", "n_admitted", "n_dropped") == [
        (iso(3), "top", "changed", 1, 0),
        (iso(5), "top", "changed", 0, 1),
    ]


def test_nothing_matching_is_an_empty_frame(ns: Namespace):
    for name in ("bars", "returns", "membership", "changes", "runs"):
        answer = ns.get("data:frame")(
            dataset=name, params={"start": "2020-01-01", "end": "2020-01-02"}
        )
        assert answer.data == []
        assert answer.columns == [c.name for c in DATASETS[name].columns]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"end": "2024-03-07"}, "bars params: start: Field required"),
        ({"start": "2024-03-07", "end": "2024-03-01"}, "end 2024-03-07"),
        ({"start": "March", "end": "2024-03-01"}, "start: Value error, 'March' is not a date"),
        ({"start": 20240301, "end": "2024-03-01"}, "YYYY-MM-DD"),
        ({"start": iso(1), "end": iso(2), "ever": True}, "ever applies only with a universe"),
        ({"start": iso(1), "end": iso(2), "src": "yahoo"}, "src: Extra inputs"),
        ({"start": iso(1), "end": iso(2), "source": "bloomberg"}, "source: Input should be"),
    ],
)
def test_bad_params_are_a_one_line_bad_request(ns: Namespace, params: dict[str, Any], message: str):
    with pytest.raises(BadRequest) as raised:
        ns.get("data:frame")(dataset="bars", params=params)
    assert message.replace("end 2024-03-07", "end 2024-03-01 precedes start 2024-03-07") in str(
        raised.value
    )
    assert "\n" not in str(raised.value)


@pytest.mark.parametrize(
    ("dataset", "params"),
    [
        ("returns", {"start": iso(1), "end": iso(2), "universe": "nope"}),
        ("bars", {"start": iso(1), "end": iso(2), "instruments": ["crypto.native.nope"]}),
        ("membership", {"start": iso(1), "end": iso(2), "universe": "nope"}),
        ("instruments", {"instruments": ["crypto.native.nope"]}),
    ],
)
def test_an_unknown_universe_or_instrument_is_not_found(
    ns: Namespace, dataset: str, params: dict[str, Any]
):
    with pytest.raises(NotFound, match="nope"):
        ns.get("data:frame")(dataset=dataset, params=params)


def test_the_row_cap_names_the_matching_count(ns: Namespace):
    with patch("cointoss.datasets.ROW_CAP", 5), pytest.raises(BadRequest) as raised:
        ns.get("data:frame")(dataset="bars", params={"start": iso(1), "end": iso(7)})
    assert "bars: 20 rows match, over the cap of 5" in str(raised.value)
    with patch("cointoss.datasets.ROW_CAP", 2), pytest.raises(BadRequest, match="3 rows match"):
        ns.get("data:frame")(dataset="instruments", params={})
    with patch("cointoss.datasets.ROW_CAP", 20):
        assert (
            len(ns.get("data:frame")(dataset="bars", params={"start": iso(1), "end": iso(7)}).data)
            == 20
        )


def test_a_new_dataset_needs_only_a_definition(ns: Namespace):
    symbols = Dataset(
        name="symbols",
        description="Symbols only.",
        params=InstrumentParams,
        columns=(
            Column(
                name="symbol", type=ColumnType.STR, role=ColumnRole.ENTITY, description="Symbol"
            ),
        ),
        rows=lambda store, _: [(i.symbol,) for i in store.load_instruments()],
    )
    with patch.dict(DATASETS, {"symbols": symbols}):
        assert "symbols" in [d.name for d in ns.get("data:datasets")()]
        assert frame(ns, "symbols") == [{"symbol": s} for s in ("BTC", "ETH", "SOL")]


# -- the store's bulk reads --


def supersede(store: Store, merged: InstrumentId, survivor: InstrumentId) -> None:
    instrument = store.load_instrument(merged)
    assert instrument is not None
    instrument.superseded_by = survivor
    store.save_instrument(instrument)


def test_bulk_bars_fold_supersession_as_bars_for_does(data_dir: Path):
    with Store(data_dir / "cointoss.db") as store:
        (wbtc,) = coins(store, "WBTC")
        store.upsert_bars([bar(wbtc, CG, 4, 104.0), bar(wbtc, CG, 5, 999.0)])
        store.record_membership("top", day(6), [wbtc, ETH])
        supersede(store, wbtc, BTC)

        bulk = store.resolved_bars(day(1), day(7), instruments=[wbtc])
        assert [(b.bar_date, b.instrument_id, b.source, b.close) for b in bulk] == [
            (b.bar_date, b.instrument_id, b.source, b.close)
            for b in store.bars_for(BTC, day(1), day(7))
        ]
        assert {b.instrument_id for b in bulk} == {BTC}
        assert [b.close for b in bulk if b.bar_date in (day(4), day(5))] == [104.0, 105.0]

        pairs = store.close_pairs(day(4), day(5), instruments=[BTC], source=CG)
        assert [(p.prev_date, p.bar_date) for p in pairs] == [(day(3), day(4)), (day(4), day(5))]

        (stay,) = store.membership_intervals(day(1), day(7), universe="top", instruments=[BTC])
        assert (stay.instrument_id, stay.valid_from, stay.valid_to) == (BTC, day(1), None)


def test_bulk_bars_apply_the_precedence_of_bars_for(data_dir: Path):
    """A predecessor's preferred-source Bar beats the survivor's fallback, and mint order breaks
    a tie between predecessors. XBTC is minted before WBTC but stored after it."""
    with Store(data_dir / "cointoss.db") as store:
        registry = InstrumentRegistry()
        xbtc, wbtc = (
            registry.observe(
                Observation(
                    type=InstrumentType.CRYPTO,
                    symbol=symbol,
                    scope="native",
                    observed_at=day(1),
                    source=CG,
                    references=(ExternalReference.provider_id("coingecko", symbol.lower()),),
                    figi_resolution=FigiResolution.NOT_FOUND,
                )
            ).instrument
            for symbol in ("XBTC", "WBTC")
        )
        assert xbtc is not None and wbtc is not None
        assert xbtc.mint_seq < wbtc.mint_seq
        store.save_instrument(wbtc)
        store.save_instrument(xbtc)
        store.upsert_bars(
            [bar(wbtc.id, YH, 2, 500.0), bar(wbtc.id, CG, 4, 3.0), bar(xbtc.id, CG, 4, 4.0)]
        )
        supersede(store, wbtc.id, BTC)
        supersede(store, xbtc.id, BTC)

        single = store.bars_for(BTC, day(1), day(7))
        bulk = store.resolved_bars(day(1), day(7), instruments=[BTC])

    assert [(b.bar_date, b.source, b.close) for b in bulk] == [
        (b.bar_date, b.source, b.close) for b in single
    ]
    closes = {b.bar_date: (b.source, b.close) for b in bulk}
    assert closes[day(2)] == (YH, 500.0)
    assert closes[day(4)] == (CG, 4.0)


def broken_chains(store: Store) -> tuple[InstrumentId, InstrumentId, InstrumentId]:
    """AAA and BBB supersede each other; GGG is superseded into an Instrument never stored."""
    aaa, bbb, ggg = coins(store, "AAA", "BBB", "GGG")
    store.upsert_bars([bar(i, CG, 2, 1.0) for i in (aaa, bbb, ggg)])
    store.record_membership("top", day(6), [BTC, ETH, aaa, ggg])
    supersede(store, aaa, bbb)
    supersede(store, bbb, aaa)
    supersede(store, ggg, InstrumentId("crypto.native.ghost"))
    return aaa, bbb, ggg


def skip_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("skipping")]


def test_bulk_reads_skip_a_broken_chain_with_one_warning(
    data_dir: Path, ns: Namespace, caplog: pytest.LogCaptureFixture
):
    with Store(data_dir / "cointoss.db") as store:
        aaa, bbb, ggg = broken_chains(store)
    broken = {aaa, bbb, ggg}

    caplog.clear()
    rows = frame(ns, "bars", start=iso(1), end=iso(7))
    assert not broken & {r["instrument_id"] for r in rows}
    assert len(rows) == 20
    (warning,) = skip_warnings(caplog)
    assert all(i in warning for i in broken)

    caplog.clear()
    members = frame(ns, "membership", start=iso(6), end=iso(7), universe="top")
    assert {r["instrument_id"] for r in members} == {BTC, ETH}
    assert len(skip_warnings(caplog)) == 1


def test_a_named_broken_chain_is_skipped_too_but_single_reads_still_raise(
    data_dir: Path, ns: Namespace, caplog: pytest.LogCaptureFixture
):
    with Store(data_dir / "cointoss.db") as store:
        aaa, _, ggg = broken_chains(store)

    caplog.clear()
    rows = frame(ns, "returns", start=iso(2), end=iso(3), instruments=[aaa, BTC])
    assert {r["instrument_id"] for r in rows} == {BTC}
    assert skip_warnings(caplog) == [
        f"skipping {aaa}: Supersession chain cycles or ends at an Instrument not stored"
    ]
    assert frame(ns, "membership", start=iso(1), end=iso(7), instruments=[ggg]) == []

    with pytest.raises(SupersessionCycle):
        ns.get("data:bars")(instrument_id=aaa, start=iso(1), end=iso(7))
    with pytest.raises(NotFound, match="ghost"):
        ns.get("data:bars")(instrument_id=ggg, start=iso(1), end=iso(7))


def test_a_bulk_read_over_its_limit_counts_rows_after_resolving(data_dir: Path):
    with Store(data_dir / "cointoss.db") as store:
        assert len(store.resolved_bars(day(1), day(7), instruments=[BTC], limit=6)) == 6
        with pytest.raises(TooManyRows) as raised:
            store.resolved_bars(day(1), day(7), instruments=[BTC], limit=5)
        assert (raised.value.count, raised.value.limit) == (6, 5)
        with pytest.raises(TooManyRows) as raised:
            store.close_pairs(day(1), day(7), limit=1)
        assert raised.value.count == 4 + 6 + 6


# -- over HTTP --


class TestOverJsonRpc(tornado.testing.AsyncHTTPTestCase):
    """The nodes as `woodglue start` serves them, answering a client."""

    home: TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def get_app(self) -> Any:
        self.home = TemporaryDirectory()
        data_dir = Path(self.home.name)
        (data_dir / "cointoss.yaml").write_text(yaml.safe_dump(CONFIG))
        seed(data_dir / "cointoss.db")
        entry = NamespaceEntry(entries=[fragment_entry()], run_engine=False)
        return create_app(namespaces=load_namespaces({"cointoss": entry}, data_dir))

    @override
    def tearDown(self) -> None:
        super().tearDown()
        self.home.cleanup()

    def client(self) -> WoodglueClient:
        return WoodglueClient(self.get_url(""), token="unused")

    def error(self, dataset: str, **params: Any) -> WoodglueRpcError:
        async def call() -> WoodglueRpcError:
            with pytest.raises(WoodglueRpcError) as raised:
                await self.client().call("cointoss.data:frame", dataset=dataset, params=params)
            return raised.value

        return self.io_loop.run_sync(call)

    def test_errors_reach_the_client_with_their_codes(self) -> None:
        unknown = self.error("prices", start=iso(1), end=iso(2))
        assert unknown.code == NotFound.code == -32001
        assert "prices" in unknown.message and "bars" in unknown.message

        bad = self.error("bars", start=iso(2), end=iso(1))
        assert bad.code == BadRequest.code == -32602
        assert bad.message == "bars params: Value error, end 2024-03-01 precedes start 2024-03-02"

        with patch("cointoss.datasets.ROW_CAP", 3):
            capped = self.error("bars", start=iso(1), end=iso(7))
        assert capped.code == -32602
        assert "20 rows match" in capped.message

    def test_load_round_trips_to_a_typed_dataframe(self) -> None:
        client = self.client()
        df = self.io_loop.run_sync(
            lambda: load(client, "bars", start=iso(5), end=iso(7), instruments=[BTC])
        )
        assert list(df.columns) == [c.name for c in DATASETS["bars"].columns]
        assert pd.api.types.is_datetime64_dtype(df["date"])
        assert df["date"].tolist() == [pd.Timestamp(day(n)) for n in (5, 6, 7)]
        assert isinstance(df["fetched_at"].dtype, pd.DatetimeTZDtype)
        assert df["fetched_at"].iloc[0] == pd.Timestamp("2024-03-07", tz="UTC")
        assert df["close"].tolist() == [105.0, 200.0, 220.0]
        assert df["open"].isna().all()

        returns = self.io_loop.run_sync(
            lambda: load(client, "returns", start=iso(6), end=iso(6), source="coingecko")
        )
        assert str(returns["gap_days"].dtype) == "Int64"
        assert returns["gap_days"].tolist() == [2, 1, 1]

        empty = self.io_loop.run_sync(
            lambda: load(client, "runs", start="2020-01-01", end="2020-01-02")
        )
        assert len(empty) == 0
        assert pd.api.types.is_datetime64_dtype(empty["source_asof"])
