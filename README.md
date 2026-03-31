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
