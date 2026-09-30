"""Ingest: where Observations are constructed, and so where ADR-0004's FIGI attempt is enforced.

`cointoss.instrument` cannot compel a FIGI attempt -- it is pure and takes the outcome as given
-- so the invariant lives here, at the only place an `Observation` is built from outside data.
It holds two paths in. Pinning a typed ticker (issue #14): a researcher types a symbol against
a source, and it is resolved to an Instrument once, at edit time, per ADR-0007. And the daily
market sweep, `sweep`, which turns one CoinGecko `markets` listing into Instruments,
membership and bars. The unresolved retry runner is a later step.

It sits apart from its neighbours because it is the one layer that combines them: it makes a
network call, which `cointoss.instrument` and `cointoss.universe` must not; it applies identity
rules, which `cointoss.store` must not; and it writes through the store, which the source
adapters must not.

The resolution policy, in order, following ADR-0004:

- A FIGI hit for a symbol that exactly one unresolved Instrument held, at that source on that
  date, is that Instrument's retry: the references are attached to it through
  `InstrumentRegistry.anchor` rather than minting a numbered second one.
- Any other FIGI hit is put through the registry, which matches by External Reference or mints.
  The result is persisted and the member pinned to it.
- A FIGI answer naming several distinct anchors is flagged for review. Picking one would guess,
  and minting from the symbol would ignore evidence that the ticker is contested.
- Otherwise -- no match, an OpenFIGI outage, or a scope with no FIGI exchange to ask -- the
  ticker is first looked up in the Ticker History already held, as that source spelled it on
  that date, and pinned if exactly one Instrument held it.
- Failing that the Observation goes through the registry's symbol-keyed last resort, which
  mints an Instrument flagged unresolved (or joins one already minted that way), and the member
  is pinned to it. An outage is recorded as `NOT_ATTEMPTED` and a miss as `NOT_FOUND`; both put
  the Instrument in the registry's unresolved queue, which `retry_resolution` works one
  Instrument at a time.
- Anything the registry flags for review, or a symbol two Instruments hold at once, leaves the
  member stored unresolved, and the edit still succeeds.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    IdentityResult,
    Instrument,
    InstrumentError,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Outcome,
    Source,
    normalize_symbol,
)
from cointoss.prices import Bar, PriceError, as_utc
from cointoss.series import EntryOrder, NotYetStarted
from cointoss.sources.coingecko import MARKETS_MAX_PER_PAGE, CoinGeckoClient, CoinsMarketItem
from cointoss.sources.openfigi import MappingJob, MappingResult, OpenFigiClient
from cointoss.store import Store
from cointoss.universe import (
    EvaluationRun,
    MemberRole,
    UniverseDefinition,
    UniverseDefinitionRevision,
    UniverseMemberRecord,
    evaluate,
)

__all__ = [
    "DEFAULT_BAR_WINDOW",
    "DEFAULT_TOP_N",
    "SweepReport",
    "SweepTooNarrow",
    "check_listing_width",
    "figi_job",
    "pin_member",
    "resolve_typed_symbol",
    "retry_resolution",
    "sweep",
]

log = logging.getLogger(__name__)


# What a symbol typed against a source denotes. Yahoo is the Identity Source for listed
# equities and CoinGecko for coins, so a symbol typed against either names that kind of thing.
_SOURCE_TYPE: dict[Source, InstrumentType] = {
    Source.YAHOO: InstrumentType.STOCK,
    Source.COINGECKO: InstrumentType.CRYPTO,
}

_DEFAULT_SCOPE: dict[InstrumentType, str] = {
    InstrumentType.STOCK: "us",
    InstrumentType.CRYPTO: "native",
}

# OpenFIGI composite exchange codes are Bloomberg's, not ISO countries (Canada is `CN`, the UK
# `LN`), so only scopes whose code is known are sent. Others get no job rather than a guess.
_COMPOSITE_EXCH_CODE: dict[str, str] = {"us": "US"}


def figi_job(source: Source, symbol: str, scope: str | None = None) -> MappingJob | None:
    """The OpenFIGI mapping request for a symbol typed against a source, if one can be formed.

    Yahoo spells a share class with a hyphen where OpenFIGI uses a slash.

    >>> figi_job(Source.YAHOO, "brk-b").model_dump(exclude_none=True)
    {'idType': 'TICKER', 'idValue': 'BRK/B', 'exchCode': 'US'}
    >>> figi_job(Source.COINGECKO, "btc").model_dump(exclude_none=True)
    {'idType': 'TICKER', 'idValue': 'BTC', 'marketSecDes': 'Curncy'}
    >>> figi_job(Source.YAHOO, "7203", scope="jp") is None
    True
    """
    return _figi_job_for(_SOURCE_TYPE[source], symbol, scope)


def _figi_job_for(
    instrument_type: InstrumentType, symbol: str, scope: str | None
) -> MappingJob | None:
    ticker = normalize_symbol(symbol)
    if instrument_type is InstrumentType.CRYPTO:
        return MappingJob(idType="TICKER", idValue=ticker, marketSecDes="Curncy")
    exch_code = _COMPOSITE_EXCH_CODE.get(scope or _DEFAULT_SCOPE[instrument_type])
    if exch_code is None:
        return None
    return MappingJob(idType="TICKER", idValue=ticker.replace("-", "/"), exchCode=exch_code)


def _anchor_references(
    instrument_type: InstrumentType, result: MappingResult
) -> tuple[ExternalReference, ...] | None:
    """The References one unambiguous FIGI hit supplies, or None when there is no single hit.

    Several records naming different anchors is not an answer: picking one would pin the member
    to whichever the response happened to list first.
    """
    refs: list[ExternalReference] = []
    for record in result.data:
        found = (
            record.crypto_references()
            if instrument_type is InstrumentType.CRYPTO
            else record.equity_references()
        )
        refs.extend(r for r in found if r not in refs)
    if len({r for r in refs if r.is_anchor_figi}) != 1:
        return None
    return tuple(refs)


async def _figi_attempt(
    figi: OpenFigiClient, instrument_type: InstrumentType, job: MappingJob | None
) -> tuple[FigiResolution, tuple[ExternalReference, ...]] | IdentityResult:
    """The FIGI outcome and the References it supplies, or a flagged result when it is ambiguous."""
    if job is None:
        return FigiResolution.NOT_ATTEMPTED, ()
    (result,) = await figi.map_identifiers([job])
    if result.resolution is not FigiResolution.RESOLVED:
        return result.resolution, ()
    refs = _anchor_references(instrument_type, result)
    if refs is None:
        return IdentityResult(
            Outcome.FLAGGED, review=f"OpenFIGI returned several anchors for {job.idValue}"
        )
    return FigiResolution.RESOLVED, refs


def _identify(store: Store, obs: Observation) -> IdentityResult:
    """Put an Observation through the registry and persist whatever it establishes.

    A FIGI answer for a symbol an unresolved Instrument already held at that source on that
    date is that Instrument's retry, so it is anchored there rather than minting a second one.
    """
    registry = store.load_registry()
    waiting = _waiting_holder(registry, obs) if obs.is_resolved else None
    result = registry.anchor(waiting.id, obs) if waiting else registry.observe(obs)
    _persist(store, registry, result)
    return result


def _waiting_holder(registry: InstrumentRegistry, obs: Observation) -> Instrument | None:
    """The one unresolved Instrument whose Ticker History held the Observation's symbol."""
    try:
        held = registry.resolve_symbol(obs.source, obs.type, obs.symbol, obs.scope, obs.observed_at)
    except AmbiguousSymbol:
        # Symbol evidence is contested, so the FIGI answer alone decides.
        return None
    return held if held is not None and held.is_unresolved else None


def _persist(store: Store, registry: InstrumentRegistry, result: IdentityResult) -> None:
    if result.instrument is not None:
        # Supersession rewrites the losers' `superseded_by`, so they are written too.
        store.save_instruments(
            [result.instrument, *(registry.get(loser) for loser in result.superseded)]
        )


async def retry_resolution(
    store: Store,
    instrument_id: InstrumentId,
    at: date,
    *,
    figi: OpenFigiClient | None = None,
) -> IdentityResult | None:
    """Retry the FIGI attempt for one stored Instrument, anchoring it on a hit.

    The unit of work for the unresolved retry runner. The request is formed from the
    Instrument's type, scope and display symbol, and a hit is attached through
    `InstrumentRegistry.anchor`, which flags contrary evidence rather than acting on it. Returns
    None when OpenFIGI still has no single answer to give -- a miss, an outage, or a scope with
    no FIGI exchange -- and nothing is written. An unknown id raises `KeyError`.
    """
    registry = store.load_registry()
    target = registry.get(instrument_id)
    job = _figi_job_for(target.type, target.symbol, target.scope)
    attempt = await _figi_attempt(figi or OpenFigiClient(), target.type, job)
    if isinstance(attempt, IdentityResult):
        return attempt
    resolution, references = attempt
    if resolution is not FigiResolution.RESOLVED:
        return None
    obs = Observation(
        type=target.type,
        symbol=target.symbol,
        scope=target.scope,
        observed_at=at,
        source=target.identity_source,
        references=references,
        figi_resolution=resolution,
    )
    result = registry.anchor(target.id, obs)
    _persist(store, registry, result)
    return result


async def resolve_typed_symbol(
    store: Store,
    source: Source,
    symbol: str,
    at: date,
    *,
    scope: str | None = None,
    figi: OpenFigiClient | None = None,
) -> InstrumentId | None:
    """Resolve a symbol typed against a source on a date, persisting whatever it establishes.

    Builds the `Observation`, makes the FIGI attempt, and resolves it by the policy in the module
    docstring. Returns None when no Instrument can be named, which is a result rather than an
    error.
    """
    instrument_type = _SOURCE_TYPE[source]
    scope = scope or _DEFAULT_SCOPE[instrument_type]
    job = figi_job(source, symbol, scope)
    attempt = await _figi_attempt(figi or OpenFigiClient(), instrument_type, job)
    if isinstance(attempt, IdentityResult):
        log.warning("%s at %s flagged for review: %s", symbol, source, attempt.review)
        return None
    resolution, references = attempt
    obs = Observation(
        type=instrument_type,
        symbol=symbol,
        scope=scope,
        observed_at=at,
        source=source,
        references=references,
        figi_resolution=resolution,
    )
    if not obs.is_resolved:
        try:
            held = store.resolve_symbol(source, obs.symbol, at, scope=scope)
        except AmbiguousSymbol as exc:
            log.warning("%s at %s not pinned: %s", symbol, source, exc)
            return None
        if held is not None:
            return held
    result = _identify(store, obs)
    if result.instrument is None:
        log.warning("%s at %s flagged for review: %s", symbol, source, result.review)
        return None
    return result.instrument.id


async def pin_member(
    store: Store,
    name: str,
    role: MemberRole,
    source: Source,
    symbol: str,
    at: date,
    *,
    scope: str | None = None,
    figi: OpenFigiClient | None = None,
) -> UniverseMemberRecord:
    """Add a typed ticker to a Universe Definition, pinned to an Instrument if it resolves.

    The edit is one new revision, dated `at`, naming the role's field as changed. It is
    recorded whether or not resolution succeeds: an unresolved member is still a change to the
    member set, and a cold registry delays membership rather than blocking the edit. Re-adding
    a ticker already standing in the same role from the same source changes nothing and makes
    no FIGI call.
    """
    definition = store.require_universe(name)
    for member in store.load_universe_members(name):
        if _stands_as(member, definition.revision, role, source, symbol):
            return member
    instrument_id = await resolve_typed_symbol(store, source, symbol, at, scope=scope, figi=figi)
    revised = _with_member(definition, at, role, instrument_id)
    record = UniverseMemberRecord(
        role=role,
        source=source,
        symbol_as_typed=symbol,
        instrument_id=instrument_id,
        added_in_revision=revised.revision,
    )
    store.save_universe(revised, [record])
    return record


def _stands_as(
    member: UniverseMemberRecord, revision: int, role: MemberRole, source: Source, symbol: str
) -> bool:
    """Whether `member` is `symbol` typed against `source`, standing in `role` at `revision`."""
    return (
        member.role is role
        and member.source is source
        and member.covers(revision)
        and normalize_symbol(member.symbol_as_typed) == normalize_symbol(symbol)
    )


def _with_member(
    definition: UniverseDefinition,
    at: date,
    role: MemberRole,
    instrument_id: InstrumentId | None,
) -> UniverseDefinition:
    """The Definition with one more revision recording a member added in `role`.

    Built directly rather than through `UniverseDefinition.edited`, which treats an edit that
    leaves the parameters unchanged as a no-op. Adding an unresolved member, or a second ticker
    for an Instrument already present, leaves the projected sets unchanged and is still an edit.
    """
    parameters = definition.parameters.pinned(role, instrument_id)
    changed = "inclusions" if role is MemberRole.INCLUSION else "exclusions"
    entry = UniverseDefinitionRevision(
        revision=definition.revision + 1,
        changed_at=at,
        changed=(changed,),
        parameters=parameters,
    )
    return UniverseDefinition(name=definition.name, revisions=(*definition.revisions, entry))


DEFAULT_TOP_N = 250
"""How many coins a sweep lists by default: one full `markets` page."""

DEFAULT_BAR_WINDOW = timedelta(hours=3)
"""How long after 00:00 UTC a listing's price still stands for that date's 00:00 price."""


class SweepTooNarrow(ValueError):
    """The listing asked for is narrower than a Definition's exit rank, so members would vanish."""


def check_listing_width(top_n: int, exit_ranks: Iterable[int | None]) -> None:
    """Refuse a listing of `top_n` coins that stops short of the widest exit rank.

    >>> check_listing_width(120, [120, None, 12])
    >>> check_listing_width(119, [120])
    Traceback (most recent call last):
    ...
    cointoss.ingest.SweepTooNarrow: top_n 119 is narrower than exit rank 120
    """
    widest = max((rank or 0 for rank in exit_ranks), default=0)
    if top_n < widest:
        raise SweepTooNarrow(f"top_n {top_n} is narrower than exit rank {widest}")


class SweepReport(BaseModel):
    """What one sweep did, for a caller to log or report.

    `skipped` maps a CoinGecko coin id to why no Instrument was named for it; `failed` maps a
    Definition name to why its membership was not recorded. Neither aborts the sweep. `runs`
    are the Evaluation Runs in the order the Definitions were named, including one already
    recorded by an earlier sweep the same day.

    `bars` counts the bars written. `bars_kept` counts coins whose bar for the day was already
    stored and was left as it was. `bars_skipped_late` means the sweep ran after the bar window,
    so it wrote no bars at all.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    at: datetime
    listed: int
    minted: int
    matched: int
    superseded: int
    skipped: dict[str, str]
    bars: int
    bars_kept: int
    bars_skipped_late: bool
    restatements: int
    runs: tuple[EvaluationRun, ...]
    failed: dict[str, str]

    @property
    def day(self) -> date:
        """The UTC date the sweep recorded membership and bars for."""
        return as_utc(self.at).date()


async def sweep(
    store: Store,
    at: datetime,
    definitions: Iterable[str],
    *,
    top_n: int = DEFAULT_TOP_N,
    bar_window: timedelta = DEFAULT_BAR_WINDOW,
    coingecko: CoinGeckoClient | None = None,
) -> SweepReport:
    """Turn one CoinGecko `markets` listing into Instruments, membership and bars for a day.

    `at` is the moment of the sweep, when the listing is fetched. Its UTC date is the date
    everything is recorded for, and it stamps the bars' `fetched_at` and the Runs' `run_at`.

    A day's bar is the price at its 00:00 UTC, which is what CoinGecko labels its own daily
    point with, and a listing's `current_price` is that price only when fetched soon after
    midnight. So bars are written only when `at` is within `bar_window` of the day's 00:00 UTC,
    and are then final, not Provisional. A later sweep still resolves Instruments and records
    membership, and reports `bars_skipped_late`; the day's bars are left for a backfill. Nor is
    a bar already stored for the day from CoinGecko overwritten: a re-sweep's later price is not
    a new vintage of the 00:00 price, and overwriting it would file a false Restatement.

    The top `top_n` coins are listed by market cap, paging as needed. Each coin is one
    `Observation` whose FIGI attempt is recorded `NOT_ATTEMPTED` -- the crypto FIGI request is
    unverified and must not gate the sweep; `retry_resolution` takes these up later -- carrying
    its CoinGecko `id` as a provider External Reference, so a coin already held is matched
    through a ticker change rather than reminted. Every coin gets `native` scope, as a
    CoinGecko symbol typed for pinning does.

    Each named Definition is evaluated against CoinGecko's own `market_cap_rank`, not the
    listing position, which the API does not keep equal to it; a coin with no rank, or not
    listed, is absent. A Definition is evaluated once per day: when a Run for the day is already
    stored, it is returned rather than evaluated again, since a moved market would otherwise
    put two memberships on one date. The Definitions are named by the caller rather than read
    from the store, because a Definition dropped from configuration stays stored. `top_n` must
    reach the widest exit rank among them, or `SweepTooNarrow` is raised before anything is
    fetched; an unknown name raises `UnknownUniverse`.

    A coin whose Observation cannot be formed, or that the registry flags for review, is logged
    and skipped. A Definition whose membership the store refuses for this date is logged and
    reported in `failed`.
    """
    moment = as_utc(at)
    day = moment.date()
    loaded = [store.require_universe(name) for name in definitions]
    check_listing_width(top_n, (d.parameters.exit_rank for d in loaded))

    listing = await _list_markets(coingecko or CoinGeckoClient(), top_n)
    identified, outcomes, skipped = _identify_listing(store, listing, day)

    ranks: dict[InstrumentId, int] = {}
    for coin in listing:
        instrument_id = identified.get(coin.id)
        if instrument_id is not None and coin.market_cap_rank is not None:
            ranks[instrument_id] = min(
                coin.market_cap_rank, ranks.get(instrument_id, coin.market_cap_rank)
            )

    runs: list[EvaluationRun] = []
    failed: dict[str, str] = {}
    for definition in loaded:
        try:
            runs.append(_evaluate_once_a_day(store, definition, ranks, day, moment))
        except EntryOrder as exc:
            log.warning("sweep %s: %s not recorded: %s", day, definition.name, exc)
            failed[definition.name] = str(exc)

    late = moment - datetime.combine(day, time(), UTC) > bar_window
    bars: list[Bar] = []
    kept = 0
    if late:
        log.warning("sweep %s: at %s, after the %s bar window; no bars", day, moment, bar_window)
    else:
        bars = _bars(listing, identified, moment)
        already_priced = store.instruments_with_bars(Source.COINGECKO, day)
        kept = sum(bar.instrument_id in already_priced for bar in bars)
        bars = [bar for bar in bars if bar.instrument_id not in already_priced]
    restated = store.upsert_bars(bars)
    return SweepReport(
        at=moment,
        listed=len(listing),
        minted=outcomes.count(Outcome.MINTED),
        matched=outcomes.count(Outcome.MATCHED),
        superseded=outcomes.count(Outcome.SUPERSEDED),
        skipped=skipped,
        bars=len(bars),
        bars_kept=kept,
        bars_skipped_late=late,
        restatements=len(restated),
        runs=tuple(runs),
        failed=failed,
    )


async def _list_markets(client: CoinGeckoClient, top_n: int) -> list[CoinsMarketItem]:
    """The first `top_n` distinct coins by market cap, one page after another.

    The listing can move between two page requests, so a coin repeated across pages is kept
    once, and a short page is the end of the listing.
    """
    per_page = min(top_n, MARKETS_MAX_PER_PAGE)
    listing: dict[str, CoinsMarketItem] = {}
    page = 1
    while len(listing) < top_n:
        items = await client.fetch_coins_markets(
            order="market_cap_desc", per_page=per_page, page=page
        )
        for item in items:
            listing.setdefault(item.id, item)
        if len(items) < per_page:
            break
        page += 1
    return list(listing.values())[:top_n]


def _identify_listing(
    store: Store, listing: Sequence[CoinsMarketItem], day: date
) -> tuple[dict[str, InstrumentId], list[Outcome], dict[str, str]]:
    """Resolve or mint every listed coin against one registry, then persist what changed.

    The registry is loaded once rather than per coin, and only Instruments that differ from
    what was loaded are written, so a repeat sweep writes nothing.
    """
    registry = store.load_registry()
    before = {i.id: copy.deepcopy(i) for i in registry.instruments()}
    identified: dict[str, InstrumentId] = {}
    outcomes: list[Outcome] = []
    skipped: dict[str, str] = {}
    for coin in listing:
        try:
            result = registry.observe(_coin_observation(coin, day))
        except (ValueError, InstrumentError) as exc:
            result = IdentityResult(Outcome.FLAGGED, review=str(exc))
        if result.instrument is None:
            log.warning("sweep %s: coin %s skipped: %s", day, coin.id, result.review)
            skipped[coin.id] = result.review or "flagged"
            continue
        identified[coin.id] = result.instrument.id
        outcomes.append(result.outcome)
    store.save_instruments(i for i in registry.instruments() if before.get(i.id) != i)
    return identified, outcomes, skipped


def _coin_observation(coin: CoinsMarketItem, day: date) -> Observation:
    return Observation(
        type=InstrumentType.CRYPTO,
        symbol=coin.symbol,
        scope=_DEFAULT_SCOPE[InstrumentType.CRYPTO],
        observed_at=day,
        source=Source.COINGECKO,
        name=coin.name,
        references=(ExternalReference.provider_id(Source.COINGECKO, coin.id),),
        figi_resolution=FigiResolution.NOT_ATTEMPTED,
    )


def _evaluate_once_a_day(
    store: Store,
    definition: UniverseDefinition,
    ranks: dict[InstrumentId, int],
    day: date,
    moment: datetime,
) -> EvaluationRun:
    """Evaluate one Definition against the day's ranks and record it, unless the day has a Run.

    The membership in force on `day` is the previous one for hysteresis.
    """
    days_runs = store.runs_on(definition.name, day)
    if days_runs:
        return days_runs[0]
    try:
        previous = frozenset(InstrumentId(i) for i in store.members_at(definition.name, day))
    except NotYetStarted:
        previous = frozenset[InstrumentId]()
    members = evaluate(definition.parameters, ranks, previous)
    return store.record_membership(
        definition.name, day, members, revision=definition.revision, run_at=moment
    )


def _bars(
    listing: Sequence[CoinsMarketItem], identified: dict[str, InstrumentId], moment: datetime
) -> list[Bar]:
    """One Bar per priced, identified coin, keyed by the sweep's UTC date.

    Close, volume and market cap are what a `markets` listing reports.
    """
    bars: dict[InstrumentId, Bar] = {}
    for coin in listing:
        instrument_id = identified.get(coin.id)
        if instrument_id is None or instrument_id in bars or coin.current_price is None:
            continue
        try:
            bars[instrument_id] = Bar(
                instrument_id=instrument_id,
                source=Source.COINGECKO,
                bar_date=moment.date(),
                close=coin.current_price,
                volume=coin.total_volume,
                market_cap=coin.market_cap,
                fetched_at=moment,
            )
        except PriceError as exc:
            log.warning("sweep %s: no bar for coin %s: %s", moment.date(), coin.id, exc)
    return list(bars.values())
