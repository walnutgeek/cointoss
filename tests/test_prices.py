"""Tests for `cointoss.prices`, which implements ADR-0009.

Driven by a recorded Yahoo frame trimmed to the days around NVIDIA's 2024 ten-for-one split,
committed under `tests/data`. No network: the fixture is replayed, never re-fetched.

The tests go through the module's public seam -- `bars_from_yahoo` and `detect_restatements` --
and assert on what comes back, never on how the comparison is arranged internally.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from cointoss import FrameData
from cointoss.instrument import InstrumentId
from cointoss.prices import (
    Bar,
    BarKeyMismatch,
    CorporateAction,
    CorporateActionKind,
    MalformedValue,
    MissingColumn,
    bars_from_yahoo,
    detect_restatements,
)

NVDA = InstrumentId("stock.us.nvda")
FETCHED = datetime(2024, 7, 1, 12, 0)
REFETCHED = datetime(2024, 8, 1, 12, 0)
SPLIT_DATE = date(2024, 6, 10)

FIXTURE = Path(__file__).parent / "data" / "yahoo_nvda_2024_split.json"


def yahoo_frame() -> FrameData:
    """The recorded frame, in the shape `cointoss.sources.yahoofinance.get_prices` returns."""
    return FrameData.model_validate(json.loads(FIXTURE.read_text()))


def split(value: float = 10.0, when: date = SPLIT_DATE) -> CorporateAction:
    return CorporateAction(
        instrument_id=NVDA,
        source="yahoo",
        action_date=when,
        kind=CorporateActionKind.SPLIT,
        value=value,
    )


def pre_split_vintage(bar: Bar) -> Bar:
    """What Yahoo reported for that session before the split rescaled its history."""
    return bar.model_copy(
        update={
            "open": None if bar.open is None else bar.open * 10.0,
            "high": None if bar.high is None else bar.high * 10.0,
            "low": None if bar.low is None else bar.low * 10.0,
            "close": bar.close * 10.0,
            "adj_close": None if bar.adj_close is None else bar.adj_close * 10.0,
            "volume": None if bar.volume is None else bar.volume / 10.0,
            "fetched_at": FETCHED,
        }
    )


def test_one_frame_yields_bars_and_actions():
    """One Yahoo frame produces both outputs in a single pass, each bar stamped and sourced."""
    bars, actions = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)

    assert [b.bar_date for b in bars] == [
        date(2024, 6, 5),
        date(2024, 6, 6),
        date(2024, 6, 7),
        date(2024, 6, 10),
        date(2024, 6, 11),
        date(2024, 6, 12),
    ]
    assert {b.source for b in bars} == {"yahoo"}
    assert {b.fetched_at for b in bars} == {FETCHED}
    assert {b.instrument_id for b in bars} == {NVDA}

    first = bars[0]
    assert first.close == pytest.approx(122.44000244140625)
    assert first.open is not None and first.high is not None and first.low is not None
    assert first.adj_close is not None and first.volume is not None

    assert [(a.action_date, a.kind, a.value) for a in actions] == [
        (SPLIT_DATE, CorporateActionKind.SPLIT, 10.0),
        (date(2024, 6, 11), CorporateActionKind.DIVIDEND, 0.01),
    ]


def test_zero_dividend_and_split_rows_are_dropped():
    """Most sessions carry 0.0 in both action columns; none of them becomes a record."""
    _, actions = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)

    assert len(actions) == 2
    assert all(a.value != 0 for a in actions)
    # Five of the six recorded sessions have a zero dividend, and five a zero split.
    assert sum(1 for a in actions if a.kind is CorporateActionKind.DIVIDEND) == 1
    assert sum(1 for a in actions if a.kind is CorporateActionKind.SPLIT) == 1


def test_close_only_bar_is_representable():
    """A close and a volume are enough; the candle fields stay null rather than invented."""
    bar = Bar(
        instrument_id=InstrumentId("crypto.eth.usdc"),
        source="coingecko",
        bar_date=date(2024, 6, 5),
        close=1.0004,
        volume=8.2e8,
        fetched_at=FETCHED,
    )

    assert (bar.open, bar.high, bar.low, bar.adj_close) == (None, None, None, None)
    assert bar.close == 1.0004


def test_frame_without_a_close_column_is_refused():
    """A close is the one price the mapping cannot do without, so its absence is named."""
    frame = FrameData(columns=["date", "Open"], data=[["2024-06-05", 1.0]])

    with pytest.raises(MissingColumn, match="Close"):
        bars_from_yahoo(NVDA, frame, FETCHED)


def test_frame_without_action_columns_yields_no_actions():
    """A close-only frame still maps; the mapping does not demand Yahoo's action columns."""
    frame = FrameData(columns=["date", "Close"], data=[["2024-06-05", 122.44]])

    bars, actions = bars_from_yahoo(NVDA, frame, FETCHED)

    assert len(bars) == 1
    assert bars[0].open is None and bars[0].volume is None
    assert actions == []


def test_negative_price_is_refused():
    """A negative price is not a price, and it would poison any return computed from it."""
    with pytest.raises(MalformedValue, match="negative"):
        Bar(
            instrument_id=NVDA,
            source="yahoo",
            bar_date=date(2024, 6, 5),
            close=-1.0,
            fetched_at=FETCHED,
        )


def test_split_rescale_is_accepted_silently():
    """A re-fetch across a split rewrites every earlier bar; none of it is a restatement."""
    bars, actions = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    incoming = [b.model_copy(update={"fetched_at": REFETCHED}) for b in bars]
    # Sessions before the split date were rewritten tenfold; the split day already traded at the
    # new ratio, so its bar and the ones after it are unchanged.
    stored = [pre_split_vintage(b) if b.bar_date < SPLIT_DATE else b for b in bars]

    detected = [
        r
        for old, new in zip(stored, incoming, strict=True)
        for r in detect_restatements(old, new, actions)
    ]

    assert any(b.bar_date < SPLIT_DATE for b in bars)
    assert detected == []


def test_bar_on_the_split_date_is_already_post_split():
    """The split day trades at the new ratio, so a tenfold change there is a real restatement."""
    bars, actions = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    split_day = next(b for b in bars if b.bar_date == SPLIT_DATE)
    rescaled = pre_split_vintage(split_day)

    detected = detect_restatements(
        rescaled, split_day.model_copy(update={"fetched_at": REFETCHED}), actions
    )

    assert {r.field for r in detected} == {"open", "high", "low", "close", "adj_close", "volume"}


def test_unexplained_change_is_detected():
    """A close that moved by something other than the split factor is reported, field by field."""
    bars, actions = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    first = bars[0]
    stored = pre_split_vintage(first)
    incoming = first.model_copy(update={"close": first.close * 1.02, "fetched_at": REFETCHED})

    detected = detect_restatements(stored, incoming, actions)

    assert [r.field for r in detected] == ["close"]
    only = detected[0]
    assert only.instrument_id == NVDA
    assert only.source == "yahoo"
    assert only.bar_date == first.bar_date
    assert only.old == pytest.approx(stored.close)
    assert only.new == pytest.approx(incoming.close)
    assert only.detected_at == REFETCHED


def test_change_with_no_corporate_action_at_all_is_detected():
    """With an empty action list there is nothing to explain a move, so it is a restatement."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    first = bars[0]
    incoming = first.model_copy(update={"close": first.close + 1.0, "fetched_at": REFETCHED})

    detected = detect_restatements(first, incoming, [])

    assert [r.field for r in detected] == ["close"]


def test_float_noise_is_ignored():
    """A drift well inside the relative tolerance is rounding, not a rewrite."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    first = bars[0]
    nudged = first.model_copy(
        update={
            "close": first.close * (1 + 1e-12),
            "open": first.open,
            "fetched_at": REFETCHED,
        }
    )

    assert detect_restatements(first, nudged, []) == []


def test_tolerance_is_relative_not_absolute():
    """A cheap coin and an expensive share are held to the same proportional standard."""
    cheap = Bar(
        instrument_id=InstrumentId("crypto.eth.shib"),
        source="coingecko",
        bar_date=date(2024, 6, 5),
        close=0.00002,
        fetched_at=FETCHED,
    )
    expensive = Bar(
        instrument_id=InstrumentId("stock.us.brk_a"),
        source="yahoo",
        bar_date=date(2024, 6, 5),
        close=620000.0,
        fetched_at=FETCHED,
    )
    # One percent of each. An absolute epsilon would wave the first through and flag the second.
    for bar in (cheap, expensive):
        moved = bar.model_copy(update={"close": bar.close * 1.01, "fetched_at": REFETCHED})
        assert [r.field for r in detect_restatements(bar, moved, [])] == ["close"]

    # And a proportionally tiny move is noise in both, despite the absolute sizes differing.
    for bar in (cheap, expensive):
        noise = bar.model_copy(update={"close": bar.close * (1 + 1e-10), "fetched_at": REFETCHED})
        assert detect_restatements(bar, noise, []) == []


def test_a_field_appearing_or_vanishing_is_a_restatement():
    """A source that stops reporting volume has changed the record, not merely omitted it."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    first = bars[0]
    without_volume = first.model_copy(update={"volume": None, "fetched_at": REFETCHED})

    detected = detect_restatements(first, without_volume, [])

    assert [r.field for r in detected] == ["volume"]
    assert detected[0].new is None


def test_split_dated_before_the_bar_does_not_excuse_a_change():
    """Only splits after the bar rescale it; an earlier one explains nothing."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    last = bars[-1]
    stored = pre_split_vintage(last)

    detected = detect_restatements(
        stored, last.model_copy(update={"fetched_at": REFETCHED}), [split(when=date(2024, 6, 1))]
    )

    assert {r.field for r in detected} == {"open", "high", "low", "close", "adj_close", "volume"}


def test_comparing_two_different_bars_is_refused():
    """Two vintages of one bar, or nothing: a key mismatch is a caller bug, not a restatement."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)

    with pytest.raises(BarKeyMismatch):
        detect_restatements(bars[0], bars[1], [])


def test_consecutive_splits_compound():
    """Two splits after a bar rescale it by their product, and that is still no restatement."""
    bars, _ = bars_from_yahoo(NVDA, yahoo_frame(), FETCHED)
    first = bars[0]
    actions = [split(4.0, date(2024, 6, 20)), split(10.0, SPLIT_DATE)]
    incoming = first.model_copy(
        update={
            "open": None if first.open is None else first.open / 40.0,
            "high": None if first.high is None else first.high / 40.0,
            "low": None if first.low is None else first.low / 40.0,
            "close": first.close / 40.0,
            "adj_close": None if first.adj_close is None else first.adj_close / 40.0,
            "volume": None if first.volume is None else first.volume * 40.0,
            "fetched_at": REFETCHED,
        }
    )

    assert detect_restatements(first, incoming, actions) == []
