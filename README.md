# cointoss

Crypto and stock portfolio tracker and research platform.

Built on [lythonic](https://github.com/walnutgeek/lythonic) (SQLite ORM, DAG
composition, CLI) and [woodglue](https://github.com/walnutgeek/woodglue)
(async server, Caddy integration).

A running instance sweeps CoinGecko `markets` once a day, shortly after 00:00 UTC, records
the configured universes (by default `cg-top-100`: enter at rank 100, exit at 120) and a
snapshot bar per coin, and serves the result over JSON-RPC on `127.0.0.1`.

## Install

From a checkout, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras
```

This puts `cointoss` and woodglue's `wgl` in `.venv/bin`. Run them with `uv run`, or by
absolute path, as the systemd unit does.

## Initialize

```bash
uv run cointoss init --systemd
```

This creates the data directory and writes into it:

- `cointoss.yaml`: the declared universes and `top_n`, the width of the listing each sweep
  fetches.
- `woodglue.yaml`: the `cointoss` namespace with its API exposed and its engine running, the
  daily sweep trigger (`5 0 * * *`, UTC), host `127.0.0.1`, port `5321`, and auth on.
- `cointoss.db`, with the declared universes stored.
- `auth.db`, holding the bearer token.

With `--systemd` it also writes `~/.config/systemd/user/cointoss.service`, pointing at this
checkout's `.venv/bin`. The template is `src/cointoss/cointoss.service`.

`init` is idempotent. It never overwrites a config or unit file that already exists, so edit
them freely, and re-run `init` to validate them. Changes take effect when the service restarts.

## Start

```bash
systemctl --user daemon-reload && systemctl --user enable --now cointoss
loginctl enable-linger "$USER"    # keep it running while you are logged out
```

The unit runs `wgl --data=<data dir> start` in the foreground and restarts it on failure.
Before starting the server, it runs `cointoss sweep`. That call does nothing if today is
already swept, and it catches up a sweep missed while the service was down. It is limited to
two minutes, and a failure there never stops the API from starting.

Without systemd, run `make serve` or `uv run wgl --data ~/.local/share/cointoss start`.

## Check

```bash
uv run cointoss status             # server, last Evaluation Run per universe, last bar
journalctl --user -u cointoss -e   # service log; woodglue also writes wgl.log in the data dir
```

The API needs the bearer token:

```bash
TOKEN=$(uv run cointoss token)
curl -s http://127.0.0.1:5321/rpc \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"cointoss.data:universes","params":{}}'
```

The other methods are `cointoss.data:members` (`universe`, `date`), `changes` (`universe`,
`start`, `end`), `bars` (`instrument_id`, `start`, `end`), `universes_of` (`instrument_id`,
`start`, `end`) and `runs` (`universe`, `limit`). Dates are `YYYY-MM-DD`.

## Sweep by hand

```bash
uv run cointoss sweep            # today's (UTC) sweep; a no-op if already done
uv run cointoss sweep --force    # re-fetch today's listing anyway
```

A sweep records CoinGecko's current listing as today's bar, so it cannot run for a past date.
`--date D` is refused unless D is today in UTC. A day missed entirely stays missing. Run late
in the day, a sweep's bar is the price at that moment, and its `fetched_at` records when that
was.

## Where the data lives

The data directory is `--data-dir`, else `$COINTOSS_HOME`, else `~/.local/share/cointoss`.
The unit sets `COINTOSS_HOME`, and `woodglue.yaml` names the directory too, in the fragment's
`init.data_dir`. If you move the directory, update both files, or delete them and re-run
`init --data-dir NEW --systemd`.

| File | What |
| --- | --- |
| `cointoss.db` | Everything cointoss knows: Instruments, universes, membership, runs, bars. |
| `cointoss.yaml`, `woodglue.yaml` | Configuration. |
| `auth.db` | API tokens. |
| `mounts/` | woodglue engine state: triggers, DAG runs, cache. Rebuildable. |
| `wgl.log`, `wgl.pid` | Server log and pid. |

## Backup

`cointoss.db` is the only file that cannot be rebuilt. Back it up while the service runs with
SQLite's online backup:

```bash
sqlite3 ~/.local/share/cointoss/cointoss.db ".backup '/path/to/cointoss-$(date -u +%F).db'"
```

Or stop the service (`systemctl --user stop cointoss`), copy `cointoss.db`, and start it again.
A plain copy of a live database can miss writes still in its WAL file (`cointoss.db-wal`).

## Development

```bash
make install   # Install dependencies
make lint      # Run linters
make test      # Run tests
make serve     # Run woodglue in the foreground against $COINTOSS_HOME
```
