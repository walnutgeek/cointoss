# MVP Handoff

Written 2026-10-01 (UTC 2026-10-02 04:30) at `722b54a`, after upgrading to woodglue 0.0.7 and
lythonic 0.0.26. Issue #18 is the MVP spec; #19-#21 are closed, #22 is open only for the record
of the live check below. #5 (persistence) is closed and its handoff,
`2026-09-29-persistence-layer-handoff.md`, is superseded by this one.

**State of `main`:** 458 tests, `make lint` clean. Nothing half-finished. Two local commits are
not pushed at the time of writing: the dependency upgrade and this file.

## Read these first

1. `docs/road-map.md`: build order (the MVP is step 4) and the "Still open" list.
2. `CONTEXT.md`: vocabulary, especially Universe Declaration, Survivor and Provisional Bar.
3. ADR-0009 with its 2026-09-29 amendments. These define a CoinGecko daily bar as the price at
   00:00 UTC, explain the bar window, and record that a stored bar is never overwritten.
4. The closing comments on #19, #20 and #21. Each one lists Standouts and Low confidence
   choices.

## The live instance

It runs on this machine as a `systemd --user` service (`cointoss.service`, enabled, linger on).
The data directory is `~/.local/share/cointoss`. On 2026-10-02 04:22 UTC it reported:

| Sweep | When | Result |
| --- | --- | --- |
| 2026-09-30 | 23:03 UTC, the catch-up when the service was installed | 100 admitted, **no bars** (after the bar window), by design |
| 2026-10-01 | 00:05 UTC, scheduled | unchanged, 249 bars |
| 2026-10-02 | 00:05 UTC, scheduled | +1 admitted (101 members, within the 100/120 band), 249 bars |

0 Restatements, 253 Instruments. One coin is skipped on every sweep: `bianrensheng`, whose
symbol `币安人生` normalises to empty.

**The service still runs woodglue 0.0.6 in memory.** `uv sync` replaced the venv, so the next
start runs 0.0.7. Restart it after the changes below land, not before. 0.0.7's lythonic 0.0.26
catch-up and cointoss's own `ExecStartPre` catch-up would otherwise both run. That is harmless,
since the sweep is idempotent for the day, but it is redundant.

The live check for #22 is satisfied: the service is active, JSON-RPC answers with the token
(`runs` returned the three runs above), and scheduled sweeps landed on two consecutive days.
Post that on #22 and close it, then close #18.

## What the upgrade gives us

All five issues filed from cointoss are fixed upstream (woodglue #4-#7, lythonic #12).
woodglue #8 and #9 also landed. Nothing in cointoss broke: tests and lint passed unchanged.
Each fix lets cointoss drop a workaround:

| Upstream change | cointoss workaround it retires | Work |
| --- | --- | --- |
| woodglue #4: `woodglue.apps.rpc.RpcError(code, message, data)` is passed through to the client | `ApiError` already carries a class-level `code`, but it subclasses `Exception`, so clients get `-32603 "Internal error"` | Make `ApiError` subclass `RpcError`, and add a JSON-RPC test asserting that `NotFound` arrives as `-32001` with its message. One-line change, plus the test. |
| woodglue #6: `current_mount.get(None).data_dir` is set while the namespace is built and during trigger runs | `cointoss init` writes `init.data_dir` into `woodglue.yaml`, and the unit exports `COINTOSS_HOME` | Have `CointossApp()` resolve the data dir from `current_mount`, falling back to `resolve_data_dir()`. Drop `init.data_dir` from `fragment_entry()`. Keep `COINTOSS_HOME` for the CLI only. Existing `woodglue.yaml` files with `init.data_dir` must keep working. |
| lythonic #12: a trigger missed while the process was down fires once on start | The `ExecStartPre=-$timeout 120 … cointoss sweep` catch-up, and the `shutil.which("timeout")` resolution when the unit is rendered | Remove `ExecStartPre` and `$timeout` from the template and from `render_unit`. The release notes say to remove external catch-ups "to avoid a double run". `TimeoutStartSec` can go too. Re-render the installed unit with `cointoss init --systemd`, after deleting the old one. |
| woodglue #7: `wgl token [--new]`, and the token is no longer printed on start | `cointoss token` | Keep it: it resolves `--data-dir`/`COINTOSS_HOME` like every other cointoss command. Optionally mention `wgl --data … token --new` for rotation in the README. The token printed by 0.0.6 is already in the journal, so rotate it once with `--new` after the restart. |
| woodglue #5: SIGTERM handled, `wgl.pid` removed | `_server_state` in `cli.py` checks that the pid is alive | Keep the check. It is cheap and still guards against `kill -9`. Its docstring and comment should stop blaming woodglue. |
| woodglue #8: journald logging under systemd, no `wgl.log`, rotation | Nothing, but `~/.local/share/cointoss/wgl.log` (written by 0.0.6) will stop growing | Delete the stale `wgl.log` after the restart. `PYTHONUNBUFFERED=1` in the unit can stay; it still helps the CLI's own output. |
| woodglue #9: a trigger deactivated through the system API stays disabled across restarts | Nothing | Document in the README how to pause the daily sweep (JSON-RPC `system.deactivate_trigger` with `{"namespace": "cointoss", "name": "daily_sweep"}`) and resume it. |

The default log level is now INFO, not DEBUG. That is a breaking change in 0.0.7 and a quieter
journal for us.

lythonic #13 is still open: a `poll_fn` returning `None` is re-polled every second. cointoss
uses `schedule` triggers only, so it does not affect us.

**Suggested ticket:** a single child of #18, "Adopt woodglue 0.0.7 / lythonic 0.0.26 and retire
the workarounds", covering the first three rows plus the README notes. Then restart the service,
rotate the token, and delete `wgl.log`.

## Small fixes noticed while writing this

- The `ApiError` docstring in `app.py` starts with two paragraphs about reads and the sweep lock,
  left by a merge. They belong in the module docstring or on `sweep_lock`. Fix this in the same
  ticket.
- `cointoss status` reports the latest run only. 2026-09-30 has membership but no bars, and
  nothing points that out. `status` could flag days with runs but no bars, which backfill will
  fill.

## What comes next

From `docs/road-map.md`, in order:

1. **History backfill (step 5).** It is already needed. 2026-09-30 has no bars, and any future
   late catch-up leaves a gap the same way. `market_chart` with `days >= 90` for members fills
   only dates that have no bar and never overwrites one (ADR-0009 amendment). Rate limits:
   CoinGecko's free tier is about 30 calls a minute, so a few coins per run, or a separate
   trigger. Decide whether backfill covers every coin listed or only universe members.
2. **UI (step 6).** A small page over the JSON-RPC API: universe today, joins and leaves, a coin's
   price and market-cap history, run health. Decide whether it is a woodglue UI plugin or a
   static page served by woodglue. Check what 0.0.7's `/ui/` offers before choosing.
3. **Portfolio (step 7)**, valued from stored bars.

Still-open items that the next work will touch (full list in the road map):

- **Delisting versus absence.** It needs settling before stocks.
- **Unfolded reads.** `corporate_actions_for`, `restatements_for` and `bars_for` consult only the
  Survivor's Price Sources.
- **No review queue for flagged identity.** Flags are only logged.
- **`save_universe` silently discards** inclusion and exclusion sets that arrive without member
  rows.

## Things that will bite

- **`make test` is not the same as `uv run pytest tests/`.** The Makefile adds doctests. Always
  verify with `make test`.
- **`self.conn.commit()` at the end of every store write method.** Do not bump `SCHEMA_VERSION`;
  there is no released schema yet. **Except:** there is now a live `cointoss.db` on this machine.
  A column added to a table needs a manual `ALTER TABLE` there, or a dropped and re-fetched
  rebuildable table. A change to a durable table (registry, definitions, membership) needs real
  care, because that data cannot be re-fetched. This is the first point where a migration
  matters.
- **lythonic filters take prefix operators:** `ne__instrument=None`, not `instrument__ne=None`.
- **Never fetch `markets` through the cached namespace node** (`fetch_coins_markets` is
  `@require_cache`). It would replay yesterday's listing as today's bars.
- **A sweep after the 3-hour bar window writes no bars on purpose.** Manual `cointoss sweep`
  runs during the day print `no bars`.
- **One sweep at a time.** A second concurrent sweep exits with `SweepInProgress`. It does not
  wait.
- **A CoinGecko pin looks up the coin's CoinGecko id** (through `markets`, highest market cap per
  symbol), so the sweep matches it. A pin made during a CoinGecko outage carries no id, and the
  sweep can then mint a `_2`; such a pin is logged.
