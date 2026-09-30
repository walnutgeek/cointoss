"""The `cointoss` CLI: `init`, `sweep`, `status`, `token`.

Each test runs `cointoss.cli.main` against a temporary data directory. CoinGecko is answered
from the recorded `markets` listing and the clock is fixed, through the same seam
`tests/test_app.py` uses, so nothing touches the network. Nothing here calls `systemctl`: the
unit is written into a temporary directory and only its text is checked.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from woodglue.cli import load_namespaces
from woodglue.config import load_config

from cointoss.app import SWEEP_TRIGGER, sweep_lock
from cointoss.cli import main, render_unit
from cointoss.config import DEFAULT_UNIVERSES, HOME_ENV, Settings
from cointoss.store import Store

FIXTURE = Path(__file__).parent / "data" / "coingecko_markets_top250.json"
AT = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
SHIPPED_UNIT = Path(__file__).parent.parent / "src" / "cointoss" / "cointoss.service"


@contextmanager
def market(at: datetime = AT) -> Iterator[MagicMock]:
    pages: list[list[dict[str, Any]]] = json.loads(FIXTURE.read_text())["pages"]

    async def fetch(url: str) -> MagicMock:
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        response = MagicMock()
        response.body = json.dumps(pages[page - 1] if page <= len(pages) else []).encode()
        return response

    client = MagicMock()
    client.fetch = AsyncMock(side_effect=fetch)
    with (
        patch("cointoss.sources.coingecko.AsyncHTTPClient", return_value=client),
        patch("cointoss.app.utcnow", return_value=at),
    ):
        yield client


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.delenv(HOME_ENV, raising=False)
    with TemporaryDirectory() as tmp:
        yield Path(tmp) / "instance"


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_init_writes_a_config_woodglue_loads_mounting_the_cointoss_namespace(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out = run(capsys, "init", "--data-dir", str(home))
    assert code == 0, out

    config = load_config(home)
    assert config.host == "127.0.0.1"
    assert config.auth.enabled
    entry = config.namespaces["cointoss"]
    assert entry.expose_api and entry.run_engine
    (ns, _) = load_namespaces(config.namespaces, home)["cointoss"]
    node, trigger = ns.get_trigger(SWEEP_TRIGGER)
    assert node is ns.get("data:sweep")
    assert trigger.schedule == "5 0 * * *"
    # The fragment is told its data directory, since woodglue does not pass its own.
    assert {n.nsref.name for n in ns.query("api")} >= {"universes", "members", "runs"}
    assert entry.entries is not None
    assert entry.entries[0]["init"] == {"data_dir": str(home)}

    assert Settings.load(home).universes == DEFAULT_UNIVERSES
    with Store(home / "cointoss.db") as store:
        assert store.universe_names() == [s.name for s in DEFAULT_UNIVERSES]
    assert (home / "auth.db").exists()


def test_a_second_init_leaves_edited_configs_untouched(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    assert run(capsys, "init", "--data-dir", str(home))[0] == 0
    woodglue_yaml = home / "woodglue.yaml"
    edited = woodglue_yaml.read_text().replace("port: 5321", "port: 5399")
    assert edited != woodglue_yaml.read_text()
    woodglue_yaml.write_text(edited)
    cointoss_yaml = home / "cointoss.yaml"
    cointoss_yaml.write_text(
        yaml.safe_dump(
            {"top_n": 250, "universes": [{"name": "cg-top-10", "enter_rank": 10, "exit_rank": 12}]}
        )
    )
    first_token = run(capsys, "token", "--data-dir", str(home))[1]

    code, out = run(capsys, "init", "--data-dir", str(home))

    assert code == 0, out
    assert woodglue_yaml.read_text() == edited
    assert load_config(home).port == 5399
    assert [s.name for s in Settings.load(home).universes] == ["cg-top-10"]
    assert "kept" in out
    assert run(capsys, "token", "--data-dir", str(home))[1] == first_token
    assert len(first_token.strip()) > 20


def test_init_refuses_a_woodglue_yaml_that_does_not_parse(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    home.mkdir(parents=True)
    (home / "woodglue.yaml").write_text("namespaces: 7\n")
    code, out = run(capsys, "init", "--data-dir", str(home))
    assert code != 0
    assert "woodglue.yaml" in out
    assert (home / "woodglue.yaml").read_text() == "namespaces: 7\n"


def test_a_woodglue_yaml_that_is_not_yaml_is_one_line(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    home.mkdir(parents=True)
    (home / "woodglue.yaml").write_text("namespaces: [\n")
    code, out = run(capsys, "token", "--data-dir", str(home))
    assert code == 1
    (line,) = out.strip().splitlines()
    assert line.startswith(f"cointoss token: {home / 'woodglue.yaml'}: ")


def test_the_data_dir_comes_from_the_environment(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(HOME_ENV, str(home))
    assert run(capsys, "init")[0] == 0
    assert (home / "woodglue.yaml").exists()


def test_sweep_writes_a_run_and_status_reports_it(home: Path, capsys: pytest.CaptureFixture[str]):
    assert run(capsys, "init", "--data-dir", str(home))[0] == 0
    code, out = run(capsys, "status", "--data-dir", str(home))
    assert code == 0, out
    assert "cg-top-100" in out and "no runs yet" in out

    with market() as client:
        code, out = run(capsys, "sweep", "--data-dir", str(home))
    assert code == 0, out
    assert client.fetch.call_count == 1
    assert "2026-09-30" in out

    with Store(home / "cointoss.db") as store:
        (evaluation,) = store.load_evaluation_runs("cg-top-100")
        assert evaluation.source_asof == AT.date()

    code, out = run(capsys, "status", "--data-dir", str(home))
    assert code == 0, out
    line = next(ln for ln in out.splitlines() if ln.startswith("cg-top-100"))
    assert "2026-09-30" in line
    assert "100 members" in line
    assert "100/100 bars" in line
    assert "last bar 2026-09-30" in out


def test_a_second_sweep_the_same_day_fetches_nothing_unless_forced(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    with market():
        assert run(capsys, "sweep", "--data-dir", str(home))[0] == 0
    with market(AT + timedelta(hours=3)) as client:
        code, out = run(capsys, "sweep", "--data-dir", str(home))
    assert code == 0
    assert client.fetch.call_count == 0
    assert "already swept" in out
    with market(AT + timedelta(hours=3)) as client:
        assert run(capsys, "sweep", "--data-dir", str(home), "--force")[0] == 0
    assert client.fetch.call_count == 1


def test_a_late_sweep_says_it_wrote_no_bars(home: Path, capsys: pytest.CaptureFixture[str]):
    with market(AT.replace(hour=6)):
        code, out = run(capsys, "sweep", "--data-dir", str(home))
    assert code == 0, out
    assert "0 bars" in out
    assert "no bars: swept after the bar window" in out


def test_a_sweep_while_another_runs_exits_with_one_line(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    home.mkdir(parents=True)
    with sweep_lock(home), market() as client:
        code, out = run(capsys, "sweep", "--data-dir", str(home))
    assert code == 1
    assert client.fetch.call_count == 0
    assert out.strip().splitlines() == [
        f"cointoss sweep: another sweep holds {home / 'sweep.lock'}; not sweeping"
    ]


@pytest.mark.parametrize("command", ["sweep", "status"])
@pytest.mark.parametrize(
    "text",
    [
        "top_n: [\n",
        "universes:\n  - {name: bad, enter_rank: 120, exit_rank: 100}\n",
        "top_n: 10\n",
        "bar_window_hours: 0\n",
    ],
)
def test_a_config_that_does_not_load_is_one_line_not_a_traceback(
    home: Path, capsys: pytest.CaptureFixture[str], command: str, text: str
):
    home.mkdir(parents=True)
    (home / "cointoss.yaml").write_text(text)
    with market() as client:
        code, out = run(capsys, command, "--data-dir", str(home))
    assert code == 1
    assert client.fetch.call_count == 0
    (line,) = out.strip().splitlines()
    assert line.startswith(f"cointoss {command}: {home / 'cointoss.yaml'}: ")


def test_an_unexpected_error_keeps_its_traceback(home: Path):
    with (
        patch("cointoss.cli.cmd_status", side_effect=ValueError("a bug")),
        pytest.raises(ValueError, match="a bug"),
    ):
        main(["status", "--data-dir", str(home)])


def test_sweep_no_longer_takes_a_date(home: Path):
    with pytest.raises(SystemExit):
        main(["sweep", "--data-dir", str(home), "--date", "2026-09-30"])


def test_status_without_a_database_says_to_run_init(home: Path, capsys: pytest.CaptureFixture[str]):
    code, out = run(capsys, "status", "--data-dir", str(home))
    assert code != 0
    assert "cointoss init" in out
    assert not (home / "cointoss.db").exists()


def test_init_systemd_writes_the_unit_for_this_install(
    home: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    unit_dir = tmp_path / "systemd"
    with patch("cointoss.cli.user_unit_dir", return_value=unit_dir):
        code, out = run(capsys, "init", "--data-dir", str(home), "--systemd")
    assert code == 0, out
    unit = (unit_dir / "cointoss.service").read_text()
    assert f"Environment=COINTOSS_HOME={home}" in unit
    assert f"wgl --data={home} start" in unit
    assert "Restart=on-failure" in unit
    assert "systemctl --user daemon-reload" in out
    assert "systemctl --user enable --now cointoss" in out

    (unit_dir / "cointoss.service").write_text(unit + "# edited\n")
    with patch("cointoss.cli.user_unit_dir", return_value=unit_dir):
        assert run(capsys, "init", "--data-dir", str(home), "--systemd")[0] == 0
    assert (unit_dir / "cointoss.service").read_text().endswith("# edited\n")


def test_the_rendered_unit_runs_woodglue_in_the_foreground_with_a_catch_up_sweep():
    unit = render_unit(
        bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/ct"), timeout=Path("/opt/cu/timeout")
    )
    lines = unit.splitlines()
    assert "ExecStart=/opt/ct/bin/wgl --data=/srv/ct start" in lines
    assert "Type=simple" in lines
    # '-' so a failed catch-up (no network at boot) does not stop the API from starting.
    assert "ExecStartPre=-/opt/cu/timeout 120 /opt/ct/bin/cointoss sweep" in lines
    assert "$" not in unit
    assert SHIPPED_UNIT.exists()


def test_the_unit_finds_timeout_on_the_path_or_refuses():
    with patch("cointoss.cli.shutil.which", return_value="/usr/local/bin/timeout"):
        unit = render_unit(bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/ct"))
    assert "ExecStartPre=-/usr/local/bin/timeout 120 " in unit
    with (
        patch("cointoss.cli.shutil.which", return_value=None),
        pytest.raises(ValueError, match="timeout"),
    ):
        render_unit(bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/ct"))


def test_a_percent_in_a_unit_path_is_escaped_for_systemd():
    unit = render_unit(
        bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/100%ct"), timeout=Path("/bin/timeout")
    )
    assert "Environment=COINTOSS_HOME=/srv/100%%ct" in unit.splitlines()
    assert "ExecStart=/opt/ct/bin/wgl --data=/srv/100%%ct start" in unit.splitlines()


def test_a_unit_path_with_whitespace_is_refused():
    with pytest.raises(ValueError, match="whitespace"):
        render_unit(
            bin_dir=Path("/opt/my ct/bin"), data_dir=Path("/srv/ct"), timeout=Path("/bin/timeout")
        )
