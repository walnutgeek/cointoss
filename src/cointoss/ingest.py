"""Ingest: where Observations are constructed, and so where ADR-0004's FIGI attempt is enforced.

`cointoss.instrument` cannot compel a FIGI attempt -- it is pure and takes the outcome as given
-- so the invariant lives here, at the only place an `Observation` is built from outside data.
This module is the narrow slice of ingest that pinning a typed ticker needs (issue #14) and no
more: a researcher types a symbol against a source, and it is resolved to an Instrument once,
at edit time, per ADR-0007. Scheduled sweeps and the unresolved retry runner are later steps.

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

import logging
from datetime import date

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    IdentityResult,
    Instrument,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
    Observation,
    Outcome,
    Source,
    normalize_symbol,
)
from cointoss.sources.openfigi import MappingJob, MappingResult, OpenFigiClient
from cointoss.store import Store, UnknownUniverse
from cointoss.universe import (
    MemberRole,
    UniverseDefinition,
    UniverseDefinitionRevision,
    UniverseMemberRecord,
)

__all__ = [
    "figi_job",
    "pin_member",
    "resolve_typed_symbol",
    "retry_resolution",
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
    definition = store.load_universe(name)
    if definition is None:
        raise UnknownUniverse(f"no Universe Definition named {name!r}")
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
    current = definition.parameters
    added: frozenset[InstrumentId] = (
        frozenset() if instrument_id is None else frozenset({instrument_id})
    )
    match role:
        case MemberRole.INCLUSION:
            changed = "inclusions"
            parameters = current.model_copy(update={changed: current.inclusions | added})
        case MemberRole.EXCLUSION:
            changed = "exclusions"
            parameters = current.model_copy(update={changed: current.exclusions | added})
    entry = UniverseDefinitionRevision(
        revision=definition.revision + 1,
        changed_at=at,
        changed=(changed,),
        parameters=parameters,
    )
    return UniverseDefinition(name=definition.name, revisions=(*definition.revisions, entry))
