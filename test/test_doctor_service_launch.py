"""``kirocrew doctor``'s "service runs" row: the program the installed service starts.

The row follows the service definition's own program -- the systemd unit's
``ExecStart`` or the launcher the launchd plist runs -- to the file it finally
executes, and warns when a host with the managed wheel install runs its service
out of a Kiro Crew source checkout. Service definitions are POSIX-only, so the
module is too.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from kiro_crew.doctor_checks import install
from kiro_crew.service import controller, live_target

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="systemd units and launchd plists are POSIX-only"
)

_WARN = "The installed service runs a source checkout, not the wheel install."


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _checkout(root: Path) -> Path:
    """A Kiro Crew source clone with its own ``.venv`` console script."""
    (root / ".git").mkdir(parents=True)
    (root / "src" / "kiro_crew").mkdir(parents=True)
    return _executable(root / ".venv" / "bin" / "kirocrew")


def _unit(tmp_path: Path, exec_start: str) -> Path:
    unit = tmp_path / "etc" / "kirocrew.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Service]\nType=simple\n" f"ExecStart={exec_start}\n" "Restart=always\n",
        encoding="utf-8",
    )
    return unit


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A host with a managed wheel install and no live target set."""
    legacy = tmp_path / "crew-venv"
    wheel = _executable(legacy / "bin" / "kirocrew")
    monkeypatch.setenv("KIROCREW_VENV", str(legacy))
    monkeypatch.setattr(live_target, "read_target", lambda: None)
    return {"wheel": wheel, "legacy": legacy}


def _point_at(monkeypatch: pytest.MonkeyPatch, unit: Path | None) -> None:
    monkeypatch.setattr(controller, "installed_unit_path", lambda: unit)


def test_a_service_running_the_wheel_install_is_ok(tmp_path, monkeypatch, host, capsys):
    _point_at(monkeypatch, _unit(tmp_path, f"{host['wheel']} gateway --no-open"))

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert f"service runs: ✅ {host['wheel'].resolve()}" in out
    assert _WARN not in out


def test_a_shared_launcher_repointed_at_a_clone_is_warned_on(tmp_path, monkeypatch, host, capsys):
    """The issue's hijack: ExecStart names ``~/.local/bin/kirocrew``, which a clone's
    installer repointed from the wheel install to the clone's own ``.venv``."""
    clone_bin = _checkout(tmp_path / "clone")
    shared = tmp_path / "home" / ".local" / "bin" / "kirocrew"
    shared.parent.mkdir(parents=True)
    shared.symlink_to(clone_bin)
    _point_at(monkeypatch, _unit(tmp_path, f"{shared} gateway --no-open"))

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert _WARN in out
    assert f"service runs:  {clone_bin.resolve()}" in out
    assert f"wheel install: {host['wheel']}" in out
    assert "Make live" in out


def test_the_stable_link_names_the_wheel_install_when_present(tmp_path, monkeypatch, host, capsys):
    stable = host["legacy"].with_name("crew-venv-current")
    stable.symlink_to(host["legacy"], target_is_directory=True)
    clone_bin = _checkout(tmp_path / "clone")
    _point_at(monkeypatch, _unit(tmp_path, f"{clone_bin} gateway --no-open"))

    install._doctor_service_launch()

    assert f"wheel install: {stable / 'bin' / 'kirocrew'}" in capsys.readouterr().out


def test_a_dev_fleet_live_target_exempts_the_checkout(tmp_path, monkeypatch, host, capsys):
    clone_bin = _checkout(tmp_path / "clone")
    _point_at(monkeypatch, _unit(tmp_path, f"{clone_bin} gateway --no-open"))
    monkeypatch.setattr(live_target, "read_target", lambda: tmp_path / "clone")

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert _WARN not in out
    assert "Dev Fleet live target is set" in out


def test_a_checkout_on_a_host_without_the_wheel_install_is_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KIROCREW_VENV", str(tmp_path / "no-such-venv"))
    monkeypatch.setattr(live_target, "read_target", lambda: None)
    clone_bin = _checkout(tmp_path / "clone")
    _point_at(monkeypatch, _unit(tmp_path, f"{clone_bin} gateway --no-open"))

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert f"service runs: ✅ {clone_bin.resolve()} (source checkout)" in out
    assert _WARN not in out


def test_no_installed_service_prints_nothing(monkeypatch, host, capsys):
    _point_at(monkeypatch, None)

    install._doctor_service_launch()

    assert capsys.readouterr().out == ""


def test_a_unit_without_a_program_is_reported_unreadable(tmp_path, monkeypatch, host, capsys):
    unit = _unit(tmp_path, "")
    _point_at(monkeypatch, unit)

    install._doctor_service_launch()

    assert f"could not read the program the service starts ({unit})" in capsys.readouterr().out


@pytest.mark.parametrize(
    "exec_start",
    [
        '"{bin}" gateway --no-open',
        "-{bin} gateway --no-open",
        "/old/kirocrew gateway\nExecStart=\nExecStart={bin} gateway",
    ],
    ids=["quoted", "systemd-prefix", "reset-then-set"],
)
def test_exec_start_is_read_as_systemd_runs_it(tmp_path, monkeypatch, host, capsys, exec_start):
    clone_bin = _checkout(tmp_path / "clone")
    _point_at(monkeypatch, _unit(tmp_path, exec_start.format(bin=clone_bin)))

    install._doctor_service_launch()

    assert f"service runs:  {clone_bin.resolve()}" in capsys.readouterr().out


def test_a_path_with_a_control_character_is_shown_escaped(tmp_path, monkeypatch, host, capsys):
    clone_bin = _checkout(tmp_path / "clone\x1b]0;x\x07")
    _point_at(monkeypatch, _unit(tmp_path, f'"{clone_bin}" gateway --no-open'))

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert _WARN in out
    assert "\x1b" not in out
    assert repr(str(clone_bin.resolve())) in out


def _plist(tmp_path: Path, program: Path) -> Path:
    plist = tmp_path / "LaunchAgents" / "dev.kiro.crew.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(program), "gateway", "--no-open"]}))
    return plist


def test_launchd_follows_the_live_program_launcher(tmp_path, monkeypatch, host, capsys):
    from kiro_crew.service import macos

    clone_bin = _checkout(tmp_path / "clone")
    launcher = tmp_path / "live-gateway" / "kirocrew"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f'#!/bin/sh\nexec "{clone_bin}" "$@"\n', encoding="utf-8")
    monkeypatch.setattr(macos, "LIVE_PROGRAM", launcher)
    _point_at(monkeypatch, _plist(tmp_path, launcher))

    install._doctor_service_launch()

    out = capsys.readouterr().out
    assert _WARN in out
    assert f"service runs:  {clone_bin.resolve()}" in out


def test_a_launcher_whose_exec_line_cannot_be_split_is_reported_unreadable(
    tmp_path, monkeypatch, host, capsys
):
    from kiro_crew.service import macos

    launcher = tmp_path / "live-gateway" / "kirocrew"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexec "/unterminated "$@"\n', encoding="utf-8")
    monkeypatch.setattr(macos, "LIVE_PROGRAM", launcher)
    _point_at(monkeypatch, _plist(tmp_path, launcher))

    install._doctor_service_launch()

    assert "could not read the program the service starts" in capsys.readouterr().out


def test_launchd_names_its_program_directly(tmp_path, monkeypatch, host, capsys):
    from kiro_crew.service import macos

    monkeypatch.setattr(macos, "LIVE_PROGRAM", tmp_path / "live-gateway" / "kirocrew")
    _point_at(monkeypatch, _plist(tmp_path, host["wheel"]))

    install._doctor_service_launch()

    assert f"service runs: ✅ {host['wheel'].resolve()}" in capsys.readouterr().out


def test_an_unreadable_plist_is_reported_unreadable(tmp_path, monkeypatch, host, capsys):
    plist = tmp_path / "LaunchAgents" / "dev.kiro.crew.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"not a plist")
    _point_at(monkeypatch, plist)

    install._doctor_service_launch()

    assert "could not read the program the service starts" in capsys.readouterr().out
