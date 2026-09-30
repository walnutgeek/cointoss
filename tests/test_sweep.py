"""The daily market sweep, through `cointoss.ingest.sweep`.

Driven by a recorded CoinGecko `markets` listing (`tests/data/coingecko_markets_top250.json`):
the full first page of 250 coins and the head of the second. `AsyncHTTPClient` is patched in
`cointoss.sources.coingecko`, as `tests/test_coingecko.py` does, and answers each request by
its `page` parameter, so nothing touches the network and the requests sent can be read back.

The recording is real, and two of its quirks are what several tests lean on: CoinGecko's
`market_cap_rank` is not the listing position (neighbours at 99/100 and 113/114 come back
swapped, and rank 250 sits on page 2), and some coins on page 2 carry no rank at all.

Assertions are on what a caller reads back from the store -- Instruments, membership, runs,
bars -- and on the returned report.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from cointoss.ingest import DEFAULT_BAR_WINDOW, SweepReport, SweepTooNarrow, sweep
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Source,
)
from cointoss.prices import is_provisional
from cointoss.store import Store
from cointoss.universe import RunOutcome, UniverseDefinition, UniverseParameters

FIXTURE = Path(__file__).parent / "data" / "coingecko_markets_top250.json"
AT = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
DAY = AT.date()
BTC = InstrumentId("crypto.native.btc")
ETH = InstrumentId("crypto.native.eth")

Page = list[dict[str, Any]]


def recorded() -> list[Page]:
    return json.loads(FIXTURE.read_text())["pages"]


def coingecko(*pages: Page) -> MagicMock:
    """An `AsyncHTTPClient` answering `/coins/markets` with the page its query names."""

    async def fetch(url: str) -> MagicMock:
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        response = MagicMock()
        response.body = json.dumps(pages[page - 1] if page <= len(pages) else []).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    return client


def queries(client: MagicMock) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []
    for call in client.fetch.call_args_list:
        url: str = call.args[0]
        sent.append({k: v[0] for k, v in parse_qs(urlparse(url).query).items()})
    return sent


async def run(
    store: Store,
    pages: list[Page] | None = None,
    *,
    at: datetime = AT,
    definitions: tuple[str, ...] = ("cg-top-100", "cg-top-10"),
    top_n: int = 250,
    bar_window: timedelta = DEFAULT_BAR_WINDOW,
) -> SweepReport:
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient",
        return_value=coingecko(*(recorded() if pages is None else pages)),
    ):
        return await sweep(store, at, definitions, top_n=top_n, bar_window=bar_window)


def define(store: Store, name: str, enter: int, leave: int) -> None:
    store.save_universe(
        UniverseDefinition.create(
            name, UniverseParameters(enter_rank=enter, exit_rank=leave), date(2026, 1, 1)
        )
    )


def ranked_ids(store: Store, page: Page, top: int) -> set[InstrumentId]:
    """The Instruments of the coins CoinGecko ranks `top` or better, looked up by coin id."""
    by_coin = coin_index(store)
    return {
        by_coin[c["id"]]
        for c in page
        if c["market_cap_rank"] is not None and c["market_cap_rank"] <= top
    }


def coin_index(store: Store) -> dict[str, InstrumentId]:
    return {
        ref.value: i.id
        for i in store.load_instruments()
        for ref in i.references
        if ref == ExternalReference.provider_id("coingecko", ref.value)
    }


@pytest.fixture
def store() -> Iterator[Store]:
    with TemporaryDirectory() as tmp, Store(Path(tmp) / "cointoss.db") as opened:
        define(opened, "cg-top-100", 100, 120)
        define(opened, "cg-top-10", 10, 12)
        yield opened


async def test_one_sweep_mints_records_membership_and_stores_bars(store: Store):
    report = await run(store)
    page = recorded()[0]

    assert report.listed == 250
    # The one coin whose symbol has no ASCII letter or digit cannot form an Instrument Id.
    assert set(report.skipped) == {"bianrensheng"}
    assert report.minted == 249
    assert len(store.load_instruments()) == 249
    assert [r.definition for r in report.runs] == ["cg-top-100", "cg-top-10"]
    for name, top in (("cg-top-100", 100), ("cg-top-10", 10)):
        (stored_run,) = store.load_evaluation_runs(name)
        assert stored_run.outcome is RunOutcome.CHANGED
        assert stored_run.source_asof == DAY
        assert stored_run.n_admitted == top
        assert set(store.members_at(name, DAY)) == ranked_ids(store, page, top)

    (bar,) = store.bars_for(BTC, DAY, DAY, source=Source.COINGECKO)
    assert bar.close == 83227
    assert bar.volume == pytest.approx(28071563826)
    assert bar.market_cap == 1672100896326
    assert bar.fetched_at == AT
    assert report.bars == 249
    assert report.bars_kept == 0
    assert not report.bars_skipped_late
    assert report.restatements == 0


async def test_coins_are_identified_by_coingecko_id_with_figi_not_attempted(store: Store):
    await run(store)
    btc = store.load_instrument(BTC)

    assert btc is not None
    assert ExternalReference.provider_id("coingecko", "bitcoin") in btc.references
    assert btc.figi_resolution is FigiResolution.NOT_ATTEMPTED
    assert btc.name == "Bitcoin"
    assert btc.identity_source is Source.COINGECKO


async def test_a_swept_bar_is_final_not_provisional(store: Store):
    """The listing fetched just after midnight is date D's bar and is final when written."""
    await run(store)
    (bar,) = store.bars_for(ETH, DAY, DAY, source=Source.COINGECKO)

    assert bar.bar_date == DAY
    assert not is_provisional(bar)


async def test_a_second_sweep_with_the_same_payload_changes_nothing(store: Store):
    await run(store)
    instruments = store.load_instruments()
    runs = {n: store.load_evaluation_runs(n) for n in ("cg-top-100", "cg-top-10")}
    series = store.load_universe_series("cg-top-100")

    again = await run(store)

    assert again.minted == 0
    assert again.restatements == 0
    assert store.load_instruments() == instruments
    assert {n: store.load_evaluation_runs(n) for n in runs} == runs
    assert store.load_universe_series("cg-top-100") == series
    assert store.restatements_for(BTC) == []
    assert [r.definition for r in again.runs] == ["cg-top-100", "cg-top-10"]


async def test_the_next_day_matches_rather_than_remints(store: Store):
    await run(store)
    report = await run(store, at=AT + timedelta(days=1))

    assert report.minted == 0
    assert report.matched == 249
    assert len(store.load_instruments()) == 249
    (run_next,) = [r for r in report.runs if r.definition == "cg-top-10"]
    assert run_next.outcome is RunOutcome.UNCHANGED


async def test_a_ticker_change_keeps_the_instrument(store: Store):
    await run(store)
    renamed = [{**c, "symbol": "xbt"} if c["id"] == "bitcoin" else c for c in recorded()[0]]
    report = await run(store, [renamed], at=AT + timedelta(days=1))

    assert report.minted == 0
    btc = store.load_instrument(BTC)
    assert btc is not None
    assert btc.symbol == "XBT"
    assert [(r.symbol, r.valid_to) for r in btc.ticker_history] == [
        ("BTC", DAY + timedelta(days=1)),
        ("XBT", None),
    ]
    assert store.resolve_symbol(Source.COINGECKO, "xbt", DAY + timedelta(days=1)) == BTC


async def test_rank_is_market_cap_rank_not_listing_position(store: Store):
    """The recording lists LayerZero (rank 100) at position 99 and Flare (rank 99) at 100."""
    define(store, "cg-top-99", 99, 99)
    await run(store, definitions=("cg-top-99",))
    by_coin = coin_index(store)
    members = set(store.members_at("cg-top-99", DAY))

    assert by_coin["flare-networks"] in members
    assert by_coin["layerzero"] not in members
    assert len(members) == 99


async def test_a_coin_missing_from_the_listing_is_absent(store: Store):
    await run(store)
    without_btc = [c for c in recorded()[0] if c["id"] != "bitcoin"]
    await run(store, [without_btc], at=AT + timedelta(days=1))

    assert BTC in store.members_at("cg-top-10", DAY)
    assert BTC not in store.members_at("cg-top-10", DAY + timedelta(days=1))


async def test_flagged_and_malformed_coins_are_skipped_not_fatal(store: Store):
    # CoinGecko's `ethereum` already names an Instrument in another scope: a merge question.
    registry = InstrumentRegistry()
    result = registry.observe(
        Observation(
            type=InstrumentType.CRYPTO,
            symbol="ETH",
            scope="eth",
            observed_at=date(2020, 1, 1),
            source=Source.COINGECKO,
            references=(ExternalReference.provider_id("coingecko", "ethereum"),),
        )
    )
    assert result.instrument is not None
    store.save_instruments(registry.instruments())
    page = [
        *recorded()[0][:20],
        {"id": "punctuation", "symbol": "!!!", "name": "Nothing", "market_cap_rank": 21},
        {"id": "unpriced", "symbol": "npx", "name": "No Price", "market_cap_rank": 22},
        {"id": "negative", "symbol": "neg", "name": "Bad Tick", "current_price": -1.0},
    ]

    report = await run(store, [page], definitions=("cg-top-10",), top_n=23)

    assert set(report.skipped) == {"ethereum", "punctuation"}
    assert report.listed == 23
    assert report.bars == 19
    assert len(store.members_at("cg-top-10", DAY)) == 9
    assert store.bars_for(coin_index(store)["unpriced"], DAY, DAY) == []


async def test_every_page_is_fetched_when_n_exceeds_one(store: Store):
    with patch(
        "cointoss.sources.coingecko.AsyncHTTPClient", return_value=coingecko(*recorded())
    ) as factory:
        report = await sweep(store, AT, ("cg-top-100",), top_n=300)
    sent = queries(factory.return_value)

    assert [(q["page"], q["per_page"]) for q in sent] == [("1", "250"), ("2", "250")]
    assert {q["order"] for q in sent} == {"market_cap_desc"}
    assert report.listed == 300
    assert "ozone-chain" in coin_index(store)


async def test_n_narrower_than_the_widest_exit_rank_is_refused_before_fetching(store: Store):
    client = coingecko(*recorded())
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=client),
        pytest.raises(SweepTooNarrow),
    ):
        await sweep(store, AT, ("cg-top-100", "cg-top-10"), top_n=119)
    client.fetch.assert_not_called()


async def test_only_the_named_definitions_are_evaluated(store: Store):
    report = await run(store, definitions=("cg-top-10",))

    assert [r.definition for r in report.runs] == ["cg-top-10"]
    assert store.load_evaluation_runs("cg-top-100") == []


async def test_a_same_day_resweep_returns_the_days_runs_rather_than_reevaluating(store: Store):
    """A moved market later the same day would otherwise put two memberships on one date."""
    first = await run(store)
    reordered = [c for c in recorded()[0] if c["id"] != "bitcoin"]

    report = await run(store, [reordered], at=AT + timedelta(minutes=30))

    assert report.failed == {}
    assert report.runs == first.runs
    assert len(store.load_evaluation_runs("cg-top-10")) == 1
    assert BTC in store.members_at("cg-top-10", DAY)


async def test_a_definition_refusing_the_date_is_reported_not_raised(store: Store):
    """A day earlier than one already evaluated cannot be slotted in behind it."""
    await run(store, at=AT + timedelta(days=1))

    report = await run(store, at=AT)

    assert set(report.failed) == {"cg-top-100", "cg-top-10"}
    assert report.runs == ()


async def test_a_resweep_keeps_the_stored_bar_and_files_no_restatement(store: Store):
    """The day's bar is the 00:00 price; a later fetch the same day is not a new vintage of it."""
    await run(store)
    moved = [{**c, "current_price": 90000.0} if c["id"] == "bitcoin" else c for c in recorded()[0]]

    report = await run(store, [moved], at=AT + timedelta(hours=1))

    assert report.bars == 0
    assert report.bars_kept == 249
    assert report.restatements == 0
    assert store.restatements_for(BTC) == []
    (bar,) = store.bars_for(BTC, DAY, DAY, source=Source.COINGECKO)
    assert (bar.close, bar.fetched_at) == (83227, AT)


async def test_a_sweep_after_the_bar_window_records_membership_but_no_bars(store: Store):
    late = datetime(2026, 9, 30, 3, 1, tzinfo=UTC)

    report = await run(store, at=late)

    assert report.bars_skipped_late
    assert report.bars == 0
    assert store.bars_for(BTC, DAY, DAY) == []
    assert [r.definition for r in report.runs] == ["cg-top-100", "cg-top-10"]
    assert len(store.members_at("cg-top-10", DAY)) == 10


async def test_the_bar_window_is_the_callers_to_set(store: Store):
    at_edge = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    assert not (await run(store, at=at_edge)).bars_skipped_late

    later = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
    report = await run(store, at=later, bar_window=timedelta(hours=6))

    assert not report.bars_skipped_late
    (bar,) = store.bars_for(BTC, DAY + timedelta(days=1), DAY + timedelta(days=1))
    assert bar.fetched_at == later
