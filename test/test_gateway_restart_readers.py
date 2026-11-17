"""The two readers ``gateway_restart`` shares with the gateway identity check and the doctor.

``_systemd_show`` is how the gateway reads its own loaded systemd unit, and
``_launcher_target`` is how it reads what the launchd launcher script finally
``exec``s. Their callers stub them out, so these cases drive the readers themselves:
``systemctl`` is replaced by a recording stand-in for ``subprocess.run`` and the
launcher is a real file under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew import gateway_restart
from kiro_crew.service import common

_SYSTEMCTL = "/usr/bin/systemctl"


class _Ran:
    """What the ``subprocess.run`` stand-in was asked, and what it answers."""

    def __init__(self, stdout: str | None) -> None:
        self.stdout = stdout
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


@pytest.fixture
def systemctl(monkeypatch):
    ran = _Ran("")
    monkeypatch.setattr(
        gateway_restart.platform_compat, "trusted_system_bin", lambda name: _SYSTEMCTL
    )
    monkeypatch.setattr(gateway_restart.subprocess, "run", ran)
    monkeypatch.setattr(common, "systemctl_user_env", lambda: {"XDG_RUNTIME_DIR": "/run/user/1"})
    return ran


def test_a_systemctl_outside_the_trusted_directories_is_inconclusive_and_spawns_nothing(
    systemctl, monkeypatch
) -> None:
    monkeypatch.setattr(gateway_restart.platform_compat, "trusted_system_bin", lambda name: None)

    with pytest.raises(gateway_restart._Inconclusive, match="trusted system directory"):
        gateway_restart._systemd_show(user=True)

    assert systemctl.calls == []


def test_the_user_scope_query_asks_the_user_manager_for_the_fixed_properties(systemctl) -> None:
    gateway_restart._systemd_show(user=True)

    [(argv, kwargs)] = systemctl.calls
    assert argv[:3] == [_SYSTEMCTL, "--user", "show"]
    assert argv[-1] == f"{common.SERVICE_NAME}.service"
    wanted = [
        "Id",
        "LoadState",
        "MainPID",
        "NeedDaemonReload",
        "ExecStart",
        "Environment",
        "EnvironmentFiles",
    ]
    assert argv[3:-1] == [word for prop in wanted for word in ("-p", prop)]
    assert kwargs["env"] == {"XDG_RUNTIME_DIR": "/run/user/1"}
    assert kwargs["check"] is False
    assert kwargs["timeout"] == gateway_restart._SYSTEMCTL_TIMEOUT_SECS


def test_the_system_scope_query_has_no_user_flag_and_inherits_the_environment(systemctl) -> None:
    gateway_restart._systemd_show(user=False)

    [(argv, kwargs)] = systemctl.calls
    assert argv[:2] == [_SYSTEMCTL, "show"]
    assert "--user" not in argv
    assert kwargs["env"] is None


def test_the_unit_properties_are_parsed_and_a_repeated_key_keeps_every_value(systemctl) -> None:
    systemctl.stdout = (
        "Id=kirocrew.service\n"
        "MainPID= 4242 \n"
        "Environment=A=1 B=2\n"
        "Environment=C=3\n"
        "not a property line\n"
        "ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m kiro_crew }\n"
    )

    props = gateway_restart._systemd_show(user=True)

    assert props == {
        "Id": ["kirocrew.service"],
        "MainPID": ["4242"],
        "Environment": ["A=1 B=2", "C=3"],
        "ExecStart": ["{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m kiro_crew }"],
    }


def test_a_query_with_no_output_reads_as_no_properties(systemctl) -> None:
    systemctl.stdout = None

    assert gateway_restart._systemd_show(user=False) == {}


def _launcher(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kirocrew-launcher"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_launcher_that_does_not_exist_is_its_own_target(tmp_path) -> None:
    missing = tmp_path / "gone"

    assert gateway_restart._launcher_target(missing) == str(missing)


def test_the_launcher_target_is_what_its_last_exec_line_runs(tmp_path) -> None:
    launcher = _launcher(
        tmp_path,
        "#!/bin/sh\n"
        "exec /old/python -m kiro_crew\n"
        'export KIROCREW_HOME="$HOME/.kiro/crew"\n'
        'exec "/Applications/Kiro Crew/bin/python3" -m kiro_crew serve\n',
    )

    assert gateway_restart._launcher_target(launcher) == "/Applications/Kiro Crew/bin/python3"


def test_a_launcher_with_no_exec_line_is_its_own_target(tmp_path) -> None:
    launcher = _launcher(tmp_path, "#!/bin/sh\n/usr/bin/python3 -m kiro_crew\n")

    assert gateway_restart._launcher_target(launcher) == str(launcher)


def test_a_bare_exec_line_names_no_target_so_the_launcher_is_its_own(tmp_path) -> None:
    launcher = _launcher(tmp_path, "#!/bin/sh\nexec \n")

    assert gateway_restart._launcher_target(launcher) == str(launcher)


def test_an_unreadable_launcher_is_inconclusive_not_a_guess(tmp_path) -> None:
    launcher = tmp_path / "kirocrew-launcher"
    launcher.write_bytes(b"#!/bin/sh\nexec /usr/bin/\xff\xfe\n")

    with pytest.raises(gateway_restart._Inconclusive, match="could not be read"):
        gateway_restart._launcher_target(launcher)
