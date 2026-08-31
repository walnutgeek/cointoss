# Covariance is Declared, Not Recomputed

Status: accepted.

Context: Research and portfolio work need covariance matrices over an instrument universe. A covariance matrix is derivable from a universe, price history, a lookback, a return frequency, and an estimator - all of which are already stored. But adjusted price history is rewritten by corporate actions, so recomputing the same date twice, months apart, legitimately yields different numbers. Reproducing a past research result exactly requires the matrix as it was, not as it would be recomputed today.

Decision:
- `CovarianceSeries` is a system of record, not a cache. A covariance matrix is declared for a date and never recomputed, whatever later happens to the price history beneath it. This is how vendor risk models behave: the published matrix for a date is the matrix for that date, permanently.
- `RiskModel` is a named, editable recipe (universe reference, lookback, return frequency, estimator). It records how a covariance came to be; it does not define what the covariance is.
- `RiskModel` is mutable rather than versioned by minting new identities. Editing it leaves existing `CovarianceSeries` entries alone and changes only what is produced from then on.
- Because the recipe mutates underneath the entries, every `RiskModel` edit bumps a monotonic revision counter and appends to a `RiskModelRevision` log, and every `CovarianceSeries` entry stamps the revision it was produced under. Without the stamp, an old entry is an uninterpretable number.
- A vendor-supplied matrix is a `RiskModel` whose estimator is external, with a source reference and no computable recipe. No special case.

Considered Options:
- Recipe only, covariance always recomputed on demand, results cached and labelled as caches. Avoids duplicating derivable data, but loses exact reproducibility precisely because the inputs are mutable - which is the whole reason the artifact is wanted.
- `RiskModelSnapshot` keyed by model, `as_of`, and `computed_at`, i.e. bitemporal, with every recomputation vintage retained. Strictly more correct and strictly more machinery; rejected as premature.
- Immutable, versioned `RiskModel` where changing a parameter mints a new identity and corrections are backfills under the new one. Cleaner reproducibility, but it accumulates near-duplicate models and imposes a versioning lifecycle before there is running code to justify it.
- No revision stamp at all. Cheapest, and makes old entries permanently unattributable to parameters.

Consequences: Deferred, deliberately, is any decision about how to correct a wrong covariance. Under this ADR there is no correction mechanism - a declared entry stands. Revisit once there is running code and a real instance of the problem. Note that this puts a derived quantity on the system-of-record side of the `Series`/`Snapshot` rule from ADR-0003; that is intentional, and the reason is reproducibility rather than provenance.
