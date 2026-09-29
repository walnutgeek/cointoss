# Persistence Layer Handoff

Written 2026-09-29 at `d508fd0`, to resume work on another machine. Issue #5 is the spec;
tickets #6-#17 are its sub-issues. Eight are closed, four remain.

**State of `main`:** 237 tests, `make lint` clean, `store.py` at 99% coverage. Nothing is
half-finished and nothing is uncommitted.

## Read these first, in this order

1. `CONTEXT.md` — the vocabulary. Every term below is defined there.
2. `docs/adr/0007`, `0008`, `0009`, and the amendment lines in `0004`. These carry the
   reasoning and the rejected alternatives; the tickets only carry what to build.
3. GitHub issue #5 — the spec, including the table sketch.
4. The closing comment on each closed ticket. Every one carries an explicit low-confidence
   list naming the choices its implementer was unsure about. Those are the places a
   follow-on ticket is most likely to trip.

## What is built

| Module | Owns | Coverage |
| --- | --- | --- |
| `cointoss.instrument` | Identity, minting, supersession, source-attributed Ticker History, `Source`, `identity_source`, `price_sources` | 99% |
| `cointoss.universe` | Universe Definition, revisions, Inclusions/Exclusions, pure `evaluate` with the rank band | 100% |
| `cointoss.prices` | `Bar`, `CorporateAction`, `Restatement`, Yahoo and CoinGecko mapping, split-aware restatement detection | 95% |
| `cointoss.store` | The only module that touches SQL. Ten tables, pragmas, `SchemaVersion` | 99% |
| `cointoss.sources.coingecko` | Coin list, markets, detail, OHLC, `fetch_market_chart` | 92% |

`Store` surface today: `save_instrument(s)`, `load_instrument(s)`, `load_registry`,
`resolve_symbol`, `save_universe`, `load_universe`, `load_universe_members`,
`unresolved_members`, `save_exposure_series`, `load_exposure_series`,
`save_covariance_series`, `load_covariance_series`, `save_risk_model`, `load_risk_model`.

Tables in `TABLES` (the dict is both the schema and the ADR-0008 durable/rebuildable
classification, so the two cannot drift): `SchemaVersion`, `InstrumentRow`,
`InstrumentReferenceRow`, `TickerRecordRow`, `ExposureEntryRow`, `RiskModelRow`,
`RiskModelRevisionRow`, `CovarianceEntryRow`, `UniverseDefinitionRow`, `UniverseRevisionRow`,
`UniverseMemberRow`.

## Remaining tickets

Dependency order. #12 and #14 are both unblocked and touch different parts of `store.py`.

### #12 — Membership intervals and Evaluation Runs (unblocked)

The write path: `record_membership(definition, when, members)` reads the intervals open at
`when`, diffs against what evaluation produced, closes leavers, opens joiners, writes the
`EvaluationRun` row. Plus `members_at(definition, when)` as one indexed query.

The point nobody should lose: **an evaluation that changed nothing writes no interval rows
at all**, so the Run is the only evidence the scheduler is alive. Both tables are needed and
neither substitutes for the other.

Needs two new tables, `UniverseMembershipRow` (durable) and `EvaluationRunRow`
(rebuildable), plus an index — the membership lookup starts from `(definition, date)`, which
no existing alternative key serves.

### #13 — Universe reads and the append-equivalence guard (blocked by #12)

`universes_containing(instrument_id, start, end)` — the cross-cutting query that decided the
whole storage shape in ADR-0008 — and `load_universe_series(name)` returning the value
object for callers typed on it.

**This is the riskiest ticket in the set and the easiest to skip.** It holds the property
test: replay one sequence of dated memberships through `store.record_membership` and through
`UniverseSeries.append`, assert the results are equal. The store's diff-and-close logic and
the value type's fold are two implementations of one rule and nothing structural forces them
to agree. #12 will look finished without this; it will not be.

### #14 — Pinning a typed ticker (unblocked)

Source plus symbol goes in, an `Observation` is built with the mandatory FIGI attempt, the
registry resolves or mints, the result is persisted, the member is pinned. Failure stores the
member unresolved and does not reject the edit.

`pin_member` was deliberately **removed** from #11 and belongs entirely here — it was the
tail of the resolution flow wearing a storage name, and two tickets claiming to own "how a
member becomes pinned" was the ambiguity worth killing. `save_universe` already writes a
member with or without a pin, which is all this ticket needs.

Tests must not make live network calls, so the OpenFIGI client needs a seam. Check how
`tests/test_openfigi.py` does it before inventing one.

### #16 — Bars accumulate (blocked by #12)

`upsert_bars` idempotent on `(instrument, source, bar_date)`, corporate actions stored,
`detect_restatements` called before overwriting so the pure logic stays testable, `bars_for`
resolving per date through the instrument's `price_sources`, and the bars for every member of
a universe on a date (which is why it waits on #12).

Two things to handle rather than discover:

- **The partial-day bar.** CoinGecko's `market_chart` emits a trailing "now" point sharing a
  date with that day's 00:00 point, so the last bar is partial. Re-fetching changes its
  close, and `detect_restatements` — which only excuses split rescales — will file it as a
  Restatement. That is a false positive generated by normal operation. Suppression belongs in
  the write path, not in the pure mapping.
- **`source` is still a plain `str` in `cointoss.prices`.** #6 landed a typed `Source` enum;
  #8 was written concurrently and did not use it. #11 harmonised `cointoss.universe` and
  immediately caught a test constructing a member with `source="cg"`, which is not a value
  the vocabulary has. Do the same here.

## Things that will bite

- **`lythonic.state.select` and `None`.** Fixed upstream in 0.0.25 (walnutgeek/lythonic#11,
  reported from here). A `None` filter now correctly means `IS NULL`; before, it rendered
  `field = NULL` and silently matched nothing. `pyproject.toml` requires `>=0.0.25`. If you
  ever see an empty result from a nullable-column filter, check the installed version first.
- **The filter operator is a prefix**, not a suffix: `ne__instrument=None`, not
  `instrument__ne=None`. 0.0.25 raises a message naming the correct form.
- **`(AK)` markers go at the start of the field description**, stripped in the order `(PK)`,
  `(FK:...)`, `(AK)`, so a field that is both writes
  `description="(FK:Instrument.instrument_row_id)(AK) ..."`. A field cannot be both `(AK)`
  and nullable.
- **`self.conn.commit()` at the end of every write method.** sqlite3 opens an implicit
  transaction on DML. Nothing reminds you.
- **Do not bump `SCHEMA_VERSION` or add to `MIGRATIONS`.** There is no released schema;
  adding tables before first release is still version 1.
- **`make test` and `uv run pytest tests/` are not the same.** The Makefile adds doctests —
  raw pytest reports ~40 fewer tests and misleading coverage. Always verify with `make test`.
- **The registry is durable, not rebuildable**, despite being derived from source data.
  Instrument Ids are minted from the first symbol seen in `mint_seq` order; re-ingesting in a
  different order mints different ids, and every membership row and matrix axis references
  them.

## Open questions I would not decide alone

- **`save_universe` silently discards** inclusion/exclusion sets on a passed-in
  `UniverseParameters` when no matching member rows come with it. Member rows are the system
  of record and the sets are projected back on load, which is what ADR-0007 specifies — but
  the silence is wrong. It should probably raise. Flagged twice, deliberately not changed.
- **#10 and #17 have no implementer's self-report.** Their agents were killed by the host
  sleeping; the work was recovered and reviewed, but the low-confidence notes on those two
  tickets are reconstructed from reading the diff, not written by whoever made the choices.
  Weaker evidence than the other six.
- **Restatement tolerance is `1e-6`, relative.** If real re-fetches start filing spurious
  restatements on a split, `1e-4` is the fix. The split-date boundary uses
  `action_date > bar_date` strictly, verified against Yahoo's NVDA 2024-06-10 frame; that is
  the assumption most likely to be wrong for another source.
- **`adj_close` is not exempt from restatement detection.** A re-fetch recomputing it across
  a new dividend will be reported. That may be correct or may become noise.

## After #5 closes

`docs/agents/road-map.md` has the build order. Steps 3 (ingest), 4 (the running instance),
6 (returns) and 7 (the estimator) come next, and its "Still open" section lists what must be
decided before step 3 — chiefly **delisting versus absence**, which nothing currently
distinguishes.
