"""SSH destination and relay credential IO boundaries, without a guest shell."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import relay


@pytest.mark.asyncio
async def test_shell_pins_both_ssh_forms_to_verified_deployment(monkeypatch):
    monkeypatch.setenv("CODER_URL", "https://unverified.example")
    run = AsyncMock(return_value=b"ok")
    monkeypatch.setattr(remote_worker, "run_collect", run)
    shell = remote_worker.CoderShell("coder", "workspace", "https://verified.example")
    expected = [
        "coder",
        "--url",
        "https://verified.example",
        "ssh",
        "--disable-autostart",
        "workspace",
    ]
    assert shell.interactive_argv() == expected
    assert shell.argv("true") == [*expected, "--", "true"]
    assert await shell.run("true") == b"ok"
    assert run.call_args.args[0] == [*expected, "--", "true"]


@pytest.mark.asyncio
async def test_workspace_server_writes_relay_token_off_loop(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    written = []
    write = relay.write_token_file

    def write_off_loop(directory, name):
        assert threading.get_ident() != loop_thread
        result = write(directory, name)
        written.append(result)
        return result

    monkeypatch.setattr(relay, "write_token_file", write_off_loop)
    monkeypatch.setattr(relay.WorkspaceRelay, "start", AsyncMock(return_value=32123))
    launch = remote_worker.RemoteWorkerLaunch("session", AsyncMock(), tmp_path / "secrets")
    monkeypatch.setattr(launch, "install", AsyncMock())
    monkeypatch.setattr(launch, "reap_stale_workers", AsyncMock())
    server = await asyncio.wait_for(launch.workspace_server("/workspace/app"), 30)
    assert len(written) == 1
    path, token = written[0]
    assert path.read_bytes() == token
    assert server["args"][-2:] == ["--token-file", str(path)]
    assert launch.relay is not None
