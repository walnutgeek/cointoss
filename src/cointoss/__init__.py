"""
Cointoss: Crypto coin portfolio tracker and research platform.

Built on lythonic (SQLite ORM, DAG composition, CLI) and woodglue
(async server, Caddy integration). Fetches data from CoinGecko API,
stores it locally, and serves it via a Tornado async API.
"""

from __future__ import annotations

from lythonic.frame import FrameData

__all__ = ["FrameData"]


def main() -> None:
    """Entry point for the cointoss CLI."""
    from cointoss.cli import run_cli

    run_cli()


## Tests


def test_frame_data_roundtrip() -> None:
    """FrameData should survive a DataFrame -> FrameData -> DataFrame roundtrip."""
    import pandas as pd

    df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
    fd = FrameData.from_pandas(df)
    assert fd.columns == ["a", "b"]
    # tight orient stores rows, not columns
    assert fd.data == [[1, 4.0], [2, 5.0], [3, 6.0]]
    restored = fd.to_pandas()
    pd.testing.assert_frame_equal(restored, df, check_names=False)


def test_frame_data_json_roundtrip() -> None:
    """FrameData should serialize to JSON and back without loss."""
    import pandas as pd

    df = pd.DataFrame({"x": [10, 20], "y": ["foo", "bar"]})
    fd = FrameData.from_pandas(df)
    fd2 = FrameData.model_validate_json(fd.model_dump_json())
    pd.testing.assert_frame_equal(fd2.to_pandas(), df, check_names=False)
    assert fd == fd2
