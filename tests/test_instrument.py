"""Identity rules of ADR-0004, driven through the public seam of `cointoss.instrument`.

Every test here is a sequence of observations with asserted outcomes: Instrument Ids,
Supersession relations, FIGI Resolution states and date-resolved symbols. Nothing below
reaches into minting internals, normalisation helpers or the shape of the collection.
"""

from __future__ import annotations

from datetime import date

import pytest

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Outcome,
    SupersessionCycle,
)

# Real FIGIs, verified against the OpenFIGI mapping endpoint during specification.
META_FIGI = "BBG000MM2P62"
GOOG_COMPOSITE = "BBG009S3NB30"
GOOG_SHARE_CLASS = "BBG009S3NB21"
GOOGL_COMPOSITE = "BBG009S39JX6"
GOOGL_SHARE_CLASS = "BBG009S39JY5"

USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_IMPOSTOR_ETH = "0xdeadbeef00000000000000000000000000000001"

IdentityState = list[tuple[object, ...]]


def stock(
    symbol: str,
    observed_at: date,
    *,
    figi: str | None = None,
    share_class_figi: str | None = None,
    issuer_name: str | None = None,
    provider: tuple[str, str] | None = None,
    scope: str = "us",
    name: str | None = None,
    resolution: FigiResolution | None = None,
) -> Observation:
    """Build a listed-equity observation."""
    refs: list[ExternalReference] = []
    if figi:
        refs.append(ExternalReference.composite_figi(figi))
    if share_class_figi:
        refs.append(ExternalReference.share_class_figi(share_class_figi))
    if provider:
        refs.append(ExternalReference.provider_id(*provider))
    if resolution is None:
        resolution = FigiResolution.RESOLVED if figi else FigiResolution.NOT_FOUND
    return Observation(
        type=InstrumentType.STOCK,
        symbol=symbol,
        scope=scope,
        observed_at=observed_at,
        name=name,
        issuer_name=issuer_name,
        references=tuple(refs),
        figi_resolution=resolution,
    )


def token(
    symbol: str,
    scope: str,
    observed_at: date,
    *,
    contract: str | None = None,
    figi: str | None = None,
    provider: tuple[str, str] | None = None,
    issuer_name: str | None = None,
) -> Observation:
    """Build a crypto observation."""
    refs: list[ExternalReference] = []
    if figi:
        refs.append(ExternalReference.asset_figi(figi))
    if contract:
        refs.append(ExternalReference.contract(scope, contract))
    if provider:
        refs.append(ExternalReference.provider_id(*provider))
    return Observation(
        type=InstrumentType.CRYPTO,
        symbol=symbol,
        scope=scope,
        observed_at=observed_at,
        issuer_name=issuer_name,
        references=tuple(refs),
        figi_resolution=FigiResolution.RESOLVED if figi else FigiResolution.NOT_FOUND,
    )


def identity_state(reg: InstrumentRegistry) -> IdentityState:
    """Everything a flagged Observation could have touched, read through the seam."""
    return [
        (
            i.id,
            i.type,
            i.scope,
            i.symbol,
            i.figi_resolution,
            i.superseded_by,
            i.issuer_name,
            tuple(i.references),
            tuple(i.ticker_history),
        )
        for i in reg.instruments()
    ]


def test_fb_to_meta_rename_is_one_instrument() -> None:
    """The June 2022 rename must not split the position: same FIGI, same Instrument."""
    reg = InstrumentRegistry()
    first = reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    assert first.outcome is Outcome.MINTED
    assert first.instrument is not None
    assert first.instrument.id == "stock.us.fb"

    renamed = reg.observe(
        stock("META", date(2022, 6, 9), figi=META_FIGI, issuer_name="Meta Platforms Inc")
    )
    assert renamed.outcome is Outcome.MATCHED
    assert renamed.instrument is not None
    assert renamed.instrument.id == "stock.us.fb"
    assert renamed.instrument.symbol == "META"
    assert [r.symbol for r in renamed.instrument.ticker_history] == ["FB", "META"]
    assert renamed.instrument.ticker_history[0].valid_to == date(2022, 6, 9)
    assert renamed.instrument.ticker_history[1].valid_to is None
    assert len(reg.instruments()) == 1


def test_reused_ticker_mints_a_distinct_instrument() -> None:
    """A different issuer under the freed FB ticker is a different asset, never a match."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    reg.observe(stock("META", date(2022, 6, 9), figi=META_FIGI, issuer_name="Meta Platforms Inc"))

    other = reg.observe(
        stock("FB", date(2023, 6, 1), figi="BBG01HKJ0000", issuer_name="ProShares Trust")
    )
    assert other.outcome is Outcome.MINTED
    assert other.instrument is not None
    assert other.instrument.id == "stock.us.fb_proshares"

    meta = reg.get("stock.us.fb")
    assert meta.symbol == "META"
    assert [r.symbol for r in meta.ticker_history] == ["FB", "META"]
    assert [r.symbol for r in other.instrument.ticker_history] == ["FB"]


def test_symbol_resolution_is_scoped_by_date() -> None:
    """A symbol on a document resolves to whatever traded under it on that document's date."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    reg.observe(stock("META", date(2022, 6, 9), figi=META_FIGI, issuer_name="Meta Platforms Inc"))
    reg.observe(stock("FB", date(2023, 6, 1), figi="BBG01HKJ0000", issuer_name="ProShares Trust"))

    before = reg.resolve_symbol(InstrumentType.STOCK, "FB", "us", date(2021, 3, 1))
    assert before is not None
    assert before.id == "stock.us.fb"

    after = reg.resolve_symbol(InstrumentType.STOCK, "FB", "us", date(2024, 3, 1))
    assert after is not None
    assert after.id == "stock.us.fb_proshares"

    assert reg.resolve_symbol(InstrumentType.STOCK, "FB", "us", date(2010, 1, 1)) is None
    meta_now = reg.resolve_symbol(InstrumentType.STOCK, "META", "us", date(2024, 3, 1))
    assert meta_now is not None
    assert meta_now.id == "stock.us.fb"


def test_late_supersession_keeps_the_earliest_instrument() -> None:
    """Coverage catching up folds two unresolved Instruments into the earlier one."""
    reg = InstrumentRegistry()
    a = reg.observe(stock("FB", date(2012, 5, 18), provider=("broker", "1"))).instrument
    b = reg.observe(stock("META", date(2022, 6, 9), provider=("gecko", "9"))).instrument
    assert a is not None and b is not None
    assert a.id == "stock.us.fb"
    assert b.id == "stock.us.meta"
    assert [i.id for i in reg.unresolved()] == ["stock.us.fb", "stock.us.meta"]

    # Retry resolves the earlier one first: matched by the broker's own stable id.
    upgraded = reg.observe(stock("FB", date(2023, 1, 5), figi=META_FIGI, provider=("broker", "1")))
    assert upgraded.outcome is Outcome.MATCHED
    assert upgraded.instrument is not None
    assert upgraded.instrument.id == "stock.us.fb"
    assert upgraded.instrument.figi_resolution is FigiResolution.RESOLVED
    assert [i.id for i in reg.unresolved()] == ["stock.us.meta"]

    # The same FIGI now arrives against the other Instrument's provider id.
    merged = reg.observe(stock("META", date(2023, 1, 6), figi=META_FIGI, provider=("gecko", "9")))
    assert merged.outcome is Outcome.SUPERSEDED
    assert merged.instrument is not None
    assert merged.instrument.id == "stock.us.fb"

    loser = reg.get("stock.us.meta")
    assert loser.superseded_by == "stock.us.fb"
    assert loser.symbol == "META"
    assert [r.symbol for r in loser.ticker_history] == ["META"]
    assert reg.survivor("stock.us.meta").id == "stock.us.fb"
    assert reg.unresolved() == []


def test_conflicting_type_is_flagged_and_not_acted_on() -> None:
    """An anchor matching across a type boundary must never merge anything."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    before = identity_state(reg)

    crossed = reg.observe(
        token("FB", "eth", date(2023, 1, 1), figi=META_FIGI, issuer_name="Impostor")
    )
    assert crossed.outcome is Outcome.FLAGGED
    assert crossed.instrument is None
    assert crossed.review is not None
    assert identity_state(reg) == before


def test_conflicting_scope_is_flagged_and_not_acted_on() -> None:
    """One anchor cannot span two countries: currency and settlement differ."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    before = identity_state(reg)

    crossed = reg.observe(stock("FB2A", date(2023, 1, 1), scope="de", figi=META_FIGI))
    assert crossed.outcome is Outcome.FLAGGED
    assert crossed.instrument is None
    assert crossed.review is not None
    assert identity_state(reg) == before


def test_already_superseded_instrument_blocks_a_further_merge() -> None:
    """A second merge claim over an Instrument already superseded elsewhere is flagged."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), provider=("broker", "1")))
    reg.observe(stock("META", date(2022, 6, 9), provider=("gecko", "9")))
    reg.observe(stock("XFB", date(2022, 7, 1), provider=("other", "7")))
    reg.observe(stock("FB", date(2023, 1, 5), figi=META_FIGI, provider=("broker", "1")))
    reg.observe(stock("META", date(2023, 1, 6), figi=META_FIGI, provider=("gecko", "9")))
    reg.observe(stock("XFB", date(2023, 1, 7), figi="BBG00OTHER01", provider=("other", "7")))
    assert reg.get("stock.us.meta").superseded_by == "stock.us.fb"
    before = identity_state(reg)

    conflicting = reg.observe(
        stock("XFB", date(2023, 2, 1), figi="BBG00OTHER01", provider=("gecko", "9"))
    )
    assert conflicting.outcome is Outcome.FLAGGED
    assert conflicting.instrument is None
    assert identity_state(reg) == before


def test_share_classes_stay_distinct() -> None:
    """GOOG and GOOGL differ at composite level, so no special handling is needed."""
    reg = InstrumentRegistry()
    goog = reg.observe(
        stock(
            "GOOG",
            date(2015, 10, 2),
            figi=GOOG_COMPOSITE,
            share_class_figi=GOOG_SHARE_CLASS,
            issuer_name="Alphabet Inc",
        )
    )
    googl = reg.observe(
        stock(
            "GOOGL",
            date(2015, 10, 2),
            figi=GOOGL_COMPOSITE,
            share_class_figi=GOOGL_SHARE_CLASS,
            issuer_name="Alphabet Inc",
        )
    )
    assert goog.instrument is not None and googl.instrument is not None
    assert goog.instrument.id == "stock.us.goog"
    assert googl.instrument.id == "stock.us.googl"

    # The XETRA listing of the same share class is a separate Instrument by scope.
    xetra = reg.observe(
        stock(
            "GOOG",
            date(2015, 10, 2),
            scope="de",
            figi="BBG009S3NBN9",
            share_class_figi=GOOG_SHARE_CLASS,
            issuer_name="Alphabet Inc",
        )
    )
    assert xetra.instrument is not None
    assert xetra.instrument.id == "stock.de.goog"


def test_crypto_scope_separates_chains_and_contract_collapses_sources() -> None:
    """Chain is part of the Instrument Id; the contract address is what matches."""
    reg = InstrumentRegistry()
    eth = reg.observe(
        token("USDC", "eth", date(2020, 1, 1), contract=USDC_ETH, provider=("gecko", "usd-coin"))
    )
    sol = reg.observe(
        token(
            "USDC",
            "sol",
            date(2021, 1, 1),
            contract="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        )
    )
    assert eth.instrument is not None and sol.instrument is not None
    assert eth.instrument.id == "crypto.eth.usdc"
    assert sol.instrument.id == "crypto.sol.usdc"

    same = reg.observe(
        token("USDC", "eth", date(2022, 1, 1), contract=USDC_ETH.upper(), provider=("cmc", "3408"))
    )
    assert same.outcome is Outcome.MATCHED
    assert same.instrument is not None
    assert same.instrument.id == "crypto.eth.usdc"

    impostor = reg.observe(
        token(
            "USDC",
            "eth",
            date(2022, 6, 1),
            contract=USDC_IMPOSTOR_ETH,
            issuer_name="Definitely Real",
        )
    )
    assert impostor.outcome is Outcome.MINTED
    assert impostor.instrument is not None
    assert impostor.instrument.id == "crypto.eth.usdc_definitely-real"

    btc = reg.observe(token("BTC", "native", date(2013, 1, 1), provider=("gecko", "bitcoin")))
    assert btc.instrument is not None
    assert btc.instrument.id == "crypto.native.btc"


def test_qualifier_falls_back_to_a_number_and_retired_ids_are_never_reissued() -> None:
    """Minting must always succeed, and a superseded Instrument keeps its id forever."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    nameless = reg.observe(stock("FB", date(2023, 6, 1), figi="BBG00NONAME1"))
    assert nameless.instrument is not None
    assert nameless.instrument.id == "stock.us.fb_2"

    third = reg.observe(stock("FB", date(2024, 6, 1), provider=("broker", "3")))
    assert third.instrument is not None
    assert third.instrument.id == "stock.us.fb_3"

    # Late resolution folds fb_3 into fb_2; the retired id stays retired.
    merged = reg.observe(
        stock("FB", date(2024, 7, 1), figi="BBG00NONAME1", provider=("broker", "3"))
    )
    assert merged.outcome is Outcome.SUPERSEDED
    assert reg.get("stock.us.fb_3").superseded_by == "stock.us.fb_2"

    fourth = reg.observe(stock("FB", date(2025, 1, 1), figi="BBG00NONAME3"))
    assert fourth.instrument is not None
    assert fourth.instrument.id == "stock.us.fb_4"


def test_supersession_chains_are_chased_and_cycles_raise() -> None:
    """Aggregation needs a fixed point; a cycle is a defect, not a loop."""
    reg = InstrumentRegistry()
    reg.observe(stock("AAA", date(2020, 1, 1), provider=("broker", "1")))
    reg.observe(stock("BBB", date(2020, 2, 1), provider=("gecko", "2")))
    reg.observe(stock("CCC", date(2020, 3, 1), provider=("cmc", "3")))

    # ccc folds into bbb first, so the later aaa merge leaves a two-link chain behind.
    reg.observe(stock("BBB", date(2021, 1, 1), figi="BBG00CHAIN01", provider=("gecko", "2")))
    reg.observe(stock("CCC", date(2021, 1, 2), figi="BBG00CHAIN01", provider=("cmc", "3")))
    assert reg.get("stock.us.ccc").superseded_by == "stock.us.bbb"

    reg.observe(stock("AAA", date(2021, 1, 3), figi="BBG00CHAIN01", provider=("broker", "1")))
    assert reg.get("stock.us.bbb").superseded_by == "stock.us.aaa"
    assert reg.get("stock.us.ccc").superseded_by == "stock.us.bbb"
    assert reg.survivor("stock.us.ccc").id == "stock.us.aaa"
    assert [i.id for i in reg.unresolved()] == []

    # Deliberately corrupt state: the rules above cannot mint a cycle, so it is written
    # directly to prove that chasing raises instead of spinning.
    reg.get("stock.us.aaa").superseded_by = "stock.us.bbb"
    with pytest.raises(SupersessionCycle):
        reg.survivor("stock.us.aaa")


def test_two_unresolved_sightings_of_one_symbol_join() -> None:
    """The last-resort tier: with nothing but a symbol, two unresolved sightings are one."""
    reg = InstrumentRegistry()
    first = reg.observe(stock("ZZZ", date(2020, 1, 1)))
    assert first.outcome is Outcome.MINTED
    assert first.instrument is not None
    assert first.instrument.id == "stock.us.zzz"

    again = reg.observe(stock("ZZZ", date(2020, 2, 1), name="Zebra Zoo"))
    assert again.outcome is Outcome.MATCHED
    assert again.instrument is not None
    assert again.instrument.id == "stock.us.zzz"
    assert len(reg.instruments()) == 1

    # A symbol never joins an Instrument whose identity is already established.
    reg.observe(stock("ZZZ", date(2020, 3, 1), provider=("broker", "z")))
    reg.observe(stock("ZZZ", date(2020, 4, 1), figi="BBG00ZZZ001", provider=("broker", "z")))
    assert reg.get("stock.us.zzz").figi_resolution is FigiResolution.RESOLVED
    later = reg.observe(stock("ZZZ", date(2020, 5, 1)))
    assert later.outcome is Outcome.MINTED
    assert later.instrument is not None
    assert later.instrument.id == "stock.us.zzz_2"


def test_several_unresolved_candidates_under_one_symbol_are_flagged() -> None:
    """Symbol evidence pointing at two unresolved Instruments is ambiguous, so nothing happens."""
    reg = InstrumentRegistry()
    reg.observe(stock("ZZZ", date(2020, 1, 1), provider=("broker", "1")))
    reg.observe(stock("YYY", date(2020, 2, 1), provider=("gecko", "2")))
    # The second Instrument is renamed onto the same symbol by its own provider id.
    reg.observe(stock("ZZZ", date(2020, 3, 1), provider=("gecko", "2")))
    assert reg.get("stock.us.yyy").symbol == "ZZZ"
    before = identity_state(reg)

    ambiguous = reg.observe(stock("ZZZ", date(2020, 4, 1)))
    assert ambiguous.outcome is Outcome.FLAGGED
    assert ambiguous.instrument is None
    assert ambiguous.review is not None
    assert identity_state(reg) == before


def test_not_attempted_is_distinguishable_from_not_found() -> None:
    """A missing ingestion step must never look like a third-party coverage gap."""
    reg = InstrumentRegistry()
    skipped = reg.observe(stock("SKIP", date(2020, 1, 1), resolution=FigiResolution.NOT_ATTEMPTED))
    missing = reg.observe(stock("MISS", date(2020, 1, 1)))
    assert skipped.outcome is Outcome.MINTED
    assert skipped.instrument is not None
    assert missing.instrument is not None
    assert skipped.instrument.figi_resolution is FigiResolution.NOT_ATTEMPTED
    assert missing.instrument.figi_resolution is FigiResolution.NOT_FOUND
    assert [i.id for i in reg.unresolved()] == ["stock.us.skip", "stock.us.miss"]

    # An attempt that comes back empty turns a missing step into a known coverage gap.
    attempted = reg.observe(stock("SKIP", date(2020, 2, 1)))
    assert attempted.instrument is not None
    assert attempted.instrument.figi_resolution is FigiResolution.NOT_FOUND


def test_ambiguous_symbol_at_a_date_raises() -> None:
    """Two live Instruments under one symbol on one date is not silently resolvable."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    reg.observe(stock("FB", date(2013, 1, 1), figi="BBG00OVERLAP", issuer_name="ProShares Trust"))
    with pytest.raises(AmbiguousSymbol):
        reg.resolve_symbol(InstrumentType.STOCK, "FB", "us", date(2015, 1, 1))


def test_a_late_arriving_older_sighting_does_not_rewrite_ticker_history() -> None:
    """An out-of-order statement must not make an old symbol current again."""
    reg = InstrumentRegistry()
    reg.observe(stock("FB", date(2012, 5, 18), figi=META_FIGI, issuer_name="Facebook Inc"))
    reg.observe(stock("META", date(2022, 6, 9), figi=META_FIGI))
    stale = reg.observe(stock("FB", date(2015, 1, 1), figi=META_FIGI))
    assert stale.outcome is Outcome.MATCHED
    assert stale.instrument is not None
    assert stale.instrument.symbol == "META"
    assert [r.symbol for r in stale.instrument.ticker_history] == ["FB", "META"]
