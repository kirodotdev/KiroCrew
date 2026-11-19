"""The one remote-session answer, and the surfaces that act on it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kiro_crew import cli_doctor, platform_compat, shell_origin
from kiro_crew.auth.shape import InstallShape, detect_shape
from kiro_crew.doctor_checks import services


@pytest.fixture(autouse=True)
def _fresh_ssm_answer() -> Iterator[None]:
    """The ancestry answer is cached per process; each test walks its own table,
    and a faked answer never leaks into a later test on the same worker."""
    cached = shell_origin.is_ssm_session
    cached.cache_clear()
    yield
    cached.cache_clear()


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A local desktop: no SSH variables, no Session Manager ancestor."""
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "KIRO_AUTH_INSTALL_SHAPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shell_origin, "is_ssm_session", lambda: False)
    monkeypatch.setattr(cli_doctor, "machine_hostname", lambda: "ip-box-own-name")
    return monkeypatch


def _process_table(monkeypatch: pytest.MonkeyPatch, chain: dict[int, tuple[str, int]]) -> None:
    """Fake ``/proc``: pid -> (comm, parent pid); this process is pid 500."""
    monkeypatch.setattr(shell_origin.os, "getpid", lambda: 500)
    monkeypatch.setattr(
        platform_compat, "linux_process_name", lambda pid: chain.get(pid, (None, 0))[0]
    )
    monkeypatch.setattr(platform_compat, "parent_pid", lambda pid: chain.get(pid, (None, None))[1])


def test_a_session_manager_ancestor_above_a_user_switch_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # shell -> sudo -> bash -> ssm-session-worker (comm cut to 15 chars) -> agent
    _process_table(
        monkeypatch,
        {
            500: ("python3", 400),
            400: ("bash", 300),
            300: ("sudo", 200),
            200: ("ssm-session-wor", 100),
            100: ("amazon-ssm-agen", 1),
        },
    )
    assert shell_origin.is_ssm_session() is True


def test_the_ancestry_walk_runs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _process_table(monkeypatch, {500: ("python3", 200), 200: ("ssm-session-wor", 1)})
    walked: list[int] = []
    read_name = platform_compat.linux_process_name
    monkeypatch.setattr(
        platform_compat, "linux_process_name", lambda pid: walked.append(pid) or read_name(pid)
    )
    assert shell_origin.is_ssm_session() is True
    assert shell_origin.is_ssm_session() is True
    assert walked == [500, 200]


def test_no_session_manager_ancestor_is_not_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    _process_table(monkeypatch, {500: ("python3", 400), 400: ("bash", 1)})
    assert shell_origin.is_ssm_session() is False


def test_a_cyclic_process_table_stops_at_the_depth_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _process_table(monkeypatch, {500: ("python3", 400), 400: ("bash", 500)})
    walked: list[int] = []
    read_name = platform_compat.linux_process_name

    def counted(pid: int) -> str | None:
        walked.append(pid)
        return read_name(pid)

    monkeypatch.setattr(platform_compat, "linux_process_name", counted)
    assert shell_origin.is_ssm_session() is False
    assert len(walked) == shell_origin.SSM_ANCESTRY_MAX_DEPTH


def test_an_unreadable_process_table_is_not_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_pid: int) -> str:
        raise OSError("no /proc")

    monkeypatch.setattr(platform_compat, "linux_process_name", boom)
    assert shell_origin.is_ssm_session() is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.0.0.1 50000 203.0.113.9 22", None),  # documentation range: not global
        ("10.0.0.1 50000 8.8.4.4 22", "8.8.4.4"),
        ("10.0.0.1 50000 10.0.0.2 22", None),
        ("garbage", None),
        ("10.0.0.1 50000 not-an-ip 22", None),
    ],
)
def test_the_ssh_server_address_is_kept_only_when_globally_routable(
    local: pytest.MonkeyPatch, value: str, expected: str | None
) -> None:
    local.setenv("SSH_CONNECTION", value)
    assert shell_origin.ssh_server_address() == expected


def test_a_session_manager_shell_gets_the_remote_login_shape(local: pytest.MonkeyPatch) -> None:
    assert detect_shape() is not InstallShape.REMOTE
    local.setattr(shell_origin, "is_ssm_session", lambda: True)
    assert detect_shape() is InstallShape.REMOTE


def test_a_local_desktop_gets_no_remote_access_hint(
    local: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    services._doctor_remote_access_hint(9999)
    assert capsys.readouterr().out == ""


def test_a_session_manager_shell_gets_a_hint_with_both_ways_to_forward(
    local: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local.setattr(shell_origin, "is_ssm_session", lambda: True)
    services._doctor_remote_access_hint(9999)
    out = capsys.readouterr().out
    assert "ssh -NL 9999:localhost:9999 <the host you reach this box on>" in out
    assert "(this machine calls itself ip-box-own-name)" in out
    assert "AWS-StartPortForwardingSession (localPortNumber=9999, portNumber=9999)" in out
    assert "kirocrew token" in out


def test_ssh_to_a_public_address_names_that_address_not_the_box_name(
    local: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local.setenv("SSH_CONNECTION", "10.0.0.1 50000 8.8.4.4 22")
    services._doctor_remote_access_hint(9999)
    out = capsys.readouterr().out
    assert "ssh -NL 9999:localhost:9999 8.8.4.4\n" in out
    assert "ip-box-own-name" not in out
    assert "Session Manager" not in out


def test_ssh_to_a_private_address_names_a_placeholder_with_the_box_name_beside_it(
    local: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local.setenv("SSH_CONNECTION", "10.0.0.1 50000 10.0.0.2 22")
    services._doctor_remote_access_hint(9999)
    out = capsys.readouterr().out
    assert "ssh -NL 9999:localhost:9999 <the host you reach this box on>\n" in out
    assert "(this machine calls itself ip-box-own-name)" in out
    assert "Session Manager" not in out
