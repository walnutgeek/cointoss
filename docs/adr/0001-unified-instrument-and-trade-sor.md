# Unified Instrument abstraction and Trade as System of Record

Status: accepted, amended 2026-10-30. The Instrument-identity portion is superseded by ADR-0004; the Portfolio, Trade, and Snapshot decisions stand.

Context: Product shifted from crypto-only to crypto+stocks. Need to support real brokers, paper/what_if portfolios, incomplete histories, and easy aggregation for net-worth views.

Decision:
- Introduce thin stable `Instrument` (id, type stock|crypto, symbol, name, provider_refs, alias history) with lazy-loaded StockInfo/CryptoInfo and price history, not a fat union.
- Model `Portfolio` as logical container with flexible metadata (kind: real|paper|what_if, custodian optional).
- Use single-leg `Trade` as system of record with signed quantity (+ buy/- sell) and `kind: real | synthetic | opening_balance` to bootstrap when trade history incomplete.
- Derive `PortfolioPosition` (quantity sum + optional market_price/cost_basis) from trades; treat `PortfolioSnapshot` as monthly materialized cache with captured prices for immutability, not source of truth.
- Defer lot-based tax accounting; future plan is lot as synthetic Instrument.
- Amended 2026-10-30, for the first portfolio build:
  - A `Trade` carries a UTC timestamp as entered. A position on date D counts every trade up to the end of D UTC and is valued at D's close, read through Price Sources with the Session Offset applied (ADR-0009 amendment).
  - Trades stay single-leg with no cash Instrument; a trade may carry its cost as a field. Valuation is in USD only. A cash leg waits for an exchange importer, whose exports carry one explicitly.
  - A mistaken trade is voided, not deleted or edited: `voided_at` marks it, reads skip it, and it stays visible as voided. A reversing `synthetic` trade was rejected because the raw history would then misstate quantities; a hard delete because earlier Snapshots would disagree with the trades with no record of why.
  - Each Portfolio has an owning User, and portfolio reads return only the caller's own portfolios.
  - Cost basis is average cost over the trades that carry one, labelled as such. FIFO is lot accounting by another name and stays deferred.
  - Trades are entered as `opening_balance` and manual trades through JSON-RPC methods that require a Role; the CLI calls them. The explorer stays read-only.
  - An Instrument traded in a Portfolio is pinned into a `portfolio-held` Universe Definition as an Inclusion, so the sweep and backfill bring it bars without widening another universe.
  - No Portfolio Snapshot yet: positions and value are derived on read, since replaying a few hand-entered trades is instant.

Considered Options:
- Snapshot as SoR (simple but cannot reconstruct evolution, hard to audit)
- Fat Instrument inheritance with StockInstrument/CryptoInstrument carrying all fields (bloated, hard to extend to ETFs, options)
- Double-entry trade legs (cash+asset) from day one (more correct but overhead for v1, can evolve to it)

Consequences: Ingestion adapters (CoinGecko, Yahoo) map to Instrument identity first; data gathering scoped separately. Trade history can be incomplete but still functional via synthetic opening balances. Snapshots remain immutable even when corporate actions rewrite adjusted price history elsewhere.
