from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_acp_provider import _build_provider

from kiro_crew.testing.ids import UNALLOCATABLE_PID


def _kiro_provider():
    provider = _build_provider(backend="")
    provider._client._work_dir = "/tmp/ws"
    provider._client._agent = "kirocrew"
    provider._client._sandbox_mode = "auto"
    provider._client._extra_env = {}
    provider._client._mcp_gateway_overlay = None
    provider._client._mcp_gateway_socket = None
    provider._client._model = "auto"
    provider._client._resume_session_id = ""
    return provider


def _runtime(create_session_error: BaseException) -> MagicMock:
    runtime = MagicMock()
    runtime.pid = UNALLOCATABLE_PID
    runtime.spawn = AsyncMock()
    runtime.kill = AsyncMock()
    runtime.saw_not_logged_in = MagicMock(return_value=False)
    runtime.saw_sandbox_init_failure = MagicMock(return_value=False)
    runtime.saw_delegate_launch_failure = MagicMock(return_value=False)
    runtime.kas_extract_broken_message = MagicMock(return_value="")
    runtime.settle_stderr = AsyncMock()
    runtime.create_session = AsyncMock(side_effect=create_session_error)
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("session limit reached"), "failed session setup cleanup"),
        (asyncio.CancelledError(), "cancelled session setup cleanup"),
    ],
)
async def test_a_setup_cleanup_kill_names_why_the_setup_ended(error, reason):
    provider = _kiro_provider()
    runtime = _runtime(error)

    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
    ):
        with pytest.raises(type(error)):
            await provider._start_kiro_runtime()

    runtime.kill.assert_awaited_once_with(expected=True, reason=reason)
