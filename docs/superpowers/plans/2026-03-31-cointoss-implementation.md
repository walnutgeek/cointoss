# Cointoss Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a crypto coin portfolio tracker and research platform that demonstrates lythonic and woodglue library features.

**Architecture:** UV-based Python package with lythonic DbModel for SQLite storage, DAG-composed data pipelines for CoinGecko API integration, Tornado async API server, and Caddy reverse proxy. CLI built with lythonic's ActionTree.

**Tech Stack:** Python 3.11-3.13, lythonic (ORM/DAG/CLI), woodglue (server/Caddy), httpx (async HTTP), Tornado, Hatchling + uv-dynamic-versioning

---

## File Structure

```
cointoss/
├── .claude/skills/do_release/SKILL.md    # Release automation skill
├── .github/workflows/
│   ├── ci.yml                             # CI matrix: 3 OS x 3 Python
│   ├── publish.yml                        # PyPI trusted publishing
│   └── docs.yml                           # MkDocs gh-pages deploy
├── .gitignore
├── CLAUDE.md                              # Coding standards (adapted from lythonic)
├── Makefile                               # install/lint/test/serve shortcuts
├── mkdocs.yml                             # Documentation config
├── pyproject.toml                         # Build config, deps, tool settings
├── devtools/
│   └── lint.py                            # Lint orchestrator (ruff, basedpyright, codespell)
├── docs/
│   └── index.md                           # Landing page
├── frontend/
│   └── index.html                         # Placeholder SPA
├── src/cointoss/
│   ├── __init__.py                        # Package entry, main()
│   ├── cli.py                             # ActionTree CLI definition
│   ├── models/
│   │   ├── __init__.py                    # Schema registry, seed data
│   │   ├── coin.py                        # Coin, CoinPrice, CoinCategory, CoinCategoryLink
│   │   ├── ontology.py                    # RelationshipType, CoinRelationship
│   │   └── portfolio.py                   # Portfolio, Holding, PortfolioSnapshot
│   ├── sources/
│   │   ├── __init__.py                    # DataSource protocol
│   │   └── coingecko.py                   # CoinGecko free API adapter with rate limiting
│   ├── ontology.py                        # Graph query helpers
│   ├── pipeline.py                        # DAG-composed fetch pipelines
│   ├── scheduler.py                       # PeriodicTask-based scheduling
│   └── server.py                          # Tornado API handlers
└── tests/
    ├── test_models.py                     # DbModel CRUD tests
    ├── test_coingecko.py                  # API adapter tests (mocked HTTP)
    ├── test_pipeline.py                   # DAG pipeline tests
    └── test_server.py                     # Tornado API endpoint tests
```

---

### Task 1: Project Scaffolding — pyproject.toml and .gitignore

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`

- [ ] **Step 1: Create pyproject.toml**

```toml
# ---- Project Info and Dependencies ----

[project.urls]
Repository = "https://github.com/walnutgeek/cointoss"

[project]
name = "cointoss"
description = "Crypto coin portfolio tracker and research platform - powered by lythonic and woodglue"
authors = [
    { name="Walnut Geek", email="wg@walnutgeek.com" },
]
readme = "README.md"
license = "MIT"
requires-python = ">=3.11,<4.0"
dynamic = ["version"]

classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Operating System :: OS Independent",
    "Programming Language :: Python",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Typing :: Typed",
]


# ---- Main dependencies ----

dependencies = [
    "lythonic",
    "woodglue",
    "httpx",
]


# ---- Dev dependencies ----

[dependency-groups]
dev = [
    "pytest>=8.3.5",
    "pytest-sugar>=1.0.0",
    "ruff>=0.14.2",
    "codespell>=2.4.1",
    "rich>=14.0.0",
    "basedpyright>=1.32.1",
    "funlog>=0.2.1",
    "coverage>=7.6.7",
    "pytest-cov>=7.0.0",
    "pytest-asyncio>=1.2.0",
]
docs = [
    "markdown-pycon>=1.0.1",
    "mkdocs>=1.6",
    "mkdocs-material>=9.5",
    "mkdocstrings[python]>=0.27",
]

[project.scripts]
cointoss = "cointoss.cli:main"


# ---- Build system ----

[build-system]
requires = ["hatchling", "uv-dynamic-versioning"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "uv-dynamic-versioning"

[tool.uv-dynamic-versioning]
vcs = "git"
style = "pep440"
bump = true

[tool.hatch.build.targets.wheel]
packages = ["src/cointoss"]


# ---- Settings ----

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = [
    "E",
    "F",
    "UP",
    "B",
    "I",
]
ignore = [
    "E501",
    "E402",
    "E731",
    "W191",
    "E111",
    "E114",
    "E117",
    "D206",
    "D300",
    "Q000",
    "Q001",
    "Q002",
    "Q003",
    "COM812",
    "COM819",
    "ISC002",
]

[tool.basedpyright]
include = ["src", "tests", "devtools"]
reportIgnoreCommentWithoutRule = false
reportUnnecessaryTypeIgnoreComment = false
reportMissingTypeStubs = false
reportUnusedCallResult = false
reportAny = false
reportExplicitAny = false
reportImplicitStringConcatenation = false
reportUnreachable = false

[tool.codespell]

[tool.pytest.ini_options]
python_files = ["*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
testpaths = [
    "src",
    "tests",
]
norecursedirs = []
filterwarnings = []

addopts = ["--import-mode=importlib", "--doctest-modules", "--cov=src", "--cov-report=term-missing", "--cov-report=xml:cov.xml"]
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "debug: marks tests for debugging (select with '-m debug')",
]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"

[tool.coverage.report]
exclude_lines = [
    "raise NotImplementedError",
    "raise AssertionError",
]
```

- [ ] **Step 2: Create .gitignore**

```gitignore
__pycache__/
*.py[cod]
*$py.class
*.so
dist/
*.egg-info/
*.egg
.eggs/
.venv/
venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
cov.xml
.coverage
*.db
*.sqlite
site/
.DS_Store
node_modules/
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "feat: add pyproject.toml and .gitignore

UV-based project setup mirroring lythonic's build configuration.
Hatchling + uv-dynamic-versioning, deps on lythonic, woodglue, httpx."
```

---

### Task 2: Project Scaffolding — Makefile, devtools/lint.py, CLAUDE.md

**Files:**
- Create: `Makefile`
- Create: `devtools/lint.py`
- Create: `CLAUDE.md`

- [ ] **Step 1: Create Makefile**

```makefile
.DEFAULT_GOAL := default

.PHONY: default install lint test upgrade build clean docs docs-serve docs-deploy serve

default: install lint test

install:
	uv sync --all-extras

lint:
	uv run python devtools/lint.py

test:
	uv run pytest

upgrade:
	uv sync --upgrade --all-extras --dev

build:
	uv build

serve:
	uv run cointoss server start

clean:
	-rm -rf dist/
	-rm -rf *.egg-info/
	-rm -rf .pytest_cache/
	-rm -rf .mypy_cache/
	-rm -rf .venv/
	-rm -rf site/
	-find . -type d -name "__pycache__" -exec rm -rf {} +

docs:
	uv run --group docs mkdocs build

docs-serve:
	uv run --group docs mkdocs serve

docs-deploy:
	uv run --group docs mkdocs gh-deploy
```

- [ ] **Step 2: Create devtools/lint.py**

Copy exactly from lythonic — no changes needed, it's generic:

```python
import subprocess

from funlog import log_calls
from rich import get_console, reconfigure
from rich import print as rprint

SRC_PATHS = ["src", "tests", "devtools"]
DOC_PATHS = ["README.md"]


reconfigure(emoji=not get_console().options.legacy_windows)


def main():
    rprint()

    errcount = 0
    errcount += run(["codespell", "--write-changes", *SRC_PATHS, *DOC_PATHS])
    errcount += run(["ruff", "check", "--fix", *SRC_PATHS])
    errcount += run(["ruff", "format", *SRC_PATHS])
    errcount += run(["basedpyright", "--stats", *SRC_PATHS])

    rprint()

    if errcount != 0:
        rprint(f"[bold red]:x: Lint failed with {errcount} errors.[/bold red]")
    else:
        rprint("[bold green]:white_check_mark: Lint passed![/bold green]")
    rprint()

    return errcount


@log_calls(level="warning", show_timing_only=True)
def run(cmd: list[str]) -> int:
    rprint()
    rprint(f"[bold green]>> {' '.join(cmd)}[/bold green]")
    errcount = 0
    try:
        subprocess.run(cmd, text=True, check=True)
    except KeyboardInterrupt:
        rprint("[yellow]Keyboard interrupt - Cancelled[/yellow]")
        errcount = 1
    except subprocess.CalledProcessError as e:
        rprint(f"[bold red]Error: {e}[/bold red]")
        errcount = 1

    return errcount


if __name__ == "__main__":
    exit(main())
```

- [ ] **Step 3: Create CLAUDE.md**

Copy from lythonic, replacing "lythonic" references with "cointoss" in examples and paths. The coding standards, Python guidelines, testing strategy, and documentation rules remain identical. Key changes:

- References to `lythonic.compose.namespace` → `cointoss.models.coin` (in examples)
- The `make` shortcuts section stays the same, with `make serve` added
- Add a note about `make serve` under the shortcuts section:
  ```
  # Start the development server:
  make serve
  ```

- [ ] **Step 4: Commit**

```bash
git add Makefile devtools/lint.py CLAUDE.md
git commit -m "feat: add Makefile, lint tooling, and coding standards

Makefile with install/lint/test/serve targets. devtools/lint.py runs
codespell, ruff, basedpyright. CLAUDE.md adapted from lythonic."
```

---

### Task 3: CI/CD Workflows and do_release Skill

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/publish.yml`
- Create: `.github/workflows/docs.yml`
- Create: `.claude/skills/do_release/SKILL.md`

- [ ] **Step 1: Create ci.yml**

Copy exactly from lythonic — no changes needed, it's generic:

```yaml
name: CI

on:
  push:
    branches: ["main", "master"]
  pull_request:
    branches: ["main", "master"]

permissions:
  contents: read

jobs:
  build:
    strategy:
      matrix:
        os: ["ubuntu-latest", "macos-latest", "windows-latest"]
        python-version: ["3.11", "3.12", "3.13"]

    runs-on: ${{ matrix.os }}

    steps:
      - name: Checkout (official GitHub action)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install uv (official Astral action)
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.8.13"
          enable-cache: true
          python-version: ${{ matrix.python-version }}

      - name: Set up Python (using uv)
        run: uv python install

      - name: Install all dependencies
        run: uv sync --all-extras

      - name: Run linting
        run: uv run python devtools/lint.py

      - name: Run tests
        run: uv run pytest
```

- [ ] **Step 2: Create publish.yml**

Copy exactly from lythonic — no changes needed:

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]
  workflow_dispatch:

jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Checkout (official GitHub action)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install uv (official Astral action)
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.8.13"
          enable-cache: true
          python-version: "3.12"

      - name: Set up Python (using uv)
        run: uv python install

      - name: Install all dependencies
        run: uv sync --all-extras

      - name: Run tests
        run: uv run pytest

      - name: Build package
        run: uv build

      - name: Publish to PyPI
        run: uv publish --trusted-publishing always
```

- [ ] **Step 3: Create docs.yml**

Copy exactly from lythonic:

```yaml
name: Deploy Docs
on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --group docs
      - run: uv run mkdocs gh-deploy --force
```

- [ ] **Step 4: Create do_release skill**

Adapted from lythonic — change repo URLs and project name:

```markdown
---
name: do_release
description: Prepare release notes and clean up superpowers docs for the next version
disable-model-invocation: true
argument-hint: <next_version>
---

Prepare a release for version $ARGUMENTS.

## Step 1: Generate Release Notes

Find the last release tag (highest `v*` tag) and generate a summary of all
commits since that tag.

\```bash
git log $(git tag --list 'v*' --sort=-v:refname | head -1)..HEAD --oneline
\```

Note that tag and substitute everywhere you see {LAST_RELEASE_TAG}.

Write a release notes file to `docs/release_notes/v$ARGUMENTS.md` following
the style of the previous release notes (see `docs/release_notes/` for
examples). The release notes should:

- Group changes by category: **New**, **Changed**, **Fixes**, **Documentation**,
  **Dependencies** (omit empty categories)
- Be concise — one bullet per logical change, not per commit
- Collapse multiple commits for the same feature into one bullet
- Reference module paths (e.g., `cointoss.models.coin`) where relevant
- Do NOT list every commit — summarize the intent of related changes

Commit and push release notes.

## Step 2: Draft release message

Come up with a title for this release, 80 characters or less. Try to catch the
common theme among all changes, yet you can cut it short with "..." if there
are too many things to mention.

Replace {RELEASE_TITLE} with that title in the message below.

Display the message for human review:

---

Draft new release at: https://github.com/walnutgeek/cointoss/releases/new

Title: v$ARGUMENTS: {RELEASE_TITLE}

**Full Changelog**: https://github.com/walnutgeek/cointoss/compare/{LAST_RELEASE_TAG}...v$ARGUMENTS

**Design docs**: [v$ARGUMENTS/docs/superpowers](https://github.com/walnutgeek/cointoss/tree/v$ARGUMENTS/docs/superpowers)

**Release notes**: [v$ARGUMENTS](https://github.com/walnutgeek/cointoss/blob/main/docs/release_notes/v$ARGUMENTS.md)

---

Wait for human to confirm that the release is triggered.

When human confirms, check if the tag exists:
\```bash
git pull && git tag --list "v$ARGUMENTS" | wc -l
\```
Output should confirm exactly one matching tag. Do not proceed to the next step
if it does not.

## Step 3: Clean up design docs

After a release is properly tagged, the design docs are accessible via the tag.
Delete them from main:
\```bash
git rm -r docs/superpowers
\```
If `docs/superpowers` does not exist, skip this step.

Commit and push.
```

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/publish.yml .github/workflows/docs.yml .claude/skills/do_release/SKILL.md
git commit -m "feat: add CI/CD workflows and do_release skill

GitHub Actions for CI (3 OS x 3 Python), PyPI publishing, and docs
deployment. Claude do_release skill for automated release workflow."
```

---

### Task 4: Documentation Setup

**Files:**
- Create: `mkdocs.yml`
- Create: `docs/index.md`
- Create: `README.md`

- [ ] **Step 1: Create mkdocs.yml**

```yaml
site_name: Cointoss
site_description: Crypto coin portfolio tracker and research platform
site_url: https://walnutgeek.github.io/cointoss/
repo_url: https://github.com/walnutgeek/cointoss
repo_name: walnutgeek/cointoss

theme:
  name: material
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: teal
      accent: teal
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: teal
      accent: teal
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
  features:
    - navigation.sections
    - navigation.expand
    - navigation.top
    - search.highlight
    - search.suggest
    - content.code.copy

plugins:
  - search
  - mkdocstrings:
      default_handler: python
      handlers:
        python:
          paths: [src]
          options:
            show_source: true
            show_root_heading: true
            show_root_full_path: false
            heading_level: 2
            members_order: source
            docstring_style: google

markdown_extensions:
  - pycon
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
  - admonition
  - pymdownx.details
  - toc:
      permalink: true

nav:
  - Home: index.md
  - API Reference:
      - cointoss: reference/core.md
      - cointoss.models: reference/models.md
      - cointoss.sources: reference/sources.md
      - cointoss.pipeline: reference/pipeline.md
      - cointoss.server: reference/server.md
```

- [ ] **Step 2: Create docs/index.md**

```markdown
# Cointoss

Crypto coin portfolio tracker and research platform, powered by
[lythonic](https://github.com/walnutgeek/lythonic) and
[woodglue](https://github.com/walnutgeek/woodglue).

## Features

- Track crypto coin portfolios with historical value snapshots
- Collect and cache coin data from CoinGecko API
- Build an extensible relationship graph (ontology) of cryptocurrencies
- Async Tornado API with Caddy reverse proxy

## Quick Start

```bash
# Install
uv sync --all-extras

# Fetch top coins from CoinGecko
cointoss coins fetch

# List tracked coins
cointoss coins list

# Start the API server
cointoss server start
```
```

- [ ] **Step 3: Create README.md**

```markdown
# cointoss

Crypto coin portfolio tracker and research platform.

Built on [lythonic](https://github.com/walnutgeek/lythonic) (SQLite ORM, DAG
composition, CLI) and [woodglue](https://github.com/walnutgeek/woodglue)
(async server, Caddy integration).

## Install

```bash
uv sync --all-extras
```

## Usage

```bash
# Fetch coin data
cointoss coins fetch

# Check portfolio value
cointoss portfolio value

# Start API server
cointoss server start
```

## Development

```bash
make install   # Install dependencies
make lint      # Run linters
make test      # Run tests
make serve     # Start dev server
```
```

- [ ] **Step 4: Commit**

```bash
git add mkdocs.yml docs/index.md README.md
git commit -m "feat: add documentation setup and README

MkDocs with material theme and mkdocstrings for API docs."
```

---

### Task 5: Install Dependencies and Verify Build

**Files:**
- Create: `src/cointoss/__init__.py` (minimal, to make uv sync work)

- [ ] **Step 1: Create minimal package init**

```python
"""
Cointoss: Crypto coin portfolio tracker and research platform.

Built on lythonic (SQLite ORM, DAG composition, CLI) and woodglue
(async server, Caddy integration). Fetches data from CoinGecko API,
stores it locally, and serves it via a Tornado async API.
"""

from __future__ import annotations


def main() -> None:
    """Entry point for the cointoss CLI."""
    from cointoss.cli import run_cli

    run_cli()
```

- [ ] **Step 2: Create stub cli.py so the entry point resolves**

```python
from __future__ import annotations


def run_cli() -> None:
    """Run the cointoss CLI. Placeholder until ActionTree is wired."""
    print("cointoss: use --help for usage")
```

- [ ] **Step 3: Run uv sync to install all dependencies**

```bash
uv sync --all-extras
```

Expected: Dependencies resolve and install, including lythonic, woodglue, httpx. If any dependency fails to resolve, check PyPI availability and adjust pyproject.toml.

- [ ] **Step 4: Verify the entry point works**

```bash
uv run cointoss
```

Expected: Prints "cointoss: use --help for usage"

- [ ] **Step 5: Commit**

```bash
git add src/cointoss/__init__.py src/cointoss/cli.py uv.lock
git commit -m "feat: add package init and verify dependency resolution

Minimal package with CLI entry point. All deps install successfully."
```

---

### Task 6: Coin Data Models

**Files:**
- Create: `src/cointoss/models/__init__.py`
- Create: `src/cointoss/models/coin.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write the failing test for Coin models**

Create `tests/test_models.py`:

```python
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path

from cointoss.models import create_schema
from cointoss.models.coin import Coin, CoinCategory, CoinCategoryLink, CoinPrice


def test_coin_schema_creation():
    """Schema creates all coin tables in a fresh database."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            schema = create_schema()
            schema.create_tables(conn)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row[0] for row in cursor.fetchall()]
            assert "coin" in tables or "Coin" in tables


def test_coin_insert_and_select():
    """Insert a Coin and select it back."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            schema = create_schema()
            schema.create_tables(conn)
            coin = Coin(
                id="bitcoin",
                symbol="btc",
                name="Bitcoin",
                description="A peer-to-peer electronic cash system",
            )
            coin.insert(conn)
            loaded = Coin.load_by_id(conn, "bitcoin")
            assert loaded is not None
            assert loaded.name == "Bitcoin"
            assert loaded.symbol == "btc"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_models.py::test_coin_schema_creation -v
```

Expected: FAIL with ImportError (modules don't exist yet)

- [ ] **Step 3: Create models/__init__.py**

```python
"""
Database schema registry for cointoss.

All DbModel classes are registered here. Use `create_schema()` to get a
Schema that can create all tables.
"""

from __future__ import annotations

from lythonic.state import Schema

from cointoss.models.coin import Coin, CoinCategory, CoinCategoryLink, CoinPrice
from cointoss.models.ontology import CoinRelationship, RelationshipType
from cointoss.models.portfolio import Holding, Portfolio, PortfolioSnapshot


def create_schema() -> Schema:
    """Create and return a Schema with all cointoss models registered."""
    schema = Schema()
    # Coin data
    schema.add(Coin)
    schema.add(CoinPrice)
    schema.add(CoinCategory)
    schema.add(CoinCategoryLink)
    # Ontology
    schema.add(RelationshipType)
    schema.add(CoinRelationship)
    # Portfolio
    schema.add(Portfolio)
    schema.add(Holding)
    schema.add(PortfolioSnapshot)
    return schema
```

- [ ] **Step 4: Create models/coin.py**

```python
"""
Coin data models for storing cryptocurrency information from external APIs.

Stores coin metadata, price history, and category classifications.
All models use lythonic's DbModel with SQLite storage.
"""

from __future__ import annotations

from datetime import date, datetime

from lythonic import utc_now
from lythonic.state import DbModel


class Coin(DbModel):
    """A cryptocurrency tracked by cointoss."""

    id: str = ""  # (PK) CoinGecko slug e.g. "bitcoin"
    symbol: str = ""  # Ticker e.g. "btc"
    name: str = ""  # Display name
    description: str = ""
    genesis_date: date | None = None
    market_cap_rank: int | None = None
    homepage: str | None = None
    repo_url: str | None = None
    last_fetched: datetime = utc_now()


class CoinPrice(DbModel):
    """A price snapshot for a coin at a point in time."""

    id: int = 0  # (PK)
    coin_id: str = ""  # (FK:Coin.id)
    timestamp: datetime = utc_now()
    price_usd: float = 0.0
    market_cap: float | None = None
    total_volume: float | None = None
    price_change_24h: float | None = None


class CoinCategory(DbModel):
    """A category grouping for coins (e.g. DeFi, Layer-1)."""

    id: int = 0  # (PK)
    name: str = ""
    description: str | None = None


class CoinCategoryLink(DbModel):
    """Many-to-many link between coins and categories."""

    id: int = 0  # (PK)
    coin_id: str = ""  # (FK:Coin.id)
    category_id: int = 0  # (FK:CoinCategory.id)
```

**Note:** The exact DbModel field description convention for PK/FK may need adjustment once we verify against lythonic's actual API. Lythonic uses Pydantic `Field(description="(PK)")` syntax. If the above default-value style doesn't work, switch to:

```python
from pydantic import Field

class Coin(DbModel):
    id: str = Field(default="", description="(PK)")
    # ...
    coin_id: str = Field(default="", description="(FK:Coin.id)")
```

Check lythonic's test files or source for the exact pattern and adapt.

- [ ] **Step 5: Create stub models/ontology.py and models/portfolio.py**

Create `src/cointoss/models/ontology.py` (stub so imports work):

```python
"""Ontology relationship models. Implemented in Task 7."""

from __future__ import annotations

from datetime import datetime

from lythonic import utc_now
from lythonic.state import DbModel


class RelationshipType(DbModel):
    """A type of relationship between two coins."""

    id: int = 0  # (PK)
    name: str = ""
    description: str = ""
    is_symmetric: bool = False


class CoinRelationship(DbModel):
    """A directed relationship between two coins."""

    id: int = 0  # (PK)
    coin_from: str = ""  # (FK:Coin.id)
    coin_to: str = ""  # (FK:Coin.id)
    relationship_type: int = 0  # (FK:RelationshipType.id)
    confidence: float = 1.0
    source: str = "manual"
    notes: str | None = None
    created_at: datetime = utc_now()
```

Create `src/cointoss/models/portfolio.py` (stub so imports work):

```python
"""Portfolio models. Implemented in Task 8."""

from __future__ import annotations

from datetime import datetime

from lythonic import utc_now
from lythonic.state import DbModel


class Portfolio(DbModel):
    """A named collection of coin holdings."""

    id: int = 0  # (PK)
    name: str = ""
    description: str | None = None
    created_at: datetime = utc_now()


class Holding(DbModel):
    """A coin holding within a portfolio."""

    id: int = 0  # (PK)
    portfolio_id: int = 0  # (FK:Portfolio.id)
    coin_id: str = ""  # (FK:Coin.id)
    amount: float = 0.0
    cost_basis_usd: float | None = None
    acquired_at: datetime | None = None


class PortfolioSnapshot(DbModel):
    """A point-in-time snapshot of a portfolio's total value."""

    id: int = 0  # (PK)
    portfolio_id: int = 0  # (FK:Portfolio.id)
    timestamp: datetime = utc_now()
    total_value_usd: float = 0.0
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_models.py -v
```

Expected: Both tests pass. If DbModel field conventions need adjustment (PK/FK syntax), fix per the note in Step 4 and re-run.

- [ ] **Step 7: Run lint**

```bash
make lint
```

Fix any issues reported by ruff or basedpyright.

- [ ] **Step 8: Commit**

```bash
git add src/cointoss/models/ tests/test_models.py
git commit -m "feat: add all DbModel classes for coins, ontology, and portfolio

Coin, CoinPrice, CoinCategory, CoinCategoryLink, RelationshipType,
CoinRelationship, Portfolio, Holding, PortfolioSnapshot. Schema
registry in models/__init__.py. Tests for schema creation and CRUD."
```

---

### Task 7: Ontology — Seed Data and Graph Queries

**Files:**
- Modify: `src/cointoss/models/__init__.py` (add seed function)
- Create: `src/cointoss/ontology.py`
- Modify: `tests/test_models.py` (add ontology tests)

- [ ] **Step 1: Write failing test for seed data and graph queries**

Add to `tests/test_models.py`:

```python
from cointoss.models import create_schema, seed_relationship_types
from cointoss.models.ontology import RelationshipType
from cointoss.ontology import get_coin_relationships


def test_seed_relationship_types():
    """Seeding creates the default relationship types."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            schema = create_schema()
            schema.create_tables(conn)
            seed_relationship_types(conn)
            types = RelationshipType.select(conn)
            names = {t.name for t in types}
            assert "fork_of" in names
            assert "competes_with" in names
            assert "ecosystem_member" in names
            assert "depends_on" in names
            assert "bridges_to" in names


def test_get_coin_relationships():
    """Query relationships for a coin returns both directions for symmetric types."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            schema = create_schema()
            schema.create_tables(conn)
            seed_relationship_types(conn)

            # Insert two coins
            Coin(id="bitcoin", symbol="btc", name="Bitcoin", description="").insert(conn)
            Coin(id="bitcoin-cash", symbol="bch", name="Bitcoin Cash", description="").insert(conn)

            # Get fork_of type id
            types = RelationshipType.select(conn)
            fork_type = next(t for t in types if t.name == "fork_of")

            # Add relationship: bitcoin-cash is a fork of bitcoin
            from cointoss.models.ontology import CoinRelationship

            CoinRelationship(
                coin_from="bitcoin-cash",
                coin_to="bitcoin",
                relationship_type=fork_type.id,
                source="manual",
            ).insert(conn)

            rels = get_coin_relationships(conn, "bitcoin")
            assert len(rels) >= 1
            assert any(r["coin"] == "bitcoin-cash" for r in rels)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_models.py::test_seed_relationship_types -v
```

Expected: FAIL — `seed_relationship_types` not defined

- [ ] **Step 3: Add seed function to models/__init__.py**

Add to `src/cointoss/models/__init__.py`:

```python
import sqlite3

from cointoss.models.ontology import RelationshipType

SEED_RELATIONSHIP_TYPES = [
    RelationshipType(name="fork_of", description="One coin forked from another", is_symmetric=False),
    RelationshipType(name="competes_with", description="Coins targeting the same use case", is_symmetric=True),
    RelationshipType(name="ecosystem_member", description="Coin belongs to another coin's ecosystem", is_symmetric=False),
    RelationshipType(name="depends_on", description="Coin depends on another for functionality", is_symmetric=False),
    RelationshipType(name="bridges_to", description="Coin provides bridging to another chain", is_symmetric=True),
]


def seed_relationship_types(conn: sqlite3.Connection) -> None:
    """Insert default relationship types if they don't already exist."""
    existing = RelationshipType.select(conn)
    existing_names = {t.name for t in existing}
    for rt in SEED_RELATIONSHIP_TYPES:
        if rt.name not in existing_names:
            rt.insert(conn)
```

- [ ] **Step 4: Create ontology.py**

```python
"""
Graph query helpers for the coin ontology.

Provides functions to query the relationship graph stored in
CoinRelationship and RelationshipType tables.
"""

from __future__ import annotations

import sqlite3
from typing import TypedDict

from cointoss.models.ontology import CoinRelationship, RelationshipType


class RelationshipInfo(TypedDict):
    coin: str
    relationship: str
    direction: str
    confidence: float
    source: str
    notes: str | None


def get_coin_relationships(conn: sqlite3.Connection, coin_id: str) -> list[RelationshipInfo]:
    """
    Get all relationships involving a coin, following symmetric
    relationships in both directions.
    """
    types_by_id: dict[int, RelationshipType] = {}
    for rt in RelationshipType.select(conn):
        types_by_id[rt.id] = rt

    results: list[RelationshipInfo] = []

    # Outgoing relationships (coin_from = coin_id)
    for rel in CoinRelationship.select(conn, coin_from=coin_id):
        rt = types_by_id.get(rel.relationship_type)
        if rt:
            results.append(
                RelationshipInfo(
                    coin=rel.coin_to,
                    relationship=rt.name,
                    direction="outgoing",
                    confidence=rel.confidence,
                    source=rel.source,
                    notes=rel.notes,
                )
            )

    # Incoming relationships (coin_to = coin_id)
    for rel in CoinRelationship.select(conn, coin_to=coin_id):
        rt = types_by_id.get(rel.relationship_type)
        if rt:
            results.append(
                RelationshipInfo(
                    coin=rel.coin_from,
                    relationship=rt.name,
                    direction="incoming" if not rt.is_symmetric else "symmetric",
                    confidence=rel.confidence,
                    source=rel.source,
                    notes=rel.notes,
                )
            )

    return results
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_models.py -v
```

Expected: All tests pass.

- [ ] **Step 6: Lint and commit**

```bash
make lint
git add src/cointoss/models/__init__.py src/cointoss/ontology.py tests/test_models.py
git commit -m "feat: add ontology seed data and graph query helpers

Seed 5 relationship types (fork_of, competes_with, ecosystem_member,
depends_on, bridges_to). Graph queries follow symmetric relationships
in both directions."
```

---

### Task 8: CoinGecko API Adapter

**Files:**
- Create: `src/cointoss/sources/__init__.py`
- Create: `src/cointoss/sources/coingecko.py`
- Create: `tests/test_coingecko.py`

- [ ] **Step 1: Write failing test for CoinGecko adapter**

Create `tests/test_coingecko.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cointoss.sources.coingecko import CoinGeckoClient, RateLimiter


def test_rate_limiter_allows_within_budget():
    limiter = RateLimiter(daily_budget=300)
    assert limiter.can_call() is True
    limiter.record_call()
    assert limiter.calls_today == 1


def test_rate_limiter_blocks_at_budget():
    limiter = RateLimiter(daily_budget=2)
    limiter.record_call()
    limiter.record_call()
    assert limiter.can_call() is False


@pytest.mark.asyncio
async def test_fetch_coin_list():
    """Fetching coin list returns parsed coin data."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum"},
    ]
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        client = CoinGeckoClient()
        coins = await client.fetch_coin_list()
        assert len(coins) == 2
        assert coins[0]["id"] == "bitcoin"


@pytest.mark.asyncio
async def test_fetch_coin_detail():
    """Fetching coin detail returns metadata."""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "description": {"en": "Bitcoin is a cryptocurrency."},
        "genesis_date": "2009-01-03",
        "market_cap_rank": 1,
        "links": {
            "homepage": ["https://bitcoin.org"],
            "repos_url": {"github": ["https://github.com/bitcoin/bitcoin"]},
        },
        "market_data": {
            "current_price": {"usd": 65000.0},
            "market_cap": {"usd": 1200000000000},
            "total_volume": {"usd": 30000000000},
            "price_change_percentage_24h": 2.5,
        },
        "categories": ["Cryptocurrency", "Layer 1 (L1)"],
    }
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        client = CoinGeckoClient()
        detail = await client.fetch_coin_detail("bitcoin")
        assert detail["id"] == "bitcoin"
        assert detail["market_data"]["current_price"]["usd"] == 65000.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_coingecko.py::test_rate_limiter_allows_within_budget -v
```

Expected: FAIL — ImportError

- [ ] **Step 3: Create sources/__init__.py**

```python
"""
Data source adapters for fetching cryptocurrency data.

Each adapter implements async methods for fetching coin lists, details,
and price history from an external API.
"""

from __future__ import annotations
```

- [ ] **Step 4: Create sources/coingecko.py**

```python
"""
CoinGecko free API adapter with rate limiting.

Uses the demo API (no key required, ~300 calls/day budget).
All responses are returned as raw dicts — conversion to DbModel
instances happens in the pipeline layer.

Rate limiting tracks calls per day and blocks when approaching the budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://api.coingecko.com/api/v3"


@dataclass
class RateLimiter:
    """Tracks API call budget per day (~300 calls/day for free tier)."""

    daily_budget: int = 300
    calls_today: int = 0
    day_start: float = field(default_factory=time.time)

    def _reset_if_new_day(self) -> None:
        elapsed = time.time() - self.day_start
        if elapsed > 86400:
            self.calls_today = 0
            self.day_start = time.time()

    def can_call(self) -> bool:
        self._reset_if_new_day()
        return self.calls_today < self.daily_budget

    def record_call(self) -> None:
        self._reset_if_new_day()
        self.calls_today += 1

    @property
    def remaining(self) -> int:
        self._reset_if_new_day()
        return self.daily_budget - self.calls_today


class CoinGeckoClient:
    """
    Async client for CoinGecko's free API.

    Wraps httpx.AsyncClient with rate limiting. Each method corresponds
    to one API endpoint and consumes one call from the daily budget.
    """

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self.rate_limiter = rate_limiter or RateLimiter()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict | list:
        if not self.rate_limiter.can_call():
            raise RuntimeError(
                f"CoinGecko daily budget exhausted ({self.rate_limiter.daily_budget} calls). "
                f"Resets in {86400 - (time.time() - self.rate_limiter.day_start):.0f}s."
            )
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}{path}", params=params)
            resp.raise_for_status()
            self.rate_limiter.record_call()
            return resp.json()

    async def fetch_coin_list(self) -> list[dict]:
        """
        Fetch the list of all coins (id, symbol, name).

        Uses /coins/list endpoint. One API call.
        """
        result = await self._get("/coins/list")
        assert isinstance(result, list)
        return result

    async def fetch_coin_detail(self, coin_id: str) -> dict:
        """
        Fetch detailed info for a single coin.

        Uses /coins/{id} endpoint. One API call.
        Returns: id, symbol, name, description, genesis_date, market_cap_rank,
        links, market_data, categories.
        """
        result = await self._get(
            f"/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
            },
        )
        assert isinstance(result, dict)
        return result

    async def fetch_price_history(
        self, coin_id: str, vs_currency: str = "usd", days: str = "30"
    ) -> dict:
        """
        Fetch price history for a coin.

        Uses /coins/{id}/market_chart endpoint. One API call.
        Returns: prices (list of [timestamp_ms, price] pairs).
        """
        result = await self._get(
            f"/coins/{coin_id}/market_chart",
            params={"vs_currency": vs_currency, "days": days},
        )
        assert isinstance(result, dict)
        return result

    async def fetch_top_coins(
        self, vs_currency: str = "usd", per_page: int = 50, page: int = 1
    ) -> list[dict]:
        """
        Fetch top coins by market cap with current prices.

        Uses /coins/markets endpoint. One API call.
        """
        result = await self._get(
            "/coins/markets",
            params={
                "vs_currency": vs_currency,
                "order": "market_cap_desc",
                "per_page": str(per_page),
                "page": str(page),
            },
        )
        assert isinstance(result, list)
        return result
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_coingecko.py -v
```

Expected: All tests pass.

- [ ] **Step 6: Lint and commit**

```bash
make lint
git add src/cointoss/sources/ tests/test_coingecko.py
git commit -m "feat: add CoinGecko API adapter with rate limiting

Async httpx client for /coins/list, /coins/{id}, /coins/{id}/market_chart,
and /coins/markets endpoints. RateLimiter tracks daily budget (~300 calls)."
```

---

### Task 9: DAG Pipeline

**Files:**
- Create: `src/cointoss/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_pipeline.py`:

```python
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cointoss.models import create_schema
from cointoss.models.coin import Coin
from cointoss.pipeline import build_fetch_pipeline, store_coin_from_list_entry


def test_store_coin_from_list_entry():
    """Convert a CoinGecko list entry to a Coin and store it."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            schema = create_schema()
            schema.create_tables(conn)

            entry = {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}
            store_coin_from_list_entry(conn, entry)

            loaded = Coin.load_by_id(conn, "bitcoin")
            assert loaded is not None
            assert loaded.symbol == "btc"


def test_build_fetch_pipeline():
    """Pipeline DAG builds without errors."""
    dag = build_fetch_pipeline()
    assert dag is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_pipeline.py::test_store_coin_from_list_entry -v
```

Expected: FAIL — ImportError

- [ ] **Step 3: Create pipeline.py**

```python
"""
DAG-composed data collection pipelines.

Uses lythonic's Dag and GlobalRef to compose fetch operations into
executable pipelines. Each node is an async callable that fetches
data from CoinGecko and stores it in the local SQLite database.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from lythonic import utc_now

from cointoss.models.coin import Coin, CoinPrice
from cointoss.sources.coingecko import CoinGeckoClient


def store_coin_from_list_entry(conn: sqlite3.Connection, entry: dict) -> None:
    """Store or update a coin from a CoinGecko list entry (id, symbol, name)."""
    existing = Coin.load_by_id(conn, entry["id"])
    if existing is None:
        Coin(
            id=entry["id"],
            symbol=entry["symbol"],
            name=entry["name"],
            description="",
            last_fetched=utc_now(),
        ).insert(conn)
    else:
        existing.symbol = entry["symbol"]
        existing.name = entry["name"]
        existing.last_fetched = utc_now()
        existing.update(conn)


def store_coin_detail(conn: sqlite3.Connection, detail: dict) -> None:
    """Store detailed coin info from CoinGecko /coins/{id} response."""
    coin = Coin.load_by_id(conn, detail["id"])
    if coin is None:
        coin = Coin(id=detail["id"], symbol=detail.get("symbol", ""), name=detail.get("name", ""))

    desc = detail.get("description", {})
    coin.description = desc.get("en", "") if isinstance(desc, dict) else str(desc)
    genesis = detail.get("genesis_date")
    coin.genesis_date = date.fromisoformat(genesis) if genesis else None
    coin.market_cap_rank = detail.get("market_cap_rank")

    links = detail.get("links", {})
    homepages = links.get("homepage", [])
    coin.homepage = homepages[0] if homepages else None
    repos = links.get("repos_url", {}).get("github", [])
    coin.repo_url = repos[0] if repos else None
    coin.last_fetched = utc_now()

    if Coin.load_by_id(conn, coin.id) is None:
        coin.insert(conn)
    else:
        coin.update(conn)

    # Store current price as a price snapshot
    market_data = detail.get("market_data", {})
    current_price = market_data.get("current_price", {}).get("usd")
    if current_price is not None:
        CoinPrice(
            coin_id=coin.id,
            timestamp=utc_now(),
            price_usd=current_price,
            market_cap=market_data.get("market_cap", {}).get("usd"),
            total_volume=market_data.get("total_volume", {}).get("usd"),
            price_change_24h=market_data.get("price_change_percentage_24h"),
        ).insert(conn)


async def run_fetch_coin_list(conn: sqlite3.Connection, client: CoinGeckoClient) -> list[str]:
    """Fetch coin list from CoinGecko and store basic entries. Returns coin IDs."""
    entries = await client.fetch_coin_list()
    for entry in entries:
        store_coin_from_list_entry(conn, entry)
    conn.commit()
    return [e["id"] for e in entries]


async def run_fetch_top_coins(
    conn: sqlite3.Connection, client: CoinGeckoClient, per_page: int = 50
) -> list[str]:
    """Fetch top coins by market cap and store details. Returns coin IDs."""
    coins = await client.fetch_top_coins(per_page=per_page)
    for coin_data in coins:
        store_coin_from_list_entry(conn, coin_data)
        # Markets endpoint includes price data
        CoinPrice(
            coin_id=coin_data["id"],
            timestamp=utc_now(),
            price_usd=coin_data.get("current_price", 0.0),
            market_cap=coin_data.get("market_cap"),
            total_volume=coin_data.get("total_volume"),
            price_change_24h=coin_data.get("price_change_percentage_24h"),
        ).insert(conn)
    conn.commit()
    return [c["id"] for c in coins]


async def run_fetch_coin_details(
    conn: sqlite3.Connection, client: CoinGeckoClient, coin_ids: list[str]
) -> None:
    """Fetch and store detailed info for a list of coins."""
    for coin_id in coin_ids:
        if not client.rate_limiter.can_call():
            break
        detail = await client.fetch_coin_detail(coin_id)
        store_coin_detail(conn, detail)
    conn.commit()


def build_fetch_pipeline():
    """
    Build a DAG for the full fetch pipeline.

    Returns a simple callable sequence. In future iterations this will use
    lythonic's Dag and DagNode for full composition, but for now we keep
    it as a straightforward async function to get the wiring right.
    """
    # Placeholder for lythonic Dag composition — the actual pipeline
    # functions above are designed to be composed into DagNodes via GlobalRef.
    # For now, return a descriptor of the pipeline stages.
    return {
        "stages": [
            "fetch_top_coins",
            "fetch_coin_details",
        ],
        "description": "Fetch top coins by market cap, then fetch details for each",
    }
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_pipeline.py -v
```

Expected: All tests pass.

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add src/cointoss/pipeline.py tests/test_pipeline.py
git commit -m "feat: add data collection pipeline with CoinGecko storage

Functions to store coin list entries and detailed coin info from
CoinGecko API responses. Pipeline DAG structure for composing
fetch operations."
```

---

### Task 10: Scheduler

**Files:**
- Create: `src/cointoss/scheduler.py`

- [ ] **Step 1: Create scheduler.py**

```python
"""
Periodic task scheduling for data collection.

Uses lythonic's PeriodicTask to schedule CoinGecko API fetches
at intervals that respect the ~300 calls/day budget.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lythonic.periodic import PeriodicTask

from cointoss.pipeline import run_fetch_coin_details, run_fetch_top_coins
from cointoss.sources.coingecko import CoinGeckoClient, RateLimiter


class FetchScheduler:
    """
    Manages periodic data collection from CoinGecko.

    Schedules:
    - Top coins refresh: every 4 hours (~6 calls per run)
    - Detail enrichment: every 8 hours (~50 calls per run)

    All tasks share a single RateLimiter to stay within budget.
    """

    def __init__(self, db_path: Path, rate_limiter: RateLimiter | None = None) -> None:
        self.db_path = db_path
        self.rate_limiter = rate_limiter or RateLimiter()
        self.client = CoinGeckoClient(rate_limiter=self.rate_limiter)
        self._coin_ids: list[str] = []

    async def fetch_top_coins_task(self) -> None:
        """Periodic task: fetch top 50 coins by market cap."""
        import contextlib

        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            self._coin_ids = await run_fetch_top_coins(conn, self.client, per_page=50)

    async def fetch_details_task(self) -> None:
        """Periodic task: fetch details for tracked coins (up to budget)."""
        import contextlib

        if not self._coin_ids:
            return
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            # Fetch details for first 10 coins per run to stay within budget
            await run_fetch_coin_details(conn, self.client, self._coin_ids[:10])

    def create_periodic_tasks(self) -> list[PeriodicTask]:
        """Create PeriodicTask instances for all scheduled work."""
        return [
            PeriodicTask(
                name="fetch_top_coins",
                interval=4 * 3600,  # Every 4 hours
                callback=self.fetch_top_coins_task,
            ),
            PeriodicTask(
                name="fetch_details",
                interval=8 * 3600,  # Every 8 hours
                callback=self.fetch_details_task,
            ),
        ]
```

**Note:** The exact `PeriodicTask` constructor API may differ from what's shown. Check lythonic's `periodic.py` source for the actual signature (it may use `Frequency` or `Interval` instead of a plain `interval` parameter). Adjust accordingly.

- [ ] **Step 2: Lint and commit**

```bash
make lint
git add src/cointoss/scheduler.py
git commit -m "feat: add periodic fetch scheduler

FetchScheduler with two periodic tasks: top coins every 4h,
detail enrichment every 8h. Shares a RateLimiter for budget control."
```

---

### Task 11: CLI with ActionTree

**Files:**
- Modify: `src/cointoss/cli.py`
- Modify: `src/cointoss/__init__.py`

- [ ] **Step 1: Rewrite cli.py with ActionTree**

```python
"""
CLI for cointoss, built with lythonic's ActionTree.

Provides commands for fetching coin data, managing portfolios,
running pipelines, and starting the API server.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path

from lythonic.compose.cli import ActionTree, Main, RunContext

from cointoss.models import create_schema, seed_relationship_types
from cointoss.pipeline import run_fetch_top_coins
from cointoss.sources.coingecko import CoinGeckoClient


DEFAULT_DB_PATH = Path("./cointoss.db")


def ensure_db(db_path: Path) -> sqlite3.Connection:
    """Open database and ensure schema exists."""
    conn = sqlite3.connect(str(db_path))
    schema = create_schema()
    schema.create_tables(conn)
    seed_relationship_types(conn)
    return conn


def coins_fetch(ctx: RunContext) -> None:
    """Fetch top coins from CoinGecko and store locally."""
    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    with contextlib.closing(ensure_db(db_path)) as conn:
        client = CoinGeckoClient()
        coin_ids = asyncio.run(run_fetch_top_coins(conn, client, per_page=50))
        print(f"Fetched {len(coin_ids)} coins")


def coins_list(ctx: RunContext) -> None:
    """List all tracked coins."""
    from cointoss.models.coin import Coin

    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    with contextlib.closing(ensure_db(db_path)) as conn:
        coins = Coin.select(conn)
        if not coins:
            print("No coins tracked yet. Run 'cointoss coins fetch' first.")
            return
        for coin in coins:
            rank = f"#{coin.market_cap_rank}" if coin.market_cap_rank else "  -"
            print(f"  {rank:>6}  {coin.symbol:<8} {coin.name}")


def coins_info(ctx: RunContext) -> None:
    """Show detailed info for a coin."""
    from cointoss.models.coin import Coin, CoinPrice
    from cointoss.ontology import get_coin_relationships

    coin_id = ctx.args.get("coin_id", "")
    if not coin_id:
        print("Usage: cointoss coins info <coin_id>")
        return

    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    with contextlib.closing(ensure_db(db_path)) as conn:
        coin = Coin.load_by_id(conn, coin_id)
        if coin is None:
            print(f"Coin '{coin_id}' not found. Run 'cointoss coins fetch' first.")
            return

        print(f"{coin.name} ({coin.symbol.upper()})")
        print(f"  Rank: #{coin.market_cap_rank or '?'}")
        if coin.description:
            # Truncate long descriptions
            desc = coin.description[:200] + "..." if len(coin.description) > 200 else coin.description
            print(f"  {desc}")
        if coin.homepage:
            print(f"  Homepage: {coin.homepage}")

        # Latest price
        prices = CoinPrice.select(conn, coin_id=coin_id)
        if prices:
            latest = max(prices, key=lambda p: p.timestamp)
            print(f"  Price: ${latest.price_usd:,.2f}")

        # Relationships
        rels = get_coin_relationships(conn, coin_id)
        if rels:
            print("  Relationships:")
            for r in rels:
                print(f"    {r['relationship']} -> {r['coin']} ({r['direction']})")


def portfolio_create(ctx: RunContext) -> None:
    """Create a new portfolio."""
    from cointoss.models.portfolio import Portfolio

    name = ctx.args.get("name", "")
    if not name:
        print("Usage: cointoss portfolio create <name>")
        return

    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    with contextlib.closing(ensure_db(db_path)) as conn:
        Portfolio(name=name, description=ctx.args.get("description", "")).insert(conn)
        conn.commit()
        print(f"Created portfolio '{name}'")


def portfolio_value(ctx: RunContext) -> None:
    """Show current value of a portfolio."""
    from cointoss.models.coin import CoinPrice
    from cointoss.models.portfolio import Holding, Portfolio

    portfolio_name = ctx.args.get("name", "")
    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    with contextlib.closing(ensure_db(db_path)) as conn:
        portfolios = Portfolio.select(conn)
        if portfolio_name:
            portfolios = [p for p in portfolios if p.name == portfolio_name]
        if not portfolios:
            print("No portfolios found.")
            return

        for portfolio in portfolios:
            holdings = Holding.select(conn, portfolio_id=portfolio.id)
            total = 0.0
            print(f"\n{portfolio.name}:")
            for h in holdings:
                prices = CoinPrice.select(conn, coin_id=h.coin_id)
                if prices:
                    latest = max(prices, key=lambda p: p.timestamp)
                    value = h.amount * latest.price_usd
                    total += value
                    print(f"  {h.coin_id}: {h.amount} x ${latest.price_usd:,.2f} = ${value:,.2f}")
                else:
                    print(f"  {h.coin_id}: {h.amount} (no price data)")
            print(f"  Total: ${total:,.2f}")


def server_start(ctx: RunContext) -> None:
    """Start the Tornado API server."""
    from cointoss.server import start_server

    db_path = Path(ctx.args.get("data", str(DEFAULT_DB_PATH)))
    port = int(ctx.args.get("port", "8888"))
    start_server(db_path, port)


def build_cli() -> Main:
    """Build the cointoss CLI tree."""
    tree = ActionTree("cointoss", description="Crypto coin tracker and research platform")

    coins = tree.group("coins", description="Manage tracked coins")
    coins.action("fetch", coins_fetch, description="Fetch top coins from CoinGecko")
    coins.action("list", coins_list, description="List all tracked coins")
    coins.action("info", coins_info, description="Show detailed coin info")

    portfolio = tree.group("portfolio", description="Manage portfolios")
    portfolio.action("create", portfolio_create, description="Create a new portfolio")
    portfolio.action("value", portfolio_value, description="Show portfolio value")

    server = tree.group("server", description="API server")
    server.action("start", server_start, description="Start the API server")

    return Main(tree)


def run_cli() -> None:
    """Entry point for the cointoss CLI."""
    cli = build_cli()
    cli.run()
```

**Note:** The exact ActionTree/Main/RunContext API may differ from what's shown. Check lythonic's `compose/cli.py` source for the actual constructor signatures and method names (`group`, `action`, etc.). Woodglue's `cli.py` is a reference for how to use it. Adjust the code to match the actual API.

- [ ] **Step 2: Verify CLI runs**

```bash
uv run cointoss --help
```

Expected: Shows help text with coins, portfolio, server subcommands. If ActionTree API differs, adjust Step 1 code.

- [ ] **Step 3: Lint and commit**

```bash
make lint
git add src/cointoss/cli.py src/cointoss/__init__.py
git commit -m "feat: add CLI with ActionTree

Commands: coins fetch/list/info, portfolio create/value, server start.
Uses lythonic's ActionTree for hierarchical CLI composition."
```

---

### Task 12: Tornado API Server

**Files:**
- Create: `src/cointoss/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_server.py`:

```python
from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
import tornado.testing
import tornado.web

from cointoss.models import create_schema, seed_relationship_types
from cointoss.models.coin import Coin, CoinPrice
from cointoss.server import make_app


class TestCoinsAPI(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        schema = create_schema()
        schema.create_tables(self.conn)
        seed_relationship_types(self.conn)

        # Insert test data
        from lythonic import utc_now

        Coin(
            id="bitcoin",
            symbol="btc",
            name="Bitcoin",
            description="Digital gold",
            market_cap_rank=1,
            last_fetched=utc_now(),
        ).insert(self.conn)
        CoinPrice(
            coin_id="bitcoin",
            timestamp=utc_now(),
            price_usd=65000.0,
            market_cap=1200000000000.0,
        ).insert(self.conn)
        self.conn.commit()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.conn.close()
        self.tmp_dir.cleanup()

    def get_app(self):
        return make_app(self.db_path)

    def test_list_coins(self):
        response = self.fetch("/api/coins")
        assert response.code == 200
        data = json.loads(response.body)
        assert len(data) == 1
        assert data[0]["id"] == "bitcoin"

    def test_get_coin(self):
        response = self.fetch("/api/coins/bitcoin")
        assert response.code == 200
        data = json.loads(response.body)
        assert data["id"] == "bitcoin"
        assert data["name"] == "Bitcoin"

    def test_get_coin_not_found(self):
        response = self.fetch("/api/coins/nonexistent")
        assert response.code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_server.py::TestCoinsAPI::test_list_coins -v
```

Expected: FAIL — ImportError

- [ ] **Step 3: Create server.py**

```python
"""
Tornado async API server for cointoss.

Serves coin data, portfolio info, and ontology graph via REST endpoints.
All handlers read from the local SQLite database.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import tornado.ioloop
import tornado.web

from cointoss.models import create_schema, seed_relationship_types


class BaseHandler(tornado.web.RequestHandler):
    """Base handler with database access."""

    def initialize(self, db_path: Path) -> None:
        self.db_path = db_path

    def get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        schema = create_schema()
        schema.create_tables(conn)
        return conn

    def set_default_headers(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")


class CoinsListHandler(BaseHandler):
    """GET /api/coins — list all tracked coins."""

    def get(self) -> None:
        from cointoss.models.coin import Coin

        with contextlib.closing(self.get_conn()) as conn:
            coins = Coin.select(conn)
            self.write(json.dumps([
                {
                    "id": c.id,
                    "symbol": c.symbol,
                    "name": c.name,
                    "market_cap_rank": c.market_cap_rank,
                }
                for c in coins
            ]))


class CoinDetailHandler(BaseHandler):
    """GET /api/coins/{id} — coin details with latest price."""

    def get(self, coin_id: str) -> None:
        from cointoss.models.coin import Coin, CoinPrice
        from cointoss.ontology import get_coin_relationships

        with contextlib.closing(self.get_conn()) as conn:
            coin = Coin.load_by_id(conn, coin_id)
            if coin is None:
                self.set_status(404)
                self.write(json.dumps({"error": f"Coin '{coin_id}' not found"}))
                return

            prices = CoinPrice.select(conn, coin_id=coin_id)
            latest_price = None
            if prices:
                latest = max(prices, key=lambda p: p.timestamp)
                latest_price = {
                    "price_usd": latest.price_usd,
                    "market_cap": latest.market_cap,
                    "total_volume": latest.total_volume,
                    "price_change_24h": latest.price_change_24h,
                    "timestamp": latest.timestamp.isoformat(),
                }

            relationships = get_coin_relationships(conn, coin_id)

            self.write(json.dumps({
                "id": coin.id,
                "symbol": coin.symbol,
                "name": coin.name,
                "description": coin.description,
                "genesis_date": coin.genesis_date.isoformat() if coin.genesis_date else None,
                "market_cap_rank": coin.market_cap_rank,
                "homepage": coin.homepage,
                "repo_url": coin.repo_url,
                "latest_price": latest_price,
                "relationships": relationships,
            }))


class CoinPricesHandler(BaseHandler):
    """GET /api/coins/{id}/prices — price history."""

    def get(self, coin_id: str) -> None:
        from cointoss.models.coin import CoinPrice

        with contextlib.closing(self.get_conn()) as conn:
            prices = CoinPrice.select(conn, coin_id=coin_id)
            self.write(json.dumps([
                {
                    "timestamp": p.timestamp.isoformat(),
                    "price_usd": p.price_usd,
                    "market_cap": p.market_cap,
                    "total_volume": p.total_volume,
                }
                for p in sorted(prices, key=lambda p: p.timestamp)
            ]))


class PortfolioListHandler(BaseHandler):
    """GET /api/portfolio — list all portfolios."""

    def get(self) -> None:
        from cointoss.models.portfolio import Portfolio

        with contextlib.closing(self.get_conn()) as conn:
            portfolios = Portfolio.select(conn)
            self.write(json.dumps([
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "created_at": p.created_at.isoformat(),
                }
                for p in portfolios
            ]))


class OntologyGraphHandler(BaseHandler):
    """GET /api/ontology/graph — full relationship graph as nodes + edges."""

    def get(self) -> None:
        from cointoss.models.coin import Coin
        from cointoss.models.ontology import CoinRelationship, RelationshipType

        with contextlib.closing(self.get_conn()) as conn:
            coins = Coin.select(conn)
            relationships = CoinRelationship.select(conn)
            types = {rt.id: rt.name for rt in RelationshipType.select(conn)}

            nodes = [{"id": c.id, "label": c.name, "symbol": c.symbol} for c in coins]
            edges = [
                {
                    "from": r.coin_from,
                    "to": r.coin_to,
                    "type": types.get(r.relationship_type, "unknown"),
                    "confidence": r.confidence,
                }
                for r in relationships
            ]

            self.write(json.dumps({"nodes": nodes, "edges": edges}))


def make_app(db_path: Path) -> tornado.web.Application:
    """Create the Tornado application with all routes."""
    handler_kwargs = {"db_path": db_path}
    return tornado.web.Application([
        (r"/api/coins", CoinsListHandler, handler_kwargs),
        (r"/api/coins/([^/]+)", CoinDetailHandler, handler_kwargs),
        (r"/api/coins/([^/]+)/prices", CoinPricesHandler, handler_kwargs),
        (r"/api/portfolio", PortfolioListHandler, handler_kwargs),
        (r"/api/ontology/graph", OntologyGraphHandler, handler_kwargs),
    ])


def start_server(db_path: Path, port: int = 8888) -> None:
    """Start the Tornado server."""
    # Ensure database exists
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        schema = create_schema()
        schema.create_tables(conn)
        seed_relationship_types(conn)

    app = make_app(db_path)
    app.listen(port)
    print(f"Cointoss API server running on http://localhost:{port}")
    print(f"Database: {db_path}")
    tornado.ioloop.IOLoop.current().start()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_server.py -v
```

Expected: All tests pass. Note: tornado.testing.AsyncHTTPTestCase handles the event loop.

- [ ] **Step 5: Lint and commit**

```bash
make lint
git add src/cointoss/server.py tests/test_server.py
git commit -m "feat: add Tornado API server

REST endpoints: /api/coins, /api/coins/{id}, /api/coins/{id}/prices,
/api/portfolio, /api/ontology/graph. CORS enabled for frontend."
```

---

### Task 13: Frontend Placeholder

**Files:**
- Create: `frontend/index.html`

- [ ] **Step 1: Create index.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cointoss - Crypto Research Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }
        h1 { color: #38bdf8; margin-bottom: 0.5rem; }
        h2 { color: #94a3b8; margin: 1.5rem 0 0.5rem; font-size: 1.1rem; }
        .subtitle { color: #64748b; margin-bottom: 2rem; }
        table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
        th, td { text-align: left; padding: 0.5rem 1rem; border-bottom: 1px solid #1e293b; }
        th { color: #94a3b8; font-weight: 600; }
        .price { font-family: monospace; }
        .rank { color: #64748b; }
        .status { padding: 1rem; background: #1e293b; border-radius: 0.5rem; margin: 1rem 0; }
        .error { color: #f87171; }
        .loading { color: #94a3b8; }
    </style>
</head>
<body>
    <h1>Cointoss</h1>
    <p class="subtitle">Crypto coin research platform</p>

    <div id="status" class="status loading">Connecting to API...</div>

    <h2>Tracked Coins</h2>
    <div id="coins"></div>

    <h2>Ontology Graph</h2>
    <div id="graph" class="status">Relationships will appear here</div>

    <script>
        const API = window.location.port === '' ? '/api' : 'http://localhost:8888/api';

        async function loadCoins() {
            try {
                const resp = await fetch(`${API}/coins`);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const coins = await resp.json();

                if (coins.length === 0) {
                    document.getElementById('coins').innerHTML =
                        '<p class="status">No coins yet. Run: cointoss coins fetch</p>';
                    return;
                }

                let html = '<table><tr><th>Rank</th><th>Symbol</th><th>Name</th></tr>';
                for (const c of coins) {
                    html += `<tr>
                        <td class="rank">${c.market_cap_rank ? '#' + c.market_cap_rank : '-'}</td>
                        <td>${c.symbol.toUpperCase()}</td>
                        <td>${c.name}</td>
                    </tr>`;
                }
                html += '</table>';
                document.getElementById('coins').innerHTML = html;
                document.getElementById('status').textContent =
                    `Connected. Tracking ${coins.length} coins.`;
                document.getElementById('status').classList.remove('loading');
            } catch (e) {
                document.getElementById('status').innerHTML =
                    `<span class="error">API unavailable. Start server: cointoss server start</span>`;
            }
        }

        async function loadGraph() {
            try {
                const resp = await fetch(`${API}/ontology/graph`);
                if (!resp.ok) return;
                const graph = await resp.json();
                if (graph.edges.length === 0) {
                    document.getElementById('graph').textContent = 'No relationships defined yet.';
                    return;
                }
                let html = '<table><tr><th>From</th><th>Relationship</th><th>To</th></tr>';
                for (const e of graph.edges) {
                    html += `<tr><td>${e.from}</td><td>${e.type}</td><td>${e.to}</td></tr>`;
                }
                html += '</table>';
                document.getElementById('graph').innerHTML = html;
            } catch (e) {
                // Graph loading is optional
            }
        }

        loadCoins();
        loadGraph();
    </script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/index.html
git commit -m "feat: add placeholder frontend

Static HTML page that fetches from the Tornado API and displays
tracked coins and ontology relationships."
```

---

### Task 14: Wire Everything Together — Final Integration

**Files:**
- Modify: `src/cointoss/server.py` (add static file serving)

- [ ] **Step 1: Add static file serving to server.py**

Add a route to serve the frontend directory. Add to the `make_app` function's route list:

```python
import os

# Add to make_app, after the API routes:
frontend_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(frontend_path):
    handlers.append(
        (r"/(.*)", tornado.web.StaticFileHandler, {"path": frontend_path, "default_filename": "index.html"})
    )
```

Restructure `make_app` to build the handler list, then append static serving:

```python
def make_app(db_path: Path, frontend_dir: Path | None = None) -> tornado.web.Application:
    """Create the Tornado application with all routes."""
    handler_kwargs = {"db_path": db_path}
    handlers: list[tuple] = [
        (r"/api/coins", CoinsListHandler, handler_kwargs),
        (r"/api/coins/([^/]+)", CoinDetailHandler, handler_kwargs),
        (r"/api/coins/([^/]+)/prices", CoinPricesHandler, handler_kwargs),
        (r"/api/portfolio", PortfolioListHandler, handler_kwargs),
        (r"/api/ontology/graph", OntologyGraphHandler, handler_kwargs),
    ]

    if frontend_dir and frontend_dir.is_dir():
        handlers.append(
            (r"/(.*)", tornado.web.StaticFileHandler, {
                "path": str(frontend_dir),
                "default_filename": "index.html",
            })
        )

    return tornado.web.Application(handlers)
```

- [ ] **Step 2: Run full test suite**

```bash
make test
```

Expected: All tests pass.

- [ ] **Step 3: Run full lint**

```bash
make lint
```

Expected: Zero errors.

- [ ] **Step 4: Manual smoke test**

```bash
# Fetch some data
uv run cointoss coins fetch

# List coins
uv run cointoss coins list

# Start server and check http://localhost:8888
uv run cointoss server start
```

Verify the frontend loads and shows the coin list.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: wire frontend serving into Tornado server

Static file handler serves frontend/index.html at root URL.
Full integration: CLI fetches data, API serves it, frontend displays it."
```

---

### Task 15: Final Cleanup and Verification

**Files:**
- Verify all existing files

- [ ] **Step 1: Run full make default (install + lint + test)**

```bash
make
```

Expected: All steps pass with zero errors.

- [ ] **Step 2: Verify CLI help**

```bash
uv run cointoss --help
uv run cointoss coins --help
uv run cointoss portfolio --help
uv run cointoss server --help
```

Expected: Help text shows all commands with descriptions.

- [ ] **Step 3: Verify build**

```bash
uv build
```

Expected: Package builds successfully as wheel and sdist.

- [ ] **Step 4: Final commit if any cleanup was needed**

```bash
git add -A
git commit -m "chore: final cleanup and verification

All lint, tests, and build passing."
```
