"""Each remote runtime's git proxy reports its port to its own tool server only.

Two runtimes can share one guest (a remote session and its subagent). A port
file shared by every proxy would hand the earlier runtime's git traffic to the
later proxy, under the other session's key, and that proxy's exit would delete
the file the earlier runtime still reads.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_remote_session_launch import _launch, sync

from kiro_crew.remote_session import workspace_tools
from kiro_crew.testing.wait import async_wait_until


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")
@sync
async def test_two_launches_in_one_guest_keep_separate_git_proxy_port_files(tmp_path):
    from test_remote_git_proxy import HttpBackendUpstream

    worker_dir = tmp_path / "guest" / ".kirocrew-worker"
    parent, child = _launch(tmp_path, "sess-share"), _launch(tmp_path, "sess-share")
    assert parent.git_proxy_port_file != child.git_proxy_port_file
    await parent.install()
    upstream = HttpBackendUpstream(tmp_path, status=404)

    async def credential(host):
        return "Basic eC1hY2Nlc3MtdG9rZW46dA=="

    async def started(launch) -> Path:
        await launch.start_git_proxy(
            f"dashboard:{id(launch)}",
            credential_source=credential,
            agent="researcher",
            egress_permitted=lambda key, host, agent: True,
            http_session=upstream,
        )
        port_file = worker_dir / launch.git_proxy_port_file
        await async_wait_until(
            port_file.exists, off_loop=True, describe=lambda: "guest proxy port is unpublished"
        )
        return port_file

    try:
        parent_file = await started(parent)
        child_file = await started(child)
        assert parent_file.read_text() != child_file.read_text()
        await child.stop_git_proxy()
        await async_wait_until(
            lambda: not child_file.exists(),
            off_loop=True,
            describe=lambda: "child proxy still owns its port file",
        )
        # The child's exit leaves the parent's proxy reachable to the parent.
        assert parent_file.exists()
        assert workspace_tools.git_proxy_env(str(parent_file), parent.git_proxy_secret)
    finally:
        await child.stop_git_proxy()
        await parent.stop_git_proxy()


def test_the_tool_server_routes_git_through_its_own_port_file(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["cwd"] == str(tmp_path.resolve())
        return SimpleNamespace(stdout=b"guest output", stderr=b"", returncode=0)

    monkeypatch.setattr(workspace_tools.subprocess, "run", run)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        mine, other = tmp_path / "mine.port", tmp_path / "other.port"
        mine.write_text(f"{port}\n")
        ws = workspace_tools.Workspace(str(tmp_path), str(mine), git_proxy_secret="a" * 64)
        assert "guest output" in ws.run_command("env")
        ws = workspace_tools.Workspace(str(tmp_path), str(other))
        assert "guest output" in ws.run_command("env")
    assert calls[0][0] == calls[1][0] == ["/bin/sh", "-c", "env"]
    key = "GIT_CONFIG_KEY_0"
    assert f"127.0.0.1:{port}" in calls[0][1]["env"][key]
    assert key not in calls[1][1]["env"]


def test_tool_server_argv_names_the_launch_port_file(tmp_path, monkeypatch):
    launch = _launch(tmp_path, "sess-argv")
    seen: list[list[str]] = []

    class Relay:
        def __init__(self, _launch, argv, *_args, **_kwargs):
            seen.append(list(argv))

        async def start(self):
            return 1

    from kiro_crew.remote_session import relay

    monkeypatch.setattr(relay, "WorkspaceRelay", Relay)

    async def nothing():
        return None

    monkeypatch.setattr(launch, "install", nothing)
    monkeypatch.setattr(launch, "reap_stale_workers", nothing)
    asyncio.run(launch.workspace_server(str(tmp_path)))
    argv = seen[0]
    assert argv[argv.index("--git-proxy-port-file") + 1] == launch.git_proxy_port_file
