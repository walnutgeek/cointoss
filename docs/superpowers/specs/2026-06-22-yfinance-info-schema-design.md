# yfinance Info Schema Discovery

Discover the schema of `yf.Ticker(...).info` by sampling tickers from a known
ticker list, calling `get_info` through the woodglue namespace cache, and
generating per-instrument-type Pydantic models from observed fields.

## Context

- Ticker source: `tests/data/201709_Samir_Khan_Yahoo_Ticker_Symbols/*.csv`
  (6 CSV files: Stock, ETF, Currency, Future, Index, Mutual_Fund)
- ~245K tickers total; many from 2017 are now obsolete/delisted
- `get_info` is already defined in `src/cointoss/sources/yahoofinance.py`
  and decorated with `@require_cache` / `@nsnode`

## Namespace Configuration

### Register yahoofinance module as a fragment

In `tests/woodglue.yaml`, add a module-level fragment entry for the entire
`yahoofinance` module. The `gref` uses module path without `:` separator:

```yaml
namespaces:
  cointoss:
    entries:
      # existing CoinGeckoClient fragment ...
      - type: fragment
        gref: "cointoss.sources.yahoofinance"
        nsref: "yfinance:"
        configs:
          get_info:
            min_ttl: 3.0
            max_ttl: 5.0
          get_prices:
            min_ttl: 3.0
            max_ttl: 5.0
          lookup:
            min_ttl: 3.0
            max_ttl: 5.0
```

All cached results are stored by the woodglue namespace cache and persist
for 3-5 days. The schema discovery script calls `get_info` through the
namespace, so repeated runs reuse cached responses.

## Schema Discovery Script

New file: `src/cointoss/sources/yf_schema.py`

### Entry point

```python
def discover_schema(
    sample_size: int = 200,
    forced: dict[str, list[str]] | None = None,
    ticker_dir: Path = DEFAULT_TICKER_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> SchemaRunResult
```

### Sampling

For each instrument type (Stock, ETF, Currency, Future, Index, Mutual_Fund):

1. Load tickers from the corresponding CSV (`Ticker` column).
2. If `forced` dict includes tickers for this type, include those first.
3. Fill remaining slots (up to `sample_size`) with random tickers from the
   CSV, using a fixed seed (`random.seed(42)`) for reproducibility.
4. Deduplicate (forced tickers may already be in the random sample).

### Calling get_info

For each sampled ticker, call `get_info(ticker)` through the namespace.
Classify the result:

- **valid**: `quoteType` field is present and not None, and dict has >= 5 keys
- **obsolete**: `quoteType` is missing/None OR dict has < 5 keys
- **error**: exception raised (HTTP errors, timeouts, etc.)

Log obsolete and error tickers. Only valid results contribute to schema
inference.

### Obsolete Detection Function

```python
def is_obsolete(info: dict[str, Any]) -> bool
```

Returns `True` if:
- `info` is empty
- `info` has fewer than 5 keys
- `info.get("quoteType")` is None

Exported for standalone use (e.g., filtering ticker lists).

### Field Metadata Collection

For each valid `info` dict, record per instrument type:

- Field name
- Set of Python type names observed (e.g., `{"str", "NoneType"}`)
- Null count vs total count
- Up to 3 example non-null values (for documentation/reference)

Stored in memory as:
```python
field_meta: dict[str, dict[str, FieldInfo]]
# e.g. {"Stock": {"symbol": FieldInfo(types={"str"}, null_count=0, total=200, examples=["AAPL", ...])}}
```

## Manifest

Each run writes a manifest file: `src/cointoss/sources/yf_schema_manifest.json`

```json
{
  "run_date": "2026-06-22T15:30:00Z",
  "sample_size": 200,
  "seed": 42,
  "forced_tickers": {"Stock": ["AAPL", "MSFT"], "ETF": ["SPY"]},
  "results": {
    "Stock": {
      "sampled": ["AAPL", "MSFT", "OEDV", "..."],
      "valid": ["AAPL", "MSFT", "..."],
      "obsolete": ["OEDV", "..."],
      "errors": {"XYZZ": "HTTPError 404"}
    },
    "ETF": {},
    "Currency": {},
    "Future": {},
    "Index": {},
    "Mutual_Fund": {}
  }
}
```

No raw JSON responses stored. Cached responses are accessible via the
namespace cache for any ticker in the `valid` or `obsolete` lists.

## Model Generation

### Output file

`src/cointoss/sources/yf_models.py`

### Structure

- `YfInfoBase(BaseModel)`: fields present in all (or nearly all) instrument
  types. `model_config = ConfigDict(extra="ignore")`.
- `StockInfo(YfInfoBase)`: fields specific to stocks
- `ETFInfo(YfInfoBase)`: fields specific to ETFs
- `CurrencyInfo(YfInfoBase)`, `FutureInfo(YfInfoBase)`,
  `IndexInfo(YfInfoBase)`, `MutualFundInfo(YfInfoBase)`

### Field type mapping

| Observed Python types | Pydantic annotation |
|-----------------------|---------------------|
| `{str}` | `str \| None = None` |
| `{int}` | `int \| None = None` |
| `{float}` or `{int, float}` | `float \| None = None` |
| `{bool}` | `bool \| None = None` |
| `{dict}` | `dict[str, Any] \| None = None` |
| `{list}` | `list[Any] \| None = None` |
| mixed (e.g., `{str, int}`) | `Any` |

All fields default to `None` since Yahoo returns different subsets per ticker.
Exception: `quoteType` is `str` (not optional) since we filter obsolete tickers
that lack it.

### Change Detection

Before writing `yf_models.py`:

1. Read existing file content (if it exists).
2. Generate new content in memory.
3. Compare byte-for-byte.
4. If identical: print "no changes detected", skip write.
5. If different: write the new file, print a summary of what changed
   (fields added, fields removed, type changes). Use simple line-level
   diff, not AST parsing.

## Files Touched

| File | Action |
|------|--------|
| `tests/woodglue.yaml` | Add yfinance module fragment entry |
| `src/cointoss/sources/yf_schema.py` | New: discovery script |
| `src/cointoss/sources/yf_models.py` | New: generated Pydantic models |
| `src/cointoss/sources/yf_schema_manifest.json` | New: run manifest |

## Not In Scope

- Calling `get_info` for all 245K tickers (only sample)
- Modeling nested objects within `info` (leave as `dict[str, Any]`)
- Historical schema tracking (only current vs previous comparison)
- Updating the 2017 ticker CSVs with current data
