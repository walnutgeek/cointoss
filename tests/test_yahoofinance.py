"""`cointoss.sources.yahoofinance` over a stubbed `yf.Ticker`; nothing touches the network."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from cointoss.sources.yahoofinance import NoSuchTicker, get_prices


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
