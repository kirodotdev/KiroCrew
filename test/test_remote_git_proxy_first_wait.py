"""The guest's first git command waits, bounded, for the proxy and the gateway's list.

Split from ``test_remote_git_proxy`` (file-size cap): the proxy starting beside
the tool server, and a partial lend list that keeps the command waiting.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import workspace_tools
from kiro_crew.remote_session.git_gateway import HOSTS

PROXY_SECRET = "a" * 64
#: The guest proxy runs inside the remote workspace, which is always POSIX.
_POSIX_GUEST = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")


def _rewritten_hosts(env):
    values = {v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")}
    return {host for host in HOSTS if f"https://{host}/" in values}


class _Clock:
    """A fake clock whose sleeps advance it; ``on_poll[n]`` runs at the n-th poll."""

    def __init__(self, monkeypatch):
        self.now = 100.0
        self.sleeps: list[float] = []
        self.on_poll: dict[int, object] = {}
        self.listening: set[int] = set()
        monkeypatch.setattr(workspace_tools, "_monotonic", lambda: self.now)
        monkeypatch.setattr(workspace_tools, "_sleep", self.sleep)
        monkeypatch.setattr(workspace_tools, "_listening", lambda port: port in self.listening)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        action = self.on_poll.get(len(self.sleeps))
        if callable(action):
            action()


PORT = 40000


def _proxy_files(tmp_path):
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    failed = Path(str(port_file) + workspace_tools.GIT_PROXY_FAILED_SUFFIX)
    return port_file, lend, failed


def test_a_command_during_an_in_flight_proxy_start_waits_for_it(tmp_path, monkeypatch):
    """The gateway said start 1 is in flight: an early first clone still gets the login."""
    clock = _Clock(monkeypatch)
    port_file, lend, failed = _proxy_files(tmp_path)

    def proxy_up():
        clock.listening.add(PORT)
        port_file.write_text(f"{PORT}\n")
        lend.write_text(f"{PORT} github.com\n")

    started = clock.now
    # An older start's failure is not this start's: the wait goes on past it.
    failed.write_text("0")
    clock.on_poll[4] = proxy_up
    env = workspace_tools.git_proxy_env(
        str(port_file), PROXY_SECRET, startup_since=started, starting="1"
    )
    assert len(clock.sleeps) == 4
    assert _rewritten_hosts(env) == {"github.com"}
    # A start that never answers: the wait ends with the tool server's startup window.
    port_file.unlink()
    clock.listening.clear()
    clock.on_poll.clear()
    assert (
        workspace_tools.git_proxy_env(
            str(port_file), PROXY_SECRET, startup_since=started, starting="1"
        )
        == {}
    )
    assert clock.now >= started + workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS


def test_a_failed_proxy_start_ends_the_wait_at_once(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    port_file, _lend, failed = _proxy_files(tmp_path)
    # The gateway marks start 2 failed while the command waits (third poll).
    clock.on_poll[3] = lambda: failed.write_text("2")
    got = workspace_tools.git_proxy_env(
        str(port_file), PROXY_SECRET, startup_since=clock.now, starting="2"
    )
    assert got == {} and len(clock.sleeps) == 3
    # Already marked when the command runs: no wait at all.
    clock.sleeps.clear()
    got = workspace_tools.git_proxy_env(
        str(port_file), PROXY_SECRET, startup_since=clock.now, starting="2"
    )
    assert got == {} and clock.sleeps == []


def test_with_no_proxy_start_in_flight_no_command_waits(tmp_path, monkeypatch):
    """No launch marker (no start, or the last one failed): a missing proxy costs nothing."""
    clock = _Clock(monkeypatch)
    port_file, _lend, _failed = _proxy_files(tmp_path)
    for starting in ("", None):
        kwargs = {} if starting is None else {"starting": starting}
        got = workspace_tools.git_proxy_env(
            str(port_file), PROXY_SECRET, startup_since=clock.now, **kwargs
        )
        assert got == {}
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    assert clock.sleeps == []


@_POSIX_GUEST
def test_the_tool_server_passes_its_start_and_the_gateways_marker(tmp_path, monkeypatch):
    seen = {}

    def env(port_file, secret, base=None, *, startup_since=None, starting=""):
        seen.update(since=startup_since, starting=starting)
        return {}

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: 7.0)
    workspace = workspace_tools.Workspace(
        str(tmp_path), "git-proxy.port", git_proxy_secret=PROXY_SECRET, git_proxy_starting="3"
    )
    monkeypatch.setattr(workspace_tools, "git_proxy_env", env)
    with contextlib.suppress(workspace_tools.ToolError):
        workspace.run_command("true")
    assert seen == {"since": 7.0, "starting": "3"}


@pytest.mark.parametrize(
    ("line", "starting"),
    [
        (f"{PROXY_SECRET} starting:3\n", "3"),
        (f"{PROXY_SECRET} none\n", ""),
        (f"{PROXY_SECRET}\n", ""),
        (f"{PROXY_SECRET} starting:x\n", ""),
    ],
)
def test_the_tool_server_reads_the_gateways_proxy_state_from_stdin(
    tmp_path, monkeypatch, line, starting
):
    import io

    made = {}

    class Recorded(workspace_tools.Workspace):
        def __init__(self, *args, **kwargs):
            made.update(kwargs)
            raise SystemExit(0)

    monkeypatch.setattr(workspace_tools, "Workspace", Recorded)
    monkeypatch.setattr(
        workspace_tools.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(line.encode()))
    )
    with pytest.raises(SystemExit):
        workspace_tools.main(
            ["--root", str(tmp_path), "--git-proxy-port-file", "p.port", "--git-proxy-secret-stdin"]
        )
    assert made["git_proxy_secret"] == PROXY_SECRET and made["git_proxy_starting"] == starting


def test_the_guest_and_gateway_spell_the_failure_marker_the_same():
    from kiro_crew.coder import remote_worker

    assert remote_worker.GIT_PROXY_FAILED_SUFFIX == workspace_tools.GIT_PROXY_FAILED_SUFFIX


def test_a_partial_first_list_keeps_the_command_waiting(tmp_path, monkeypatch):
    """A login published while another host is still being looked up does not
    end the wait: the slower host would be left out of this command's rewrite."""
    now = [0.0]
    sleeps: list[float] = []
    on_poll: dict[int, object] = {}

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        action = on_poll.get(len(sleeps))
        if callable(action):
            action()

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: now[0])
    monkeypatch.setattr(workspace_tools, "_sleep", sleep)
    monkeypatch.setattr(workspace_tools, "_listening", lambda _port: True)
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    port = 40000
    port_file.write_text(f"{port}\n")
    # GitHub is found first; Azure's lookup completes the list at the fifth poll.
    lend.write_text(f"{port} * github.com\n")
    on_poll[5] = lambda: lend.write_text(f"{port} dev.azure.com github.com\n")
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert len(sleeps) == 5
    assert _rewritten_hosts(env) == {"github.com", "dev.azure.com"}
    # Lookups still running at the bound: the command takes what was found.
    sleeps.clear()
    on_poll.clear()
    lend.write_text(f"{port} * github.com\n")
    start = now[0]
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert now[0] - start >= workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS
    assert _rewritten_hosts(env) == {"github.com"}
    # A partial list with nothing found yet routes nothing at the bound.
    lend.write_text(f"{port} *\n")
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}


def test_the_guest_proxy_marks_a_partial_list(tmp_path):
    from kiro_crew.remote_session import git_proxy

    path = str(tmp_path / "git-proxy.port.lend")
    git_proxy._write_lend(40000, ["github.com"], path, True)
    assert Path(path).read_text() == "40000 * github.com\n"
    git_proxy._write_lend(40000, ["github.com", "*"], path, False)
    assert Path(path).read_text() == "40000 github.com\n"
