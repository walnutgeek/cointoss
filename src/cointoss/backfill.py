"""History backfill (#26): fill every date with no stored bar, from CoinGecko and from Yahoo.

The daily sweep writes one CoinGecko bar per coin per day, and only within the bar window, so a
day the machine was down or swept late has no bar, and nothing before the first sweep has one.
`backfill` fills those dates for every Instrument that has ever been a member of any universe
(folded through Supersession), from two sources:

- CoinGecko `market_chart` over the last `days` days (90 to 365, the free tier's reach). Each
  date gets the bar the sweep would have written: the 00:00 UTC price, with volume and market
  cap. A day whose payload has no point at exactly 00:00 -- today, before CoinGecko publishes
  it -- is left alone rather than given a later price.
- Yahoo, for a coin linked to its Yahoo pair: `get_prices(period="max")`, back to the pair's
  listing, for dates strictly before today UTC, so no Provisional Bar is written.

A coin is linked by trying `{SYMBOL}-USD` (ADR-0009 amendment of 2026-10-30). The pair is
accepted when Yahoo's close for session D and CoinGecko's bar stamped D+1, which closes D,
differ by less than `LINK_TOLERANCE` in median over the last `LINK_SESSIONS` sessions both hold.
An accepted pair is recorded through the Instrument Registry as an Observation from Yahoo
carrying the coin's CoinGecko id and a Yahoo provider id, which adds the Yahoo External
Reference and opens a Ticker History record at source `yahoo` without touching the display
symbol or the Price Sources. A rejected pair is logged, stores nothing, and is tried again on
the next backfill. A pair whose Yahoo reference another Instrument already holds is rejected
too: recording it would be read as evidence the two are one coin, and supersede one of them.

Only dates with no stored bar from the source are written, so no stored bar is overwritten and
no Restatement is filed. Membership is not backfilled: `record_membership` refuses a date
earlier than the latest Evaluation Run.

Network fetches run outside the sweep lock and each write inside it, through the `lock` the
caller passes: the store is read again for what is missing once the lock is held, so a sweep
that wrote today's bar meanwhile keeps it, and a sweep waiting for the lock is held up for one
coin's write rather than the whole backfill. CoinGecko is paced by its client's `rpm`, Yahoo by
`YAHOO_INTERVAL` between requests.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import date, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict
from tornado.httpclient import HTTPClientError

from cointoss import FrameData
from cointoss.instrument import (
    ExternalReference,
    FigiResolution,
    Instrument,
    InstrumentId,
    InstrumentType,
    Observation,
    Outcome,
    ReferenceKind,
    Source,
)
from cointoss.prices import (
    Bar,
    CorporateAction,
    PriceError,
    as_utc,
    bars_from_market_chart,
    bars_from_yahoo,
)
from cointoss.sources.coingecko import MARKET_CHART_MAX_DAYS, CoinGeckoClient, MarketChart
from cointoss.sources.yahoofinance import get_prices
from cointoss.store import Store

__all__ = [
    "COINGECKO_RPM",
    "DEFAULT_DAYS",
    "LINK_SESSIONS",
    "LINK_TOLERANCE",
    "MIN_DAYS",
    "YAHOO_INTERVAL",
    "BackfillReport",
    "backfill",
    "median_disagreement",
]

log = logging.getLogger(__name__)

DEFAULT_DAYS = MARKET_CHART_MAX_DAYS
"""The CoinGecko window asked for by default: all the free tier serves."""

MIN_DAYS = 90
"""The shortest CoinGecko window: below it the API spaces points hourly, off 00:00 UTC."""

LINK_SESSIONS = 30
"""How many of the latest sessions both sources hold a Yahoo pair is checked over."""

LINK_TOLERANCE = 0.02
"""The median relative difference under which a Yahoo pair is the coin CoinGecko names."""

COINGECKO_RPM = 10.0
"""Requests per minute to CoinGecko's free tier, which throttles a second immediate call."""

YAHOO_INTERVAL = 2.0
"""Seconds between Yahoo requests. yfinance has no limiter of its own."""

_DAY_MS = 86_400_000

WriteLock = Callable[[], AbstractAsyncContextManager[None]]


class BackfillReport(BaseModel):
    """What one backfill did, for a caller to log or report.

    `linked` are the Instruments whose Yahoo pair was accepted this run; `rejected` maps an
    Instrument whose pair was tried and refused, or has no Yahoo history, to why. `failed` maps
    an Instrument to a fetch that failed, which leaves its dates for the next run. `skipped` maps a target that cannot be
    backfilled -- not a coin, or with no CoinGecko id -- to why.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    at: datetime
    days: int
    targets: int
    coingecko_bars: int
    yahoo_bars: int
    linked: list[InstrumentId]
    rejected: dict[InstrumentId, str]
    failed: dict[InstrumentId, str]
    skipped: dict[InstrumentId, str]


def median_disagreement(
    yahoo: Mapping[date, float],
    coingecko: Mapping[date, float],
    sessions: int = LINK_SESSIONS,
) -> float | None:
    """Median relative difference of two close series over the last `sessions` both hold.

    Both are keyed by the session closed, so a CoinGecko close is keyed by its stamped date
    less its Session Offset. Relative to CoinGecko's close. None when fewer sessions overlap.

    >>> d = [date(2026, 10, n) for n in range(1, 5)]
    >>> median_disagreement({d[0]: 99.0, d[1]: 110.0, d[2]: 200.0}, {d[0]: 100.0, d[1]: 100.0, d[2]: 100.0}, 3)
    0.1
    >>> median_disagreement({d[0]: 1.0}, {d[0]: 1.0, d[1]: 1.0}, 2) is None
    True
    """
    both = sorted(set(yahoo) & set(coingecko))[-sessions:]
    if len(both) < sessions:
        return None
    return statistics.median(abs(yahoo[d] - coingecko[d]) / coingecko[d] for d in both)


async def backfill(
    store: Store,
    at: datetime,
    *,
    days: int = DEFAULT_DAYS,
    coingecko: CoinGeckoClient | None = None,
    lock: WriteLock | None = None,
) -> BackfillReport:
    """Fill every missing CoinGecko and Yahoo date for every Instrument ever a member.

    `at` is the moment of the run: its UTC date is today, the first date no Yahoo bar is written
    for, and it stamps the bars' `fetched_at`. `lock` is entered around each write. Raises
    `ValueError` for a `days` outside `MIN_DAYS` to `MARKET_CHART_MAX_DAYS`, before anything
    is fetched. See the module docstring for what is written and how a pair is linked.
    """
    if not MIN_DAYS <= days <= MARKET_CHART_MAX_DAYS:
        raise ValueError(f"days={days} is outside {MIN_DAYS}..{MARKET_CHART_MAX_DAYS}")
    moment = as_utc(at)
    run = _Run(
        store,
        moment,
        coingecko or CoinGeckoClient(rpm=COINGECKO_RPM),
        lock or nullcontext,
    )
    targets = _targets(store)
    for instrument in targets:
        await run.instrument(instrument, days)
    log.info(
        "backfill %s: %d targets, %d CoinGecko bars, %d Yahoo bars, linked %s, rejected %d",
        moment.date(),
        len(targets),
        run.coingecko_bars,
        run.yahoo_bars,
        sorted(run.linked) or "none",
        len(run.rejected),
    )
    return BackfillReport(
        at=moment,
        days=days,
        targets=len(targets),
        coingecko_bars=run.coingecko_bars,
        yahoo_bars=run.yahoo_bars,
        linked=run.linked,
        rejected=run.rejected,
        failed=run.failed,
        skipped=run.skipped,
    )


def _targets(store: Store) -> list[Instrument]:
    """Every survivor that has ever been a member of any universe, in mint order."""
    ever = {i.instrument_id for i in store.membership_intervals(date.min, date.max)}
    loaded = [store.load_instrument(i) for i in ever]
    return sorted((i for i in loaded if i is not None), key=lambda i: i.mint_seq)


def _provider_id(instrument: Instrument, source: Source) -> str | None:
    """The first of the Instrument's provider ids from `source`, if it has one."""
    for ref in instrument.references:
        if ref.kind is ReferenceKind.PROVIDER_ID and ref.qualifier == source.value:
            return ref.value
    return None


def _midnight_days(chart: MarketChart) -> set[date]:
    """The UTC days with a price point at exactly 00:00, which is then the day's bar."""
    return {p.dt.date() for p in chart.prices if p.timestamp % _DAY_MS == 0}


class _Run:
    """One backfill's clients, clock and running totals."""

    store: Store
    moment: datetime
    today: date
    coingecko: CoinGeckoClient
    lock: WriteLock
    coingecko_bars: int
    yahoo_bars: int
    linked: list[InstrumentId]
    rejected: dict[InstrumentId, str]
    failed: dict[InstrumentId, str]
    skipped: dict[InstrumentId, str]
    _last_yahoo: float

    def __init__(
        self, store: Store, moment: datetime, coingecko: CoinGeckoClient, lock: WriteLock
    ) -> None:
        self.store = store
        self.moment = moment
        self.today = moment.date()
        self.coingecko = coingecko
        self.lock = lock
        self.coingecko_bars = 0
        self.yahoo_bars = 0
        self.linked = []
        self.rejected = {}
        self.failed = {}
        self.skipped = {}
        self._last_yahoo = float("-inf")

    async def instrument(self, instrument: Instrument, days: int) -> None:
        if instrument.type is not InstrumentType.CRYPTO:
            self.skipped[instrument.id] = f"a {instrument.type}, not a coin"
            return
        coin_id = _provider_id(instrument, Source.COINGECKO)
        if coin_id is None:
            log.warning("backfill: %s has no CoinGecko id; skipped", instrument.id)
            self.skipped[instrument.id] = "no CoinGecko id"
            return
        await self._coingecko(instrument.id, coin_id, days)
        linked = _provider_id(instrument, Source.YAHOO)
        ticker = linked or f"{instrument.symbol}-USD"
        frame = await self._yahoo(ticker)
        if isinstance(frame, str):
            if linked is None:
                self._reject(instrument.id, ticker, f"no Yahoo history: {frame}")
            else:
                log.warning("backfill: Yahoo %s for %s failed: %s", ticker, instrument.id, frame)
                self.failed[instrument.id] = f"Yahoo {ticker}: {frame}"
            return
        if linked is not None or await self._link(instrument, coin_id, ticker, frame):
            await self._write_yahoo(instrument.id, frame)

    async def _coingecko(self, instrument_id: InstrumentId, coin_id: str, days: int) -> None:
        try:
            chart = await self.coingecko.fetch_market_chart(coin_id, days=days)
        except (HTTPClientError, OSError) as exc:
            log.warning("backfill: %s market_chart failed: %s", coin_id, exc)
            self.failed[instrument_id] = f"CoinGecko market_chart {coin_id}: {exc}"
            return
        midnights = _midnight_days(chart)
        bars = [
            b
            for b in bars_from_market_chart(instrument_id, chart, self.moment)
            if b.bar_date in midnights
        ]
        if not bars:
            return
        async with self.lock():
            held = self._stamped(instrument_id, Source.COINGECKO, bars)
            missing = [b for b in bars if b.bar_date not in held]
            self.store.upsert_bars(missing)
        self.coingecko_bars += len(missing)

    async def _yahoo(self, ticker: str) -> FrameData | str:
        """Yahoo's whole daily history for `ticker`, or why there is none."""
        wait = self._last_yahoo + YAHOO_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            # yfinance blocks, and raises an assortment of types for a ticker it does not know.
            return await asyncio.to_thread(get_prices, ticker, period="max")
        except Exception as exc:
            return str(exc) or type(exc).__name__
        finally:
            self._last_yahoo = time.monotonic()

    def _settled(
        self, instrument_id: InstrumentId, frame: FrameData
    ) -> tuple[list[Bar], list[CorporateAction]]:
        """The frame's bars and actions for sessions before today, which have closed."""
        bars, actions = bars_from_yahoo(instrument_id, frame, self.moment)
        return (
            [b for b in bars if b.bar_date < self.today],
            [a for a in actions if a.action_date < self.today],
        )

    async def _link(
        self, instrument: Instrument, coin_id: str, ticker: str, frame: FrameData
    ) -> bool:
        """Record `ticker` as the coin's Yahoo pair if its prices agree; whether it was."""
        try:
            bars, _ = self._settled(instrument.id, frame)
        except PriceError as exc:
            return self._reject(instrument.id, ticker, f"unreadable frame: {exc}")
        yahoo = {b.bar_date: b.close for b in bars}
        coingecko = {
            Source.COINGECKO.session_of(b.bar_date): b.close
            for b in self.store.bars_for(instrument.id, date.min, date.max, source=Source.COINGECKO)
        }
        disagreement = median_disagreement(yahoo, coingecko)
        if disagreement is None:
            return self._reject(
                instrument.id, ticker, f"fewer than {LINK_SESSIONS} sessions overlap CoinGecko"
            )
        if disagreement >= LINK_TOLERANCE:
            return self._reject(
                instrument.id, ticker, f"median difference {disagreement:.1%} from CoinGecko"
            )
        async with self.lock():
            review = self._record_pair(instrument.id, coin_id, ticker)
        if review is not None:
            return self._reject(instrument.id, ticker, review)
        log.info(
            "backfill: %s linked to Yahoo %s (median %.2f%%)",
            instrument.id,
            ticker,
            disagreement * 100,
        )
        self.linked.append(instrument.id)
        return True

    def _reject(self, instrument_id: InstrumentId, ticker: str, why: str) -> bool:
        log.warning("backfill: Yahoo %s rejected for %s: %s", ticker, instrument_id, why)
        self.rejected[instrument_id] = f"{ticker}: {why}"
        return False

    def _record_pair(self, instrument_id: InstrumentId, coin_id: str, ticker: str) -> str | None:
        """Add the Yahoo reference and Ticker History; why not, or None once it is stored.

        Read from the store again under the lock, so a pair another run linked meanwhile is
        found rather than recorded twice.
        """
        registry = self.store.load_registry()
        target = registry.survivor(instrument_id)
        pair = ExternalReference.provider_id(Source.YAHOO, ticker)
        held_by = {
            registry.survivor(i.id).id for i in registry.instruments() if pair in i.references
        }
        if held_by == {target.id}:
            return None
        if held_by:
            return f"already the Yahoo pair of {sorted(held_by)}"
        result = registry.observe(
            Observation(
                type=target.type,
                symbol=ticker,
                scope=str(target.scope),
                observed_at=self.today,
                source=Source.YAHOO,
                references=(ExternalReference.provider_id(Source.COINGECKO, coin_id), pair),
                figi_resolution=FigiResolution.NOT_ATTEMPTED,
            )
        )
        if result.outcome is not Outcome.MATCHED or result.instrument is not target:
            return result.review or f"{result.outcome} rather than matched to {target.id}"
        self.store.save_instrument(target)
        return None

    async def _write_yahoo(self, instrument_id: InstrumentId, frame: FrameData) -> None:
        try:
            bars, actions = self._settled(instrument_id, frame)
        except PriceError as exc:
            log.warning("backfill: Yahoo frame for %s unreadable: %s", instrument_id, exc)
            self.failed[instrument_id] = f"Yahoo frame: {exc}"
            return
        if not bars:
            return
        async with self.lock():
            held = self._stamped(instrument_id, Source.YAHOO, bars)
            missing = [b for b in bars if b.bar_date not in held]
            stored = {
                (a.source, a.action_date, a.kind)
                for a in self.store.corporate_actions_for(instrument_id, date.min, date.max)
            }
            new_actions = [a for a in actions if (a.source, a.action_date, a.kind) not in stored]
            self.store.upsert_bars(missing, new_actions)
        self.yahoo_bars += len(missing)

    def _stamped(self, instrument_id: InstrumentId, source: Source, bars: list[Bar]) -> set[date]:
        """The dates `source` already holds a bar for, as stamped, over the span of `bars`."""
        first = min(b.bar_date for b in bars)
        last = max(b.bar_date for b in bars)
        return {b.bar_date for b in self.store.bars_for(instrument_id, first, last, source=source)}
