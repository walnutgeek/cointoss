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
    print("TBD")
