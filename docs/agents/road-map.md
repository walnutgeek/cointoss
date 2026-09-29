# Road Map

Where cointoss is, where it is going, and what has to be decided before it gets there. Updated
as directions change. Decisions that survive belong in `docs/adr/`; vocabulary belongs in
`CONTEXT.md`. This file holds the parts that are still moving.

Last updated: 2026-09-27, at `2e72c9d`.

## Where we are

Four ADRs designed and implemented in one pass. 112 tests, lint clean, no open issues. Three further
ADRs - 0007, 0008, 0009 - are designed and unimplemented: they settle persistence and are the
subject of the next build step.

### Built

| Module | What it owns |
| --- | --- |
| `cointoss.instrument` | Instrument identity. Immutable `{type}.{scope}.{symbol}` ids, resolution by External Reference, supersession, Ticker History. ADR-0004. |
| `cointoss.series` | `UniverseSeries` and `ExposureSeries` over lythonic value types. Step lookup, whole values per date. ADR-0003. |
| `cointoss.risk` | `RiskModel` with a revision log, `CovarianceSeries` declared and never recomputed. Exact-date lookup. ADR-0005. |
| `cointoss.sources.openfigi` | OpenFIGI v3 batch mapping. Uncached, deliberately. ADR-0006. |
| `cointoss.sources.yahoofinance` | `get_info`, `get_prices`, `lookup`. Cached. |
| `cointoss.sources.coingecko` | Coin list, markets, detail, OHLC. Cached. |

### Not built

Everything that makes it a system rather than a library:

- **No persistence.** Every module above is storage-agnostic and in-memory by design. That was
  right for getting the rules correct, and it means nothing is written down yet. Designed in
  ADR-0008; `cointoss.store` does not exist.
- **No ingest layer.** Nothing constructs an `Observation` from a source row, so ADR-0004's
  "a FIGI attempt is mandatory at instrument creation" has no home to be enforced in.
- **No price storage.** `get_prices` returns a `FrameData` that is cached and then discarded.
  Designed in ADR-0009. CoinGecko's `market_chart` endpoint, which ADR-0009 needs for coins Yahoo
  does not carry, is not in the adapter.
- **No returns.** Nothing turns prices into returns, so no covariance can be computed; the only
  way one enters the system today is a vendor import.
- **No universe construction.** `UniverseSeries` records what something else produced. Nothing
  produces anything. Designed in ADR-0007; `UniverseDefinition` does not exist.
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
declared covariance exist and are tested. What is missing is the plumbing between them and the
storage underneath.

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

So "run an instance" is mostly configuration and wiring, plus the three genuinely new pieces:
an ingest layer, price storage, and returns.

## Proposed build order

Each step is independently useful and leaves the system working.

1. **Persistence for what exists.** Build `cointoss.store` to ADR-0008: one `cointoss.db`, explicit
   pragmas, `SchemaVersion`, surrogate keys with alternative keys, membership and Ticker History as
   intervals, matrices as row-per-entry payloads. Nothing else can accumulate until state does.
2. **Universe Definitions.** ADR-0007's recipe, revision log, Inclusions and Exclusions, pinned
   members, and the evaluator that turns a Definition plus source data into a Universe Series entry
   and an Evaluation Run.
3. **Ingest.** One path from a source row to an `Observation` to an `InstrumentRegistry`, enforcing
   the mandatory-FIGI-attempt invariant and driving the unresolved retry queue. Pinning a manual
   member is one of its entry points.
4. **The instance.** `lyth.yaml`, a real CLI, cron triggers on the ingest and evaluation DAGs,
   woodglue serving. First point at which data accumulates unattended.
5. **Price storage.** ADR-0009's Bars, Corporate Actions and Restatements, from `get_prices` and
   CoinGecko `market_chart`. Needs `market_chart` added to the CoinGecko adapter first.
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
  needs settling before #3.
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
