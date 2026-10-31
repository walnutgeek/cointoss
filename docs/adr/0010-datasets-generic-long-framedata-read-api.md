# Datasets: a Generic, Long, FrameData Read API

Status: accepted, amended 2026-10-23.

Context: The read nodes (`members`, `changes`, `bars`, `universes_of`, `runs`, `universes`) each answer one question in their own shape: one Instrument's bars, a universe's members nested in a view. An explorer built on them has to know every node, and every new collection (Yahoo, a Portfolio, derived returns) would mean new UI code. The explorer is to be a Streamlit app that discovers what it can read and lets a person filter, chart and pivot it. Woodglue Grants are per namespace, not per method, so splitting reads across nodes buys no access control.

Decision:
- A **Dataset** is a named, typed, long-form table, defined in code in `cointoss.datasets` and read through `cointoss.store`. Each definition has a name, a description, a pydantic params model, its columns, and `read(store, params) -> FrameData`.
- Two nodes on the existing `CointossApp` fragment: `datasets()` lists every Dataset with its columns and the JSON Schema of its params; `frame(dataset, params)` validates `params` against that Dataset's model and returns a lythonic `FrameData` (`columns` plus row-major `data`). The existing read nodes stay; Datasets sit beside them.
- Every Dataset is long: one row per fact, a fixed column list. Pivoting to a date-by-Instrument matrix is the client's job.
- Each column carries a type (`date`, `datetime`, `str`, `float`, `int`, `bool`), a description, and a role (`time`, `entity`, `measure`, `attribute`), so a client can pick axes and series without guessing from names.
- Time-ranged Datasets require `start` and `end` (`YYYY-MM-DD`), and take an optional `instruments` list and an optional `source`. A `universe` filter means membership on each row's own date; `ever: true` widens it to Instruments that were members at any point in the range.
- A response is capped at 250k rows. Over the cap is `BadRequest` (`-32602`) naming the matching count; there is no paging. An unknown Dataset is `NotFound` (`-32001`), invalid params are `BadRequest` with the first error on one line, and an empty result is an empty frame, not an error.
- Rows carry `instrument_id` and `symbol`; the rest of an Instrument's attributes live in the `instruments` Dataset.
- The first Datasets: `instruments`, `bars`, `returns`, `membership` (one row per membership interval), `changes` (one row per date and Instrument that joined or left), `runs`.
- `returns` is derived on read, never stored: simple close-to-close over consecutive stored Bars of one source, never spliced across sources. A gap yields one return over the whole gap with a `gap_days` column. The read fetches one Bar before `start` so `start` has a return.
- Datasets are additive: new Datasets and columns any time; a rename or removal gets a new Dataset name and a note in the release notes.
- `cointoss.datasets.load(client, name, **params) -> pd.DataFrame` applies the column types to a frame. pandas is imported only there, from the `[ui]` extra.
- Amended 2026-10-23: `returns` resolves the source per date as `bars` does. A date's return pairs the Bar `bars` shows for it with the Bar for the Instrument's previous date that has one, so there is one row per date and Instrument. Where the resolved source switches between the two, that date has no return rather than one spliced across vendors. With `source`, it is that source's consecutive Bars, as before.
- Amended 2026-10-23: a Dataset read skips an Instrument whose Supersession chain cycles or ends at an Instrument not stored, and logs one warning naming it, whether it was named in `instruments` or reached through a range or universe. One bad chain does not fail a whole table. The single-Instrument read nodes still raise.
- Amended 2026-10-23: `load` is async, because `WoodglueClient.call` is. A sync caller such as Streamlit wraps it in `asyncio.run`.
- Amended 2026-10-31: an owned Dataset holds one User's records, the Portfolios (`trades`, `positions`, `portfolio_value`, ADR-0001 amendment of 2026-10-30). `frame` reads it for the Caller's User id, which no param can set, and the namespace's `call` Grant is all it needs, so the explorer reads as its viewer through the Delegated Token. Ownership is handled as the portfolio writes handle it: another User's Portfolio named in params is `NotFound`, exactly as one that does not exist; with auth disabled the anonymous Caller reads as `anonymous`; an in-process read with no Caller is `Unauthorized` rather than an empty frame, which would look like a User with no Portfolios. A position with no Bar on its date has a null close and value, never zero or an earlier close; `portfolio_value` is then null for that Portfolio and date, with `unpriced` counting the open positions without a close, since a partial sum would chart as a fall in value.

Considered Options:
- Replace the read nodes with Datasets. One way to read, at the cost of breaking released API for no gain to the callers that want one answer.
- Records (a dict per row) or column-major arrays. Records repeat every key per row; column-major needs a type lythonic does not have. `FrameData` exists and converts to pandas, polars and pyarrow.
- Arrow IPC. Smallest and typed, but binary does not travel in JSON-RPC and would need its own route in woodglue.
- A node per Dataset. Typed docs in OpenAPI, but a UI would need code per node to discover and drive it.
- A read-only SQL node. The most flexible, and it couples every client to the table layout and breaks ADR-0008's rule that only `cointoss.store` touches SQL.
- Wide frames, or a `shape` parameter. Ready for a covariance matrix, but the column list changes as members come and go.
- Paging with a cursor. State the explorer does not need at this scale.

Consequences: A Dataset is a read projection, not a system of record: it is neither a Series nor a Snapshot, and nothing writes through it. `returns` here answers the road map's "stored or derived" question for exploration only; the estimator may still want stored returns. The row cap bounds memory per call, so a year of bars for a few hundred coins fits but a decade will need a narrower request. The JSON Schema in `datasets()` is the only documentation of a Dataset's params, since the generic `frame` node's own signature says nothing about them.
