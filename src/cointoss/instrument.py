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

import re
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

__all__ = [
    "AmbiguousSymbol",
    "Chain",
    "ExternalReference",
    "FigiResolution",
    "IdentityResult",
    "Instrument",
    "InstrumentError",
    "InstrumentRegistry",
    "InstrumentType",
    "Observation",
    "Outcome",
    "ReferenceKind",
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

    def __post_init__(self) -> None:
        has_anchor = any(r.is_anchor_figi for r in self.references)
        resolved = self.figi_resolution is FigiResolution.RESOLVED
        if has_anchor != resolved:
            raise ValueError(
                "figi_resolution RESOLVED and an anchor FIGI reference must accompany each other"
            )
        if not normalize_symbol(self.symbol):
            raise ValueError("observation symbol is empty after normalisation")


@dataclass
class Instrument:
    """The canonical, stable identity for a tradable asset."""

    id: str
    type: InstrumentType
    scope: str
    symbol: str
    mint_seq: int
    minted_at: date
    name: str | None = None
    issuer_name: str | None = None
    references: list[ExternalReference] = field(default_factory=list)
    figi_resolution: FigiResolution = FigiResolution.NOT_ATTEMPTED
    superseded_by: str | None = None
    ticker_history: list[TickerRecord] = field(default_factory=list)

    @property
    def is_unresolved(self) -> bool:
        return self.figi_resolution is not FigiResolution.RESOLVED


@dataclass(frozen=True)
class IdentityResult:
    """What an Observation meant. `instrument` is None exactly when the outcome is FLAGGED."""

    outcome: Outcome
    instrument: Instrument | None = None
    superseded: tuple[str, ...] = ()
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


def validate_scope(instrument_type: InstrumentType, scope: str) -> str:
    """Check a Scope against its authority and return it normalised.

    >>> validate_scope(InstrumentType.STOCK, "US")
    'us'
    >>> validate_scope(InstrumentType.CRYPTO, "eth")
    'eth'
    """
    value = scope.strip().lower()
    if instrument_type is InstrumentType.CRYPTO:
        if value not in set(Chain):
            raise UnknownScope(f"{value!r} is not in the chain vocabulary: {sorted(set(Chain))}")
        return value
    if not re.fullmatch(r"[a-z]{2}", value):
        raise UnknownScope(f"{value!r} is not an ISO 3166-1 alpha-2 country")
    return value


class InstrumentRegistry:
    """The identity seam over an in-memory collection of Instruments.

    Write side: `observe`. Read side: `resolve_symbol`, `survivor`, `unresolved`. Storage is out
    of scope; the collection is passed in and handed back, and no ordering beyond mint order --
    which the registry tracks itself -- is assumed.
    """

    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self._instruments: list[Instrument] = instruments if instruments is not None else []
        self._by_id: dict[str, Instrument] = {i.id: i for i in self._instruments}
        self._next_seq: int = max((i.mint_seq for i in self._instruments), default=0) + 1

    # -- Read side --

    def instruments(self) -> list[Instrument]:
        """Every Instrument, superseded ones included, in mint order."""
        return sorted(self._instruments, key=lambda i: i.mint_seq)

    def get(self, instrument_id: str) -> Instrument:
        """Look up by Instrument Id exactly as stored, without chasing Supersession."""
        return self._by_id[instrument_id]

    def survivor(self, instrument_id: str) -> Instrument:
        """Chase a Supersession chain to its fixed point."""
        seen: set[str] = set()
        current = self._by_id[instrument_id]
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
        wanted_scope = validate_scope(instrument_type, scope)
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
        scope = validate_scope(obs.type, obs.scope)
        matched = self._match_by_reference(obs)
        if matched:
            return self._from_reference_match(obs, scope, matched)
        symbol_match = self._match_by_symbol(obs, scope)
        if isinstance(symbol_match, IdentityResult):
            return symbol_match
        if symbol_match is not None:
            self._apply(symbol_match, obs)
            return IdentityResult(Outcome.MATCHED, symbol_match)
        return IdentityResult(Outcome.MINTED, self._mint(obs, scope))

    # -- Matching --

    def _keys(
        self, refs: list[ExternalReference] | tuple[ExternalReference, ...]
    ) -> set[tuple[int, str, str, str]]:
        keys: set[tuple[int, str, str, str]] = set()
        for ref in refs:
            group = ref.group()
            if group is not None:
                keys.add((*group, ref.value))
        return keys

    def _match_by_reference(self, obs: Observation) -> list[Instrument]:
        """Instruments sharing an External Reference with the Observation, in mint order."""
        obs_keys = self._keys(obs.references)
        if not obs_keys:
            return []
        best: dict[int, list[Instrument]] = {}
        for inst in self.instruments():
            shared = obs_keys & self._keys(inst.references)
            if shared:
                best.setdefault(min(tier for tier, _, _, _ in shared), []).append(inst)
        # Higher-priority evidence wins outright, but every tier that fired is reported, so a
        # FIGI and a provider id pointing at two Instruments is seen as the Supersession it is.
        return [inst for tier in sorted(best) for inst in best[tier]]

    def _match_by_symbol(self, obs: Observation, scope: str) -> Instrument | IdentityResult | None:
        """Last-resort tier. Only two unresolved parties may be joined by a symbol."""
        if obs.figi_resolution is FigiResolution.RESOLVED:
            return None
        eligible = [
            i
            for i in self.instruments()
            if i.superseded_by is None
            and i.type is obs.type
            and i.scope == scope
            and i.symbol == obs.symbol
            and i.is_unresolved
            and not self._conflict(obs.references, i.references)
        ]
        if len(eligible) > 1:
            return IdentityResult(
                Outcome.FLAGGED,
                review=f"{obs.symbol} in {scope} matches several unresolved Instruments: "
                f"{sorted(i.id for i in eligible)}",
            )
        return eligible[0] if eligible else None

    def _conflict(
        self,
        left: list[ExternalReference] | tuple[ExternalReference, ...],
        right: list[ExternalReference] | tuple[ExternalReference, ...],
    ) -> bool:
        """Whether the two reference sets disagree within a comparison group."""
        by_group: dict[tuple[int, str, str], set[str]] = {}
        for ref in left:
            group = ref.group()
            if group is not None:
                by_group.setdefault(group, set()).add(ref.value)
        for ref in right:
            group = ref.group()
            if group is not None and group in by_group and ref.value not in by_group[group]:
                return True
        return False

    def _from_reference_match(
        self, obs: Observation, scope: str, matched: list[Instrument]
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
                if self._conflict(target.references, other.references):
                    return IdentityResult(
                        Outcome.FLAGGED,
                        review=f"{target.id} and {other.id} match on one reference but "
                        f"disagree on another",
                    )
        if self._conflict(obs.references, target.references):
            return IdentityResult(
                Outcome.FLAGGED,
                review=f"observation disagrees with {target.id} on an External Reference",
            )
        # Earliest-minted survives, so the outcome does not depend on processing order.
        for other in alive_list[1:]:
            other.superseded_by = target.id
        self._apply(target, obs)
        if len(alive_list) > 1:
            return IdentityResult(Outcome.SUPERSEDED, target, tuple(i.id for i in alive_list[1:]))
        return IdentityResult(Outcome.MATCHED, target)

    # -- Mutation --

    def _apply(self, inst: Instrument, obs: Observation) -> None:
        """Fold an Observation into an Instrument it has been identified with."""
        known = set(inst.references)
        inst.references.extend(r for r in obs.references if r not in known)
        if obs.figi_resolution is FigiResolution.RESOLVED:
            inst.figi_resolution = FigiResolution.RESOLVED
        elif inst.figi_resolution is FigiResolution.NOT_ATTEMPTED:
            inst.figi_resolution = obs.figi_resolution
        inst.name = inst.name or obs.name
        inst.issuer_name = inst.issuer_name or obs.issuer_name
        self._record_symbol(inst, obs)

    def _record_symbol(self, inst: Instrument, obs: Observation) -> None:
        """Append Ticker History when a successor symbol is observed.

        Only forward renames are recorded; a symbol observed before the current record began is
        an out-of-order sighting and is ignored rather than rewriting history.
        """
        if obs.symbol == inst.symbol:
            return
        current = inst.ticker_history[-1]
        if obs.observed_at < current.valid_from:
            return
        inst.ticker_history[-1] = replace(current, valid_to=obs.observed_at)
        inst.ticker_history.append(TickerRecord(obs.symbol, inst.scope, valid_from=obs.observed_at))
        inst.symbol = obs.symbol

    def _mint(self, obs: Observation, scope: str) -> Instrument:
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

    def _mint_id(self, obs: Observation, scope: str) -> str:
        base = f"{obs.type}.{scope}.{obs.symbol.lower()}"
        if base not in self._by_id:
            return base
        # The symbol is taken by an unrelated Instrument: qualify by issuer, else by number.
        # Ids are never reused, so a superseded Instrument keeps its id out of circulation.
        qualifier = issuer_qualifier(obs.issuer_name)
        if qualifier and f"{base}_{qualifier}" not in self._by_id:
            return f"{base}_{qualifier}"
        n = 2
        while f"{base}_{n}" in self._by_id:
            n += 1
        return f"{base}_{n}"


## Tests


def test_scope_vocabulary_is_closed() -> None:
    for bad in ("ethereum", "1", "polygon"):
        try:
            validate_scope(InstrumentType.CRYPTO, bad)
            raise AssertionError(f"{bad} should not be a chain")
        except UnknownScope:
            pass
    for bad in ("usa", "u", "nasdaq"):
        try:
            validate_scope(InstrumentType.STOCK, bad)
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
