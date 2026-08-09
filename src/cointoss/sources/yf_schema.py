"""yfinance info schema discovery: sample tickers, collect field metadata, generate Pydantic models."""

from __future__ import annotations

import csv
import difflib
import json
import keyword
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


def load_tickers(ticker_dir: Path, instrument_type: str) -> list[str]:
    """Load ticker symbols from CSV file for the given instrument type."""
    csv_path = ticker_dir / f"{instrument_type}.csv"
    tickers: list[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
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


_MODEL_NAMES: dict[str, str] = {
    "Stock": "StockInfo",
    "ETF": "ETFInfo",
    "Currency": "CurrencyInfo",
    "Future": "FutureInfo",
    "Index": "IndexInfo",
    "Mutual_Fund": "MutualFundInfo",
}


def _needs_alias(name: str) -> bool:
    """True if field name is not a valid Python identifier or is a keyword."""
    return not name.isidentifier() or keyword.iskeyword(name)


def _python_name(name: str) -> str:
    """Convert an invalid field name to a valid Python identifier."""
    if name[0].isdigit():
        return f"f_{name}"
    if keyword.iskeyword(name):
        return f"{name}_"
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    if sanitized[0].isdigit():
        sanitized = f"f_{sanitized}"
    return sanitized


def _make_field_line(name: str, fi: FieldInfo) -> str:
    """Generate a complete Pydantic field line, with alias for invalid Python identifiers."""
    alias = _needs_alias(name)
    py_name = _python_name(name) if alias else name

    # quoteType is required (not optional)
    if name == "quoteType":
        if alias:
            return f'    {py_name}: str = Field(alias="{name}")'
        return f"    {py_name}: str"

    base = map_field_type(fi.types)
    if base == "Any":
        if alias:
            return f'    {py_name}: Any = Field(None, alias="{name}")'
        return f"    {py_name}: Any = None"

    # base is like "str | None = None" — extract the "str | None" part
    type_part = base.replace(" = None", "")
    if alias:
        return f'    {py_name}: {type_part} = Field(None, alias="{name}")'
    return f"    {py_name}: {base}"


def generate_model_code(field_meta: dict[str, dict[str, FieldInfo]]) -> str:
    """Generate Python source for Pydantic models from collected field metadata.

    Fields present in ALL types with data go in YfInfoBase.
    Remaining fields go in per-type subclasses.
    """
    types_with_fields = {t: fields for t, fields in field_meta.items() if fields}
    if not types_with_fields:
        return ""

    # Common fields: present in ALL types that have valid results
    all_field_sets: list[set[str]] = [set(fields.keys()) for fields in types_with_fields.values()]
    common_fields: set[str] = set[str].intersection(*all_field_sets) if all_field_sets else set()

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

    # Check if any field needs aliasing (for import statement)
    all_fields: set[str] = set()
    for fields in types_with_fields.values():
        all_fields |= fields.keys()
    needs_field_import = any(_needs_alias(f) for f in all_fields)

    pydantic_imports = "BaseModel, ConfigDict"
    if needs_field_import:
        pydantic_imports = "BaseModel, ConfigDict, Field"

    lines: list[str] = [
        '"""yfinance info models -- auto-generated by yf_schema.discover_schema."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, ClassVar",
        "",
        f"from pydantic import {pydantic_imports}",
        "",
        "",
        "class YfInfoBase(BaseModel):",
        '    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")',
    ]
    for fname in sorted(common_meta):
        lines.append(_make_field_line(fname, common_meta[fname]))
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
                lines.append(_make_field_line(fname, type_specific[fname]))
        lines.extend(["", ""])

    return "\n".join(lines).rstrip() + "\n"


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
    output_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _setup_cached_get_info(cache_db: Path) -> Any:
    """Set up namespace-cached get_info. Falls back to direct call if setup fails."""
    try:
        from lythonic import GlobalRef
        from lythonic.compose.engine import StorageConfig
        from lythonic.compose.namespace import Namespace, NsCacheConfig

        cache_db.parent.mkdir(parents=True, exist_ok=True)
        ns = Namespace()
        cfg = NsCacheConfig(
            nsref="yfinance:get_info",
            gref=GlobalRef("cointoss.sources.yahoofinance:get_info"),
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
        run_date=datetime.now(UTC).isoformat(),
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
        old_content = models_path.read_text(encoding="utf-8")
        diff = detect_changes(old_content, model_code)
        if diff is None:
            log.info("No changes detected in yf_models.py")
        else:
            log.info("Changes detected in yf_models.py:\n%s", diff)
            models_path.write_text(model_code, encoding="utf-8")
    else:
        models_path.write_text(model_code, encoding="utf-8")
        log.info("Created yf_models.py")

    # Write manifest
    manifest_path = output_dir / "yf_schema_manifest.json"
    write_manifest(run_result, manifest_path)
    log.info("Wrote manifest to %s", manifest_path)

    return run_result


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
    assert "quoteType: str" in code
    assert "symbol: str | None = None" in code
    assert "price: float | None = None" in code
    assert "nav: float | None = None" in code
    compile(code, "<test>", "exec")


def test_generate_model_code_quote_type_not_optional() -> None:
    fm: dict[str, dict[str, FieldInfo]] = {
        "Stock": {"quoteType": FieldInfo(types={"str"}, total=10)},
    }
    code = generate_model_code(fm)
    assert "quoteType: str\n" in code


def test_generate_model_code_invalid_names() -> None:
    fm: dict[str, dict[str, FieldInfo]] = {
        "Stock": {
            "quoteType": FieldInfo(types={"str"}, total=5),
            "52WeekChange": FieldInfo(types={"float"}, total=5),
            "yield": FieldInfo(types={"float"}, total=5),
        },
    }
    code = generate_model_code(fm)
    # Must compile (no SyntaxError)
    compile(code, "<test>", "exec")
    # Digit-leading field gets f_ prefix and alias
    assert "f_52WeekChange" in code
    assert 'alias="52WeekChange"' in code
    # Keyword gets underscore suffix and alias
    assert "yield_" in code
    assert 'alias="yield"' in code
    assert "Field" in code


def test_generate_model_code_empty() -> None:
    assert generate_model_code({}) == ""


def test_detect_changes_identical() -> None:
    assert detect_changes("same content", "same content") is None


def test_detect_changes_different() -> None:
    old = "line1\nline2\nline3\n"
    new = "line1\nchanged\nline3\n"
    diff = detect_changes(old, new)
    assert diff is not None
    assert "changed" in diff


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
