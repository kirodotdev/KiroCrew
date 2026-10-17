"""The ACP exit error names the signal and says only what the client recorded.

A Crew-initiated kill leaves a negative return code, and so does every signal
the client did not send: the OOM killer, a ``kill -9`` from a shell, another
Crew path's sweep, and a crash the child raises on itself (SIGSEGV, SIGABRT).
The error credits Crew only for the signal ``_kill_process`` recorded sending
and says of every other one that this client did not send it. These tests pin
that text, the INFO line ``_kill_process`` writes when it sends a signal
itself, and the two writes of ``_kill_sent`` the credit depends on: the SIGTERM
record and the per-spawn reset.
"""

from __future__ import annotations

import logging
import signal
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.acp.client as client_mod
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.testing.ids import UNALLOCATABLE_PID

IS_POSIX = hasattr(signal, "SIGKILL")
posix_only = pytest.mark.skipif(not IS_POSIX, reason="needs POSIX signal numbers")
# The label for a signal the client has no record of sending, pinned as text.
NOT_SENT = "not sent by this client"


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


async def _stop(client: AcpClient, *, force: bool) -> None:
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = proc.stdout = proc.stderr = None
    proc.wait = AsyncMock(return_value=-signal.SIGKILL if force else -signal.SIGTERM)
    client._process = proc
    client._pid = 4321
    client._child_pids = {}
    with (
        patch.object(client_mod.platform_compat, "IS_WINDOWS", False),
        patch.object(client_mod.platform_compat, "kill_process_tree_async", AsyncMock()),
        patch.object(client_mod.runtime_process_tree, "_get_child_pids", return_value=[]),
        patch.object(client_mod.runtime_process_tree, "_kill_escaped_children"),
    ):
        await client._kill_process(force=force)


@posix_only
@pytest.mark.asyncio
async def test_self_kill_names_signal_and_crew(caplog: pytest.LogCaptureFixture) -> None:
    client = AcpClient()
    with caplog.at_level(logging.INFO, logger=client_mod.logger.name):
        await _stop(client, force=True)
    assert any(
        "Sending SIGKILL to ACP PID 4321 (reason: forced stop)" in r.getMessage()
        for r in caplog.records
    )
    dead = _dead_client(-signal.SIGKILL)
    dead._kill_sent = client._kill_sent
    message = await _exit_message(dead)
    assert "SIGKILL" in message
    assert "killed by Crew (forced stop)" in message
    assert NOT_SENT not in message


@posix_only
@pytest.mark.asyncio
async def test_graceful_stop_is_credited_to_crew() -> None:
    client = AcpClient()
    await _stop(client, force=False)
    dead = _dead_client(-signal.SIGTERM)
    dead._kill_sent = client._kill_sent
    message = await _exit_message(dead)
    assert message == (
        f"ACP process exited (code={-signal.SIGTERM}, SIGTERM, killed by Crew (stop requested))"
    )


@posix_only
@pytest.mark.asyncio
async def test_unsent_signal_is_not_credited_to_crew() -> None:
    message = await _exit_message(_dead_client(-signal.SIGKILL))
    assert message == f"ACP process exited (code={-signal.SIGKILL}, SIGKILL, {NOT_SENT})"


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGSEGV, signal.SIGABRT], ids=lambda sig: sig.name)
async def test_self_raised_crash_signal_is_not_called_external(sig: signal.Signals) -> None:
    message = await _exit_message(_dead_client(-sig))
    assert message == f"ACP process exited (code={-sig}, {sig.name}, {NOT_SENT})"


@posix_only
@pytest.mark.asyncio
async def test_different_signal_than_sent_is_not_credited_to_crew() -> None:
    dead = _dead_client(-signal.SIGKILL)
    dead._kill_sent = (int(signal.SIGTERM), "stop requested")
    message = await _exit_message(dead)
    assert message == f"ACP process exited (code={-signal.SIGKILL}, SIGKILL, {NOT_SENT})"


@posix_only
@pytest.mark.asyncio
async def test_a_respawn_does_not_credit_the_previous_childs_kill(tmp_path: Path) -> None:
    from test_acp_client import _stop_stderr_drain

    client = AcpClient(work_dir=tmp_path, session_key="k")
    client._kill_sent = (int(signal.SIGKILL), "forced stop")
    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = UNALLOCATABLE_PID
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        await client._spawn()
    await _stop_stderr_drain(client)
    assert client._kill_sent is None
    dead = _dead_client(-signal.SIGKILL)
    dead._kill_sent = client._kill_sent
    assert "killed by Crew" not in await _exit_message(dead)


@pytest.mark.asyncio
async def test_positive_code_message_unchanged() -> None:
    dead = _dead_client(1)
    dead._kill_sent = (9, "forced stop")
    assert await _exit_message(dead) == "ACP process exited (code=1)"
