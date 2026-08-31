# Exposure Matrix and Universe as Time-Versioned Series

Status: superseded by ADR-0003. Retained as the record of what was decided before lythonic owned the value types.

Context: Need to classify instruments across dimensions (GICS sectors, crypto narratives, geography, supplier/customer exposure) and scope data gathering. Both classifications and universes change over time, and weight semantics vary by matrix.

Decision:
- `Universe` is a time series of instrument lists: `{as_of, instruments[], diff}`. Construction logic may be rule-based (e.g. market_cap > $500M, SP500 constituents) running periodically, or manual curation. Tracks evolution explicitly.
- `ExposureMatrix` is matrix-centric, not instrument-centric. Metadata defines `weight_semantics: percent|value|score` and whether weights sum to 100%. Entries are time-versioned: `{matrix_id, bucket_id, instrument_id, as_of, weight}`. Time granularity per matrix (daily, quarterly, versioned).
- Instrument only advertises which exposure matrices have series available, it does not own them. Lazy loading of matrices via `instrument.available_exposures()`.
- Portfolio exposure (valued allocation) is computed at query time: sum(positions * market_price * exposure_weight), not stored in position.

Considered Options:
- Tags on Instrument (simple but cannot capture time evolution, weight semantics, or dollar-valued supplier exposure; stale copies).
- Instrument-owned exposure list (convenient for "what is AAPL exposed to?" but creates coupling and duplication when matrix changes).
- Forcing weights to always sum to 100% (fails for thematic tagging and supplier value exposure).

Consequences: Enables queries like "what instruments in DeFi bucket as of Q2?" and "AAPL tech exposure over time" without mutating Instrument. Requires join table storage and time-aware queries. Universes bound data gathering and can feed what-if Portfolio creation. Future work: exposure matrices may themselves depend on universes (e.g. supplier matrix limited to SP500 universe).
