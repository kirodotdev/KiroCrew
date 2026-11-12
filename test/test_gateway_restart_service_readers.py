"""What the service manager has loaded, and what the launchd launcher runs.

``gateway_restart._systemd_show`` and ``gateway_restart._launcher_target`` are the
two readers the restart identity check and the doctor share. Each case gives the
reader a stand-in answer: no ``systemctl`` runs and no service is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import gateway_restart, platform_compat
from kiro_crew.service import common

_SYSTEMCTL = "/usr/bin/systemctl"


def _manager_answers(monkeypatch, stdout: str | None) -> list[tuple[list[str], dict]]:
    """Answer ``systemctl show`` with ``stdout``; return the calls that were made."""
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: _SYSTEMCTL if name == "systemctl" else None,
    )
    monkeypatch.setattr(gateway_restart.subprocess, "run", run)
    return calls


def test_the_unit_is_inconclusive_when_systemctl_is_outside_the_trusted_directories(
    monkeypatch,
) -> None:
    calls = _manager_answers(monkeypatch, "Id=kirocrew.service\n")
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)

    with pytest.raises(gateway_restart._Inconclusive, match="trusted system directory"):
        gateway_restart._systemd_show(user=True)

    assert calls == []


@pytest.mark.parametrize("user", [True, False])
def test_each_scope_is_asked_about_the_service_unit_through_its_own_manager(
    monkeypatch, user
) -> None:
    calls = _manager_answers(monkeypatch, "Id=kirocrew.service\n")
    bus = {"XDG_RUNTIME_DIR": "/run/user/1000"}
    monkeypatch.setattr(common, "systemctl_user_env", lambda: bus)

    gateway_restart._systemd_show(user=user)

    [(argv, kwargs)] = calls
    assert argv[0] == _SYSTEMCTL
    assert argv[1:3] == (["--user", "show"] if user else ["show", "-p"])
    assert argv[-1] == f"{common.SERVICE_NAME}.service"
    asked = {argv[i + 1] for i, word in enumerate(argv) if word == "-p"}
    # What the restart identity check reads to tell whose gateway the unit runs.
    assert {"Id", "LoadState", "MainPID"} <= asked
    # Only the user manager needs the session-bus pointers backfilled.
    assert kwargs["env"] == (bus if user else None)
    assert kwargs["timeout"] == gateway_restart._SYSTEMCTL_TIMEOUT_SECS
    assert kwargs["check"] is False


def test_a_property_listed_more_than_once_keeps_every_value_in_order(monkeypatch) -> None:
    _manager_answers(
        monkeypatch,
        "Id=kirocrew.service\n"
        " MainPID = 4242 \n"
        "ExecStart={ path=/usr/bin/kirocrew ; argv[]=/usr/bin/kirocrew gateway }\n"
        "\n"
        "a line that is not a property\n"
        "ExecStart={ path=/opt/crew/kirocrew ; argv[]=/opt/crew/kirocrew gateway }\n"
        "Environment=KIROCREW_HOME=/srv/crew LANG=C.UTF-8\n",
    )

    assert gateway_restart._systemd_show(user=False) == {
        "Id": ["kirocrew.service"],
        "MainPID": ["4242"],
        "ExecStart": [
            "{ path=/usr/bin/kirocrew ; argv[]=/usr/bin/kirocrew gateway }",
            "{ path=/opt/crew/kirocrew ; argv[]=/opt/crew/kirocrew gateway }",
        ],
        # Only the first "=" separates the name from the value.
        "Environment": ["KIROCREW_HOME=/srv/crew LANG=C.UTF-8"],
    }


@pytest.mark.parametrize("stdout", [None, ""])
def test_a_manager_that_prints_nothing_yields_no_properties(monkeypatch, stdout) -> None:
    _manager_answers(monkeypatch, stdout)

    assert gateway_restart._systemd_show(user=False) == {}


def test_a_launcher_that_is_not_a_file_is_its_own_target(tmp_path) -> None:
    missing = tmp_path / "launcher"

    assert gateway_restart._launcher_target(missing) == str(missing)


def test_the_target_is_the_program_on_the_last_exec_line(tmp_path) -> None:
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        "if [ -x /old/python ]; then\n"
        "exec /old/python -m kiro_crew gateway\n"
        "fi\n"
        'exec "/opt/crew dir/bin/python" -m kiro_crew gateway "$@"\n'
        "# exec /a/comment/is/not/an/exec/line\n",
        encoding="utf-8",
    )

    assert gateway_restart._launcher_target(launcher) == "/opt/crew dir/bin/python"


@pytest.mark.parametrize("script", ["#!/bin/sh\necho no hand-off\n", "#!/bin/sh\nexec \n"])
def test_a_launcher_that_names_no_program_is_its_own_target(tmp_path, script) -> None:
    launcher = tmp_path / "launcher"
    launcher.write_text(script, encoding="utf-8")

    assert gateway_restart._launcher_target(launcher) == str(launcher)


def test_a_launcher_that_cannot_be_read_as_text_proves_nothing(tmp_path) -> None:
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"\xff\xfe exec /opt/crew/bin/python\n")

    with pytest.raises(gateway_restart._Inconclusive, match="could not be read") as refused:
        gateway_restart._launcher_target(launcher)

    assert str(launcher) in str(refused.value)


def test_an_exec_line_the_shell_grammar_cannot_split_is_a_value_error(tmp_path) -> None:
    """The doctor's caller turns this into "no program", so it must not be swallowed here."""
    launcher = tmp_path / "launcher"
    launcher.write_text('exec "/opt/crew/bin/python -m kiro_crew\n', encoding="utf-8")

    with pytest.raises(ValueError, match="closing quotation"):
        gateway_restart._launcher_target(launcher)
