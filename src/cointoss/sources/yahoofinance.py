from __future__ import annotations

from typing import Any, Literal, cast

import yfinance as yf
from lythonic.compose.namespace import nsnode, require_cache

from cointoss import FrameData

YfPeriod = Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]


@require_cache
@nsnode(tags=["api"])
def get_info(ticker: str) -> dict[str, Any]:
    return cast(dict[str, Any], yf.Ticker(ticker).info)


@require_cache
@nsnode(tags=["api"])
def get_prices(ticker: str, period: YfPeriod = "max") -> FrameData:
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False, timeout=10)  # pyright: ignore[reportUnknownMemberType]
    dd: list[str] = list(map(str, df.index.date))  # pyright: ignore[reportAttributeAccessIssue,reportUnknownArgumentType]
    df.insert(loc=0, column="date", value=dd)  # pyright: ignore
    return FrameData.pd.from_frame(df)


@require_cache
@nsnode(tags=["api"])
def lookup(query: str) -> FrameData:
    df = yf.Lookup(query).all
    df.insert(loc=0, column="ticker", value=df.index)  # pyright: ignore[reportUnknownMemberType]
    return FrameData.pd.from_frame(df)
