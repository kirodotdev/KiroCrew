"""``kirocrew stop --port N --expect-pid P`` stops P only while it is provably this home's gateway.

Every seam the stop reads is faked: the listener lookup, the argv identity check,
the gateway-lock oracle, the pidfd and the signal itself. A refusal must send no signal at all, and the plain ``--port`` path must
not consult any of the new checks.
"""

import os
import signal
from pathlib import Path

import pytest

from kiro_crew import cli, cli_server, platform_compat
from kiro_crew.gateway_lock import LockHolder, LockProbeError


class _SelRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw) -> None:
        self.calls.append(kw)


@pytest.fixture
def sel_rec(monkeypatch):
    rec = _SelRecorder()
    monkeypatch.setattr(cli_server, "sel", lambda: rec)
    return rec


@pytest.fixture
def host(monkeypatch):
    """A host without pidfd whose port 5476 is held by pid 4242, this home's lock holder."""
    state = {
        "listeners": [4242],
        "lock": LockHolder(pid=4242, alive=True, source="flock_owner"),
        "kirocrew": True,
        "signals": [],
        "kill_error": None,
        "closed": [],
    }

    def kill(pid, sig):
        if state["kill_error"] is not None:
            raise state["kill_error"]
        state["signals"].append(("pid", pid, sig))

    def holder(home):
        if isinstance(state["lock"], Exception):
            raise state["lock"]
        return state["lock"]

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: p)
    monkeypatch.setattr(
        platform_compat, "find_listening_pids", lambda port: list(state["listeners"])
    )
    monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: state["kirocrew"])
    monkeypatch.setattr(cli_server, "lock_holder", holder)
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda pid: True)
    monkeypatch.setattr(cli_server, "_stop_mcp_gateway_daemon", lambda: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return state


@pytest.fixture
def pidfd_host(host, monkeypatch):
    """The same host with Linux pidfd support."""

    def pidfd_open(pid):
        return 900 + pid

    def pidfd_send_signal(fd, sig):
        if host["kill_error"] is not None:
            raise host["kill_error"]
        host["signals"].append(("pidfd", fd, sig))

    real_close = os.close

    def close(fd):
        if fd >= 900:
            host["closed"].append(fd)
            return
        real_close(fd)

    monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", pidfd_send_signal, raising=False)
    monkeypatch.setattr(os, "close", close)
    return host


def _refused(host, sel_rec, capsys, reason):
    with pytest.raises(SystemExit) as exc:
        cli_server._stop(5476, expect_pid=4242)
    assert exc.value.code == 1
    assert host["signals"] == [], "a refusal sends no signal"
    assert "Not signalling anything" in capsys.readouterr().out
    assert sel_rec.calls[-1]["outcome"] == "denied"
    assert f"reason={reason}" in sel_rec.calls[-1]["resources"]


def test_matching_pid_is_sent_sigterm_through_the_pidfd(pidfd_host, sel_rec, capsys) -> None:
    cli_server._stop(5476, expect_pid=4242)
    assert pidfd_host["signals"] == [("pidfd", 900 + 4242, signal.SIGTERM)]
    assert pidfd_host["closed"] == [900 + 4242]
    assert "Sent SIGTERM to gateway (pid 4242)" in capsys.readouterr().out
    assert sel_rec.calls[-1]["outcome"] == "allowed"
    assert "via=expect_pid" in sel_rec.calls[-1]["resources"]


def test_with_pidfd_a_refusal_still_closes_the_handle(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["listeners"] = [5151]
    _refused(pidfd_host, sel_rec, capsys, "listener_mismatch")
    assert pidfd_host["closed"] == [900 + 4242]


def test_without_pidfd_it_refuses(host, sel_rec, capsys) -> None:
    _refused(host, sel_rec, capsys, "unpinnable_platform")


def test_pidfd_open_without_a_pidfd_sender_refuses(
    pidfd_host, sel_rec, capsys, monkeypatch
) -> None:
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    _refused(pidfd_host, sel_rec, capsys, "unpinnable_platform")
    assert pidfd_host["closed"] == [], "no pidfd is opened when it cannot be used"


def test_windows_is_refused(pidfd_host, sel_rec, capsys, monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _refused(pidfd_host, sel_rec, capsys, "unpinnable_platform")


def test_a_different_listener_is_refused(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["listeners"] = [5151]
    _refused(pidfd_host, sel_rec, capsys, "listener_mismatch")


def test_an_exited_pid_with_a_free_port_is_refused(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["listeners"] = []
    _refused(pidfd_host, sel_rec, capsys, "listener_mismatch")


def test_a_listener_that_does_not_hold_this_homes_lock_is_refused(
    pidfd_host, sel_rec, capsys
) -> None:
    pidfd_host["lock"] = LockHolder(pid=7777, alive=True, source="flock_owner")
    _refused(pidfd_host, sel_rec, capsys, "lock_holder_mismatch")


def test_a_free_lock_is_refused(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["lock"] = LockHolder(pid=None, alive=False, source="none")
    _refused(pidfd_host, sel_rec, capsys, "lock_holder_mismatch")


def test_an_indeterminate_lock_probe_is_refused(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["lock"] = LockProbeError(Path("gateway.lock"), OSError("probe failed"))
    _refused(pidfd_host, sel_rec, capsys, "lock_probe_indeterminate")


def test_a_non_kirocrew_process_is_refused(pidfd_host, sel_rec, capsys) -> None:
    pidfd_host["kirocrew"] = False
    _refused(pidfd_host, sel_rec, capsys, "not_kirocrew")


def test_a_pid_that_exits_before_the_signal_is_reported_not_stopped(
    pidfd_host, sel_rec, capsys
) -> None:
    pidfd_host["kill_error"] = ProcessLookupError()
    _refused(pidfd_host, sel_rec, capsys, "process_already_exited")


def test_an_oversized_pid_is_refused_not_a_traceback(
    pidfd_host, sel_rec, capsys, monkeypatch
) -> None:
    def overflow(pid):
        raise OverflowError("signed integer is greater than maximum")

    monkeypatch.setattr(os, "pidfd_open", overflow, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli_server._stop(5476, expect_pid=2**40)
    assert exc.value.code == 1
    assert pidfd_host["signals"] == []
    assert "reason=invalid_pid" in sel_rec.calls[-1]["resources"]


@pytest.mark.parametrize("pid", [0, -1])
def test_a_non_positive_pid_is_refused(pidfd_host, sel_rec, capsys, pid) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_server._stop(5476, expect_pid=pid)
    assert exc.value.code == 1
    assert pidfd_host["signals"] == []
    assert pidfd_host["closed"] == [], "no pidfd is opened for a non-positive pid"
    assert "reason=invalid_pid" in sel_rec.calls[-1]["resources"]


def test_expect_pid_without_port_is_a_usage_error(host, sel_rec, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_server._stop(None, expect_pid=4242)
    assert exc.value.code == 2
    assert host["signals"] == []


def test_plain_port_stop_never_consults_the_lock(host, sel_rec, capsys, monkeypatch) -> None:
    def unexpected(home):
        raise AssertionError("plain --port must keep its own path")

    monkeypatch.setattr(cli_server, "lock_holder", unexpected)
    cli_server._stop(5476)
    assert host["signals"] == [("pid", 4242, signal.SIGTERM)]


def test_the_cli_passes_expect_pid_through(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        cli_server,
        "_stop",
        lambda port, expect_pid=None: seen.update(port=port, expect_pid=expect_pid),
    )
    monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "5476", "--expect-pid", "4242"])
    cli.main()
    assert seen == {"port": 5476, "expect_pid": 4242}
