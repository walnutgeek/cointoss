"""Instrument identity: minting Instrument Ids and establishing identity by External Reference.

Implements ADR-0004. Pure: no network, no clock, no database. Observations carry their own
observation date and their own FIGI Resolution outcome, so every rule here is testable without
a resolver. The module cannot compel a FIGI attempt; ADR-0004's "resolution attempt is mandatory
at creation" is an ingest-orchestration invariant enforced where Observations are constructed.

Chain vocabulary (permanent, baked into Instrument Ids): the scope of an on-chain token is the
short symbolic form of its chain -- `eth`, not `ethereum` and not the numeric chain id `1`.
Other chains follow the same convention (`sol`, `bsc`, `avax`); `native` is reserved for a
chain's own coin, so BTC is `crypto.native.btc`. The vocabulary is `Chain` below and is
deliberately small: a new chain is a code change, because an Instrument Id minted under a name
can never be reminted under another.

Instrument Id grammar: `{type}.{scope}.{symbol}[_{qualifier}]`. Symbols are normalised to
alphanumerics separated by `-`, so the first `_` in the third part always starts the Qualifier.

Note for the next reader: `stock.us.fb` denotes Meta permanently, because the id is minted from
the symbol first seen and never changed. This reads backwards and is the deliberate price of
immutable readable ids -- see ADR-0004.
"""

from __future__ import annotations

import copy
import pickle
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Any

__all__ = [
    "AmbiguousSymbol",
    "Chain",
    "ExternalReference",
    "FigiResolution",
    "IdentityResult",
    "Instrument",
    "InstrumentError",
    "InstrumentId",
    "InstrumentRegistry",
    "InstrumentType",
    "Observation",
    "Outcome",
    "ReferenceKind",
    "Scope",
    "ScopeKind",
    "SupersessionCycle",
    "TickerRecord",
    "UnknownScope",
]


class InstrumentError(Exception):
    """Base class for identity errors."""


class UnknownScope(InstrumentError):
    """Scope is not a known chain or a plausible ISO country."""


class AmbiguousSymbol(InstrumentError):
    """More than one Instrument traded under a symbol on the given date."""


class SupersessionCycle(InstrumentError):
    """A Supersession chain does not terminate. Always a defect."""


class InstrumentType(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"


class Chain(StrEnum):
    """Controlled vocabulary for crypto Scope. Short symbolic chain names, plus `native`."""

    NATIVE = "native"
    ETH = "eth"
    BTC = "btc"
    SOL = "sol"
    BSC = "bsc"
    AVAX = "avax"
    MATIC = "matic"
    TRX = "trx"
    ARB = "arb"
    OP = "op"
    BASE = "base"


class FigiResolution(StrEnum):
    """Outcome of attempting to anchor an Instrument to a FIGI.

    `NOT_FOUND` is a coverage gap; `NOT_ATTEMPTED` is a missing ingestion step. Both are
    retryable and both appear in the unresolved work queue.
    """

    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    NOT_ATTEMPTED = "not_attempted"


class ReferenceKind(StrEnum):
    """Kinds of External Reference. `SHARE_CLASS_FIGI` is stored but never used for matching."""

    COMPOSITE_FIGI = "composite_figi"
    ASSET_FIGI = "asset_figi"
    SHARE_CLASS_FIGI = "share_class_figi"
    CONTRACT = "contract"
    PROVIDER_ID = "provider_id"


class Outcome(StrEnum):
    """What an Observation did to the collection."""

    MINTED = "minted"
    MATCHED = "matched"
    SUPERSEDED = "superseded"
    FLAGGED = "flagged"


class ScopeKind(StrEnum):
    """Which authority a Scope belongs to."""

    CHAIN = "chain"
    COUNTRY = "country"


class Scope(str):
    """A validated Scope: the authority under which a symbol is unique.

    Construction is validation, so a `Scope` cannot hold an unchecked value and there is no
    way to reach one that was never checked. It is a `str`, so its stored and printed form is
    the scope itself.

    >>> Scope(InstrumentType.STOCK, "US")
    Scope('us')
    >>> Scope(InstrumentType.CRYPTO, "eth").kind
    <ScopeKind.CHAIN: 'chain'>
    >>> Scope(InstrumentType.CRYPTO, "ethereum")
    Traceback (most recent call last):
        ...
    cointoss.instrument.UnknownScope: 'ethereum' is not in the chain vocabulary...
    """

    kind: ScopeKind

    def __new__(cls, instrument_type: InstrumentType, value: str) -> Scope:
        normalized = value.strip().lower()
        if instrument_type is InstrumentType.CRYPTO:
            if normalized not in set(Chain):
                raise UnknownScope(
                    f"{normalized!r} is not in the chain vocabulary: {sorted(set(Chain))}"
                )
            kind = ScopeKind.CHAIN
        else:
            if not re.fullmatch(r"[a-z]{2}", normalized):
                raise UnknownScope(f"{normalized!r} is not an ISO 3166-1 alpha-2 country")
            kind = ScopeKind.COUNTRY
        scope = super().__new__(cls, normalized)
        scope.kind = kind
        return scope

    def __reduce__(self) -> tuple[Any, tuple[str, ScopeKind]]:
        """Rebuild from the validated value and its kind.

        The default `str` reduction would call `__new__` with one argument, so a `Scope` would
        be uncopyable and unpicklable. Reconstruction skips revalidation because a `Scope` that
        exists was validated when it was made.
        """
        return (_rebuild_scope, (str.__str__(self), self.kind))

    def __repr__(self) -> str:
        return f"Scope({str.__str__(self)!r})"


def _rebuild_scope(value: str, kind: ScopeKind) -> Scope:
    scope = str.__new__(Scope, value)
    scope.kind = kind
    return scope


class InstrumentId(str):
    """An Instrument Id: `{type}.{scope}.{symbol}[_{qualifier}]`, minted once and immutable.

    A `str` subclass, so the readable slug ADR-0004 chose is exactly what is stored, printed,
    and used as a key -- but a distinct type, so a symbol or a name cannot be passed where an
    id belongs. Construction parses and validates; there is no partial value.

    >>> InstrumentId("stock.us.fb").symbol
    'FB'
    >>> InstrumentId("stock.us.fb_proshares").qualifier
    'proshares'
    >>> InstrumentId.mint(InstrumentType.CRYPTO, Scope(InstrumentType.CRYPTO, "eth"), "USDC")
    InstrumentId('crypto.eth.usdc')
    >>> InstrumentId("AAPL")
    Traceback (most recent call last):
        ...
    ValueError: 'AAPL' is not an Instrument Id...
    """

    type: InstrumentType
    scope: Scope
    symbol: str
    qualifier: str | None

    def __new__(cls, text: str) -> InstrumentId:
        parts = text.split(".")
        if len(parts) != 3:
            raise ValueError(f"{text!r} is not an Instrument Id: expected type.scope.symbol")
        raw_type, raw_scope, tail = parts
        try:
            instrument_type = InstrumentType(raw_type)
        except ValueError:
            raise ValueError(
                f"{text!r} is not an Instrument Id: unknown type {raw_type!r}"
            ) from None
        # A normalised symbol never contains "_", so the first one always starts the Qualifier.
        symbol, _, qualifier = tail.partition("_")
        if not symbol:
            raise ValueError(f"{text!r} is not an Instrument Id: empty symbol")
        instrument_id = super().__new__(cls, text)
        instrument_id.type = instrument_type
        instrument_id.scope = Scope(instrument_type, raw_scope)
        instrument_id.symbol = symbol.upper()
        instrument_id.qualifier = qualifier or None
        return instrument_id

    @classmethod
    def mint(
        cls,
        instrument_type: InstrumentType,
        scope: Scope,
        symbol: str,
        qualifier: str | None = None,
    ) -> InstrumentId:
        """Compose an id from its parts. The only way to build one that has never existed."""
        base = f"{instrument_type}.{scope}.{symbol.lower()}"
        return cls(f"{base}_{qualifier}" if qualifier else base)

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        """Rebuild by reparsing the slug, which is the whole of the value."""
        return (InstrumentId, (str.__str__(self),))

    def __repr__(self) -> str:
        return f"InstrumentId({str.__str__(self)!r})"


# Identity resolution order. FIGI first, then an on-chain contract, then a provider's own
# stable id. Symbol and scope are the last resort and are handled outside this table, because
# a symbol match alone never establishes identity.
_MATCH_GROUPS: dict[ReferenceKind, tuple[int, str]] = {
    ReferenceKind.COMPOSITE_FIGI: (1, "figi"),
    ReferenceKind.ASSET_FIGI: (1, "figi"),
    ReferenceKind.CONTRACT: (2, "contract"),
    ReferenceKind.PROVIDER_ID: (3, "provider"),
}


@dataclass(frozen=True, order=True)
class ExternalReference:
    """An identifier issued by an outside authority.

    `qualifier` namespaces the value: the provider for a provider id, the chain for a contract
    address, empty for a FIGI, which is globally unique.
    """

    kind: ReferenceKind
    value: str
    qualifier: str = ""

    @classmethod
    def composite_figi(cls, figi: str) -> ExternalReference:
        return cls(ReferenceKind.COMPOSITE_FIGI, figi.strip().upper())

    @classmethod
    def asset_figi(cls, figi: str) -> ExternalReference:
        return cls(ReferenceKind.ASSET_FIGI, figi.strip().upper())

    @classmethod
    def share_class_figi(cls, figi: str) -> ExternalReference:
        return cls(ReferenceKind.SHARE_CLASS_FIGI, figi.strip().upper())

    @classmethod
    def contract(cls, chain: str, address: str) -> ExternalReference:
        """Chain and contract address. Addresses are compared case-insensitively.

        >>> ExternalReference.contract("eth", "0xA0B8").value
        '0xa0b8'
        """
        return cls(ReferenceKind.CONTRACT, address.strip().lower(), chain.strip().lower())

    @classmethod
    def provider_id(cls, provider: str, value: str) -> ExternalReference:
        return cls(ReferenceKind.PROVIDER_ID, value.strip(), provider.strip().lower())

    @property
    def is_anchor_figi(self) -> bool:
        """Whether this reference is the FIGI an Instrument of its type anchors on."""
        return self.kind in (ReferenceKind.COMPOSITE_FIGI, ReferenceKind.ASSET_FIGI)

    def group(self) -> tuple[int, str, str] | None:
        """Priority tier and comparison key, or None when the reference never matches."""
        tier = _MATCH_GROUPS.get(self.kind)
        return None if tier is None else (tier[0], tier[1], self.qualifier)


@dataclass(frozen=True)
class TickerRecord:
    """One time-bounded stretch of Ticker History. `valid_to` None means current."""

    symbol: str
    scope: str
    valid_from: date
    valid_to: date | None = None

    def covers(self, as_of: date) -> bool:
        return self.valid_from <= as_of and (self.valid_to is None or as_of < self.valid_to)


@dataclass(frozen=True)
class Observation:
    """A normalised sighting of an asset from some source.

    `figi_resolution` is the outcome the caller obtained, not something this module can compel.
    An anchor FIGI reference and a `RESOLVED` state must agree.
    """

    type: InstrumentType
    symbol: str
    scope: str
    observed_at: date
    name: str | None = None
    issuer_name: str | None = None
    references: tuple[ExternalReference, ...] = ()
    figi_resolution: FigiResolution = FigiResolution.NOT_ATTEMPTED

    @property
    def is_resolved(self) -> bool:
        return self.figi_resolution is FigiResolution.RESOLVED

    def __post_init__(self) -> None:
        has_anchor = any(r.is_anchor_figi for r in self.references)
        if has_anchor != self.is_resolved:
            raise ValueError(
                "figi_resolution RESOLVED and an anchor FIGI reference must accompany each other"
            )
        if not normalize_symbol(self.symbol):
            raise ValueError("observation symbol is empty after normalisation")


@dataclass
class Instrument:
    """The canonical, stable identity for a tradable asset."""

    id: InstrumentId
    type: InstrumentType
    scope: Scope
    symbol: str
    mint_seq: int
    minted_at: date
    name: str | None = None
    issuer_name: str | None = None
    references: list[ExternalReference] = field(default_factory=list)
    figi_resolution: FigiResolution = FigiResolution.NOT_ATTEMPTED
    superseded_by: InstrumentId | None = None
    ticker_history: list[TickerRecord] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.figi_resolution is FigiResolution.RESOLVED

    @property
    def is_unresolved(self) -> bool:
        return not self.is_resolved

    def absorb(self, obs: Observation) -> None:
        """Fold an Observation this Instrument has been identified with into itself."""
        known = set(self.references)
        self.references.extend(r for r in obs.references if r not in known)
        if obs.is_resolved:
            self.figi_resolution = obs.figi_resolution
        elif self.figi_resolution is FigiResolution.NOT_ATTEMPTED:
            self.figi_resolution = obs.figi_resolution
        self.name = self.name or obs.name
        self.issuer_name = self.issuer_name or obs.issuer_name
        self._record_symbol(obs)

    def _record_symbol(self, obs: Observation) -> None:
        """Append Ticker History when a successor symbol is observed.

        Only forward renames are recorded; a symbol observed before the current record began is
        an out-of-order sighting and is ignored rather than rewriting history.
        """
        if obs.symbol == self.symbol:
            return
        current = self.ticker_history[-1]
        if obs.observed_at < current.valid_from:
            return
        self.ticker_history[-1] = replace(current, valid_to=obs.observed_at)
        self.ticker_history.append(TickerRecord(obs.symbol, self.scope, valid_from=obs.observed_at))
        self.symbol = obs.symbol


@dataclass(frozen=True)
class IdentityResult:
    """What an Observation meant. `instrument` is None exactly when the outcome is FLAGGED."""

    outcome: Outcome
    instrument: Instrument | None = None
    superseded: tuple[InstrumentId, ...] = ()
    review: str | None = None


_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "lp",
        "plc",
        "trust",
        "sa",
        "ag",
        "nv",
        "spa",
        "group",
        "holdings",
    }
)


def normalize_symbol(symbol: str) -> str:
    """Normalise a symbol so casing and punctuation cannot mint two Instruments.

    >>> normalize_symbol(" brk.b ")
    'BRK-B'
    >>> normalize_symbol("BRK-B") == normalize_symbol("brk b")
    True
    """
    return _NON_ALNUM.sub("-", symbol.strip()).strip("-").upper()


def issuer_qualifier(issuer_name: str | None) -> str | None:
    """Slugify an issuer name into a Qualifier, dropping legal-form words.

    >>> issuer_qualifier("ProShares Trust")
    'proshares'
    >>> issuer_qualifier("Meta Platforms, Inc.")
    'meta-platforms'
    >>> issuer_qualifier("Trust Inc") is None
    True
    """
    if not issuer_name:
        return None
    words = [w for w in _NON_ALNUM.sub(" ", issuer_name).lower().split() if w]
    kept = [w for w in words if w not in _LEGAL_SUFFIXES]
    return "-".join(kept) or None


def _group_values(refs: Iterable[ExternalReference]) -> dict[tuple[int, str, str], set[str]]:
    """Matchable references bucketed by comparison group. The one walk both matchers need."""
    grouped: dict[tuple[int, str, str], set[str]] = {}
    for ref in refs:
        group = ref.group()
        if group is not None:
            grouped.setdefault(group, set()).add(ref.value)
    return grouped


def _match_keys(refs: Iterable[ExternalReference]) -> set[tuple[int, str, str, str]]:
    """Every matchable reference as a comparable key."""
    return {(*group, value) for group, values in _group_values(refs).items() for value in values}


def _conflict(left: Iterable[ExternalReference], right: Iterable[ExternalReference]) -> bool:
    """Whether the two reference sets disagree within a comparison group."""
    by_group = _group_values(left)
    return any(
        group in by_group and bool(values - by_group[group])
        for group, values in _group_values(right).items()
    )


class InstrumentRegistry:
    """The identity seam over an in-memory collection of Instruments.

    Write side: `observe`. Read side: `resolve_symbol`, `survivor`, `unresolved`. Storage is out
    of scope; the collection is passed in and handed back, and no ordering beyond mint order --
    which the registry tracks itself -- is assumed.
    """

    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self._instruments: list[Instrument] = instruments if instruments is not None else []
        self._by_id: dict[InstrumentId, Instrument] = {i.id: i for i in self._instruments}
        self._next_seq: int = max((i.mint_seq for i in self._instruments), default=0) + 1

    # -- Read side --

    def instruments(self) -> list[Instrument]:
        """Every Instrument, superseded ones included, in mint order."""
        return sorted(self._instruments, key=lambda i: i.mint_seq)

    def get(self, instrument_id: InstrumentId | str) -> Instrument:
        """Look up by Instrument Id exactly as stored, without chasing Supersession.

        A plain `str` is parsed first, so a value that is not an Instrument Id at all raises
        `ValueError` or `UnknownScope`, while an id that is well formed but unknown raises
        `KeyError`. Malformed input and a genuine miss are different failures.
        """
        return self._by_id[InstrumentId(instrument_id)]

    def survivor(self, instrument_id: InstrumentId | str) -> Instrument:
        """Chase a Supersession chain to its fixed point."""
        seen: set[InstrumentId] = set()
        current = self.get(instrument_id)
        while current.superseded_by is not None:
            if current.id in seen:
                raise SupersessionCycle(f"supersession cycle through {sorted(seen)}")
            seen.add(current.id)
            current = self._by_id[current.superseded_by]
        return current

    def unresolved(self) -> list[Instrument]:
        """Work queue: surviving Instruments whose FIGI Resolution is not resolved."""
        return [i for i in self.instruments() if i.is_unresolved and i.superseded_by is None]

    def resolve_symbol(
        self, instrument_type: InstrumentType, symbol: str, scope: str, as_of: date
    ) -> Instrument | None:
        """Resolve a symbol supplied by a person or a document, as of a date.

        The date is required: defaulting it would silently mis-resolve historical statements.
        Overlapping Ticker History across Instruments is expected -- that is the reused-ticker
        case -- and is disambiguated by the date, so an overlap on one date is an error.
        """
        wanted = normalize_symbol(symbol)
        wanted_scope = Scope(instrument_type, scope)
        found: dict[str, Instrument] = {}
        for inst in self.instruments():
            if inst.type is not instrument_type:
                continue
            if any(
                r.symbol == wanted and r.scope == wanted_scope and r.covers(as_of)
                for r in inst.ticker_history
            ):
                alive = self.survivor(inst.id)
                found[alive.id] = alive
        if not found:
            return None
        if len(found) > 1:
            raise AmbiguousSymbol(f"{wanted} in {wanted_scope} on {as_of}: {sorted(found)}")
        return next(iter(found.values()))

    # -- Write side --

    def observe(self, obs: Observation) -> IdentityResult:
        """Turn a source Observation into the Instrument it belongs to.

        Identity is established by External Reference in a fixed order: FIGI, then contract
        address, then a provider's own stable id. Symbol and scope match only between two
        Instruments that are both still unresolved, and never merge two Instruments.
        """
        obs = replace(obs, symbol=normalize_symbol(obs.symbol))
        scope = Scope(obs.type, obs.scope)
        matched = self._match_by_reference(obs)
        if matched:
            return self._from_reference_match(obs, scope, matched)
        symbol_match = self._match_by_symbol(obs, scope)
        if isinstance(symbol_match, IdentityResult):
            return symbol_match
        if symbol_match is not None:
            symbol_match.absorb(obs)
            return IdentityResult(Outcome.MATCHED, symbol_match)
        return IdentityResult(Outcome.MINTED, self._mint(obs, scope))

    # -- Matching --

    def _match_by_reference(self, obs: Observation) -> list[Instrument]:
        """Instruments sharing an External Reference with the Observation, in mint order."""
        obs_keys = _match_keys(obs.references)
        if not obs_keys:
            return []
        best: dict[int, list[Instrument]] = {}
        for inst in self.instruments():
            shared = obs_keys & _match_keys(inst.references)
            if shared:
                best.setdefault(min(tier for tier, _, _, _ in shared), []).append(inst)
        # Higher-priority evidence wins outright, but every tier that fired is reported, so a
        # FIGI and a provider id pointing at two Instruments is seen as the Supersession it is.
        return [inst for tier in sorted(best) for inst in best[tier]]

    def _match_by_symbol(
        self, obs: Observation, scope: Scope
    ) -> Instrument | IdentityResult | None:
        """Last-resort tier. Only two unresolved parties may be joined by a symbol."""
        if obs.is_resolved:
            return None
        eligible = [
            i
            for i in self.instruments()
            if i.superseded_by is None
            and i.type is obs.type
            and i.scope == scope
            and i.symbol == obs.symbol
            and i.is_unresolved
            and not _conflict(obs.references, i.references)
        ]
        if len(eligible) > 1:
            return IdentityResult(
                Outcome.FLAGGED,
                review=f"{obs.symbol} in {scope} matches several unresolved Instruments: "
                f"{sorted(i.id for i in eligible)}",
            )
        return eligible[0] if eligible else None

    def _from_reference_match(
        self, obs: Observation, scope: Scope, matched: list[Instrument]
    ) -> IdentityResult:
        survivors: dict[str, Instrument] = {}
        for inst in matched:
            alive = self.survivor(inst.id)
            survivors[alive.id] = alive
        alive_list = sorted(survivors.values(), key=lambda i: i.mint_seq)
        target = alive_list[0]
        mismatched = [i for i in alive_list if i.type is not obs.type or i.scope != scope]
        if mismatched:
            return IdentityResult(
                Outcome.FLAGGED,
                review=f"anchor matches {sorted(i.id for i in mismatched)} across a type or "
                f"scope boundary ({obs.type}.{scope})",
            )
        if len(alive_list) > 1:
            if any(i.superseded_by is not None for i in matched):
                return IdentityResult(
                    Outcome.FLAGGED,
                    review=f"merge claim over an Instrument already superseded elsewhere: "
                    f"{sorted(i.id for i in matched if i.superseded_by is not None)}",
                )
            for other in alive_list[1:]:
                if _conflict(target.references, other.references):
                    return IdentityResult(
                        Outcome.FLAGGED,
                        review=f"{target.id} and {other.id} match on one reference but "
                        f"disagree on another",
                    )
        if _conflict(obs.references, target.references):
            return IdentityResult(
                Outcome.FLAGGED,
                review=f"observation disagrees with {target.id} on an External Reference",
            )
        # Earliest-minted survives, so the outcome does not depend on processing order.
        for other in alive_list[1:]:
            other.superseded_by = target.id
        target.absorb(obs)
        if len(alive_list) > 1:
            return IdentityResult(Outcome.SUPERSEDED, target, tuple(i.id for i in alive_list[1:]))
        return IdentityResult(Outcome.MATCHED, target)

    # -- Mutation --

    def _mint(self, obs: Observation, scope: Scope) -> Instrument:
        """Mint a new Instrument. The id is fixed here for good -- see ADR-0004."""
        instrument_id = self._mint_id(obs, scope)
        inst = Instrument(
            id=instrument_id,
            type=obs.type,
            scope=scope,
            symbol=obs.symbol,
            mint_seq=self._next_seq,
            minted_at=obs.observed_at,
            name=obs.name,
            issuer_name=obs.issuer_name,
            references=list(obs.references),
            figi_resolution=obs.figi_resolution,
            ticker_history=[TickerRecord(obs.symbol, scope, valid_from=obs.observed_at)],
        )
        self._next_seq += 1
        self._instruments.append(inst)
        self._by_id[inst.id] = inst
        return inst

    def _mint_id(self, obs: Observation, scope: Scope) -> InstrumentId:
        """Find the first Instrument Id this Observation may claim.

        Ids are never reused, so a superseded Instrument keeps its id out of circulation.
        """
        bare = InstrumentId.mint(obs.type, scope, obs.symbol)
        if bare not in self._by_id:
            return bare
        # The symbol is taken by an unrelated Instrument: qualify by issuer, else by number.
        qualifier = issuer_qualifier(obs.issuer_name)
        if qualifier:
            by_issuer = InstrumentId.mint(obs.type, scope, obs.symbol, qualifier)
            if by_issuer not in self._by_id:
                return by_issuer
        n = 2
        while (numbered := InstrumentId.mint(obs.type, scope, obs.symbol, str(n))) in self._by_id:
            n += 1
        return numbered


## Tests


def test_instrument_id_round_trips_through_its_string_form() -> None:
    """The readable slug is the stored form; parsing it back must lose nothing."""
    for text in ("stock.us.fb", "stock.us.fb_proshares", "crypto.eth.usdc", "stock.us.brk-b_2"):
        parsed = InstrumentId(text)
        assert parsed == text
        assert str(parsed) == text
        assert InstrumentId.mint(parsed.type, parsed.scope, parsed.symbol, parsed.qualifier) == text


def test_malformed_instrument_ids_are_rejected_whole() -> None:
    """A partial parse would be worse than no parse: every part is validated or nothing is."""
    for bad in (
        "AAPL",
        "stock.us",
        "stock.us.fb.extra",
        "bond.us.x",
        "stock.usa.fb",
        "stock.us._q",
    ):
        try:
            InstrumentId(bad)
            raise AssertionError(f"{bad!r} should not parse as an Instrument Id")
        except (ValueError, UnknownScope):
            pass


def test_a_qualifier_is_separated_from_the_symbol() -> None:
    """A normalised symbol never contains "_", so the first one always starts the Qualifier."""
    plain = InstrumentId("stock.us.brk-b")
    assert (plain.symbol, plain.qualifier) == ("BRK-B", None)
    qualified = InstrumentId("stock.us.brk-b_proshares")
    assert (qualified.symbol, qualified.qualifier) == ("BRK-B", "proshares")
    # The id renders its symbol lowercase, but `symbol` means what it means on an Instrument.
    assert plain.symbol == normalize_symbol("brk.b")


def test_ids_and_scopes_survive_copying() -> None:
    """A str subclass carrying attributes is uncopyable unless it says how to rebuild, and
    both types end up inside dataclasses that callers will copy and stores will serialize."""
    for original in (InstrumentId("stock.us.fb_proshares"), Scope(InstrumentType.CRYPTO, "eth")):
        for restored in (copy.deepcopy(original), pickle.loads(pickle.dumps(original))):
            assert restored == original
            assert type(restored) is type(original)
    assert copy.deepcopy(InstrumentId("crypto.eth.usdc")).scope.kind is ScopeKind.CHAIN


def test_lookup_separates_a_malformed_id_from_a_genuine_miss() -> None:
    reg = InstrumentRegistry()
    try:
        reg.get("AAPL")
        raise AssertionError("a bare symbol is not an Instrument Id")
    except ValueError:
        pass
    try:
        reg.get("stock.us.nosuch")
        raise AssertionError("a well-formed but unknown id is a miss")
    except KeyError:
        pass


def test_scope_reports_its_authority() -> None:
    assert Scope(InstrumentType.CRYPTO, "eth").kind is ScopeKind.CHAIN
    assert Scope(InstrumentType.STOCK, "US").kind is ScopeKind.COUNTRY


def test_scope_vocabulary_is_closed() -> None:
    for bad in ("ethereum", "1", "polygon"):
        try:
            Scope(InstrumentType.CRYPTO, bad)
            raise AssertionError(f"{bad} should not be a chain")
        except UnknownScope:
            pass
    for bad in ("usa", "u", "nasdaq"):
        try:
            Scope(InstrumentType.STOCK, bad)
            raise AssertionError(f"{bad} should not be a country")
        except UnknownScope:
            pass


def test_observation_requires_figi_state_and_references_to_agree() -> None:
    for refs, resolution in (
        ((ExternalReference.composite_figi("BBG000B9XRY4"),), FigiResolution.NOT_FOUND),
        ((), FigiResolution.RESOLVED),
    ):
        try:
            Observation(
                type=InstrumentType.STOCK,
                symbol="AAPL",
                scope="us",
                observed_at=date(2020, 1, 1),
                references=refs,
                figi_resolution=resolution,
            )
            raise AssertionError("mismatched FIGI Resolution should be rejected")
        except ValueError:
            pass
