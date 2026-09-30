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

The resolution policy, in order:

- A FIGI hit is put through the registry, which matches by External Reference or mints. The
  result is persisted and the member pinned to it.
- Without a FIGI hit nothing is minted. The ticker is looked up in the Ticker History already
  held, as that source spelled it on that date, and pinned if exactly one Instrument held it.
- Otherwise the member is stored unresolved and the edit still succeeds.

Minting only on a FIGI hit is deliberate. A typed symbol with no FIGI behind it is as likely a
typo as a real listing, and a minted Instrument Id is durable and never reused. An Instrument
minted from a symbol alone would also be unreachable by a later FIGI-resolved sighting, because
the symbol tier only joins two unresolved parties, so the next ingest would mint a second one.
"""

from __future__ import annotations

import logging
from datetime import date

from cointoss.instrument import (
    AmbiguousSymbol,
    ExternalReference,
    FigiResolution,
    InstrumentId,
    InstrumentType,
    Observation,
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
    UniverseParameters,
)

__all__ = [
    "figi_job",
    "pin_member",
    "resolve_typed_symbol",
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

_ROLE_FIELD: dict[MemberRole, str] = {
    MemberRole.INCLUSION: "inclusions",
    MemberRole.EXCLUSION: "exclusions",
}


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
    instrument_type = _SOURCE_TYPE[source]
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
) -> tuple[FigiResolution, tuple[ExternalReference, ...]]:
    if job is None:
        return FigiResolution.NOT_ATTEMPTED, ()
    (result,) = await figi.map_identifiers([job])
    if result.resolution is not FigiResolution.RESOLVED:
        return result.resolution, ()
    refs = _anchor_references(instrument_type, result)
    if refs is None:
        log.warning("OpenFIGI returned several anchors for %s; not pinning", job.idValue)
        return FigiResolution.NOT_FOUND, ()
    return FigiResolution.RESOLVED, refs


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

    Builds the `Observation`, makes the FIGI attempt, and puts a resolved one through the
    registry; see the module docstring for why nothing is minted without a FIGI hit. Returns
    None when no Instrument can be named, which is a result rather than an error.
    """
    instrument_type = _SOURCE_TYPE[source]
    scope = scope or _DEFAULT_SCOPE[instrument_type]
    job = figi_job(source, symbol, scope)
    resolution, references = await _figi_attempt(figi or OpenFigiClient(), instrument_type, job)
    obs = Observation(
        type=instrument_type,
        symbol=symbol,
        scope=scope,
        observed_at=at,
        source=source,
        references=references,
        figi_resolution=resolution,
    )
    if obs.is_resolved:
        registry = store.load_registry()
        result = registry.observe(obs)
        if result.instrument is None:
            log.warning("%s at %s not pinned: %s", symbol, source, result.review)
            return None
        # Supersession rewrites the losers' `superseded_by`, so they are written too.
        store.save_instruments(
            [result.instrument, *(registry.get(loser) for loser in result.superseded)]
        )
        return result.instrument.id
    try:
        return store.resolve_symbol(source, obs.symbol, at, scope=scope)
    except AmbiguousSymbol as exc:
        log.warning("%s at %s not pinned: %s", symbol, source, exc)
        return None


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
    wanted = normalize_symbol(symbol)
    for member in store.load_universe_members(name):
        if (
            member.role is role
            and member.source is source
            and member.covers(definition.revision)
            and normalize_symbol(member.symbol_as_typed) == wanted
        ):
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
    field = _ROLE_FIELD[role]
    current = definition.parameters
    held: frozenset[InstrumentId] = getattr(current, field)
    parameters = UniverseParameters.model_validate(
        {
            **current.model_dump(),
            field: held if instrument_id is None else held | {instrument_id},
        }
    )
    entry = UniverseDefinitionRevision(
        revision=definition.revision + 1,
        changed_at=at,
        changed=(field,),
        parameters=parameters,
    )
    return UniverseDefinition(name=definition.name, revisions=(*definition.revisions, entry))
