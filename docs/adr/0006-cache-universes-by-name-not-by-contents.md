# Cache Universes by Name, Not by Contents

Status: accepted.

Context: The other source adapters cache their API calls with `@require_cache`, so an uncached one looks like an oversight. The OpenFIGI adapter cannot be cached the same way. lythonic turns a cached callable's parameters into its cache table's primary key, so every parameter must resolve to a `KnownType` with `simple_type=True`. Both OpenFIGI entry points take a collection of identifiers, and no collection type qualifies - neither `list[str]` nor `Universe` is a registered `KnownType`. The constraint is enforced at namespace mount, so a callable that claims a cache it cannot have fails late and only when mounted.

Decision:
- The OpenFIGI adapter is not cached at all. Neither entry point carries `@require_cache`.
- FIGI mappings are cached one layer up, keyed by a universe's **name and date** rather than by its contents: `(universe_name, as_of)`, both already `simple_type=True`. This is what ADR-0003's `UniverseSeries` provides.
- `map_tickers` takes a `Universe` rather than a `list[str]`. This buys nothing for caching, but it makes the ticker set ordered and duplicate-free, so a repeated ticker raises instead of costing a wasted job and returning a redundant result.
- A test asserts that no method in the adapter claims a cache it cannot have, since the failure would otherwise surface only at mount.

Considered Options:
- Register `Universe` as a `KnownType` with `simple_type=True`. Direct, but its string form is unbounded - an index universe runs to kilobytes as a primary key column - and `Universe` equality is order-sensitive by design, so two orderings of one set would be two cache entries. Making it work needs a canonical ordering rule that the type deliberately does not have.
- Key on a content hash of a canonicalized universe. Fixed-width and a sound primary key, but opaque: the cache table no longer says what was cached, and a hash collision or a canonicalization change is undebuggable.
- Cache nothing, ever, and re-map on every ingest. Simple, but OpenFIGI is a third-party dependency on the mandatory path of instrument creation (ADR-0004), so uncached mapping makes bulk ingest slow and rate-limit-bound.

Consequences: Instrument creation stays dependent on a live OpenFIGI call until `UniverseSeries` lands, so bulk ingest is rate-limited in the interim and offline work needs the unresolved path from ADR-0004. The cached noun becomes the named universe rather than the request, which means an ad-hoc mapping of an arbitrary ticker list is deliberately uncacheable - the intended way to map a set of instruments repeatedly is to name it first. A future adapter taking a collection will hit the same wall; the general rule is that a cache key is a name, not a payload.
