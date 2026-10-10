"""The ACP exit error names the signal and says whether Crew sent it.

A Crew-initiated kill and an external kill (OOM killer, ``kill -9`` from a
shell) both leave a negative return code. Without an origin marker the two
read the same in the error, so these tests pin the text for each case and the
INFO line ``_kill_process`` writes when it sends a signal itself.
"""

from __future__ import annotations

import logging
import signal
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.acp.client as client_mod
from kiro_crew.acp.client import AcpClient, AcpError

IS_POSIX = hasattr(signal, "SIGKILL")
posix_only = pytest.mark.skipif(not IS_POSIX, reason="needs POSIX signal numbers")


def _dead_client(returncode: int) -> AcpClient:
    client = AcpClient()
    client._cancelled = False
    client._buffer = MagicMock()
    client._buffer.__bool__ = lambda s: False
    client._process = MagicMock()
    client._process.returncode = returncode
    client._process.stdout = MagicMock()
    client._process.stdout.readline = AsyncMock(return_value=b"")
    client._stderr_lines = deque()
    client._stderr_task = MagicMock()
    client._stderr_task.done.return_value = True
    return client


async def _exit_message(client: AcpClient) -> str:
    with pytest.raises(AcpError, match="ACP process exited") as exc_info:
        await client._read_message(timeout=1.0)
    return str(exc_info.value)


async def _force_kill(client: AcpClient) -> None:
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = proc.stdout = proc.stderr = None
    proc.wait = AsyncMock(return_value=-9)
    client._process = proc
    client._pid = 4321
    client._child_pids = {}
    with (
        patch.object(client_mod.platform_compat, "IS_WINDOWS", False),
        patch.object(client_mod.platform_compat, "kill_process_tree_async", AsyncMock()),
        patch.object(client_mod.runtime_process_tree, "_get_child_pids", return_value=[]),
        patch.object(client_mod.runtime_process_tree, "_kill_escaped_children"),
    ):
        await client._kill_process(force=True)


@posix_only
@pytest.mark.asyncio
async def test_self_kill_names_signal_and_crew(caplog: pytest.LogCaptureFixture) -> None:
    client = AcpClient()
    with caplog.at_level(logging.INFO, logger=client_mod.logger.name):
        await _force_kill(client)
    assert any(
        "Sending SIGKILL to ACP PID 4321 (reason: forced stop)" in r.getMessage()
        for r in caplog.records
    )
    dead = _dead_client(-signal.SIGKILL)
    dead._kill_sent = client._kill_sent
    message = await _exit_message(dead)
    assert "SIGKILL" in message
    assert "killed by Crew (forced stop)" in message
    assert "external" not in message


@posix_only
@pytest.mark.asyncio
async def test_unsent_signal_is_external() -> None:
    message = await _exit_message(_dead_client(-signal.SIGKILL))
    assert "SIGKILL" in message
    assert "external" in message
    assert "killed by Crew" not in message


@posix_only
@pytest.mark.asyncio
async def test_different_signal_than_sent_is_external() -> None:
    dead = _dead_client(-signal.SIGKILL)
    dead._kill_sent = (int(signal.SIGTERM), "stop requested")
    message = await _exit_message(dead)
    assert "external" in message
    assert "killed by Crew" not in message


@pytest.mark.asyncio
async def test_positive_code_message_unchanged() -> None:
    dead = _dead_client(1)
    dead._kill_sent = (9, "forced stop")
    assert await _exit_message(dead) == "ACP process exited (code=1)"
