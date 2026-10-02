# cointoss

Crypto and stock portfolio tracker and research platform.

Built on [lythonic](https://github.com/walnutgeek/lythonic) (SQLite ORM, DAG
composition, CLI) and [woodglue](https://github.com/walnutgeek/woodglue)
(async server, Caddy integration).

A running instance sweeps CoinGecko `markets` once a day, shortly after 00:00 UTC, records
the configured universes (by default `cg-top-100`: enter at rank 100, exit at 120) and the
day's bar per coin (close, volume, market cap), and serves the result over JSON-RPC on
`127.0.0.1`.

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

- `cointoss.yaml`: the declared universes; `top_n`, the width of the listing each sweep
  fetches; and `bar_window_hours` (default 3), how long after 00:00 UTC a sweep still writes
  the day's bars.
- `woodglue.yaml`: the `cointoss` namespace with its API exposed and its engine running, the
  daily sweep trigger (`5 0 * * *`, UTC), host `127.0.0.1`, port `5321`, and auth on.
- `cointoss.db`, with the declared universes stored.
- `auth.db`, holding the bearer token.

With `--systemd` it also writes `~/.config/systemd/user/cointoss.service`, pointing at this
checkout's `.venv/bin`. The template is `src/cointoss/cointoss.service`.

`init` is idempotent. It never overwrites a config or unit file that already exists, so edit
them freely, and re-run `init` to validate them. Changes take effect when the service restarts.

### A second instance on the same host

`--unit NAME` installs the unit as `NAME.service` (only with `--systemd`). `--port N` and
`--schedule CRON` (5-field, UTC) set the port and the `daily_sweep` schedule in a newly
written `woodglue.yaml`. For example, a dev checkout beside an instance installed from PyPI,
each with its own virtualenv:

```bash
cointoss init --systemd                                  # the installed instance: cointoss.service
uv run cointoss init --systemd --data-dir ~/.local/share/cointoss-dev \
  --unit cointoss-dev --port 5322 --schedule "35 0 * * *"
systemctl --user daemon-reload && systemctl --user enable --now cointoss-dev
```

Give each instance its own `--data-dir`, unit name and port. A different schedule keeps the
two from calling CoinGecko at the same minute. A schedule that first fires later than
`bar_window_hours` after 00:00 UTC is accepted with a warning: those sweeps record membership
but no bars. If `woodglue.yaml` already exists, `init` keeps it and says which of `--port` and
`--schedule` it differs from.

## Start

```bash
systemctl --user daemon-reload && systemctl --user enable --now cointoss
loginctl enable-linger "$USER"    # keep it running while you are logged out
```

The unit runs `wgl --data=<data dir> start` in the foreground and restarts it on failure.
If the daily sweep was missed while the service was down, it fires once when the server
starts. The sweep does nothing if today is already swept.

Without systemd, run `make serve` or `uv run wgl --data ~/.local/share/cointoss start`.

## Check

```bash
uv run cointoss status             # server, last Evaluation Run per universe, last bar
journalctl --user -u cointoss -e   # service log (without systemd: wgl.log in the data dir)
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
`start`, `end`) and `runs` (`universe`, `limit`). Dates are `YYYY-MM-DD`. A request the store
cannot answer is a JSON-RPC error: `-32001` for an unknown universe or Instrument, or a date
with nothing stored; `-32002` before `init`; `-32602` for a malformed parameter.

To replace the token with a new one (the old one stops working at once):

```bash
uv run wgl --data ~/.local/share/cointoss token --new
```

## Pause the daily sweep

```bash
curl -s http://127.0.0.1:5321/rpc \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"system.deactivate_trigger",
       "params":{"namespace":"cointoss","name":"daily_sweep"}}'
```

The trigger stays paused across restarts. Resume it with `system.activate_trigger` and the
same params. A sweep missed while paused is not caught up.

## Sweep by hand

```bash
uv run cointoss sweep            # today's (UTC) sweep; a no-op if already done
uv run cointoss sweep --force    # re-fetch today's listing anyway
```

A sweep records CoinGecko's current listing for today (UTC), so it cannot run for a past date.
A day's bar is its price at 00:00 UTC, so bars are written only by a sweep within
`bar_window_hours` of midnight; a later sweep records membership and says `no bars`. A
re-sweep never replaces a bar already stored for the day. Days without bars are left for a
future backfill.

Only one sweep runs at a time per data directory: a second one, such as a manual sweep while
the scheduled one runs, exits at once with `another sweep holds .../sweep.lock`.

## Where the data lives

The data directory is `--data-dir`, else `$COINTOSS_HOME`, else `~/.local/share/cointoss`.
The server takes it from `wgl --data`, which the unit sets, along with `COINTOSS_HOME`. If you
move the directory, delete the unit and re-run `init --data-dir NEW --systemd`. A
`woodglue.yaml` written before woodglue 0.0.7 names the directory in the fragment's
`init.data_dir`. That still works, but it overrides `wgl --data`, so remove it before moving.

| File | What |
| --- | --- |
| `cointoss.db` | Everything cointoss knows: Instruments, universes, membership, runs, bars. |
| `cointoss.yaml`, `woodglue.yaml` | Configuration. |
| `auth.db` | API tokens. |
| `mounts/` | woodglue engine state: triggers, DAG runs, cache. Rebuildable. |
| `wgl.log`, `wgl.pid` | Server log (only when not under systemd, which logs to the journal) and pid. |
| `sweep.lock` | Held while a sweep runs. Safe to delete when none is. |

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
