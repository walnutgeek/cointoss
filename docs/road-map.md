# Road Map

Where cointoss is, where it is going, and what has to be decided before it gets there. Updated
as directions change. Decisions that survive belong in `docs/adr/`; vocabulary belongs in
`CONTEXT.md`. This file holds the parts that are still moving.

Last updated: 2026-09-29, at `0d15c29`.

## Where we are

Nine ADRs. 347 tests, lint clean. The persistence layer (issue #5, ADR-0007, 0008 and 0009) is
built: identity, Universe Definitions, membership intervals, Evaluation Runs, matrix entries and
price history all persist, and every read folds supersession.

### Built

| Module | What it owns |
| --- | --- |
| `cointoss.instrument` | Instrument identity. Immutable `{type}.{scope}.{symbol}` ids, resolution by External Reference, supersession, Ticker History. ADR-0004. |
| `cointoss.series` | `UniverseSeries` and `ExposureSeries` over lythonic value types. Step lookup, whole values per date. ADR-0003. |
| `cointoss.risk` | `RiskModel` with a revision log, `CovarianceSeries` declared and never recomputed. Exact-date lookup. ADR-0005. |
| `cointoss.sources.openfigi` | OpenFIGI v3 batch mapping. Uncached, deliberately. ADR-0006. |
| `cointoss.sources.yahoofinance` | `get_info`, `get_prices`, `lookup`. Cached. |
| `cointoss.sources.coingecko` | Coin list, markets, detail, OHLC, market chart. Cached. |
| `cointoss.universe` | `UniverseDefinition` with a revision log, Inclusions and Exclusions, pure rank-band evaluation. ADR-0007. |
| `cointoss.prices` | `Bar`, `CorporateAction`, `Restatement`, source mapping, split-aware restatement detection. ADR-0009. |
| `cointoss.store` | The only module that touches SQL. Sixteen tables, enforced pragmas, `SchemaVersion`. Membership by date or Instrument, bars resolved through Price Sources. ADR-0008. |
| `cointoss.ingest` | Pinning a typed ticker: the mandatory FIGI attempt, resolve or mint, retrying an unresolved Instrument. ADR-0004. |

### Not built

Everything that makes it a system rather than a library:

- **No ingest sweep.** `cointoss.ingest` covers pinning a typed ticker and retrying one
  unresolved Instrument. Nothing yet turns a source listing into Observations on a schedule, and
  nothing runs the retry queue.
- **No returns.** Nothing turns prices into returns, so no covariance can be computed; the only
  way one enters the system today is a vendor import.
- **No universe accumulation on a schedule.** `record_membership` stores an evaluation, and
  nothing calls it unattended.
- **No portfolio.** ADR-0001's `Portfolio`, `Trade`, `Position` and `PortfolioSnapshot` are
  designed and unimplemented.
- **No running instance.** `cointoss.cli:main` prints `TBD`. There is no `lyth.yaml`, no
  scheduled work, and woodglue is a declared dependency that nothing imports.

## Direction

Stand up a running cointoss instance that accumulates market data on a schedule, maps it to
stable Instrument identity, maintains universes per source, derives returns, and thereby has
everything in place for portfolio tracking and risk estimation.

The shape of it:

```
Yahoo Finance ─┐                    ┌─ UniverseSeries "yahoo-listed"
               ├─ ingest ─ Instrument ─┤
CoinGecko ─────┘   (FIGI-anchored)  └─ UniverseSeries "coingecko-listed"
                        │
                        ├─ price history ─ returns ─ CovarianceSeries
                        └─ Portfolio / Trade ─ Position ─ valuation
```

Nothing in that diagram is speculative about the parts already built — identity, universes and
declared covariance exist and are tested. Storage is built too. What is missing is the plumbing that feeds
it.

## What a running instance needs

lythonic already supplies most of the machinery, which is worth knowing before designing any of
it:

- **`lythonic.state`** is a SQLite ORM: `DbModel`, `DbConfig`, `DbFile`, `Schema`, alternative
  keys. This is the persistence answer; there is no need to choose a database.
- **`lythonic.compose.engine`** loads an `EngineConfig` from `lyth.yaml` and resolves three
  stores: `cache.db`, `dags.db`, `triggers.db`.
- **`lythonic.compose.namespace`** registers fragments and nodes, and applies cache config.
  Both source adapters are already `NamespaceFragment`s with `@nsnode(tags=["api"])`.
- **`lythonic.compose.trigger`** runs cron-scheduled polls against registered DAGs.
- **`lythonic.compose.dag_runner`** executes DAGs with provenance.
- **woodglue** serves it: apps, service, mount, CLI, UI.

So "run an instance" is mostly configuration and wiring, plus the genuinely new pieces: the
ingest sweep and returns.

## Proposed build order

Each step is independently useful and leaves the system working.

1. **Persistence for what exists.** Done (#5). `cointoss.store` to ADR-0008, with a seeded
   property test keeping the store's diff-and-close honest against `UniverseSeries.append`.
2. **Universe Definitions.** Done. ADR-0007's recipe, revision log, Inclusions and Exclusions, the
   pure evaluator, and pinning a typed ticker.
3. **Ingest.** One path from a source row to an `Observation` to an `InstrumentRegistry`, in
   `cointoss.ingest` beside pinning, enforcing the mandatory-FIGI-attempt invariant and driving
   the unresolved retry queue through `retry_resolution`.
4. **The instance.** `lyth.yaml`, a real CLI, cron triggers on the ingest and evaluation DAGs,
   woodglue serving. First point at which data accumulates unattended.
5. **Price storage.** Done (#16). ADR-0009's Bars, Corporate Actions and Restatements, with
   provisional bars excused from restatement. Ingest feeds it.
6. **Returns.** Whatever representation the estimator needs.
7. **The estimator.** Closes ADR-0005's gap so a covariance can be computed rather than only
   imported.
8. **Portfolio.** ADR-0001's `Trade` as system of record, `Position`, `PortfolioSnapshot`.
9. **The FIGI mapping cache.** ADR-0006's `(universe_name, as_of)` key. Unblocked since #3; until it
   lands, instrument creation hits a live OpenFIGI call on every ingest.

## Settled in grilling

Storage, universes and price history were grilled on 2026-09-27 and closed. The reasoning lives in
the ADRs; what follows is only the index, so nothing here is re-litigated from memory.

- **Blob or table, and where the append boundary sits** - ADR-0008. Different answers per Series
  type, and the value type stops being the storage unit.
- **One database or several** - ADR-0008. One `cointoss.db`; split only if a backfill actually
  blocks.
- **Migrations** - ADR-0008. cointoss's own `SchemaVersion`, plus a durable/rebuildable split so
  only hand-authored tables ever need one.
- **Is a source universe a `UniverseSeries`?** - ADR-0007. The word covered two concepts. A
  Universe Definition is the recipe; a Universe Series is what it produced.
- **Adjusted or unadjusted** - ADR-0009. The question was malformed: Yahoo's "unadjusted" OHLC is
  already split-adjusted and raw is unobtainable. Splits leave returns invariant, so the worry was
  misplaced; dividends do not, so `close` and `adj_close` are both stored.
- **What is a bar, and vendor disagreement** - ADR-0009. Session date in the venue's own calendar,
  keyed by source so disagreement is recorded rather than resolved.

## Still open

- **Delisting versus absence.** Absence from a source is not the same fact as a delisting, and
  nothing yet distinguishes them. ADR-0007 drops an instrument from membership either way. This
  needs settling before step 3.
- **CoinGecko and Yahoo crypto bars are about a day apart.** CoinGecko's 00:00 UTC point for day
  D is roughly D-1's close; Yahoo's bar closes at the end of D. `bars_for` falling back between
  them splices series offset by a day. ADR-0009's "agree on what a crypto day is" holds for the
  label only. Settle before ingest mixes the two.
- **A fixed `days` for `market_chart`.** The bar for a date depends on `days` (hourly versus
  daily points), so overlapping fetches with different `days` file a Restatement on every
  overlapping date. Ingest must pick one.
- **Backfills.** `record_membership` refuses a `when` earlier than the latest Evaluation Run, so
  evaluating a past date after a later one is not supported (ADR-0008 amendment).
- **Unfolded reads.** `corporate_actions_for`, `restatements_for` and the action lookup in
  `upsert_bars` do not fold supersession; `bars_for` consults only the Survivor's Price Sources.
- **A review queue for flagged identity.** Registry flags, ambiguous FIGI answers and refused
  anchors are only logged.
- **`save_universe` silently discards** Inclusion and Exclusion sets that arrive without member
  rows. Probably should raise.
- **Restatement noise.** Tolerance is `1e-6` relative, and `adj_close` changes from a new dividend
  are reported. Either may need loosening once real re-fetches run.
- **ADR-0008's schema block** omits `ExposureEntry`, `CovarianceEntry`, `RiskModel` and
  `RiskModelRevision`.
- **Full sweep versus incremental.** What triggers each, and at what cadence the unresolved retry
  queue runs given OpenFIGI's coverage lag.
- **Returns.** Representation (`FrameData`, a `KeyedVector` per date, a new Series type, or
  something the estimator owns privately); stored or derived; simple or log; and what happens
  across a gap, a halt, or a chain split.
- **Lot-based tax accounting.** ADR-0001 defers it with "lot as synthetic Instrument" as the
  sketch - worth testing against ADR-0004's grammar now that identity exists, since a synthetic
  instrument would need an Instrument Id and a Scope.
- **Exact arithmetic.** ADR-0008 stores prices as `float` because `lythonic.state` registers no
  `Decimal`. Fine for returns; revisit if `Trade` accounting ever needs a ledger.

## Parked

Recorded so they are not rediscovered as new:

- **`Issuer` as an entity, keyed by LEI.** ADR-0004 defers it; grouping GOOG and GOOGL is an
  `ExposureSeries` today. Revisit when fundamentals are ingested, since an `ExposureSeries` has
  nowhere to hang issuer attributes.
- **Correcting a declared covariance.** ADR-0005 leaves a wrong covariance permanent, and
  `CovarianceSeries.correct()` raises to make that surface. Revisit with a real instance of the
  problem.
- **`RiskModel` versioning by minting new identities.** Rejected for now in favour of the
  revision counter.
- **Bridged tokens and share classes** are several Instruments each, grouped by an
  `ExposureSeries` rather than collapsed.
- **`InstrumentId` at matrix axes.** `lythonic.Universe` is keyed by `str`, so an `InstrumentId`
  degrades to its string form exactly where the type safety would matter most. Fixing it
  properly means asking whether `Universe` should be generic over its key type — a lythonic
  question, not a cointoss one.
- **Rule-based universe construction beyond market-cap rank.** Screens on fundamentals, index
  constituents. ADR-0007's recipe has room for one rule and deliberately offers only the rank band.
- **Review queue for universe changes.** Rule proposes, human approves. ADR-0007 notes it is
  strictly buildable on top of what was decided, since `UniverseSeries.change()` already yields the
  diff such a queue would show.
- **Bitemporal price history.** ADR-0009 keeps one vintage plus `fetched_at` and a Restatement log.
  Revisit if a restatement ever turns out to matter.
- **Exchange calendars.** ADR-0009 stores no session time, so bars on one date are not
  contemporaneous across venues. A lead-lag correction needs a calendar table, not a bar column.
