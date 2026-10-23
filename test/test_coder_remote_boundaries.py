"""SSH destination and relay credential IO boundaries, without a guest shell."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import relay


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


pytestmark = pytest.mark.asyncio


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


async def test_workspace_server_writes_no_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "write_token_file", MagicMock(side_effect=AssertionError))
    monkeypatch.setattr(relay.WorkspaceRelay, "start", AsyncMock(return_value=32123))
    launch = remote_worker.RemoteWorkerLaunch("session", AsyncMock())
    monkeypatch.setattr(launch, "install", AsyncMock())
    monkeypatch.setattr(launch, "reap_stale_workers", AsyncMock())
    server = await asyncio.wait_for(launch.workspace_server("/workspace/app"), 30)
    # The runtime fills the path in from its own scratch before session/new.
    assert server["args"][-2:] == ["--token-file", ""]
    assert launch.relay is not None and launch.relay.token_path is None


@pytest.mark.parametrize("bound", [False, True])
async def test_cleared_config_does_not_clear_persisted_placement(tmp_path, monkeypatch, bound):
    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config import live, paths
    from kiro_crew.providers import acp as acp_facade
    from kiro_crew.providers import acp_runtime_start as start
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(
        live,
        "snapshot",
        lambda: SimpleNamespace(session_environments=SimpleNamespace(coder_url="", profiles=[])),
    )
    if bound:
        SessionEnvironmentChoices(tmp_path / "coder/session-choices.json").set("chat", "dev")
    runtime = MagicMock()
    runtime.spawn = AsyncMock(side_effect=RuntimeError("local spawn reached"))
    # Through the facade: AcpRuntime is bound by several mirrored owners.
    monkeypatch.setattr(acp_facade, "AcpRuntime", MagicMock(return_value=runtime))
    provider = MagicMock(memory_mode="persistent", _shared_scratch=tmp_path)
    provider._client._session_key = "chat"
    provider._client._resume_session_id = ""
    provider._client._work_dir = tmp_path
    error = SessionEnvironmentUnavailable if bound else RuntimeError
    with pytest.raises(error, match="not configured" if bound else "local spawn reached"):
        await start._start_kiro_runtime_impl(provider, {}, {})
    assert runtime.spawn.await_count == (0 if bound else 1)


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
async def test_collect_refuses_overflow_before_eof_and_reaps(tmp_path, monkeypatch, pipe):
    from kiro_crew.testing.ids import UNALLOCATABLE_PID

    streams = {name: asyncio.StreamReader() for name in ("stdout", "stderr")}
    streams[pipe].feed_data(b"x" * 9)  # No EOF: refusal must not wait for one.
    process = SimpleNamespace(
        **streams,
        stdin=MagicMock(drain=AsyncMock()),
        pid=UNALLOCATABLE_PID,
        returncode=None,
        wait=AsyncMock(return_value=-9),
        kill=MagicMock(),
        communicate=AsyncMock(side_effect=AssertionError("unbounded collection")),
    )

    def killed():
        for stream in streams.values():
            stream.feed_eof()

    process.kill.side_effect = killed
    spawn = AsyncMock(return_value=process)
    kill = AsyncMock()
    monkeypatch.setattr(remote_worker.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(remote_worker.platform_compat, "kill_process_tree_async", kill)
    with pytest.raises(remote_worker.RemoteWorkerError, match="too much data"):
        await asyncio.wait_for(
            remote_worker.run_collect(["guest"], b"request", {}, max_output=8), 30
        )
    kill.assert_awaited_once_with(process.pid, remote_worker.platform_compat.SIGKILL)
    process.kill.assert_called_once()
    assert process.wait.await_count == 2  # Cancel the initial wait, then reap after killing.
    process.communicate.assert_not_awaited()


async def test_collect_accepts_exact_limit_and_delivers_stdin(tmp_path, monkeypatch):
    streams = [asyncio.StreamReader(), asyncio.StreamReader()]
    for stream in streams:
        stream.feed_data(b"12345678")
        stream.feed_eof()
    process = SimpleNamespace(
        stdout=streams[0],
        stderr=streams[1],
        stdin=MagicMock(drain=AsyncMock()),
        returncode=0,
        wait=AsyncMock(return_value=0),
        communicate=AsyncMock(return_value=(b"12345678", b"12345678")),
    )
    monkeypatch.setattr(
        remote_worker.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    assert await remote_worker.run_collect(["guest"], b"request", {}, max_output=8) == b"12345678"
    process.stdin.write.assert_called_once_with(b"request")
    process.stdin.close.assert_called_once()
