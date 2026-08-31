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

**Ticker History**:
The record of which symbols an Instrument traded under, and between which dates. Resolves symbols supplied by people and documents at a point in time. Not a means of establishing identity.
_Avoid_: alias, rename, symbol mapping

### Scoping and Classification

**Universe Series**:
A Series of instrument lists that defines the scope for data gathering and research. Construction may be rule-based or manual.
_Avoid_: Watchlist, List, Screen, Instrument Set

**Exposure Series**:
A named Series of exposure matrices over a fixed target axis, relating instruments to the things they are exposed to.
_Avoid_: classification, tagging, category system

**Exposure Semantics**:
The declared meaning of the values in an Exposure Series: a percentage, a currency value, or an unscaled score.
_Avoid_: weight semantics, units, scale

### Risk

**Risk Model**:
A named, editable recipe for producing a covariance matrix: a universe reference, a lookback, a return frequency, and an estimator. Records how a covariance came to be; does not define what it is.
_Avoid_: factor model, estimator config, spec

**Risk Model Revision**:
One recorded edit to a Risk Model, numbered in sequence. Revisions accumulate and are never removed.
_Avoid_: version, change, migration

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
