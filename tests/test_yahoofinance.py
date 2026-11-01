"""`cointoss.sources.yahoofinance` over a stubbed `yf.Ticker`; nothing touches the network."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from cointoss.sources.yahoofinance import NoSuchTicker, get_prices, search_pairs


def test_an_empty_history_raises_no_such_ticker():
    with patch("cointoss.sources.yahoofinance.yf.Ticker") as ticker:
        ticker.return_value.history.return_value = pd.DataFrame()
        with pytest.raises(NoSuchTicker, match="no such ticker") as raised:
            get_prices("NOSUCH-USD")
    assert raised.value.ticker == "NOSUCH-USD"


def test_a_history_gets_its_session_dates_as_the_first_column():
    index = pd.DatetimeIndex(["2026-10-29", "2026-10-30"], tz="America/New_York")
    frame = pd.DataFrame({"Close": [1.0, 2.0]}, index=index)
    with patch("cointoss.sources.yahoofinance.yf.Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        prices = get_prices("BTC-USD")
    assert prices.columns[0] == "date"
    assert [row[0] for row in prices.data] == ["2026-10-29", "2026-10-30"]


def test_search_keeps_the_crypto_usd_tickers_of_the_symbol_with_any_numeric_suffix():
    quotes = [
        {"symbol": "PEPE24478-USD", "quoteType": "CRYPTOCURRENCY"},
        {"symbol": "PEP-USD", "quoteType": "CRYPTOCURRENCY"},
        {"symbol": "PEPECOIN-USD", "quoteType": "CRYPTOCURRENCY"},
        {"symbol": "PEPE24478-EUR", "quoteType": "CRYPTOCURRENCY"},
        {"symbol": "PEPE-USD", "quoteType": "ETF"},
        {"quoteType": "CRYPTOCURRENCY"},
        {"symbol": "pepe-usd", "quoteType": "CRYPTOCURRENCY"},
    ]
    with patch("cointoss.sources.yahoofinance.yf.Search") as search:
        search.return_value.quotes = quotes
        found = search_pairs("pepe")
    assert search.call_args.args == ("pepe USD",)
    assert found == ["PEPE24478-USD", "pepe-usd"]
