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
