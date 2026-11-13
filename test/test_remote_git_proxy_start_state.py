"""The gateway's side of the first-command wait: which proxy start a tool server waits for.

Split out of ``test_remote_git_proxy_admission`` (file-size cap): the launch tells
each new tool server whether a git proxy start is in flight, and a start that
fails marks itself failed in the guest so no command waits on it.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import remote_worker
from kiro_crew.coder.remote_worker import RemoteWorkerError, RemoteWorkerLaunch
from kiro_crew.remote_session import git_proxy


class _Shell:
    def __init__(self):
        self.commands: list[str] = []

    async def run(self, command, stdin=b""):
        self.commands.append(command)
        return b""

    def argv(self, command):
        return ["sh", "-c", command]


def _launch():
    shell = _Shell()
    return RemoteWorkerLaunch("proxy-state", shell), shell


@pytest.mark.asyncio
async def test_no_start_and_a_failed_start_both_read_as_no_proxy(monkeypatch):
    launch, shell = _launch()
    assert launch.git_proxy_state() == "none"  # nothing started: no command waits
    gate = asyncio.Event()

    async def slow_fail(*args, **kwargs):
        await gate.wait()
        raise RemoteWorkerError("python3 is not installed")

    monkeypatch.setattr(launch, "start_worker", slow_fail)
    monkeypatch.setattr(launch, "stop_worker", AsyncMock())
    launch.begin_git_proxy("dashboard:s", agent="researcher")
    # In flight: a tool server opened now waits for start 1.
    assert launch.git_proxy_state() == "starting:1"
    gate.set()
    await asyncio.wait_for(launch._git_proxy_start, 30)
    assert launch.git_proxy_state() == "none"
    # The failure is marked in the guest, by number, beside the port file.
    marker = launch.git_proxy_port_file + remote_worker.GIT_PROXY_FAILED_SUFFIX
    assert any(marker in c and "printf %s 1 >" in c for c in shell.commands), shell.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", None])
async def test_a_start_refused_before_any_worker_still_marks_itself_failed(agent):
    launch, shell = _launch()
    launch.begin_git_proxy("dashboard:s", agent=agent)
    await asyncio.wait_for(launch._git_proxy_start, 30)
    assert launch.git_proxy_state() == "none"
    assert any("printf %s 1 >" in c for c in shell.commands)


@pytest.mark.asyncio
async def test_a_running_proxy_reads_as_starting_and_marks_nothing(monkeypatch):
    launch, shell = _launch()
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=1))
    monkeypatch.setattr(launch, "dial", lambda port: None)
    link = SimpleNamespace(start=lambda: None, write=AsyncMock())

    class Channel:
        heard = True

        def __init__(self, *a, **k):
            pass

        async def serve(self):
            await asyncio.Event().wait()

    monkeypatch.setattr("kiro_crew.remote_session.git_gateway.GitProxyChannel", Channel)
    launch.begin_git_proxy("dashboard:s", agent="researcher", link_factory=lambda *a, **k: link)
    await asyncio.wait_for(launch._git_proxy_start, 30)
    try:
        assert launch.git_proxy_state() == "starting:1"
        assert shell.commands == []
    finally:
        launch.git_proxy[2].cancel()
        launch.git_proxy = None


@pytest.mark.asyncio
@pytest.mark.parametrize("heard", [False, True])
async def test_a_guest_that_ends_before_its_first_frame_marks_its_start_failed(monkeypatch, heard):
    """A guest that refuses to start (another build's gateway, no python) ends
    its link without a frame: git's wait for it ends then, not at its bound."""
    from kiro_crew.testing.wait import async_wait_until

    launch, shell = _launch()
    start = AsyncMock(return_value=1)
    monkeypatch.setattr(launch, "start_worker", start)
    monkeypatch.setattr(launch, "dial", lambda port: None)
    link = SimpleNamespace(start=lambda: None, write=AsyncMock())

    class Channel:
        def __init__(self, *a, **k):
            self.heard = heard

        async def serve(self):
            return None

    monkeypatch.setattr("kiro_crew.remote_session.git_gateway.GitProxyChannel", Channel)
    launch.begin_git_proxy("dashboard:s", agent="researcher", link_factory=lambda *a, **k: link)
    await asyncio.wait_for(launch._git_proxy_start, 30)
    argv = start.await_args.args[1]
    assert argv[argv.index("--build") + 1] == git_proxy.build_digest()
    await asyncio.wait_for(asyncio.shield(launch.git_proxy[2]), 30)
    marked = lambda: any("printf %s 1 >" in c for c in shell.commands)  # noqa: E731
    if heard:
        await asyncio.sleep(0)
        assert not marked()
    else:
        await async_wait_until(marked, describe=lambda: f"no failure mark: {shell.commands}")
    launch.git_proxy = None


def test_the_guest_proxy_binds_without_a_name_lookup(monkeypatch):
    """``HTTPServer.server_bind`` resolves ``getfqdn`` before listening, which stalls
    for tens of seconds on some macOS hosts and holds back the port file."""

    def no_lookup(*args):
        raise AssertionError("the proxy resolved a host name to bind")

    monkeypatch.setattr(socket, "getfqdn", no_lookup)
    server = git_proxy.Server(("127.0.0.1", 0), git_proxy.Handler)
    try:
        assert server.server_name == "127.0.0.1" and server.server_port > 0
    finally:
        server.server_close()
