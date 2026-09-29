# Rows, Not Blobs: Series Value Types Become Projections of the Store

Status: accepted.

Context: Every module built so far is storage-agnostic by design, which was right for getting the rules correct and means nothing is written down. `lythonic.state` supplies the persistence answer - a Pydantic-to-SQLite ORM with `DbModel`, `Schema`, `DbFile`, and alternative keys - so no database choice is needed. What is needed is a shape. All three Series types are immutable values whose `append` returns a new whole Series, so loading, appending and rewriting a ten-year daily Series to add one entry is absurd; either storage grows a row-level append that bypasses the value type, or the value type stops being the storage unit.

Decision:
- The value type stops being the storage unit. `UniverseSeries` becomes a projection of the store, not its contents. Reads are point queries first: membership at a date is one indexed query and builds no Series object. Loading a whole Series exists for callers typed on it and is understood to be the expensive path.
- Writes never go through `append`. The store reads the currently open intervals, diffs, closes leavers, opens joiners, and writes the run-log row. `append` stays exactly as it is - pure, immutable, used in memory and in tests - but it is not how data reaches the database. Ticker History is treated the same way: resolution is a point query, and `Instrument.ticker_history` is populated on load.
- Decomposition follows the read pattern, so the answer differs per Series type. Universe membership is stored as intervals, `(definition, instrument, valid_from, valid_to)`, which is what a `DatedUniverse` already means and which matches `TickerRecord`'s existing idiom. `ExposureSeries` and `CovarianceSeries` are stored one row per dated entry with the matrix as a JSON payload. Decomposing an N-by-N matrix into N-squared-over-two rows per date to support a cell-wise query nobody asks is how a small database becomes a large one.
- Membership intervals record change, so an evaluation that changed nothing leaves no trace. Liveness lives in `EvaluationRun` instead. Neither table pretends to be the other.
- `UniverseMember` is versioned by revision rather than by date - `added_in_revision` and `removed_in_revision` - the same interval trick one axis over, so a revision need not copy the whole override set.
- One `cointoss.db` for domain state and price history together, alongside lythonic's `cache.db`, `dags.db` and `triggers.db`. Its path comes from cointoss's own config, since `EngineConfig` has three fixed store paths and no extension point.
- cointoss opens its own connections with `PRAGMA foreign_keys = ON` and `journal_mode = WAL`. `lythonic.state` issues no pragmas, so the `REFERENCES` clauses its DDL generates are documentation rather than constraints, and the default rollback journal gives none of the reader concurrency the layout assumes.
- Natural keys are alternative keys over surrogate integer primary keys, which is the shape `lythonic.state` auto-increment and `AltKey` support.
- `DbFile.check()` carries a `TODO` where migration belongs. cointoss carries its own `SchemaVersion` row and an ordered list of migration steps rather than waiting for lythonic.
- Tables are classified durable or rebuildable, and only durable tables need migrations written. Bars, corporate actions, restatements and run logs are re-fetchable: on an awkward change, drop and backfill. Universe definitions and revisions, declared covariances, risk models and trades are hand-authored and have no source to re-fetch from.
- The instrument registry is durable, despite being entirely derived from source data. Ids are minted from the first symbol seen in `mint_seq` order, so re-ingesting the same history in a different order mints different ids - and every membership row, matrix axis and trade references them.

The shape this produces, with natural keys as alternative keys over surrogate integer primary keys:

```
durable
  Instrument              instrument_id AK | type | scope | symbol | mint_seq | minted_at
                          name | issuer_name | figi_resolution | superseded_by
                          identity_source | price_sources
  InstrumentReference     instrument FK | kind | value
  TickerRecord            instrument FK | source | symbol | scope | valid_from | valid_to
  UniverseDefinition      name AK
  UniverseRevision        definition FK | revision | changed_at | changed | enter_rank | exit_rank
  UniverseMember          definition FK | role | source | symbol_as_typed | instrument FK NULL
                          added_in_revision | removed_in_revision NULL
  UniverseMembership      definition FK | instrument FK | valid_from | valid_to
rebuildable
  EvaluationRun           definition FK | revision | run_at | source_asof | outcome
                          n_admitted | n_dropped | n_unresolved
  Bar                     instrument FK | source | bar_date | open | high | low | close
                          adj_close | volume | fetched_at
  CorporateAction         instrument FK | source | action_date | kind | value
  Restatement             instrument FK | source | bar_date | field | old | new | detected_at
meta
  SchemaVersion           version | applied_at
```

External References are decomposed into rows rather than kept as a payload on `Instrument`, because
resolution by reference is the identity path and queries them directly.

Considered Options:
- One row per Series with a JSON payload. Trivial to build, unqueryable, and rewrites the whole history on every append. "Which universes contained AAPL in 2024" needs SQL over decomposed rows.
- One row per dated entry with the universe as JSON. Append becomes one insert, and the cross-cutting query still means scanning JSON.
- One row per membership snapshot per date. Fully queryable, and it rewrites the entire membership on every run whether or not anything changed: a hundred-name universe evaluated daily is hundreds of thousands of rows a year to record a few dozen real events.
- Separate `domain.db` and `prices.db`. Bars dominate by orders of magnitude and are re-fetchable, and SQLite is single-writer per file, so a long backfill blocks universe edits. Rejected for now because WAL gives one writer plus concurrent readers, which is the actual access pattern, while splitting permanently costs the bar-to-instrument join, which is the join written most often. Split later if a backfill actually blocks; moving a table between SQLite files is easy.

Consequences: The store's diff-and-close logic and the value type's fold over appends are two implementations of one semantics, and nothing structural forces them to agree. A property test that replays the same sequence through both and asserts equality is the guard. Anything reading membership must read intervals rather than assume a snapshot exists for every date. Prices are `float`: `lythonic.state` registers no `Decimal` and every vendor supplies floats anyway. That is fine for returns and means this schema is not a ledger; if trade accounting ever needs exact arithmetic, that is a decision about `Trade`, not about `Bar`.
