# Road Map

Where cointoss is, where it is going, and what has to be decided before it gets there. Updated
as directions change. Decisions that survive belong in `docs/adr/`; vocabulary belongs in
`CONTEXT.md`. This file holds the parts that are still moving.

Last updated: 2026-09-03, at `bb873c6`.

## Where we are

Four ADRs designed and implemented in one pass. 112 tests, lint clean, no open issues.

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
  right for getting the rules correct, and it means nothing is written down yet.
- **No ingest layer.** Nothing constructs an `Observation` from a source row, so ADR-0004's
  "a FIGI attempt is mandatory at instrument creation" has no home to be enforced in.
- **No price storage.** `get_prices` returns a `FrameData` that is cached and then discarded.
- **No returns.** Nothing turns prices into returns, so no covariance can be computed; the only
  way one enters the system today is a vendor import.
- **No universe construction.** `UniverseSeries` records what something else produced. Nothing
  produces anything.
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

1. **Persistence for what exists.** Give `Instrument`, `UniverseSeries`, `ExposureSeries`,
   `RiskModel` and `CovarianceSeries` somewhere to live via `lythonic.state`. Nothing else can
   accumulate until state does.
2. **Ingest.** One path from a source row to an `Observation` to an `InstrumentRegistry`,
   enforcing the mandatory-FIGI-attempt invariant and driving the unresolved retry queue.
3. **Source universes.** `UniverseSeries` per source, appended by the ingest run.
4. **The instance.** `lyth.yaml`, a real CLI, cron triggers on the ingest DAGs, woodglue
   serving. First point at which data accumulates unattended.
5. **Price storage.** Bars per instrument per date, from `get_prices` and CoinGecko OHLC.
6. **Returns.** Whatever representation the estimator needs.
7. **The estimator.** Closes ADR-0005's gap so a covariance can be computed rather than only
   imported.
8. **Portfolio.** ADR-0001's `Trade` as system of record, `Position`, `PortfolioSnapshot`.
9. **The FIGI mapping cache.** ADR-0006's `(universe_name, as_of)` key. Unblocked since #2;
   until it lands, instrument creation hits a live OpenFIGI call on every ingest.

## Open decisions

To settle in grilling before building. Roughly in dependency order — the storage ones gate
almost everything else.

### Storage

- **Blob or table?** Series types serialize themselves, so a Series could be one row with a
  JSON payload. That is trivial to build and unqueryable: "which universes contained AAPL in
  2024" needs SQL over decomposed rows. Which reads win, and is the answer the same for
  `UniverseSeries`, `ExposureSeries` and `CovarianceSeries`?
- **Where does the append boundary sit?** All three Series types are immutable values whose
  `append` returns a new whole Series. Loading, appending and rewriting a ten-year daily Series
  to add one entry is absurd. So either storage grows a row-level append that bypasses the
  value type, or the value type stops being the storage unit. This is the sharpest question on
  the list.
- **One database or several?** lythonic already separates `cache.db`, `dags.db` and
  `triggers.db`. Does domain state get its own, and does price history get its own beyond that?
- **Migrations.** `lythonic.state` has `Schema`; what happens when a model changes.

### Ingest

- **Is a source universe a `UniverseSeries`?** A "universe of what Yahoo lists" is source
  coverage; a research scope is a curated question. They may be the same type, or the same word
  covering two different concepts — the exact confusion the grilling session that produced
  ADR-0003 was called to fix. Worth checking rather than assuming.
- **What triggers a full ticker sweep** versus an incremental one, and how does a delisting
  reach the universe? Absence from a source is not the same fact as a delisting.
- **Where does the mandatory FIGI attempt live**, concretely, now that ADR-0004 makes it an
  ingest-orchestration invariant the identity module cannot enforce.
- **How does the unresolved retry queue get run?** A cron trigger over
  `InstrumentRegistry.unresolved()`, and at what cadence given OpenFIGI's coverage lag.

### Price history

- **Adjusted or unadjusted, or both?** This one is load-bearing rather than a detail. ADR-0005
  exists because adjusted history is rewritten by corporate actions; a covariance is declared
  precisely so it survives that rewrite. If prices are stored adjusted-as-of-fetch, the same
  rewrite silently changes stored history too, and the reproducibility ADR-0005 buys is undone
  one layer below it. Storing unadjusted prices plus a corporate action record is the
  reconstructable option, and is more work.
- **What is a bar?** OHLCV per instrument per date, presumably, but Yahoo and CoinGecko do not
  agree on fields, timezones, or what a day is for a 24/7 market.
- **Vendor disagreement.** Two sources for one instrument giving different closes. First writer
  wins, per-source rows, or a declared primary?

### Returns

- **What is the representation?** A `FrameData`, a `KeyedVector` per date, a new Series type, or
  something the estimator owns privately and nothing else sees.
- **Is it stored or derived?** Derived is honest and recomputable; stored is fast and, given the
  adjusted-price problem above, may be the only stable record of what a return actually was.
- **Simple or log returns**, and what happens across a gap, a halt, or a chain split.

### Portfolio

- Largely settled by ADR-0001 and not yet contradicted. The open part is lot-based tax
  accounting, which ADR-0001 defers with "lot as synthetic Instrument" as the sketch — worth
  testing against the identity model now that identity actually exists, since a synthetic
  instrument would need an Instrument Id and a Scope under ADR-0004's grammar.

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
- **Rule-based universe construction.** Screens and index constituents. Deferred by #2.
