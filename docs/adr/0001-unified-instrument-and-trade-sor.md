# Unified Instrument abstraction and Trade as System of Record

Context: Product shifted from crypto-only to crypto+stocks. Need to support real brokers, paper/what_if portfolios, incomplete histories, and easy aggregation for net-worth views.

Decision:
- Introduce thin stable `Instrument` (id, type stock|crypto, symbol, name, provider_refs, alias history) with lazy-loaded StockInfo/CryptoInfo and price history, not a fat union.
- Model `Portfolio` as logical container with flexible metadata (kind: real|paper|what_if, custodian optional).
- Use single-leg `Trade` as system of record with signed quantity (+ buy/- sell) and `kind: real | synthetic | opening_balance` to bootstrap when trade history incomplete.
- Derive `PortfolioPosition` (quantity sum + optional market_price/cost_basis) from trades; treat `PortfolioSnapshot` as monthly materialized cache with captured prices for immutability, not source of truth.
- Defer lot-based tax accounting; future plan is lot as synthetic Instrument.

Considered Options:
- Snapshot as SoR (simple but cannot reconstruct evolution, hard to audit)
- Fat Instrument inheritance with StockInstrument/CryptoInstrument carrying all fields (bloated, hard to extend to ETFs, options)
- Double-entry trade legs (cash+asset) from day one (more correct but overhead for v1, can evolve to it)

Consequences: Ingestion adapters (CoinGecko, Yahoo) map to Instrument identity first; data gathering scoped separately. Trade history can be incomplete but still functional via synthetic opening balances. Snapshots remain immutable even when corporate actions rewrite adjusted price history elsewhere.
