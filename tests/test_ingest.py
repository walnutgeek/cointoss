"""Pinning a typed ticker to an Instrument, ADR-0004 and ADR-0007, through `cointoss.ingest`.

Every test opens a real `Store` on a temporary file and answers OpenFIGI through the same seam
`tests/test_openfigi.py` uses: `AsyncHTTPClient` is patched in `cointoss.sources.openfigi`, so
nothing touches the network and the requests actually sent can be read back. Assertions are on
what a caller sees -- the member record returned, the Definition and Instruments read back from
the store -- not on the rows behind them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tornado.httpclient import HTTPClientError

from cointoss.ingest import pin_member, retry_resolution
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentType,
    Observation,
    Outcome,
    Source,
    TickerRecord,
)
from cointoss.sources.openfigi import API_KEY_ENV
from cointoss.store import Store, UnknownUniverse
from cointoss.universe import MemberRole, UniverseDefinition, UniverseParameters, evaluate

META_FIGI = "BBG000MM2P62"
AAPL_FIGI = "BBG000B9XRY4"
PROSHARES_FIGI = "BBG00PROSHARE1"
BTC_FIGI = "KKG000000M81"
NOT_FOUND = {"warning": "No identifier found."}


def hit(composite: str, share_class: str | None = None) -> dict[str, Any]:
    return {
        "data": [{"figi": composite, "compositeFIGI": composite, "shareClassFIGI": share_class}]
    }


def openfigi(*bodies: Any) -> MagicMock:
    """An `AsyncHTTPClient` answering each mapping request with the next body, one job each."""
    responses = []
    for body in bodies:
        response = MagicMock()
        response.body = json.dumps([body]).encode()
        responses.append(response)
    client = MagicMock()
    client.fetch = AsyncMock(side_effect=responses)
    return client


def sent_jobs(client: MagicMock) -> list[dict[str, Any]]:
    return [json.loads(call.kwargs["body"])[0] for call in client.fetch.call_args_list]


@pytest.fixture(autouse=True)
def anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)


@pytest.fixture
def store() -> Iterator[Store]:
    with TemporaryDirectory() as tmp, Store(Path(tmp) / "cointoss.db") as opened:
        opened.save_universe(
            UniverseDefinition.create("watchlist", UniverseParameters(), date(2024, 1, 1))
        )
        yield opened


async def pin(
    store: Store,
    client: MagicMock,
    symbol: str,
    at: date,
    *,
    role: MemberRole = MemberRole.INCLUSION,
    source: Source = Source.YAHOO,
    scope: str | None = None,
    name: str = "watchlist",
):
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        return await pin_member(store, name, role, source, symbol, at, scope=scope)


def seed(store: Store, *observations: Observation) -> None:
    registry = store.load_registry()
    for obs in observations:
        registry.observe(obs)
    store.save_instruments(registry.instruments())


def stock(symbol: str, at: date, figi: str, scope: str = "us") -> Observation:
    return Observation(
        type=InstrumentType.STOCK,
        symbol=symbol,
        scope=scope,
        observed_at=at,
        source=Source.YAHOO,
        references=(ExternalReference.composite_figi(figi),),
        figi_resolution=FigiResolution.RESOLVED,
    )


async def test_a_resolved_ticker_mints_an_instrument_and_pins_the_member(store: Store):
    client = openfigi(hit(AAPL_FIGI, "BBG001S5N8V8"))
    record = await pin(store, client, "aapl", date(2024, 3, 1))

    assert record.instrument_id == InstrumentId("stock.us.aapl")
    assert record.symbol_as_typed == "aapl"
    assert record.source is Source.YAHOO
    assert store.load_universe_members("watchlist") == [record]
    assert store.unresolved_members("watchlist") == []
    reloaded = store.load_universe("watchlist")
    assert reloaded is not None
    assert reloaded.parameters.inclusions == frozenset({record.instrument_id})


async def test_the_observation_carries_its_source_and_date(store: Store):
    await pin(store, openfigi(hit(AAPL_FIGI)), "AAPL", date(2024, 3, 1))
    instrument = store.load_instrument("stock.us.aapl")
    assert instrument is not None
    assert instrument.minted_at == date(2024, 3, 1)
    assert instrument.ticker_history == [
        TickerRecord(Source.YAHOO, "AAPL", "us", valid_from=date(2024, 3, 1))
    ]


async def test_the_figi_attempt_is_made_and_its_outcome_recorded(store: Store):
    client = openfigi(hit(AAPL_FIGI, "BBG001S5N8V8"))
    await pin(store, client, "AAPL", date(2024, 3, 1))
    assert sent_jobs(client) == [{"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}]
    instrument = store.load_instrument("stock.us.aapl")
    assert instrument is not None
    assert instrument.figi_resolution is FigiResolution.RESOLVED
    assert ExternalReference.composite_figi(AAPL_FIGI) in instrument.references
    assert ExternalReference.share_class_figi("BBG001S5N8V8") in instrument.references


async def test_a_yahoo_share_class_ticker_is_sent_in_openfigi_spelling(store: Store):
    client = openfigi(hit("BBG000DWG505"))
    record = await pin(store, client, "BRK-B", date(2024, 3, 1))
    assert sent_jobs(client)[0]["idValue"] == "BRK/B"
    assert record.instrument_id == InstrumentId("stock.us.brk-b")


async def test_an_unknown_ticker_mints_an_unresolved_instrument_and_pins_it(store: Store):
    record = await pin(store, openfigi(NOT_FOUND), "NOSUCH", date(2024, 3, 1))

    assert record.instrument_id == InstrumentId("stock.us.nosuch")
    assert store.unresolved_members("watchlist") == []
    instrument = store.load_instrument("stock.us.nosuch")
    assert instrument is not None
    assert instrument.figi_resolution is FigiResolution.NOT_FOUND
    assert instrument.references == []
    # Flagged unresolved is what puts it in the retry queue ADR-0004 requires.
    assert [i.id for i in store.load_registry().unresolved()] == [record.instrument_id]
    reloaded = store.load_universe("watchlist")
    assert reloaded is not None
    assert reloaded.parameters.inclusions == frozenset({record.instrument_id})


async def test_an_openfigi_outage_mints_unresolved_as_not_attempted(store: Store):
    failing = MagicMock()
    failing.fetch = AsyncMock(side_effect=HTTPClientError(429, "Too Many Requests"))
    record = await pin(store, failing, "AAPL", date(2024, 3, 1))
    assert record.instrument_id == InstrumentId("stock.us.aapl")
    instrument = store.load_instrument("stock.us.aapl")
    assert instrument is not None
    assert instrument.figi_resolution is FigiResolution.NOT_ATTEMPTED
    assert [i.id for i in store.load_registry().unresolved()] == [record.instrument_id]


async def test_a_second_miss_on_the_same_ticker_reaches_the_same_instrument(store: Store):
    other = UniverseDefinition.create("other", UniverseParameters(), date(2024, 1, 1))
    store.save_universe(other)
    first = await pin(store, openfigi(NOT_FOUND), "NOSUCH", date(2024, 3, 1))
    second = await pin(store, openfigi(NOT_FOUND), "nosuch", date(2024, 4, 1), name="other")
    assert second.instrument_id == first.instrument_id
    assert [i.id for i in store.load_instruments()] == [InstrumentId("stock.us.nosuch")]


async def test_a_later_figi_hit_reaches_the_instrument_minted_unresolved(store: Store):
    other = UniverseDefinition.create("other", UniverseParameters(), date(2024, 1, 1))
    store.save_universe(other)
    first = await pin(store, openfigi(NOT_FOUND), "AAPL", date(2024, 3, 1))
    second = await pin(store, openfigi(hit(AAPL_FIGI)), "AAPL", date(2024, 4, 1), name="other")
    assert [i.id for i in store.load_instruments()] == [InstrumentId("stock.us.aapl")]
    assert second.instrument_id == first.instrument_id


async def test_a_figi_hit_held_elsewhere_does_not_anchor_the_waiting_instrument(store: Store):
    # AAPL was minted unresolved, but the FIGI OpenFIGI now answers is already another
    # Instrument's: whether the two are one is a merge question, so nothing is anchored.
    await pin(store, openfigi(NOT_FOUND), "AAPL", date(2024, 3, 1))
    seed(store, stock("APPLE", date(2024, 3, 2), AAPL_FIGI))
    other = UniverseDefinition.create("other", UniverseParameters(), date(2024, 1, 1))
    store.save_universe(other)
    record = await pin(store, openfigi(hit(AAPL_FIGI)), "AAPL", date(2024, 4, 1), name="other")
    assert record.instrument_id is None
    waiting = store.load_instrument("stock.us.aapl")
    assert waiting is not None
    assert waiting.figi_resolution is FigiResolution.NOT_FOUND
    assert len(store.load_instruments()) == 2


async def retry(store: Store, client: MagicMock, instrument_id: str, at: date):
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        return await retry_resolution(store, InstrumentId(instrument_id), at)


async def test_a_retry_with_a_figi_hit_resolves_the_instrument_in_place(store: Store):
    failing = MagicMock()
    failing.fetch = AsyncMock(side_effect=HTTPClientError(503, "Service Unavailable"))
    record = await pin(store, failing, "BRK-B", date(2024, 3, 1))

    client = openfigi(hit("BBG000DWG505", "BBG001S5N8V8"))
    result = await retry(store, client, "stock.us.brk-b", date(2024, 3, 2))
    assert sent_jobs(client)[0]["idValue"] == "BRK/B"
    assert result is not None and result.outcome is Outcome.MATCHED
    instrument = store.load_instrument("stock.us.brk-b")
    assert instrument is not None
    assert instrument.figi_resolution is FigiResolution.RESOLVED
    assert ExternalReference.composite_figi("BBG000DWG505") in instrument.references
    assert store.load_registry().unresolved() == []
    assert store.load_universe_members("watchlist") == [record]


async def test_a_retry_that_misses_again_changes_nothing(store: Store):
    await pin(store, openfigi(NOT_FOUND), "NOSUCH", date(2024, 3, 1))
    before = store.load_instruments()
    assert await retry(store, openfigi(NOT_FOUND), "stock.us.nosuch", date(2024, 4, 1)) is None
    assert store.load_instruments() == before


async def test_a_retry_against_contrary_evidence_is_flagged(store: Store):
    await pin(store, openfigi(NOT_FOUND), "AAPL", date(2024, 3, 1))
    seed(store, stock("APPLE", date(2024, 3, 2), AAPL_FIGI))
    before = store.load_instruments()
    result = await retry(store, openfigi(hit(AAPL_FIGI)), "stock.us.aapl", date(2024, 4, 1))
    assert result is not None and result.outcome is Outcome.FLAGGED
    assert store.load_instruments() == before


async def test_an_unresolved_sighting_the_registry_flags_pins_nothing(store: Store):
    # Two unresolved Instruments told apart only by a provider id both answer to XYZ, but no
    # Yahoo Ticker History names either, so the registry's symbol tier sees two candidates.
    seed(
        store,
        *(
            Observation(
                type=InstrumentType.STOCK,
                symbol="XYZ",
                scope="us",
                observed_at=date(2020, 1, 2),
                source=Source.COINGECKO,
                references=(ExternalReference.provider_id("vendor", n),),
            )
            for n in ("1", "2")
        ),
    )
    record = await pin(store, openfigi(NOT_FOUND), "XYZ", date(2024, 3, 1))
    assert record.instrument_id is None
    assert store.unresolved_members("watchlist") == [record]
    assert len(store.load_instruments()) == 2


async def test_a_reference_match_pins_to_the_existing_instrument(store: Store):
    seed(store, stock("FB", date(2012, 5, 18), META_FIGI))
    record = await pin(store, openfigi(hit(META_FIGI)), "META", date(2024, 3, 1))

    assert record.instrument_id == InstrumentId("stock.us.fb")
    assert [i.id for i in store.load_instruments()] == [InstrumentId("stock.us.fb")]
    # The match is persisted: the rename it observed is now Ticker History.
    assert store.resolve_symbol(Source.YAHOO, "META", date(2024, 3, 1)) == "stock.us.fb"


async def test_a_ticker_the_registry_already_knows_pins_without_a_figi_hit(store: Store):
    seed(
        store,
        Observation(
            type=InstrumentType.CRYPTO,
            symbol="BTC",
            scope="native",
            observed_at=date(2013, 4, 28),
            source=Source.COINGECKO,
            references=(ExternalReference.provider_id("coingecko", "bitcoin"),),
        ),
    )
    client = openfigi(NOT_FOUND)
    record = await pin(store, client, "btc", date(2024, 3, 1), source=Source.COINGECKO)
    assert len(sent_jobs(client)) == 1
    assert record.instrument_id == InstrumentId("crypto.native.btc")


async def test_a_coin_resolves_on_its_asset_figi(store: Store):
    client = openfigi({"data": [{"figi": BTC_FIGI}]})
    record = await pin(store, client, "BTC", date(2024, 3, 1), source=Source.COINGECKO)
    assert record.instrument_id == InstrumentId("crypto.native.btc")
    instrument = store.load_instrument("crypto.native.btc")
    assert instrument is not None
    assert instrument.references == [ExternalReference.asset_figi(BTC_FIGI)]
    assert instrument.identity_source is Source.COINGECKO


async def test_an_ambiguous_figi_answer_is_flagged_and_mints_nothing(
    store: Store, caplog: pytest.LogCaptureFixture
):
    client = openfigi({"data": [hit(AAPL_FIGI)["data"][0], hit(META_FIGI)["data"][0]]})
    record = await pin(store, client, "AAPL", date(2024, 3, 1))
    assert record.instrument_id is None
    assert store.unresolved_members("watchlist") == [record]
    # Several anchors is an answer, not a miss: minting from the symbol would guess.
    assert store.load_instruments() == []
    assert "flagged" in caplog.text


async def test_an_observation_the_registry_flags_pins_nothing(store: Store):
    # The FIGI is already held by a listing in another country, so the registry flags the
    # observation for review rather than joining across a scope boundary.
    seed(store, stock("SHOP", date(2015, 5, 21), "BBG00CA0SHOP1", scope="ca"))
    record = await pin(store, openfigi(hit("BBG00CA0SHOP1")), "SHOP", date(2024, 3, 1))
    assert record.instrument_id is None
    assert [i.id for i in store.load_instruments()] == [InstrumentId("stock.ca.shop")]


async def test_a_scope_with_no_figi_exchange_is_not_sent_and_mints_unresolved(store: Store):
    client = openfigi()
    record = await pin(store, client, "7203", date(2024, 3, 1), scope="jp")
    assert client.fetch.call_count == 0
    assert record.instrument_id == InstrumentId("stock.jp.7203")
    instrument = store.load_instrument("stock.jp.7203")
    assert instrument is not None
    assert instrument.figi_resolution is FigiResolution.NOT_ATTEMPTED


async def test_resolution_happens_once_at_edit_time(store: Store):
    client = openfigi(hit(META_FIGI))
    record = await pin(store, client, "FB", date(2019, 3, 1))
    # Years later an unrelated issuer takes the ticker.
    seed(store, stock("FB", date(2023, 6, 1), PROSHARES_FIGI))

    reloaded = store.load_universe("watchlist")
    assert reloaded is not None
    assert evaluate(reloaded.parameters, ranked=[]) == frozenset({record.instrument_id})
    assert record.instrument_id == InstrumentId("stock.us.fb")
    assert client.fetch.call_count == 1


async def test_each_pin_is_a_revision_naming_the_role_it_edited(store: Store):
    kept = await pin(store, openfigi(hit(AAPL_FIGI)), "AAPL", date(2024, 3, 1))
    dropped = await pin(
        store, openfigi(hit(META_FIGI)), "META", date(2024, 4, 1), role=MemberRole.EXCLUSION
    )
    reloaded = store.load_universe("watchlist")
    assert reloaded is not None
    assert reloaded.revision == 3
    assert [r.changed for r in reloaded.revisions[1:]] == [("inclusions",), ("exclusions",)]
    assert [r.changed_at for r in reloaded.revisions[1:]] == [date(2024, 3, 1), date(2024, 4, 1)]
    assert (kept.added_in_revision, dropped.added_in_revision) == (2, 3)
    assert reloaded.parameters.exclusions == frozenset({dropped.instrument_id})
    assert reloaded.parameters_at(2).exclusions == frozenset()


async def test_re_adding_a_standing_ticker_changes_nothing(store: Store):
    first = await pin(store, openfigi(hit(AAPL_FIGI)), "AAPL", date(2024, 3, 1))
    again = openfigi()
    second = await pin(store, again, "aapl", date(2024, 4, 1))
    assert second == first
    assert again.fetch.call_count == 0
    reloaded = store.load_universe("watchlist")
    assert reloaded is not None
    assert reloaded.revision == 2


async def test_pinning_into_an_unknown_universe_is_refused(store: Store):
    client = openfigi()
    with pytest.raises(UnknownUniverse):
        await pin(store, client, "AAPL", date(2024, 3, 1), name="never-defined")
    assert client.fetch.call_count == 0


async def test_a_ticker_two_known_instruments_share_pins_nothing(store: Store):
    seed(
        store,
        stock("FB", date(2012, 5, 18), META_FIGI),
        stock("FB", date(2013, 1, 2), PROSHARES_FIGI),
    )
    record = await pin(store, openfigi(NOT_FOUND), "FB", date(2014, 1, 2))
    assert record.instrument_id is None
    assert store.unresolved_members("watchlist") == [record]
