# Lythonic Value Types with cointoss Series Wrappers

Status: accepted. Supersedes ADR-0002.

Context: `lythonic` now owns general-purpose portable value types - `Universe` (an ordered, duplicate-free axis of string keys), `ExposureMatrix` (sparse subject-by-target), `SymmetricMatrix` (one universe on both axes), and `KeyedVector`. ADR-0002 described cointoss as owning `Universe` and `ExposureMatrix` itself, and named their parts `Bucket` and `weight`. Both claims are now wrong, and the two projects were using the same words for different things in the same import namespace.

Decision:
- lythonic owns the values; cointoss owns time. Each cointoss concept is a `...Series` whose value at an instant is a lythonic type: `UniverseSeries.as_of(t) -> Universe`, `ExposureSeries.as_of(t) -> ExposureMatrix`, `CovarianceSeries.as_of(t) -> SymmetricMatrix`.
- lythonic's types stay pure: no name, no `as_of`, no unit semantics pushed down into them. Dating and identity are portfolio-domain concerns, not linear-algebra ones.
- Adopt lythonic's vocabulary wholesale. `Bucket` becomes `Target`, `weight` becomes `Exposure`. The unit semantics that lythonic deliberately refuses to carry live on the wrapper as `exposure_semantics: percent | value | score`.
- `...Series` means a time-versioned system of record; `...Snapshot` means a materialized cache with inputs captured as observed and is never the system of record. `PortfolioSnapshot` was already on the second side of this line; the rule is now explicit and applies to every future name.
- `KeyedVector` gets no cointoss term. It is the transport for quantities already named - position quantities, target allocations, per-instrument scores - aligned to a `Universe`.

Considered Options:
- Keep both `Universe` definitions and rely on module qualification (`lythonic.Universe` vs `cointoss.Universe`). Cheapest, but the ambiguity resurfaces in every design discussion and every code review.
- Push `name` and `as_of` down into `lythonic.ExposureMatrix` so a matrix carries its own identity. One fewer type, but it pollutes a general-purpose value library with portfolio semantics and contradicts lythonic's own ADR-0001, which makes the matrix a pure immutable value.
- Retain `Bucket` for categorical target axes and use `Target` only for instrument-to-instrument matrices. Rejected because ADR-0002 already promises supplier and customer exposure, where the target axis holds instruments; `Bucket` is factually too narrow for what the model must express.

Consequences: `Instrument Set` and `Exposure Entry` are retired - `UniverseSeries`, `ExposureSeries`, `Target`, and `Exposure` cover everything they said. Storage grain must be described in prose rather than by a noun. An `ExposureSeries` whose subject and target universes are both instruments is expressible, and where it is also symmetric a `SymmetricMatrix` is the better fit.
