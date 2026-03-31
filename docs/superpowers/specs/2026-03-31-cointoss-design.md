# Cointoss Design Spec

A crypto coin portfolio tracker and research platform that builds an ontology of cryptocurrencies. Demonstrates real-world usage of lythonic and woodglue libraries.

## Goals

- Track crypto coin portfolios with historical value snapshots
- Collect and cache coin data from CoinGecko free API (~300 calls/day budget)
- Build an extensible relationship graph (ontology) of cryptocurrencies
- Serve data via async Tornado API with Caddy in front for HTTPS and static files
- Showcase lythonic features: DbModel, DAG composition, PeriodicTask, GlobalRef, Result, ActionTree CLI
- Showcase woodglue features: Tornado server, Caddy integration, workflow engine, CLI patterns

## Project Setup

Mirrors lythonic's UV/Hatchling project structure exactly.

### Build System

- Hatchling with `uv-dynamic-versioning` for git-tag-based PEP 440 versioning
- UV for dependency management and virtual environments
- `uv.lock` for reproducible installs

### Dependencies

**Runtime:**
- `lythonic` — ORM, DAG composition, periodic tasks, CLI, types
- `woodglue` — server framework, Caddy integration, workflow engine
- `httpx` — async HTTP client for CoinGecko API

**Dev (mirrors lythonic):**
- pytest, pytest-sugar, pytest-cov, pytest-asyncio, coverage
- ruff, codespell, basedpyright
- rich, funlog

**Docs (mirrors lythonic):**
- mkdocs, mkdocs-material, mkdocstrings[python], markdown-pycon

### Entry Point

`cointoss = "cointoss.cli:main"`

### CI/CD (GitHub Actions, copied from lythonic, renamed)

- `ci.yml` — Matrix: ubuntu/macos/windows x Python 3.11/3.12/3.13. Runs lint + pytest.
- `publish.yml` — Triggered on GitHub release. Builds and publishes to PyPI via trusted publishing.
- `docs.yml` — Deploys MkDocs to gh-pages on push to main.

### do_release Claude Skill

Copied from lythonic's `.claude/skills/do_release/SKILL.md`, with names changed from lythonic to cointoss. Same workflow: generate release notes, draft release message, clean up design docs.

### CLAUDE.md

Copied from lythonic, adapted for cointoss. Same coding standards: three-tier testing, type annotations, absolute imports, uv-first workflow.

### Makefile

```makefile
install: uv sync --all-extras
lint:    uv run python devtools/lint.py
test:    uv run pytest
serve:   uv run cointoss server start
```

## Project Structure

```
cointoss/
├── .claude/skills/do_release/SKILL.md
├── .github/workflows/
│   ├── ci.yml
│   ├── publish.yml
│   └── docs.yml
├── devtools/lint.py
├── docs/
├── src/cointoss/
│   ├── __init__.py
│   ├── cli.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── coin.py
│   │   └── portfolio.py
│   ├── sources/
│   │   ├── __init__.py
│   │   └── coingecko.py
│   ├── ontology.py
│   ├── pipeline.py
│   ├── server.py
│   └── scheduler.py
├── tests/
├── CLAUDE.md
├── Makefile
├── mkdocs.yml
├── pyproject.toml
└── uv.lock
```

## Data Model

All models use lythonic's `DbModel` with `"(PK)"` and `"(FK:Table.field)"` field description conventions. A single `Schema` object registers all models.

### Coin Data (from API)

**Coin**
- `id: str` (PK) — CoinGecko slug (e.g. "bitcoin")
- `symbol: str` — ticker (e.g. "btc")
- `name: str` — display name
- `description: str` — from CoinGecko
- `genesis_date: date | None`
- `market_cap_rank: int | None`
- `homepage: str | None`
- `repo_url: str | None`
- `last_fetched: datetime`

**CoinPrice**
- `id: int` (PK, autoincrement)
- `coin_id: str` (FK:Coin.id)
- `timestamp: datetime`
- `price_usd: float`
- `market_cap: float | None`
- `total_volume: float | None`
- `price_change_24h: float | None`

**CoinCategory**
- `id: int` (PK, autoincrement)
- `name: str` — e.g. "DeFi", "Layer-1"
- `description: str | None`

**CoinCategoryLink**
- `id: int` (PK, autoincrement)
- `coin_id: str` (FK:Coin.id)
- `category_id: int` (FK:CoinCategory.id)

### Ontology (Relationship Graph)

**RelationshipType**
- `id: int` (PK, autoincrement)
- `name: str` — e.g. "fork_of", "competes_with"
- `description: str`
- `is_symmetric: bool` — "competes_with" is symmetric, "fork_of" is not

**CoinRelationship**
- `id: int` (PK, autoincrement)
- `coin_from: str` (FK:Coin.id)
- `coin_to: str` (FK:Coin.id)
- `relationship_type: int` (FK:RelationshipType.id)
- `confidence: float` — 0.0 to 1.0
- `source: str` — where this relationship was established (e.g. "manual", "coingecko")
- `notes: str | None`
- `created_at: datetime`

**Seeded relationship types:** `fork_of`, `competes_with`, `ecosystem_member`, `depends_on`, `bridges_to`

### Portfolio

**Portfolio**
- `id: int` (PK, autoincrement)
- `name: str`
- `description: str | None`
- `created_at: datetime`

**Holding**
- `id: int` (PK, autoincrement)
- `portfolio_id: int` (FK:Portfolio.id)
- `coin_id: str` (FK:Coin.id)
- `amount: float`
- `cost_basis_usd: float | None`
- `acquired_at: datetime | None`

**PortfolioSnapshot**
- `id: int` (PK, autoincrement)
- `portfolio_id: int` (FK:Portfolio.id)
- `timestamp: datetime`
- `total_value_usd: float`

## Architecture

### CLI (lythonic ActionTree)

```
cointoss
├── coins
│   ├── fetch          # Fetch/update coin data from CoinGecko
│   ├── list           # List tracked coins
│   ├── info <coin>    # Show detailed coin info + relationships
│   └── relate         # Add/view relationships between coins
├── portfolio
│   ├── create         # Create a named portfolio
│   ├── add            # Add a holding to a portfolio
│   ├── value          # Show current portfolio value
│   └── snapshot       # Take a value snapshot
├── pipeline
│   ├── run            # Run a data pipeline DAG once
│   └── schedule       # Start periodic data collection
└── server
    ├── start          # Start Tornado API server
    └── stop           # Stop server
```

### DAG Pipelines (lythonic compose)

A composable data collection pipeline built with lythonic's `Dag` and `DagNode`:

```
fetch_coin_list >> fetch_coin_details >> fetch_prices >> update_categories
```

Each node is a `GlobalRef`-registered async callable. The pipeline can be run once via `cointoss pipeline run` or scheduled via `cointoss pipeline schedule`.

Rate limiting is built into the CoinGecko adapter — it tracks calls per day and pauses when approaching the budget.

### Scheduler (lythonic PeriodicTask)

- Price updates: every 4 hours (uses ~6 calls per run for top 50 coins)
- Full metadata refresh: once daily
- Portfolio snapshots: after each price update
- Budget-aware: all scheduled tasks respect the ~300 calls/day limit

### Server (Tornado async)

REST API endpoints:
- `GET /api/coins` — list tracked coins (with filters)
- `GET /api/coins/{id}` — coin details + relationships
- `GET /api/coins/{id}/prices` — price history
- `GET /api/portfolio` — list portfolios
- `GET /api/portfolio/{id}` — portfolio details with current value
- `GET /api/portfolio/{id}/history` — value snapshots over time
- `GET /api/ontology/graph` — relationship graph data (nodes + edges)
- `POST` endpoints for creating portfolios, adding holdings, adding relationships

### Caddy (via woodglue)

- Terminates HTTPS
- Serves static frontend from `frontend/` directory via `EnsureStaticSite`
- Reverse-proxies `/api/*` to Tornado on localhost
- Configuration generated by woodglue's Caddy models

### Frontend

Placeholder static site in `frontend/`:
- `index.html` with basic layout
- Fetches from `/api/*` endpoints
- Displays: coin list, portfolio value, relationship graph visualization
- Not the focus — just enough to prove the wiring works
- Future: React SPA with proper build pipeline

## Implementation Scope

### Fully Implemented

- Project setup: pyproject.toml, Makefile, CI workflows, do_release skill, CLAUDE.md
- All DbModel classes with working schema creation and seeded relationship types
- CoinGecko adapter: fetch coin list, coin details, price history (httpx async, rate-limited)
- CLI: `coins fetch`, `coins list`, `coins info` fully working
- One DAG pipeline wired end-to-end (fetch chain)
- PeriodicTask scheduler with rate-limit budget tracking
- Tornado API handlers for coins and portfolio endpoints
- Tests: doctests in modules, integration tests for pipeline and API

### Scaffolded (Minimal Implementation)

- Portfolio CLI commands: create, add, value work; snapshot is basic
- Ontology commands: add/query relationships works, no auto-discovery
- Caddy config generation: produces valid config, requires Caddy installed
- Frontend: static index.html that hits the API, no React build

### Not Implemented (Future)

- Additional data sources beyond CoinGecko
- Auto-relationship discovery / ML-based ontology building
- Woodglue auth integration (available but not enforced)
- Full React frontend with build pipeline
- Plugin system for sharing ontology insights

## Testing Strategy

Three-tier approach matching lythonic:

1. **Doctests** in module docstrings — for pure functions and simple examples
2. **Inline tests** below `## Tests` comment in source files — for unit-level validation
3. **Separate test files** in `tests/` — for integration tests (API, pipeline, database)

Key test areas:
- DbModel CRUD operations for all models
- CoinGecko adapter (with mocked HTTP responses)
- DAG pipeline composition and execution
- Tornado API endpoint responses
- Rate limiter budget tracking
