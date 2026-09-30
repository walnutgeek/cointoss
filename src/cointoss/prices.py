"""Price history: Bars, Corporate Actions, and the Restatement log that keeps them honest.

Implements ADR-0009. A Bar is keyed by instrument, source and session date, so two sources that
disagree about a close each keep their own row rather than one overwriting the other. Only
`close` is required: a CoinGecko point is a close and a volume and nothing else, and dressing it
up as a full candle with invented open, high and low would make a synthetic candle
indistinguishable from a real one.

One vintage of history is kept, stamped with `fetched_at`. What a re-fetch changed is recorded
separately as a Restatement rather than by keeping every vintage, which is why the comparison
below has to be careful: a split legitimately rescales every bar before it, so a naive float
comparison would file a Restatement for every date in the series and bury the real anomalies.
`detect_restatements` therefore asks whether the ratio between the stored and incoming value is
the cumulative split factor implied by the actions dated after that bar, and only reports a
change that neither that factor nor float noise explains.

Yahoo's "unadjusted" prices are already split-adjusted -- `auto_adjust=False` withholds only the
dividend adjustment -- so the split rescale is a fact about the stored history, not something
this module could avoid by asking for raw prices.

Pure: no network, no clock, no database. Fetch times and detection times are supplied by the
caller, which keeps the module testable and makes replaying a recorded frame ordinary.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from cointoss import FrameData
from cointoss.instrument import InstrumentId, Source

if TYPE_CHECKING:
    # Type-only so the pure mapping module does not drag in the HTTP stack the adapter needs.
    from cointoss.sources.coingecko import MarketChart

__all__ = [
    "PRICE_FIELDS",
    "RELATIVE_TOLERANCE",
    "Bar",
    "BarKeyMismatch",
    "CorporateAction",
    "CorporateActionKind",
    "MalformedValue",
    "MissingColumn",
    "PriceError",
    "Restatement",
    "as_utc",
    "bars_from_market_chart",
    "bars_from_yahoo",
    "detect_restatements",
    "is_provisional",
    "split_factor_after",
]

RELATIVE_TOLERANCE = 1e-6
"""How far two values for one field may drift, relative to their size, before it is a change.

Relative rather than absolute so a coin quoted at 0.0003 and a share quoted at 400 are held to
the same standard: an absolute epsilon would either wave through every real move in the cheap
one or file the expensive one's rounding as a restatement.
"""

PRICE_FIELDS = ("open", "high", "low", "close", "adj_close")
"""The Bar fields a split rescales downward. `volume` rescales upward and is handled apart."""


"""`InstrumentId` as a Pydantic field.

The type declares no core schema of its own and `cointoss.instrument` owns it, so the parsing
that `InstrumentId.__new__` already does is attached here rather than bolted onto the type.
"""

_YAHOO_PRICE_COLUMNS = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "adj_close": "Adj Close",
}


class PriceError(Exception):
    """Base class for price history errors."""


class MissingColumn(PriceError):
    """A source frame does not carry a column the mapping requires."""


class MalformedValue(PriceError):
    """A Bar or Corporate Action carries a value that cannot be a price, size or ratio."""


class BarKeyMismatch(PriceError):
    """Two bars offered for comparison are not two vintages of one bar."""


class CorporateActionKind(StrEnum):
    """What a Corporate Action did. A dividend carries its amount, a split its ratio."""

    DIVIDEND = "dividend"
    SPLIT = "split"


class Bar(BaseModel):
    """One session's prices for one instrument, as one source reported them.

    `close` is the only required price. Open, high, low, adjusted close and volume are nullable
    because a close-only source -- CoinGecko's `market_chart` is the one in hand -- has nothing
    to put in them, and a null says so where a fabricated value would not.

    `source` and `fetched_at` are part of the record rather than of the fetch job: a
    heterogeneous series stays legible, and a vintage stays identifiable even though only one is
    kept.

    >>> b = Bar(
    ...     instrument_id=InstrumentId("crypto.eth.usdc"),
    ...     source=Source.COINGECKO,
    ...     bar_date=date(2024, 6, 5),
    ...     close=1.0004,
    ...     volume=8.2e8,
    ...     fetched_at=datetime(2024, 6, 6, 0, 0),
    ... )
    >>> b.open is None and b.adj_close is None
    True
    >>> b.key
    (InstrumentId('crypto.eth.usdc'), <Source.COINGECKO: 'coingecko'>, datetime.date(2024, 6, 5))
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    instrument_id: InstrumentId
    source: Source
    bar_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    adj_close: float | None = None
    volume: float | None = None
    fetched_at: datetime

    @model_validator(mode="after")
    def _validate(self) -> Bar:
        for name in (*PRICE_FIELDS, "volume"):
            value = getattr(self, name)
            if value is None:
                continue
            if math.isnan(value) or math.isinf(value):
                raise MalformedValue(f"{self.instrument_id} {self.bar_date}: {name} is {value}")
            if value < 0:
                raise MalformedValue(f"{self.instrument_id} {self.bar_date}: {name} is negative")
        return self

    @property
    def key(self) -> tuple[InstrumentId, Source, date]:
        """What identifies the bar. Two sources on one date are two bars, not a conflict."""
        return (self.instrument_id, self.source, self.bar_date)


class CorporateAction(BaseModel):
    """A dividend or a split, as one source reported it.

    Stored as its own record so a stored history that changed has a reason on file. Only
    non-zero rows exist: a source that reports a zero dividend on every ordinary day is saying
    nothing happened, and storing that would bury the days something did.

    >>> CorporateAction(
    ...     instrument_id=InstrumentId("stock.us.nvda"),
    ...     source=Source.YAHOO,
    ...     action_date=date(2024, 6, 10),
    ...     kind=CorporateActionKind.SPLIT,
    ...     value=10.0,
    ... ).kind
    <CorporateActionKind.SPLIT: 'split'>
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    instrument_id: InstrumentId
    source: Source
    action_date: date
    kind: CorporateActionKind
    value: float

    @model_validator(mode="after")
    def _validate(self) -> CorporateAction:
        if not math.isfinite(self.value) or self.value <= 0:
            raise MalformedValue(
                f"{self.instrument_id} {self.action_date}: {self.kind} value must be positive, "
                f"got {self.value}"
            )
        return self


class Restatement(BaseModel):
    """A stored value a re-fetch changed, with no corporate action to explain it.

    One record per field, not per bar, so the log says what moved rather than only that
    something did.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    instrument_id: InstrumentId
    source: Source
    bar_date: date
    field: str
    old: float | None
    new: float | None
    detected_at: datetime


def _column_index(frame: FrameData, column: str) -> int:
    try:
        return frame.columns.index(column)
    except ValueError:
        raise MissingColumn(f"frame has no {column!r} column: {frame.columns}") from None


def _optional_index(frame: FrameData, column: str) -> int | None:
    return frame.columns.index(column) if column in frame.columns else None


def _as_date(value: Any) -> date:
    """The adapter inserts `date` as ISO strings; accept a real date too rather than refuse it."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _as_float(value: Any) -> float | None:
    """A missing cell and a NaN both mean the source had nothing to say for that field."""
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def _utc_day(timestamp_ms: int) -> date:
    """The UTC calendar date a CoinGecko millisecond timestamp falls in."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date()


def bars_from_yahoo(
    instrument_id: InstrumentId,
    frame: FrameData,
    fetched_at: datetime,
) -> tuple[list[Bar], list[CorporateAction]]:
    """Map one Yahoo daily frame into Bars and Corporate Actions in a single pass.

    Reads the `date` column `cointoss.sources.yahoofinance.get_prices` inserts, plus Yahoo's own
    `Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits`. Only `date` and
    `Close` are required; a frame without the action columns yields bars and no actions.

    Zero dividend and zero split cells are ordinary days, so they produce no record. Splits are
    reported as a ratio, dividends as an amount per share; both go into `value` under their own
    kind rather than into two differently shaped records.
    """
    date_at = _column_index(frame, "date")
    close_at = _column_index(frame, "Close")
    price_at = {
        name: (close_at if name == "close" else _optional_index(frame, column))
        for name, column in _YAHOO_PRICE_COLUMNS.items()
    }
    volume_at = _optional_index(frame, "Volume")
    action_at = {
        CorporateActionKind.DIVIDEND: _optional_index(frame, "Dividends"),
        CorporateActionKind.SPLIT: _optional_index(frame, "Stock Splits"),
    }

    bars: list[Bar] = []
    actions: list[CorporateAction] = []
    for row in frame.data:
        bar_date = _as_date(row[date_at])
        close = _as_float(row[close_at])
        if close is None:
            # A session Yahoo has no close for carries no bar; the other columns describe it.
            continue
        prices = {
            name: (_as_float(row[at]) if at is not None else None)
            for name, at in price_at.items()
            if name != "close"
        }
        bars.append(
            Bar(
                instrument_id=instrument_id,
                source=Source.YAHOO,
                bar_date=bar_date,
                close=close,
                volume=_as_float(row[volume_at]) if volume_at is not None else None,
                fetched_at=fetched_at,
                **prices,
            )
        )
        for kind, at in action_at.items():
            if at is None:
                continue
            value = _as_float(row[at])
            if value is None or value == 0:
                continue
            actions.append(
                CorporateAction(
                    instrument_id=instrument_id,
                    source=Source.YAHOO,
                    action_date=bar_date,
                    kind=kind,
                    value=value,
                )
            )
    return bars, actions


def is_provisional(bar: Bar) -> bool:
    """Whether the bar was fetched before its session could be known to have closed.

    A bar fetched on or before its own session date may describe a day still in progress: Yahoo
    serves the running session intraday. Its values are expected to move once the day finishes,
    so a later re-fetch replacing it is the day completing, not a Restatement.

    A CoinGecko bar is the exception. Its bar for a date is the price at that date's 00:00 UTC
    -- the label CoinGecko puts on its own daily point, and what the daily sweep snapshots just
    after midnight (#18) -- so it is final from the moment that day begins, and provisional only
    if fetched before it.

    Judged on the UTC calendar, because no session time is stored (ADR-0009). That is exact for
    a crypto bar, whose session is the UTC day, and conservative for a listed venue, whose
    session for a date closes before that date ends in UTC: a bar fetched after the close but
    before UTC midnight is called provisional although it is final. The cost of that error is
    one re-fetch left unchecked; the opposite error files ordinary progress as a Restatement.
    A naive `fetched_at` is read as UTC.

    >>> bar = Bar(
    ...     instrument_id=InstrumentId("stock.us.nvda"), source=Source.YAHOO,
    ...     bar_date=date(2026, 9, 29), close=118.4, fetched_at=datetime(2026, 9, 29, 21, 0),
    ... )
    >>> is_provisional(bar)
    True
    >>> is_provisional(bar.model_copy(update={"fetched_at": datetime(2026, 9, 30, 0, 5)}))
    False
    >>> snapshot = bar.model_copy(
    ...     update={"source": Source.COINGECKO, "fetched_at": datetime(2026, 9, 29, 0, 5)}
    ... )
    >>> is_provisional(snapshot)
    False
    """
    fetched_on = as_utc(bar.fetched_at).date()
    if bar.source is Source.COINGECKO:
        return fetched_on < bar.bar_date
    return fetched_on <= bar.bar_date


def as_utc(moment: datetime) -> datetime:
    """A moment in UTC, reading a naive one as already UTC.

    Naive times are labelled rather than converted, since `astimezone` would read them as the
    machine's local time. Aware ones are converted, so their UTC calendar date is the right one.

    >>> as_utc(datetime(2026, 9, 29, 23, 30)).isoformat()
    '2026-09-29T23:30:00+00:00'
    >>> from datetime import timedelta, timezone
    >>> as_utc(datetime(2026, 9, 29, 23, 30, tzinfo=timezone(timedelta(hours=-5)))).isoformat()
    '2026-09-30T04:30:00+00:00'
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def split_factor_after(actions: Sequence[CorporateAction], when: date) -> float:
    """The cumulative split ratio implied by splits dated strictly after `when`.

    Strictly after, because a source reports the split on the first session that trades at the
    new ratio: that day's bar is already post-split and only the days before it get rescaled.
    """
    factor = 1.0
    for action in actions:
        if action.kind is CorporateActionKind.SPLIT and action.action_date > when:
            factor *= action.value
    return factor


def _unchanged(old: float | None, new: float | None, factor: float) -> bool:
    """True when the move is float noise or exactly the rescale `factor` accounts for."""
    if old is None or new is None:
        return old is None and new is None
    if math.isclose(new, old, rel_tol=RELATIVE_TOLERANCE, abs_tol=0.0):
        return True
    return math.isclose(new, old * factor, rel_tol=RELATIVE_TOLERANCE, abs_tol=0.0)


def detect_restatements(
    stored: Bar,
    incoming: Bar,
    actions: Sequence[CorporateAction],
) -> list[Restatement]:
    """Which of the bar's fields a re-fetch changed for a reason the actions do not explain.

    Both bars must be two vintages of one `(instrument, source, date)`; comparing across keys is
    a caller bug and is refused rather than silently reported as a restatement of everything.

    A split rescales prices down by its cumulative ratio and volume up by the same ratio, so a
    re-fetch across one rewrites every earlier bar. That is the expected shape of the change,
    not an anomaly, and it is accepted silently. So is a difference within
    `RELATIVE_TOLERANCE`. What remains -- a value that is neither where it was nor where the
    split would have put it -- is the Restatement.

    `detected_at` is taken from the incoming bar's `fetched_at`: the change became visible at
    the fetch that carried it, and the module holds no clock of its own.

    >>> aapl = InstrumentId("stock.us.aapl")
    >>> when = datetime(2024, 7, 1, 0, 0)
    >>> stored = Bar(
    ...     instrument_id=aapl, source=Source.YAHOO, bar_date=date(2024, 6, 3),
    ...     close=100.0, volume=1000.0, fetched_at=datetime(2024, 6, 4, 0, 0),
    ... )
    >>> split = CorporateAction(
    ...     instrument_id=aapl, source=Source.YAHOO, action_date=date(2024, 6, 10),
    ...     kind=CorporateActionKind.SPLIT, value=10.0,
    ... )
    >>> rescaled = stored.model_copy(update={"close": 10.0, "volume": 10000.0, "fetched_at": when})
    >>> detect_restatements(stored, rescaled, [split])
    []
    >>> [r.field for r in detect_restatements(stored, rescaled.model_copy(update={"close": 9.5}), [split])]
    ['close']
    """
    if stored.key != incoming.key:
        raise BarKeyMismatch(f"{stored.key} and {incoming.key} are not two vintages of one bar")

    price_factor = split_factor_after(actions, stored.bar_date)
    changes: list[Restatement] = []
    for field in (*PRICE_FIELDS, "volume"):
        old: float | None = getattr(stored, field)
        new: float | None = getattr(incoming, field)
        # A split divides prices and multiplies volume, so the shares outstanding still trade.
        factor = 1.0 / price_factor if field != "volume" else price_factor
        if _unchanged(old, new, factor):
            continue
        changes.append(
            Restatement(
                instrument_id=stored.instrument_id,
                source=stored.source,
                bar_date=stored.bar_date,
                field=field,
                old=old,
                new=new,
                detected_at=incoming.fetched_at,
            )
        )
    return changes


def bars_from_market_chart(
    instrument_id: InstrumentId,
    payload: MarketChart,
    fetched_at: datetime,
) -> list[Bar]:
    """Map one CoinGecko market chart payload into one close-only Bar per UTC day.

    The endpoint is close-only, so `open`, `high` and `low` stay null. So does `adj_close`:
    CoinGecko applies no dividend adjustment, and ADR-0009 keeps the field null rather than
    mirroring `close` so that equal by coincidence and equal by definition do not look alike.

    The three series are aligned by UTC day rather than by position, since nothing in the
    payload promises they are parallel. A day with a price but no volume point yields a bar with
    a null volume; a volume point on a day with no price yields nothing, because a Bar needs a
    close.

    Within one day the first point wins. A CoinGecko bar for a date is the price at that date's
    00:00 UTC, the point CoinGecko itself labels with the date and the one the daily sweep
    snapshots (#18). The API appends a trailing point at the current time, so the final day of a
    365-day fetch carries both its 00:00 point and a later one, and short windows space points
    hourly; taking the first keeps the 00:00 point in both cases, so the bar does not depend on
    `days` or on the time of the fetch, and `is_provisional` can treat it as final. The one
    exception is the first day of an hourly window, which starts at the fetch time `days` back
    and has no 00:00 point; a backfill that must match the sweep asks for 90 days or more.

    Only the window the payload covers is mapped. The free tier stops at 365 days
    (`cointoss.sources.coingecko.MARKET_CHART_MAX_DAYS`), and asking for more raises there
    rather than arriving here as a short result.
    """
    volume_by_day: dict[date, float] = {}
    for point in payload.total_volumes:
        volume_by_day.setdefault(_utc_day(point.timestamp), point.value)
    close_by_day: dict[date, float] = {}
    for point in payload.prices:
        close_by_day.setdefault(_utc_day(point.timestamp), point.value)
    return [
        Bar(
            instrument_id=instrument_id,
            source=Source.COINGECKO,
            bar_date=bar_date,
            close=close,
            volume=volume_by_day.get(bar_date),
            fetched_at=fetched_at,
        )
        for bar_date, close in sorted(close_by_day.items())
    ]
