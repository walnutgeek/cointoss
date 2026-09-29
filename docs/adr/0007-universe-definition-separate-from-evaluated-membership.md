# Universe Definition is Separate From Evaluated Membership

Status: accepted.

Context: ADR-0002 and ADR-0003 made `UniverseSeries` a Series of instrument lists and left construction unspecified: the Series records what something else produced, and nothing produced anything. Two universes are now wanted. A stock universe is a hand-maintained list of Yahoo tickers. A crypto universe is "the big coins by market cap, with the ability to edit." Both are recipes, and a recipe is a mutable thing with no date axis, while a Series is append-only and dated. Collapsing them makes "I changed my mind about the threshold" and "the market moved" the same event.

Decision:
- A `UniverseDefinition` is a named, editable recipe, revision-logged exactly as `RiskModel` is under ADR-0005: every edit bumps a monotonic counter and appends a `UniverseDefinitionRevision`, and every `UniverseSeries` entry stamps the revision it was produced under.
- One recipe shape covers both universes: an optional rank rule, an Inclusion set, and an Exclusion set. Membership at a run is `(rule output U inclusions) - exclusions`. A manual universe is the case where the rule is absent. `kind` is derivable and therefore not stored.
- The rule is expressed in market-cap *rank* with a hysteresis band, `enter_rank` and `exit_rank`, not an absolute market-cap threshold. An absolute threshold makes universe size a function of the market: a broad drawdown empties the universe and the Series records a mass exodus that is really one macro event. Rank normalises the market-wide move away. Only one formulation is offered, because optional mutually-exclusive recipe fields are the defect `RiskParameters.RaggedRecipe` already exists to catch.
- Overrides are standing policy, not one-day acts. An Exclusion holds until a later revision removes it, including across stretches when the rule would not have admitted the instrument anyway. There is no date axis on an override; the revision's `changed_at` is the date axis.
- A manually added member is pinned to an Instrument Id at edit time, and the symbol as typed and its source are kept alongside for provenance and display. A ticker is a lease, not an identity; resolving it on every run would let a reassigned ticker silently change what an old recipe means.
- Resolution failure does not reject the edit. The member is stored with a null Instrument Id, contributes nothing to the Series, and is retried. Unresolved is a predicate over the member rows, not a separate queue table.
- The rule leg cannot be pinned, because its output is not known until the run. Pinned overrides alongside unpinned rule output is an asymmetry in the recipe, and an honest one.
- Evaluation cadence is not part of the recipe. It is scheduling, so changing it does not mint a revision.
- An `EvaluationRun` records each execution - definition, revision, run time, source as-of, outcome, and counts. Membership records what is true; the run log records that the job ran.

Considered Options:
- One type, with the "definition" being simply the latest Series entry. Trivially smaller, and it destroys the ability to answer whether an instrument left because the rule fired or because someone edited the list.
- Rule as a seed: materialise once into a manual list and edit freely thereafter. The first manual edit permanently kills the automation the threshold was built for, and the universe rots as the market moves.
- Rule proposes, human approves each admission and drop. A workflow rather than a data model, and strictly buildable later on top of this ADR, since `UniverseSeries.change()` already produces the diff such a queue would display.
- Recipe holds raw tickers, resolved on every evaluation. Literal to the request and subject to ticker reuse changing the meaning of an old recipe.
- Recipe holds Instrument Ids only, resolution required before an edit is accepted. Stable, but a cold registry blocks editing.
- Plain `top_n` with no hysteresis. One parameter fewer, and a coin on the boundary writes a Series entry every time it wobbles, burying the entries that mean something.

Consequences: Hysteresis makes evaluation path-dependent - membership is a function of the recipe, today's data, and the previous entry - so a past universe cannot be recomputed from the recipe alone without replaying from the start. This is consistent with ADR-0005: a Series is a system of record, not a cache, and the Series is the answer rather than a derivation of one. A pinned member keeps displaying the symbol as typed while the Series keeps producing the pinned Instrument Id, so a renamed ticker looks stale unless the UI reads the current symbol from Ticker History. Adding a ticker to a universe is one of the few places an `Observation` is genuinely constructed, and is therefore a natural home for ADR-0004's mandatory FIGI attempt.
