"""A Windows policy block on the direct client spawn path is named, not a transport error.

The harnesses ``AcpClient`` starts one process per session for (claude, OpenCode,
goose, pi, Copilot, the DeepSeek Harness) launch through ``client_start._spawn``,
not the runtime's retrying factory. A spawn ``CreateProcess`` refuses with
``winerror`` 5 or 1260 keeps the client's one retry, then raises
``AcpSpawnBlocked`` naming the binary, so the chat shows the policy card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.spawn_blocked import AcpSpawnBlocked
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

_BINARY = r"C:\Users\me\AppData\Roaming\npm\agent.exe"

_DIRECT_BACKENDS = [
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_PI,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
]


def _win_error(winerror: int) -> OSError:
    # What ``CreateProcess`` raises: a code and no file name.
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


class _Adapter:
    async def resolve_spawn(self, ctx):
        return SpawnPlan(argv=[_BINARY, "--acp"], spawn_label="agent")

    async def prepare_spawn_env(self, env, *, to_thread):
        return None


async def _unwrapped(argv, **kwargs):
    return list(argv), None


def _client(backend: str, tmp_path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    # Hermetic: the real one materializes the agent spec, which probes kiro-cli.
    client._prepare_spawn_workspace = lambda: None
    client._cleanup_failed_live_spawn = AsyncMock()
    return client


async def _start(client: AcpClient, error: OSError) -> tuple[BaseException, int]:
    # The real launch tail runs; only process creation fails, as CreateProcess would.
    create = AsyncMock(side_effect=error)
    with (
        patch("kiro_crew.acp.client.process_adapter_for", return_value=_Adapter()),
        patch("kiro_crew.acp.client.create_subprocess_limited", create),
        # No OS sandbox, as on Windows: the wrap is not what this test is about.
        patch("kiro_crew.acp.client.wrap_argv_async", _unwrapped),
        patch("kiro_crew.acp.client._ACP_RESPAWN_BACKOFF_S", 0),
        patch("kiro_crew.acp.client.assert_voice_runtime_outside_agent_workspace"),
    ):
        with pytest.raises(Exception) as raised:
            await client.ensure_ready()
    return raised.value, create.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", _DIRECT_BACKENDS)
@pytest.mark.parametrize("code", [5, 1260])
async def test_a_policy_block_names_the_binary_after_the_retry(backend, code, tmp_path):
    error, spawns = await _start(_client(backend, tmp_path), _win_error(code))
    assert spawns == 2  # the client's one retry still runs first
    assert isinstance(error, AcpSpawnBlocked)
    assert error.binary == _BINARY
    assert error.transient is False
    assert _terminal_error_meta(error) == {
        "notice": "windows_spawn_blocked",
        "notice_args": {"binary": _BINARY},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", _DIRECT_BACKENDS)
async def test_any_other_spawn_error_is_unchanged(backend, tmp_path):
    other = _win_error(32)
    error, spawns = await _start(_client(backend, tmp_path), other)
    assert spawns == 2
    assert error is other
    assert not isinstance(error, AcpSpawnBlocked)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", _DIRECT_BACKENDS)
async def test_access_denied_after_the_spawn_is_not_a_policy_block(backend, tmp_path):
    # A started agent whose later file cleanup is denied is not a blocked binary.
    client = _client(backend, tmp_path)
    denied = _win_error(5)
    client._initialize_session = AsyncMock(side_effect=denied)
    started = AsyncMock(return_value=None)

    async def _spawned():
        await started()
        client._process = MagicMock(returncode=None)

    client._spawn = _spawned
    with patch("kiro_crew.acp.client._ACP_RESPAWN_BACKOFF_S", 0):
        with pytest.raises(OSError) as raised:
            await client.ensure_ready()
    assert raised.value is denied
    assert started.await_count == 2
