# cointoss — Crypto and Stock Research Platform

Portfolio tracker for crypto and stocks that unifies research, data gathering, and valuation across brokers and paper portfolios.

Value types for matrices, vectors, and axes come from `lythonic` and keep their meanings from that project's glossary: `Universe`, `ExposureMatrix`, `SymmetricMatrix`, `KeyedVector`, `Subject`, `Target`, `Exposure`, `Aligned`. Terms below are the ones specific to cointoss.

## Language

### Time and Truth

**Series**:
A named, time-versioned system of record whose value at an instant is a lythonic value type. The truth about what was decided or declared.
_Avoid_: history, timeline, log

**Snapshot**:
A materialized, immutable cache of something derivable, with inputs captured as observed. An audit point, never the system of record.
_Avoid_: balance, state, checkpoint

### Core Identity

**Instrument**:
The canonical, stable identity for a tradable asset. Thin object carrying its identifier, type, current symbol, name, and external references.
_Avoid_: Asset, Security, Ticker, Coin (use as subtype qualifiers, not generic term)

**Instrument Id**:
An immutable, readable identifier of the form `{type}.{scope}.{symbol}`, minted once from the symbol first seen and never changed thereafter. Also the key naming an Instrument on any matrix axis.
_Avoid_: slug, key, code, symbol

**Scope**:
The authority under which a symbol is unique: an ISO country for a listed instrument, a chain for an on-chain token, or `native` for a chain's own coin.
_Avoid_: market, region, venue, namespace

**Qualifier**:
A suffix appended to an Instrument Id when its symbol is already taken by an unrelated Instrument, derived from the issuer name or, failing that, a number.
_Avoid_: disambiguator, discriminator, suffix

**External Reference**:
An identifier for an Instrument issued by an outside authority: a FIGI, a chain and contract address, or a data provider's own stable id. Identity is established by these, never by symbol alone.
_Avoid_: provider ref, mapping, external id, xref

**FIGI Resolution**:
The recorded outcome of attempting to anchor an Instrument to a FIGI: resolved, not found, or not attempted. An Instrument that is not resolved is visibly unresolved and is retried.
_Avoid_: enrichment, lookup status

**Supersession**:
The relation recording that one Instrument was later found to be the same thing as an earlier one. The earlier Instrument survives, nothing already stored is rewritten, and reads fold the two together.
_Avoid_: merge, duplicate, alias, link

**Survivor**:
The Instrument a chain of Supersessions ends at. Reads that return or aggregate Instruments answer in terms of it, counting whatever was recorded under the Instruments superseded into it.
_Avoid_: canonical, master, primary, winner

**Instrument Registry**:
The collection of known Instruments, and the authority that turns an observation from a source into the Instrument it belongs to.
_Avoid_: catalog, directory, master, book

**Ticker History**:
The record of which symbols an Instrument traded under at which source, and between which dates. Resolves symbols supplied by people and documents at a point in time. Not a means of establishing identity, and not vendor-neutral: two sources may name one Instrument differently at the same moment.
_Avoid_: alias, rename, symbol mapping

**Identity Source**:
The source whose account of what an Instrument is and what it is called is taken as authoritative.
_Avoid_: primary source, master source

**Price Sources**:
The ordered preference of sources supplying an Instrument's Bars. The first holding a Bar for a date supplies it; the rest are fallback.
_Avoid_: data feed, provider list

### Scoping and Classification

**Universe Series**:
A Series of instrument lists that defines the scope for data gathering and research. Records what a Universe Definition produced, never how.
_Avoid_: Watchlist, List, Screen, Instrument Set

**Universe Definition**:
A named, editable recipe for producing a Universe Series. Revisions accumulate and are never removed.
_Avoid_: universe model, spec, screen, config

**Universe Declaration**:
A Universe Definition's name and rank band as an instance's configuration states them now. Reconciled into the stored Definition by appending a revision where they differ; never the Definition itself.
_Avoid_: spec, config entry

**Universe Parameters**:
The recipe held by a Universe Definition at one revision: an optional rank rule, an Inclusion set, and an Exclusion set.
_Avoid_: rule, criteria, settings

**Universe Definition Revision**:
One recorded edit to a Universe Definition, numbered in sequence.
_Avoid_: version, change

**Inclusion**:
A member pinned into a Universe Definition regardless of what its rule produces. Standing policy, not a one-day act.
_Avoid_: whitelist, manual add, override

**Exclusion**:
A member held out of a Universe Definition regardless of what its rule produces, until a later revision removes it.
_Avoid_: blacklist, ban, suppression

**Universe Membership**:
One Instrument's time-bounded stretch inside a Universe Series. The recorded form of membership; the Universe Series is its projection.
_Avoid_: constituent row, holding, entry

**Evaluation Run**:
One execution of a Universe Definition against source data: what it produced, under which revision, and whether anything changed. Evidence the job ran, not a record of what is true.
_Avoid_: snapshot, job, sync

**Exposure Series**:
A named Series of exposure matrices over a fixed target axis, relating instruments to the things they are exposed to.
_Avoid_: classification, tagging, category system

**Exposure Semantics**:
The declared meaning of the values in an Exposure Series: a percentage, a currency value, or an unscaled score.
_Avoid_: weight semantics, units, scale

### Price History

**Bar**:
One Instrument's open, high, low, close and volume, with market cap where the source reports it, for one session date from one source. Stored as fetched; two sources disagreeing about a date each keep their own Bar.
_Avoid_: candle, quote, price point, tick

**Provisional Bar**:
A Bar fetched on or before its own session date, judged on the UTC calendar, so possibly describing a day still in progress. Its replacement by a later fetch is the day finishing, not a Restatement. A CoinGecko Bar is the price at its date's 00:00 UTC, so it is final once that day has begun.
_Avoid_: partial bar, live bar, intraday bar

**Corporate Action**:
A dividend or split recorded against an Instrument on a date. The only record of why a stored price history changed.
_Avoid_: event, adjustment, split factor

**Restatement**:
A re-fetch that changed an already-stored Bar in a way no Corporate Action explains.
_Avoid_: correction, revision, fixup

### Risk

**Risk Model**:
A named, editable recipe for producing a covariance matrix: a universe reference, a lookback, a return frequency, and an estimator. Records how a covariance came to be; does not define what it is.
_Avoid_: factor model, estimator config, spec

**Risk Model Revision**:
One recorded edit to a Risk Model, numbered in sequence. Revisions accumulate and are never removed.
_Avoid_: version, change, migration

**Risk Parameters**:
The recipe held by a Risk Model at one revision: a universe reference, a lookback, a return frequency, and an estimator. An external estimator names a source instead of a computable recipe.
_Avoid_: config, settings, spec

**Revision Stamp**:
The recipe revision a Series entry was produced under, recorded on it at the time: the Risk Model revision on a declared covariance, the Universe Definition revision on a Universe Series entry. What keeps an entry interpretable after the recipe has been edited.
_Avoid_: version tag, provenance

**Covariance Series**:
A Series of symmetric matrices over a universe of instruments. Each entry is declared for a date under a stated Risk Model revision and is never recomputed, even when the price history beneath it is later rewritten.
_Avoid_: risk matrix, covariance cache, correlation series

### Portfolio Tracking

**Portfolio**:
A logical container for holdings, not necessarily tied to a broker. Holds metadata like kind (real, paper, what_if) and custodian.
_Avoid_: Account (unless broker-specific), Wallet

**Trade**:
Single-leg system of record for portfolio evolution. Signed quantity: positive is buy/deposit, negative is sell/withdrawal. Includes kind to support incomplete histories.
_Avoid_: Transaction (too generic), Lot

**Trade Kind**:
Discriminator for Trade provenance: `real` (broker import), `synthetic` (adjustment), `opening_balance` (bootstrap when prior history unknown).

**Portfolio Position**:
Derived value object: aggregated quantity of an Instrument in a Portfolio, with optional market price and cost basis. Reusable in live views and snapshots.

**Portfolio Snapshot**:
A Snapshot of a Portfolio's positions at a point in time, with market prices captured as observed. Supports monthly archival to avoid full trade replay.
_Avoid_: Balance, State
