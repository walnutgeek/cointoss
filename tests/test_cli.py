"""The `cointoss` CLI: `init`, `sweep`, `status`, `token`.

Each test runs `cointoss.cli.main` against a temporary data directory. CoinGecko is answered
from the recorded `markets` listing and the clock is fixed, through the same seam
`tests/test_app.py` uses, so nothing touches the network. Nothing here calls `systemctl`: the
unit is written into a temporary directory and only its text is checked.
"""

from __future__ import annotations

import json
import shutil
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
    assert {n.nsref.name for n in ns.query("api")} >= {"universes", "members", "runs"}
    # The fragment takes woodglue's data directory, so the file names none.
    assert entry.entries is not None
    assert "init" not in entry.entries[0]

    assert Settings.load(home).universes == DEFAULT_UNIVERSES
    with Store(home / "cointoss.db") as store:
        assert store.universe_names() == [s.name for s in DEFAULT_UNIVERSES]
    assert (home / "auth.db").exists()


def test_a_copied_instance_serves_its_own_store(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    assert run(capsys, "init", "--data-dir", str(home))[0] == 0
    copy = tmp_path / "copy"
    shutil.copytree(home, copy)
    (home / "cointoss.db").unlink()
    # woodglue's data directory wins over the environment the CLI resolves.
    monkeypatch.setenv(HOME_ENV, str(home))
    (ns, _) = load_namespaces(load_config(copy).namespaces, copy)["cointoss"]
    names = [u.name for u in ns.get("data:universes")()]
    assert names == [s.name for s in DEFAULT_UNIVERSES]


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


def test_the_rendered_unit_runs_woodglue_in_the_foreground():
    unit = render_unit(bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/ct"))
    lines = unit.splitlines()
    assert "ExecStart=/opt/ct/bin/wgl --data=/srv/ct start" in lines
    assert "Type=simple" in lines
    # lythonic catches up a missed sweep when the server starts.
    assert "ExecStartPre" not in unit
    assert "$" not in unit
    assert SHIPPED_UNIT.exists()


def test_a_percent_in_a_unit_path_is_escaped_for_systemd():
    unit = render_unit(bin_dir=Path("/opt/ct/bin"), data_dir=Path("/srv/100%ct"))
    assert "Environment=COINTOSS_HOME=/srv/100%%ct" in unit.splitlines()
    assert "ExecStart=/opt/ct/bin/wgl --data=/srv/100%%ct start" in unit.splitlines()


def test_a_unit_path_with_whitespace_is_refused():
    with pytest.raises(ValueError, match="whitespace"):
        render_unit(bin_dir=Path("/opt/my ct/bin"), data_dir=Path("/srv/ct"))


def test_two_instances_install_side_by_side_under_their_own_unit_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    unit_dir = tmp_path / "systemd"
    integration, dev = tmp_path / "integration", tmp_path / "dev"
    with patch("cointoss.cli.user_unit_dir", return_value=unit_dir):
        code, out = run(capsys, "init", "--data-dir", str(integration), "--systemd")
        assert code == 0, out
        code, out = run(
            capsys,
            "init",
            "--data-dir",
            str(dev),
            "--systemd",
            "--unit",
            "cointoss-dev",
            "--port",
            "5322",
        )
        assert code == 0, out
    assert "systemctl --user enable --now cointoss-dev" in out
    for name, data_dir in (("cointoss", integration), ("cointoss-dev", dev)):
        lines = (unit_dir / f"{name}.service").read_text().splitlines()
        assert f"Environment=COINTOSS_HOME={data_dir}" in lines
        assert any(ln.startswith("ExecStart=") and f"--data={data_dir} " in ln for ln in lines)
        assert any(ln.startswith(f"Description={name}: ") for ln in lines)
    assert sorted(p.name for p in unit_dir.iterdir()) == [
        "cointoss-dev.service",
        "cointoss.service",
    ]


def test_init_writes_the_port_and_schedule_it_is_given(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out = run(
        capsys, "init", "--data-dir", str(home), "--port", "5322", "--schedule", "35 0 * * *"
    )
    assert code == 0, out
    assert "warning" not in out
    config = load_config(home)
    assert config.port == 5322
    (ns, _) = load_namespaces(config.namespaces, home)["cointoss"]
    _, trigger = ns.get_trigger(SWEEP_TRIGGER)
    assert trigger.schedule == "35 0 * * *"


def test_init_keeps_a_woodglue_yaml_with_a_different_port_or_schedule_and_says_so(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    assert run(capsys, "init", "--data-dir", str(home))[0] == 0
    before = (home / "woodglue.yaml").read_text()

    code, out = run(
        capsys, "init", "--data-dir", str(home), "--port", "5322", "--schedule", "35 0 * * *"
    )

    assert code == 0, out
    assert (home / "woodglue.yaml").read_text() == before
    differs = [ln for ln in out.splitlines() if "differs" in ln]
    assert len(differs) == 2
    assert "5321" in differs[0] and "5322" in differs[0]
    assert "5 0 * * *" in differs[1] and "35 0 * * *" in differs[1]

    code, out = run(
        capsys, "init", "--data-dir", str(home), "--port", "5321", "--schedule", "5  0 * * *"
    )
    assert code == 0, out
    assert "differs" not in out

    # The kept file's schedule is what runs, so a late one asked for now is not warned about.
    code, out = run(capsys, "init", "--data-dir", str(home), "--schedule", "0 12 * * *")
    assert code == 0, out
    assert "differs" in out
    assert "warning" not in out


def test_an_explicit_default_unit_matches_a_plain_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    unit_dir = tmp_path / "systemd"
    outputs: list[str] = []
    units: list[str] = []
    for name, extra in (("plain", []), ("explicit", ["--unit", "cointoss"])):
        data_dir = tmp_path / name
        with patch("cointoss.cli.user_unit_dir", return_value=unit_dir / name):
            code, out = run(capsys, "init", "--data-dir", str(data_dir), "--systemd", *extra)
        assert code == 0, out
        outputs.append(out.replace(str(data_dir), "D").replace(str(unit_dir / name), "U"))
        units.append((unit_dir / name / "cointoss.service").read_text().replace(str(data_dir), "D"))
        assert (data_dir / "woodglue.yaml").read_text() == (
            tmp_path / "plain" / "woodglue.yaml"
        ).read_text()
    assert outputs[0] == outputs[1]
    assert units[0] == units[1]


@pytest.mark.parametrize(
    "args",
    [
        ["--systemd", "--unit", "a/b"],
        ["--systemd", "--unit", "x.service"],
        ["--systemd", "--unit", ""],
        ["--systemd", "--unit", "cointoss@dev"],
        ["--unit", "cointoss-dev"],
        ["--schedule", "not cron"],
        ["--schedule", "0 0 * * * *"],
        ["--schedule", "0 0 30 2 *"],
        ["--port", "0"],
        ["--port", "70000"],
    ],
)
def test_init_refuses_a_bad_option_with_one_line_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str]
):
    data_dir, unit_dir = tmp_path / "instance", tmp_path / "systemd"
    with patch("cointoss.cli.user_unit_dir", return_value=unit_dir):
        code, out = run(capsys, "init", "--data-dir", str(data_dir), *args)
    assert code == 1
    (line,) = out.strip().splitlines()
    assert line.startswith("cointoss init: ")
    assert not data_dir.exists()
    assert not unit_dir.exists()


def test_a_schedule_after_the_bar_window_is_written_with_a_warning(
    home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out = run(capsys, "init", "--data-dir", str(home), "--schedule", "0 12 * * *")
    assert code == 0, out
    (warning,) = [ln for ln in out.splitlines() if ln.startswith("warning")]
    assert "12:00" in warning and "no bars" in warning
    assert "0 12 * * *" in (home / "woodglue.yaml").read_text()
