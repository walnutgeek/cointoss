# yfinance Info Schema Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover the schema of `yf.Ticker(...).info` by sampling tickers from CSV files, calling `get_info` through a lythonic namespace cache, and generating per-instrument-type Pydantic models.

**Architecture:** A standalone discovery script (`yf_schema.py`) samples tickers from 6 CSV files (Stock, ETF, Currency, Future, Index, Mutual\_Fund), calls `get_info` through a lythonic `Namespace` with `NsCacheConfig` for 3-5 day caching, classifies results as valid/obsolete/error, collects field metadata, and generates Pydantic models (`yf_models.py`). A JSON manifest records run outcomes.

**Tech Stack:** Python 3.11+, Pydantic, lythonic namespace/caching (`Namespace`, `NsCacheConfig`, `StorageConfig`), yfinance, CSV stdlib

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `tests/woodglue.yaml` | Create | Namespace config with CoinGecko + yfinance fragments |
| `src/cointoss/sources/yf_schema.py` | Create | Schema discovery: sampling, calling, collecting, generating |
| `src/cointoss/sources/yf_models.py` | Generated | Pydantic models per instrument type |
| `src/cointoss/sources/yf_schema_manifest.json` | Generated | Run manifest with ticker outcomes |

## Reference: Existing Patterns

- **Pydantic models** in `src/cointoss/sources/coingecko.py` use `ClassVar[ConfigDict]`:
  ```python
  from typing import ClassVar
  model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
  ```
- **Namespace cache setup** (from lythonic tests):
  ```python
  from lythonic.compose.namespace import Namespace, NsCacheConfig
  from lythonic.compose.engine import StorageConfig
  ns = Namespace()
  cfg = NsCacheConfig(nsref="ref:name", gref="module:func", min_ttl=3.0, max_ttl=5.0)
  ns.register("module:func", nsref="ref:name", config=cfg)
  ns.mount(StorageConfig(cache_db=Path("cache.db")))
  node = ns.get("ref:name")
  result = node(ticker="AAPL")  # cached!
  ```
- **Inline tests** (Tier 2): plain `test_*` functions below `## Tests` comment, no pytest imports.
- **Ticker CSVs**: All have `Ticker` column. Stock has `Ticker,Name,Exchange,Category Name,Country`; others have `Ticker,Name,Exchange`.

---

### Task 1: Create namespace config

**Files:**
- Create: `tests/woodglue.yaml`

- [ ] **Step 1: Create `tests/woodglue.yaml`**

```yaml
namespaces:
  cointoss:
    entries:
      - type: fragment
        gref: "cointoss.sources.coingecko:CoinGeckoClient"
        nsref: "coingecko:"
        init:
          rpm: 25
        configs:
          fetch_coin_list:
            min_ttl: 0.9
            max_ttl: 1.0
          fetch_coins_markets:
            min_ttl: 0.9
            max_ttl: 1.0
          fetch_coin:
            min_ttl: 0.9
            max_ttl: 1.0
          fetch_coin_ohlc:
            min_ttl: 0.9
            max_ttl: 1.0
      - type: fragment
        gref: "cointoss.sources.yahoofinance"
        nsref: "yfinance:"
        defaults:
          cache:
            min_ttl: 3.0
            max_ttl: 5.0
        configs:
          get_prices:
            min_ttl: 0.9
            max_ttl: 1.0
```

- [ ] **Step 2: Commit**

```bash
git add tests/woodglue.yaml
git commit -m "feat: add woodglue namespace config for CoinGecko and yfinance"
```

---

### Task 2: Core data structures and is_obsolete

**Files:**
- Create: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Write the full file with data structures, is_obsolete, and inline tests**

```python
from __future__ import annotations

import csv
import difflib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TICKER_DIR = PROJECT_ROOT / "tests" / "data" / "201709_Samir_Khan_Yahoo_Ticker_Symbols"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent

INSTRUMENT_TYPES = ("Stock", "ETF", "Currency", "Future", "Index", "Mutual_Fund")


@dataclass
class FieldInfo:
    """Metadata collected for a single field across all sampled tickers of one type."""

    types: set[str] = field(default_factory=set)
    null_count: int = 0
    total: int = 0
    examples: list[Any] = field(default_factory=list)


@dataclass
class TypeResult:
    """Outcome of sampling one instrument type."""

    sampled: list[str] = field(default_factory=list)
    valid: list[str] = field(default_factory=list)
    obsolete: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class SchemaRunResult:
    """Full result of a schema discovery run."""

    run_date: str
    sample_size: int
    seed: int
    forced_tickers: dict[str, list[str]]
    results: dict[str, TypeResult]
    field_meta: dict[str, dict[str, FieldInfo]]


def is_obsolete(info: dict[str, Any]) -> bool:
    """Check if a ticker's info dict indicates an obsolete/delisted ticker.

    Returns True if info is empty, has fewer than 5 keys, or quoteType is missing/None.
    """
    if not info:
        return True
    if len(info) < 5:
        return True
    if info.get("quoteType") is None:
        return True
    return False


## Tests


def test_is_obsolete_empty() -> None:
    assert is_obsolete({}) is True


def test_is_obsolete_few_keys() -> None:
    assert is_obsolete({"a": 1, "b": 2, "c": 3}) is True


def test_is_obsolete_no_quote_type() -> None:
    assert is_obsolete({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}) is True


def test_is_obsolete_valid() -> None:
    assert is_obsolete({"quoteType": "EQUITY", "a": 1, "b": 2, "c": 3, "d": 4}) is False


def test_is_obsolete_none_quote_type() -> None:
    assert is_obsolete({"quoteType": None, "a": 1, "b": 2, "c": 3, "d": 4}) is True
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 5 `test_is_obsolete_*` tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add core data structures and is_obsolete"
```

---

### Task 3: Ticker loading and sampling

**Files:**
- Modify: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Add `load_tickers` and `sample_tickers` functions**

Insert after `is_obsolete`, before the `## Tests` section:

```python
def load_tickers(ticker_dir: Path, instrument_type: str) -> list[str]:
    """Load ticker symbols from CSV file for the given instrument type."""
    csv_path = ticker_dir / f"{instrument_type}.csv"
    tickers: list[str] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("Ticker", "").strip()
            if ticker:
                tickers.append(ticker)
    return tickers


def sample_tickers(
    all_tickers: list[str],
    sample_size: int,
    forced: list[str] | None = None,
    seed: int = 42,
) -> list[str]:
    """Sample tickers: forced tickers first, then random fill up to sample_size."""
    forced = forced or []
    forced_set = set(forced)
    remaining = [t for t in all_tickers if t not in forced_set]
    rng = random.Random(seed)
    slots = max(0, sample_size - len(forced))
    sampled = rng.sample(remaining, min(slots, len(remaining)))
    return (list(forced) + sampled)[:sample_size]
```

- [ ] **Step 2: Add inline tests**

Append to the `## Tests` section:

```python
def test_load_tickers() -> None:
    tickers = load_tickers(DEFAULT_TICKER_DIR, "Stock")
    assert len(tickers) > 100
    assert "AAPL" in tickers


def test_sample_tickers_size() -> None:
    all_t = [f"T{i}" for i in range(500)]
    result = sample_tickers(all_t, sample_size=10, seed=42)
    assert len(result) == 10


def test_sample_tickers_forced() -> None:
    all_t = [f"T{i}" for i in range(500)]
    result = sample_tickers(all_t, sample_size=5, forced=["AAPL", "MSFT"], seed=42)
    assert result[0] == "AAPL"
    assert result[1] == "MSFT"
    assert len(result) == 5


def test_sample_tickers_reproducible() -> None:
    all_t = [f"T{i}" for i in range(500)]
    r1 = sample_tickers(all_t, sample_size=10, seed=42)
    r2 = sample_tickers(all_t, sample_size=10, seed=42)
    assert r1 == r2


def test_sample_tickers_forced_dedup() -> None:
    all_t = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
    result = sample_tickers(all_t, sample_size=4, forced=["AAPL"], seed=42)
    assert result[0] == "AAPL"
    assert len(result) == 4
    assert len(set(result)) == 4
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 10 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add ticker loading and sampling"
```

---

### Task 4: Field type mapping and metadata collection

**Files:**
- Modify: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Add `map_field_type` and `collect_field_meta`**

Insert after `sample_tickers`, before `## Tests`:

```python
_SIMPLE_TYPES: dict[frozenset[str], str] = {
    frozenset({"str"}): "str | None = None",
    frozenset({"int"}): "int | None = None",
    frozenset({"float"}): "float | None = None",
    frozenset({"int", "float"}): "float | None = None",
    frozenset({"bool"}): "bool | None = None",
    frozenset({"dict"}): "dict[str, Any] | None = None",
    frozenset({"list"}): "list[Any] | None = None",
}


def map_field_type(observed_types: set[str]) -> str:
    """Map observed Python type names to a Pydantic field annotation string."""
    clean = observed_types - {"NoneType"}
    if not clean:
        return "Any"
    return _SIMPLE_TYPES.get(frozenset(clean), "Any")


def collect_field_meta(info: dict[str, Any], meta: dict[str, FieldInfo]) -> None:
    """Accumulate field metadata from one valid info dict."""
    for key, value in info.items():
        if key not in meta:
            meta[key] = FieldInfo()
        fi = meta[key]
        fi.total += 1
        if value is None:
            fi.null_count += 1
        else:
            fi.types.add(type(value).__name__)
            if len(fi.examples) < 3:
                fi.examples.append(value)
```

- [ ] **Step 2: Add inline tests**

Append to `## Tests`:

```python
def test_map_field_type_str() -> None:
    assert map_field_type({"str"}) == "str | None = None"


def test_map_field_type_int() -> None:
    assert map_field_type({"int"}) == "int | None = None"


def test_map_field_type_float() -> None:
    assert map_field_type({"float"}) == "float | None = None"


def test_map_field_type_int_float() -> None:
    assert map_field_type({"int", "float"}) == "float | None = None"


def test_map_field_type_bool() -> None:
    assert map_field_type({"bool"}) == "bool | None = None"


def test_map_field_type_dict() -> None:
    assert map_field_type({"dict"}) == "dict[str, Any] | None = None"


def test_map_field_type_list() -> None:
    assert map_field_type({"list"}) == "list[Any] | None = None"


def test_map_field_type_mixed() -> None:
    assert map_field_type({"str", "int"}) == "Any"


def test_map_field_type_none_only() -> None:
    assert map_field_type({"NoneType"}) == "Any"


def test_collect_field_meta() -> None:
    meta: dict[str, FieldInfo] = {}
    collect_field_meta({"symbol": "AAPL", "price": 150.0, "name": None}, meta)
    collect_field_meta({"symbol": "MSFT", "price": 300, "volume": 1000}, meta)
    assert meta["symbol"].types == {"str"}
    assert meta["symbol"].null_count == 0
    assert meta["symbol"].total == 2
    assert meta["price"].types == {"float", "int"}
    assert meta["name"].null_count == 1
    assert meta["name"].total == 1
    assert meta["volume"].total == 1
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 21 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add field type mapping and metadata collection"
```

---

### Task 5: Model code generation

**Files:**
- Modify: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Add `generate_model_code` and helper**

Insert after `collect_field_meta`, before `## Tests`:

```python
_MODEL_NAMES: dict[str, str] = {
    "Stock": "StockInfo",
    "ETF": "ETFInfo",
    "Currency": "CurrencyInfo",
    "Future": "FutureInfo",
    "Index": "IndexInfo",
    "Mutual_Fund": "MutualFundInfo",
}


def _field_annotation(name: str, fi: FieldInfo) -> str:
    """Build annotation string for a single field."""
    if name == "quoteType":
        return "str"
    return map_field_type(fi.types)


def generate_model_code(field_meta: dict[str, dict[str, FieldInfo]]) -> str:
    """Generate Python source for Pydantic models from collected field metadata.

    Fields present in ALL types with data go in YfInfoBase.
    Remaining fields go in per-type subclasses.
    """
    types_with_fields = {t: fields for t, fields in field_meta.items() if fields}
    if not types_with_fields:
        return ""

    # Common fields: present in ALL types that have valid results
    all_field_sets = [set(fields.keys()) for fields in types_with_fields.values()]
    common_fields = set.intersection(*all_field_sets) if all_field_sets else set()

    # Merge type info for common fields across all types
    common_meta: dict[str, FieldInfo] = {}
    for fname in sorted(common_fields):
        merged = FieldInfo()
        for fields in types_with_fields.values():
            fi = fields[fname]
            merged.types |= fi.types
            merged.null_count += fi.null_count
            merged.total += fi.total
            for ex in fi.examples:
                if len(merged.examples) < 3:
                    merged.examples.append(ex)
        common_meta[fname] = merged

    lines: list[str] = [
        '"""yfinance info models -- auto-generated by yf_schema.discover_schema."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, ClassVar",
        "",
        "from pydantic import BaseModel, ConfigDict",
        "",
        "",
        "class YfInfoBase(BaseModel):",
        '    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")',
    ]
    for fname in sorted(common_meta):
        annotation = _field_annotation(fname, common_meta[fname])
        lines.append(f"    {fname}: {annotation}")
    lines.extend(["", ""])

    # Per-type models in canonical order
    for itype in INSTRUMENT_TYPES:
        if itype not in types_with_fields:
            continue
        model_name = _MODEL_NAMES[itype]
        type_specific = {
            k: v for k, v in types_with_fields[itype].items() if k not in common_fields
        }
        lines.append(f"class {model_name}(YfInfoBase):")
        if not type_specific:
            lines.append("    pass")
        else:
            for fname in sorted(type_specific):
                annotation = _field_annotation(fname, type_specific[fname])
                lines.append(f"    {fname}: {annotation}")
        lines.extend(["", ""])

    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 2: Add inline tests**

Append to `## Tests`:

```python
def test_generate_model_code_basic() -> None:
    fm: dict[str, dict[str, FieldInfo]] = {
        "Stock": {
            "quoteType": FieldInfo(types={"str"}, total=10),
            "symbol": FieldInfo(types={"str"}, total=10),
            "price": FieldInfo(types={"float"}, total=10, null_count=2),
        },
        "ETF": {
            "quoteType": FieldInfo(types={"str"}, total=5),
            "symbol": FieldInfo(types={"str"}, total=5),
            "nav": FieldInfo(types={"float"}, total=5),
        },
    }
    code = generate_model_code(fm)
    assert "class YfInfoBase(BaseModel):" in code
    assert "class StockInfo(YfInfoBase):" in code
    assert "class ETFInfo(YfInfoBase):" in code
    # quoteType and symbol are common -> in base
    assert "quoteType: str" in code
    assert "symbol: str | None = None" in code
    # price only in Stock, nav only in ETF
    assert "price: float | None = None" in code
    assert "nav: float | None = None" in code
    # Must be valid Python
    compile(code, "<test>", "exec")


def test_generate_model_code_quote_type_not_optional() -> None:
    fm: dict[str, dict[str, FieldInfo]] = {
        "Stock": {"quoteType": FieldInfo(types={"str"}, total=10)},
    }
    code = generate_model_code(fm)
    # quoteType should be `str`, not `str | None = None`
    assert "quoteType: str\n" in code


def test_generate_model_code_empty() -> None:
    assert generate_model_code({}) == ""
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 24 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add model code generation"
```

---

### Task 6: Change detection and manifest

**Files:**
- Modify: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Add `detect_changes` and `write_manifest`**

Insert after `generate_model_code`, before `## Tests`:

```python
def detect_changes(old_content: str, new_content: str) -> str | None:
    """Compare old and new model file content. Returns unified diff or None if identical."""
    if old_content == new_content:
        return None
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile="old", tofile="new")
    return "".join(diff)


def write_manifest(result: SchemaRunResult, output_path: Path) -> None:
    """Write the run manifest as JSON (no raw API responses, just ticker outcomes)."""
    manifest = {
        "run_date": result.run_date,
        "sample_size": result.sample_size,
        "seed": result.seed,
        "forced_tickers": result.forced_tickers,
        "results": {
            itype: {
                "sampled": tr.sampled,
                "valid": tr.valid,
                "obsolete": tr.obsolete,
                "errors": tr.errors,
            }
            for itype, tr in result.results.items()
        },
    }
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
```

- [ ] **Step 2: Add inline tests**

Append to `## Tests`:

```python
def test_detect_changes_identical() -> None:
    assert detect_changes("same content", "same content") is None


def test_detect_changes_different() -> None:
    old = "line1\nline2\nline3\n"
    new = "line1\nchanged\nline3\n"
    diff = detect_changes(old, new)
    assert diff is not None
    assert "changed" in diff
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 26 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add change detection and manifest writing"
```

---

### Task 7: discover_schema entry point with namespace caching

**Files:**
- Modify: `src/cointoss/sources/yf_schema.py`

- [ ] **Step 1: Explore namespace setup API**

Read these files to understand the exact registration/mount/call API:
- `/Users/sergeyk/w/lythonic/src/lythonic/compose/namespace.py` — look for `Namespace` class, `register()`, `mount()`, `get()` methods, and `NsCacheConfig` class
- `/Users/sergeyk/w/lythonic/src/lythonic/compose/engine.py` — look for `StorageConfig` class

The expected pattern (from lythonic test suite) is:
```python
from lythonic.compose.namespace import Namespace, NsCacheConfig
from lythonic.compose.engine import StorageConfig

ns = Namespace()
cfg = NsCacheConfig(
    nsref="yfinance:get_info",
    gref="cointoss.sources.yahoofinance:get_info",
    min_ttl=3.0,
    max_ttl=5.0,
)
ns.register("cointoss.sources.yahoofinance:get_info", nsref="yfinance:get_info", config=cfg)
ns.mount(StorageConfig(cache_db=cache_db_path))
node = ns.get("yfinance:get_info")
result = node(ticker="AAPL")
```

**Verify this works.** If the API differs (different constructor args, different method names), adapt accordingly. The cache\_db path should be `PROJECT_ROOT / ".cache" / "yf_cache.db"`.

**Fallback:** If namespace setup proves problematic, fall back to calling `get_info` directly from `cointoss.sources.yahoofinance` (no caching). The script will still work; repeated runs will re-fetch from Yahoo.

- [ ] **Step 2: Add `_setup_cached_get_info` and `discover_schema`**

Insert after `write_manifest`, before `## Tests`:

```python
def _setup_cached_get_info(cache_db: Path) -> Any:
    """Set up namespace-cached get_info. Falls back to direct call if setup fails."""
    try:
        from lythonic.compose.namespace import Namespace, NsCacheConfig
        from lythonic.compose.engine import StorageConfig

        cache_db.parent.mkdir(parents=True, exist_ok=True)
        ns = Namespace()
        cfg = NsCacheConfig(
            nsref="yfinance:get_info",
            gref="cointoss.sources.yahoofinance:get_info",
            min_ttl=3.0,
            max_ttl=5.0,
        )
        ns.register(
            "cointoss.sources.yahoofinance:get_info",
            nsref="yfinance:get_info",
            config=cfg,
        )
        ns.mount(StorageConfig(cache_db=cache_db))
        node = ns.get("yfinance:get_info")
        log.info("Using namespace-cached get_info (cache: %s)", cache_db)
        return node
    except Exception:
        log.warning("Namespace cache setup failed, calling get_info directly", exc_info=True)
        from cointoss.sources.yahoofinance import get_info

        return get_info


def discover_schema(
    sample_size: int = 200,
    forced: dict[str, list[str]] | None = None,
    ticker_dir: Path = DEFAULT_TICKER_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_db: Path | None = None,
) -> SchemaRunResult:
    """Run schema discovery: sample tickers, call get_info, collect fields, generate models."""
    forced = forced or {}
    if cache_db is None:
        cache_db = PROJECT_ROOT / ".cache" / "yf_cache.db"

    get_info_fn = _setup_cached_get_info(cache_db)

    results: dict[str, TypeResult] = {}
    all_field_meta: dict[str, dict[str, FieldInfo]] = {}

    for itype in INSTRUMENT_TYPES:
        log.info("Processing %s...", itype)
        all_tickers = load_tickers(ticker_dir, itype)
        sampled = sample_tickers(all_tickers, sample_size, forced.get(itype), seed=42)

        tr = TypeResult(sampled=sampled)
        meta: dict[str, FieldInfo] = {}

        for i, ticker in enumerate(sampled):
            try:
                info = get_info_fn(ticker=ticker)
            except Exception as exc:
                tr.errors[ticker] = str(exc)
                log.warning("%s: error: %s", ticker, exc)
                continue

            if is_obsolete(info):
                tr.obsolete.append(ticker)
                log.debug("%s: obsolete", ticker)
            else:
                tr.valid.append(ticker)
                collect_field_meta(info, meta)

            if (i + 1) % 50 == 0:
                log.info("%s: %d/%d done", itype, i + 1, len(sampled))

        results[itype] = tr
        all_field_meta[itype] = meta
        log.info(
            "%s: %d valid, %d obsolete, %d errors",
            itype,
            len(tr.valid),
            len(tr.obsolete),
            len(tr.errors),
        )

    run_result = SchemaRunResult(
        run_date=datetime.now(timezone.utc).isoformat(),
        sample_size=sample_size,
        seed=42,
        forced_tickers=forced,
        results=results,
        field_meta=all_field_meta,
    )

    # Generate models
    model_code = generate_model_code(all_field_meta)
    models_path = output_dir / "yf_models.py"
    if models_path.exists():
        old_content = models_path.read_text()
        diff = detect_changes(old_content, model_code)
        if diff is None:
            log.info("No changes detected in yf_models.py")
        else:
            log.info("Changes detected in yf_models.py:\n%s", diff)
            models_path.write_text(model_code)
    else:
        models_path.write_text(model_code)
        log.info("Created yf_models.py")

    # Write manifest
    manifest_path = output_dir / "yf_schema_manifest.json"
    write_manifest(run_result, manifest_path)
    log.info("Wrote manifest to %s", manifest_path)

    return run_result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    forced_arg: dict[str, list[str]] = {
        "Stock": ["AAPL", "MSFT", "GOOG"],
        "ETF": ["SPY", "QQQ"],
    }
    result = discover_schema(forced=forced_arg)
    valid_total = sum(len(tr.valid) for tr in result.results.values())
    error_total = sum(len(tr.errors) for tr in result.results.values())
    print(f"Done: {valid_total} valid, {error_total} errors")
    sys.exit(0 if error_total == 0 else 1)
```

- [ ] **Step 3: Run lint**

```bash
make lint
```

Fix any ruff/basedpyright issues. Expected issues:
- `_setup_cached_get_info` returns `Any` — this is intentional, add `# pyright: ignore` if needed
- The `node` variable from `ns.get()` may need a type annotation

- [ ] **Step 4: Run tests (unit tests only, not the full discovery)**

```bash
uv run pytest src/cointoss/sources/yf_schema.py -v
```

Expected: All 26 inline tests PASS. The `discover_schema` function is not tested here (it requires network).

- [ ] **Step 5: Commit**

```bash
git add src/cointoss/sources/yf_schema.py
git commit -m "feat(yf_schema): add discover_schema entry point with namespace caching"
```

---

### Task 8: Run discovery, verify output, commit

**Files:**
- Generated: `src/cointoss/sources/yf_models.py`
- Generated: `src/cointoss/sources/yf_schema_manifest.json`

**Important:** This task makes ~1200 network calls to Yahoo Finance (200 tickers x 6 types). It will take a while. The namespace cache means subsequent runs reuse cached responses.

- [ ] **Step 1: Run the discovery script**

```bash
uv run python -m cointoss.sources.yf_schema
```

Watch the output. Expect many obsolete tickers (the CSVs are from 2017). If the script errors out completely, debug and fix the issue. Common problems:
- Namespace API mismatch (check `_setup_cached_get_info` fallback path)
- Yahoo rate limiting (yfinance handles retries internally but may need adjustment)

- [ ] **Step 2: Verify generated models compile**

```bash
uv run python -c "import cointoss.sources.yf_models; print('OK')"
```

- [ ] **Step 3: Read and verify `yf_models.py`**

Check that the generated file has:
- `class YfInfoBase(BaseModel)` with `model_config = ConfigDict(extra="ignore")`
- `quoteType: str` (not optional)
- Per-type subclasses matching `INSTRUMENT_TYPES`
- All other fields are `X | None = None` format

- [ ] **Step 4: Read and verify manifest**

Read `src/cointoss/sources/yf_schema_manifest.json`. Check:
- Has `run_date`, `sample_size: 200`, `seed: 42`
- Each instrument type has `sampled`, `valid`, `obsolete`, `errors`
- Forced tickers ("AAPL", "MSFT", "GOOG") appear in Stock sampled list

- [ ] **Step 5: Run lint and full test suite**

```bash
make lint && make test
```

Fix any issues in generated code (ruff formatting, import order, etc.).

- [ ] **Step 6: Commit generated files**

```bash
git add src/cointoss/sources/yf_models.py src/cointoss/sources/yf_schema_manifest.json
git commit -m "feat: generate yfinance info models from schema discovery"
```
