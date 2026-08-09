# cointoss — Crypto and Stock Research Platform

Portfolio tracker for crypto and stocks that unifies research, data gathering, and valuation across brokers and paper portfolios.

## Language

### Core Identity

**Instrument**:
The canonical, stable identity for a tradable asset (e.g. AAPL stock, BTC coin). Thin object with type, symbol, name, and provider references.
_Avoid_: Asset, Security, Ticker, Coin (use as subtype qualifiers, not generic term)

**Instrument Alias**:
Historical mapping for renames (FB -> META). Preserves stable Instrument ID.

### Scoping and Classification

**Universe**:
A time series of instrument lists that defines the scope for data gathering and research. Construction may be rule-based or manual, and its evolution is tracked over time.
_Avoid_: Watchlist, List, Screen

**Exposure Matrix**:
A named classification system that is matrix-centric and time-versioned, mapping instruments to buckets with weights. Weight semantics (percent, value, score) are defined by matrix metadata.
_Avoid_: Tag, Category, Sector list (these are buckets within a matrix)

**Exposure Entry**:
A single versioned membership of an Instrument in an Exposure Matrix bucket at a point in time, with a weight.

**Bucket**:
A named category within an Exposure Matrix (e.g. Tech, DeFi, US).

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
Materialized cache of a Portfolio's positions at a point in time, with market prices captured as observed. Immutable audit point, not the system of record. Supports monthly archival to avoid full trade replay.
_Avoid_: Balance, State

**Instrument Set**:
Internal shared storage concept for a set of instrument references at a version. Used by both Universe and Exposure Matrix.
