# Cointoss

Crypto and stock portfolio tracker and research platform, built on
[lythonic](https://github.com/walnutgeek/lythonic) (SQLite ORM, DAG composition, CLI) and
[woodglue](https://github.com/walnutgeek/woodglue) (async server, auth, UIs).

A running instance sweeps CoinGecko `markets` once a day, shortly after 00:00 UTC, records the
configured universes (by default `cg-top-100`) and the day's bar per coin (close, volume,
market cap), and serves the result over JSON-RPC. The
[README](https://github.com/walnutgeek/cointoss#readme) is the operator's guide; this site
adds the design records and the API reference.

## Install

From a checkout, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras
```

From PyPI, `uv tool install 'cointoss[ui]'`. The `ui` extra (pandas and Streamlit) is what the
Dataset explorer needs.

## Initialize and start

```bash
uv run cointoss init --systemd
systemctl --user daemon-reload && systemctl --user enable --now cointoss
```

`init` creates the data directory (`~/.local/share/cointoss` unless `--data-dir` or
`$COINTOSS_HOME` says otherwise) with `cointoss.yaml`, `woodglue.yaml`, `cointoss.db` and
`auth.db`, and writes the systemd unit. It is idempotent and never overwrites an existing file.
See the README for
[a second instance on the same host](https://github.com/walnutgeek/cointoss#a-second-instance-on-the-same-host),
[backup and restore](https://github.com/walnutgeek/cointoss#backup), and
[where the data lives](https://github.com/walnutgeek/cointoss#where-the-data-lives).

## Check

```bash
uv run cointoss status             # server, last Evaluation Run per universe, last bar
journalctl --user -u cointoss -e   # service log
```

## The API

Every method is JSON-RPC at `/rpc` and needs a bearer token (`uv run cointoss token`). Besides
the point reads (`universes`, `members`, `changes`, `bars`, `universes_of`, `runs`), there are
two generic ones (see [ADR-0010](adr/0010-datasets-generic-long-framedata-read-api.md)):

- `cointoss.data:datasets` lists the Datasets (`instruments`, `bars`, `returns`, `membership`,
  `changes`, `runs`, and `trades`, `positions` and `portfolio_value`, which hold only the
  caller's own Portfolios), each with its typed columns and the JSON Schema of its params.
- `cointoss.data:frame` (`dataset`, `params`) returns one as a long table.

From Python, with the `ui` extra, `await cointoss.datasets.load(client, "bars", start=...,
end=...)` returns a pandas DataFrame. See [`cointoss.datasets`](reference/datasets.md) and
[`cointoss.app`](reference/app.md).

## Explore

With the `ui` extra, woodglue serves a Streamlit Dataset explorer at `/ui/explore/`: pick a
Dataset, set its parameters, and get a table, a chart, a pivot and CSV downloads. It reads only
through `datasets` and `frame`, as the signed-in User. Its logic is
[`cointoss.explorer`](reference/explorer.md); the Streamlit script itself is
`cointoss/explorer_app.py`.

## Further reading

- [Glossary](glossary.md): the domain language.
- [Road map](road-map.md): where cointoss is going and what is still open.
- Decisions: the ADRs, starting with
  [ADR-0001](adr/0001-unified-instrument-and-trade-sor.md).
- [Privacy](privacy.md): what the instances this project runs collect and keep.
