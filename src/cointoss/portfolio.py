"""Portfolios, Trades, and the positions derived from them.

Implements ADR-0001 as amended 2026-10-30. A `Portfolio` is owned by one woodglue User and holds
single-leg `Trade`s with a signed quantity and no cash leg. A Trade is never edited or deleted:
a mistaken one is voided, stays recorded, and is skipped by every position.

There is no Portfolio Snapshot yet. `derive_positions` replays the Trades on every read, which
is instant for a few hand-entered Trades, and is pure so that what a position means is stated
here once rather than in SQL.

Quantities and costs are `float`, as every other number in the store is (ADR-0008). This is not
a ledger; exact arithmetic would be a decision about `Trade` alone.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import ClassVar, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator

from cointoss.instrument import InstrumentId
from cointoss.prices import as_utc

__all__ = [
    "PORTFOLIO_HELD",
    "Portfolio",
    "PortfolioKind",
    "PortfolioPosition",
    "Trade",
    "TradeKind",
    "UserId",
    "derive_positions",
    "end_of_day",
]

PORTFOLIO_HELD = "portfolio-held"
"""The Universe Definition every traded Instrument is included in, so the sweep prices it. It
has no rank rule and is never declared in configuration: trades alone add to it."""

UserId = str
"""A woodglue User's `id` (`Caller.user_id`), such as `usr_1a2b3c4d5e6f`. Users live in
woodglue's own database, so it is a reference by value rather than a foreign key."""


class PortfolioKind(StrEnum):
    """Whether a Portfolio holds real assets, simulated ones, or a hypothetical."""

    REAL = "real"
    PAPER = "paper"
    WHAT_IF = "what_if"


class TradeKind(StrEnum):
    """A Trade's provenance: imported, an adjustment, or a bootstrap where history is unknown."""

    REAL = "real"
    SYNTHETIC = "synthetic"
    OPENING_BALANCE = "opening_balance"


class Portfolio(BaseModel):
    """A logical container for holdings, owned by one User. `name` is unique per owner."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: int
    owner: UserId
    name: str
    kind: PortfolioKind
    custodian: str | None = None


class Trade(BaseModel):
    """One single-leg Trade: a signed quantity of one Instrument at one moment.

    Positive quantity is a buy or deposit, negative a sell or withdrawal. `traded_at` is held in
    UTC; a naive time is read as already UTC. `cost` is the total USD paid for the Trade, or
    received for a disposal, when known.

    >>> t = Trade(
    ...     id=1,
    ...     portfolio_id=1,
    ...     instrument_id=InstrumentId("crypto.native.btc"),
    ...     traded_at=datetime(2024, 3, 1, 12, 0),
    ...     quantity=0.5,
    ...     kind=TradeKind.REAL,
    ... )
    >>> t.traded_at.isoformat(), t.voided
    ('2024-03-01T12:00:00+00:00', False)
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: int
    portfolio_id: int
    instrument_id: InstrumentId
    traded_at: datetime
    quantity: float
    kind: TradeKind
    cost: float | None = None
    note: str | None = None
    voided_at: datetime | None = None

    @field_validator("traded_at", "voided_at")
    @classmethod
    def _in_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)

    @field_validator("quantity")
    @classmethod
    def _nonzero(cls, value: float) -> float:
        if not math.isfinite(value) or value == 0:
            raise ValueError(f"a trade's quantity must be finite and nonzero, got {value}")
        return value

    @field_validator("cost")
    @classmethod
    def _cost(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"a trade's cost must be finite and not negative, got {value}")
        return value

    @property
    def voided(self) -> bool:
        return self.voided_at is not None


class PortfolioPosition(NamedTuple):
    """The aggregate quantity of one Instrument in a Portfolio, with its average cost per unit.

    `average_cost` is None when no acquisition counted toward the position carried a cost.
    """

    instrument_id: InstrumentId
    quantity: float
    average_cost: float | None


def end_of_day(day: date) -> datetime:
    """The first UTC moment after `day`, which a position as of `day` counts up to.

    >>> end_of_day(date(2024, 2, 29)).isoformat()
    '2024-03-01T00:00:00+00:00'
    """
    return datetime.combine(day + timedelta(days=1), time(), UTC)


def derive_positions(trades: Iterable[Trade], as_of: date) -> list[PortfolioPosition]:
    """Positions as of the end of `as_of` in UTC, one per Instrument, ordered by Instrument Id.

    Voided Trades are skipped whenever they were voided: a void says the Trade never happened.
    Instruments are aggregated as given, so a caller folds Supersession first.

    Average cost is the USD cost of the acquisitions that carry one divided by their quantity.
    Acquisitions without a cost are ignored, and a disposal removes its share of the costed
    quantity at the average, so it does not move the average. Once a position reaches zero or
    goes short its cost is forgotten: a short has no average cost, and an acquisition that
    reopens a long position counts only the part of it that is long. A position whose trades
    net to zero is still returned.

    >>> btc = InstrumentId("crypto.native.btc")
    >>> def trade(day: int, quantity: float, cost: float | None = None) -> Trade:
    ...     return Trade(id=day, portfolio_id=1, instrument_id=btc, kind=TradeKind.REAL,
    ...                  traded_at=datetime(2024, 3, day, 12), quantity=quantity, cost=cost)
    >>> history = [trade(1, 1.0, 30_000.0), trade(2, 1.0), trade(3, 2.0, 90_000.0), trade(4, -1.0)]
    >>> derive_positions(history, date(2024, 3, 4))
    [PortfolioPosition(instrument_id=InstrumentId('crypto.native.btc'), quantity=3.0, average_cost=40000.0)]
    >>> derive_positions(history[1:2], date(2024, 3, 4))[0].average_cost is None
    True
    """
    cutoff = end_of_day(as_of)
    quantity: dict[InstrumentId, float] = {}
    costed_quantity: dict[InstrumentId, float] = {}
    average: dict[InstrumentId, float] = {}
    for t in sorted(trades, key=lambda t: t.traded_at):
        if t.voided or t.traded_at >= cutoff:
            continue
        i = t.instrument_id
        before = quantity.get(i, 0.0)
        after = before + t.quantity
        quantity[i] = after
        if after <= 0:
            costed_quantity.pop(i, None)
            average.pop(i, None)
        elif t.quantity < 0:
            if i in average:
                costed_quantity[i] *= after / before
        elif t.cost is not None:
            # Of an acquisition that covers a short, only the part that ends up long is held.
            held = min(t.quantity, after)
            had = costed_quantity.get(i, 0.0)
            costed_quantity[i] = had + held
            average[i] = (average.get(i, 0.0) * had + t.cost * held / t.quantity) / (had + held)
    return [PortfolioPosition(i, quantity[i], average.get(i)) for i in sorted(quantity)]


## Tests

_BTC = InstrumentId("crypto.native.btc")
_ETH = InstrumentId("crypto.native.eth")


def _trade(
    instrument: InstrumentId,
    traded_at: datetime,
    quantity: float,
    cost: float | None = None,
    voided: bool = False,
) -> Trade:
    return Trade(
        id=0,
        portfolio_id=1,
        instrument_id=instrument,
        traded_at=traded_at,
        quantity=quantity,
        kind=TradeKind.REAL,
        cost=cost,
        voided_at=datetime(2024, 6, 1, tzinfo=UTC) if voided else None,
    )


def test_a_position_counts_trades_up_to_the_end_of_the_utc_day_and_none_after() -> None:
    day = date(2024, 3, 1)
    trades = [
        _trade(_BTC, datetime(2024, 3, 1, 0, 0, tzinfo=UTC), 1.0),
        _trade(_BTC, end_of_day(day) - timedelta(microseconds=1), 2.0),
        _trade(_BTC, end_of_day(day), 4.0),
    ]
    assert derive_positions(trades, day - timedelta(days=1)) == []
    assert derive_positions(trades, day) == [PortfolioPosition(_BTC, 3.0, None)]
    assert derive_positions(trades, day + timedelta(days=1)) == [PortfolioPosition(_BTC, 7.0, None)]


def test_a_voided_trade_counts_toward_nothing() -> None:
    when = datetime(2024, 3, 1, tzinfo=UTC)
    trades = [_trade(_BTC, when, 1.0, 100.0), _trade(_BTC, when, 5.0, 1.0, voided=True)]
    assert derive_positions(trades, date(2024, 3, 1)) == [PortfolioPosition(_BTC, 1.0, 100.0)]
    only_voided = [_trade(_ETH, when, 5.0, voided=True)]
    assert derive_positions(only_voided, date(2024, 3, 1)) == []


def test_average_cost_ignores_trades_without_a_cost_and_disposals() -> None:
    when = datetime(2024, 3, 1, tzinfo=UTC)
    trades = [
        _trade(_ETH, when, 2.0, 4_000.0),
        _trade(_ETH, when, 10.0),
        _trade(_ETH, when, 2.0, 8_000.0),
        _trade(_ETH, when, -3.0, 9_000.0),
        _trade(_BTC, when, 1.0),
    ]
    assert derive_positions(trades, date(2024, 3, 1)) == [
        PortfolioPosition(_BTC, 1.0, None),
        PortfolioPosition(_ETH, 11.0, 3_000.0),
    ]


def test_average_cost_starts_afresh_once_a_position_closes() -> None:
    def at(day: int) -> datetime:
        return datetime(2024, 3, day, tzinfo=UTC)

    reopened = [
        _trade(_BTC, at(1), 1.0, 100.0),
        _trade(_BTC, at(2), -1.0),
        _trade(_BTC, at(3), 1.0, 200.0),
    ]
    assert derive_positions(reopened, date(2024, 3, 3)) == [PortfolioPosition(_BTC, 1.0, 200.0)]
    short = [_trade(_BTC, at(1), 1.0, 100.0), _trade(_BTC, at(2), -3.0)]
    assert derive_positions(short, date(2024, 3, 2)) == [PortfolioPosition(_BTC, -2.0, None)]
    covered = [*short, _trade(_BTC, at(3), 4.0, 1_200.0)]
    assert derive_positions(covered, date(2024, 3, 3)) == [PortfolioPosition(_BTC, 2.0, 300.0)]


def test_a_trade_refuses_a_zero_quantity_and_a_negative_cost() -> None:
    when = datetime(2024, 3, 1, tzinfo=UTC)
    for quantity, cost in ((0.0, None), (math.nan, None), (1.0, -5.0)):
        try:
            _trade(_BTC, when, quantity, cost)
            raise AssertionError(f"quantity {quantity} at cost {cost} should be refused")
        except ValueError:
            pass
