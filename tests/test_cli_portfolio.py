"""`cointoss portfolio` and `cointoss trade`: the CLI as a JSON-RPC client of a running instance.

Each test runs `cointoss init` into a temporary data directory, then serves that instance the way
`wgl start` does -- its `woodglue.yaml`, namespaces and `auth.db` -- from a thread, on the port
`init` was given, so `cointoss.cli.main` reaches it over real HTTP with a real bearer token.
CoinGecko, OpenFIGI and the clock are patched as `tests/test_app_portfolio.py` patches them.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from woodglue.apps.server import create_app
from woodglue.auth_store import AuthStore
from woodglue.cli import load_namespaces
from woodglue.config import load_config

from cointoss.app import PORTFOLIO_GRANTS, PORTFOLIO_ROLE
from cointoss.cli import main
from cointoss.config import HOME_ENV
from cointoss.portfolio import PORTFOLIO_HELD
from cointoss.sources.openfigi import API_KEY_ENV
from cointoss.store import Store
from tests.test_app_portfolio import DAY1_AT, market


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for name in (HOME_ENV, API_KEY_ENV, "COINTOSS_URL", "COINTOSS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "instance"


@contextmanager
def serving(home: Path) -> Iterator[AuthStore]:
    """`cointoss init` an instance on a free port and serve it from a thread until the block ends."""
    sock, port = bind_unused_port()
    assert main(["init", "--data-dir", str(home), "--port", str(port)]) == 0
    config = load_config(home)
    auth = AuthStore(home / "auth.db", config.auth.resolved_roles())
    namespaces = load_namespaces(config.namespaces, home)
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        server = HTTPServer(create_app(namespaces, config=config, auth_store=auth))
        server.add_sockets([sock])
        started.set()
        loop.run_forever()
        server.stop()
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started.wait()
    try:
        yield auth
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()


def run(capsys: pytest.CaptureFixture[str], home: Path, *args: str) -> tuple[int, str]:
    capsys.readouterr()
    code = main([*args, "--data-dir", str(home)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_init_defines_the_portfolio_role(home: Path):
    assert main(["init", "--data-dir", str(home)]) == 0
    assert load_config(home).auth.roles == {PORTFOLIO_ROLE: list(PORTFOLIO_GRANTS)}


def test_init_reports_the_role_missing_from_a_kept_woodglue_yaml(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["init", "--data-dir", str(home)]) == 0
    written = yaml.safe_load((home / "woodglue.yaml").read_text())
    del written["auth"]["roles"]
    (home / "woodglue.yaml").write_text(yaml.safe_dump(written))

    code, out = run(capsys, home, "init")

    assert code == 0, out
    assert f"no `{PORTFOLIO_ROLE}` Role" in out
    assert "call cointoss.portfolio" in out


def test_create_trade_and_void_against_the_local_instance(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    with serving(home), market(DAY1_AT):
        code, out = run(capsys, home, "portfolio", "create", "main", "--kind", "paper")
        assert (code, out) == (0, "portfolio 1: main (paper), owned by admin\n")

        code, out = run(capsys, home, "trade", "add", "1", "neo", "10", "--cost", "25")
        assert code == 0, out
        assert out == "trade 1: +10 crypto.native.neo at 2026-09-30T00:05:00Z, cost 25 USD\n"

        code, out = run(capsys, home, "trade", "add", "1", "crypto.native.neo", "-2.5")
        assert (code, out.split(":")[0]) == (0, "trade 2")
        assert "-2.5 crypto.native.neo" in out

        code, out = run(capsys, home, "trade", "void", "1")
        assert (code, out) == (0, "trade 1: voided at 2026-09-30T00:05:00Z\n")

        code, out = run(capsys, home, "trade", "void", "1")
        assert code == 1
        assert out.startswith("cointoss trade: ") and "voided" in out
        assert len(out.splitlines()) == 1

    with Store(home / "cointoss.db") as store:
        assert [t.voided for t in store.trades_of(1)] == [True, False]
        assert "crypto.native.neo" in store.require_universe(PORTFOLIO_HELD).parameters.inclusions


def test_a_token_without_the_role_is_refused_in_one_line(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    with serving(home) as auth, market(DAY1_AT):
        auth.ensure_admin_token()
        explorer = auth.delegate(auth.create_session("admin"), "explore", ["call cointoss"])
        url = f"http://127.0.0.1:{load_config(home).port}"
        monkeypatch.setenv("COINTOSS_URL", url)

        code, out = run(capsys, home, "portfolio", "create", "main", f"--token={explorer}")

    assert code == 1
    assert out == ("cointoss portfolio: Forbidden: requires 'call' on 'cointoss.portfolio'\n")
    with Store(home / "cointoss.db") as store:
        assert store.portfolios_of("admin") == []


@pytest.mark.parametrize("by_env", [False, True], ids=["option", "env"])
def test_a_token_starting_with_a_dash_reaches_the_server(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, by_env: bool
):
    # A token is random base64url, so about one in 64 starts with `-`.
    token = "-" + "x" * 42
    with serving(home):
        if by_env:
            monkeypatch.setenv("COINTOSS_TOKEN", token)
            code, out = run(capsys, home, "portfolio", "create", "main")
        else:
            code, out = run(capsys, home, "portfolio", "create", "main", f"--token={token}")

    assert code == 1
    # Refused by the server, not by argparse, which would read the token as an option.
    assert (
        out
        == "cointoss portfolio: Unauthorized; give --token=TOKEN, or `wgl login` to the server\n"
    )


def test_an_unreachable_server_is_one_line(home: Path, capsys: pytest.CaptureFixture[str]):
    sock, port = bind_unused_port()
    sock.close()
    code, out = run(
        capsys, home, "trade", "void", "1", "--url", f"http://127.0.0.1:{port}", "--token=x"
    )
    assert code == 1
    assert out.startswith(f"cointoss trade: http://127.0.0.1:{port}: ")
    assert len(out.splitlines()) == 1


def test_without_an_instance_or_a_url_it_says_how_to_name_one(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out = run(capsys, home, "portfolio", "create", "main")
    assert code == 1
    assert "--url" in out
