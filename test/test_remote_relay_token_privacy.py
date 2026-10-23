"""The remote-workspace relay token lives only in its runtime's private scratch.

Another session's agent shares the gateway's UID, so a token in a shared
directory would let it reach the relay and run guest commands outside this
session's governance. The scratch is masked from every other session.
"""

from __future__ import annotations

import os
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import relay

pytestmark = pytest.mark.asyncio


async def _launched(tmp_path, monkeypatch):
    launch = remote_worker.RemoteWorkerLaunch("binding", AsyncMock())
    monkeypatch.setattr(launch, "install", AsyncMock())
    monkeypatch.setattr(launch, "reap_stale_workers", AsyncMock())
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=40000))
    monkeypatch.setattr(launch, "stop_worker", AsyncMock())
    server = await launch.workspace_server("/workspace/app")
    return launch, {"server": server, "workdir": "/workspace/app"}


def _runtime(workspace, scratch):
    return SimpleNamespace(_remote_workspace=workspace, work_scratch_dir=scratch)


async def test_the_token_file_lives_in_the_runtime_scratch_only(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    write = relay.write_token_file

    def write_off_loop(path, token):
        assert threading.get_ident() != loop_thread
        write(path, token)

    monkeypatch.setattr(relay, "write_token_file", write_off_loop)
    launch, workspace = await _launched(tmp_path, monkeypatch)
    try:
        assert not (tmp_path / "data").exists()
        scratch = tmp_path / "scratch"
        scratch.mkdir(mode=0o700)
        workspace["write_token"] = launch.relay.write_token
        runtime = _runtime(workspace, scratch)
        await AcpRuntime._write_remote_token(runtime)
        await AcpRuntime._write_remote_token(runtime)  # a reload sends it again
        [token_file] = list(scratch.iterdir())
        assert token_file.read_bytes() == launch.relay._token
        if os.name != "nt":  # Windows has no POSIX owner-only bits; its ACL is inherited
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert workspace["server"]["args"][-2:] == ["--token-file", str(token_file)]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["scratch"]
    finally:
        await launch.close_relay()
    assert not token_file.exists()


async def test_a_runtime_without_scratch_refuses(tmp_path, monkeypatch):
    launch, workspace = await _launched(tmp_path, monkeypatch)
    try:
        workspace["write_token"] = launch.relay.write_token
        with pytest.raises(AcpRuntimeError, match="private scratch"):
            await AcpRuntime._write_remote_token(_runtime(workspace, None))
        assert launch.relay.token_path is None
        assert workspace["server"]["args"][-1] == ""
        assert list(tmp_path.iterdir()) == []
    finally:
        await launch.close_relay()
