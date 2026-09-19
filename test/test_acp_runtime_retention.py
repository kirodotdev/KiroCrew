"""Restricted provider diagnostics retain lifecycle facts without session bodies."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead, AcpRuntimeError

CANARY = "RESTRICTED_SESSION_BODY_CANARY"


def _stream(payload):
    reader = asyncio.StreamReader()
    reader.feed_data(payload.encode())
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("expected", [False, True])
async def test_runtime_shutdown_diagnostics_respect_retention(mode, expected, caplog):
    runtime = AcpRuntime(memory_mode=mode)
    runtime._process = SimpleNamespace(
        stderr=_stream(f"not logged in: {CANARY}\n"), returncode=None
    )
    pending = asyncio.get_running_loop().create_future()
    runtime._pending_requests[1] = pending

    with caplog.at_level(logging.DEBUG):
        await runtime._drain_stderr()
        exit_reason = runtime._exit_reason(1)
        runtime._mark_dead(f"reader failed: {CANARY}", expected=expected)

    assert runtime.saw_not_logged_in()
    error = pending.exception()
    assert isinstance(error, AcpRuntimeDead)
    assert "returncode=None" in runtime.death_summary()
    if mode == "persistent":
        assert CANARY in caplog.text
        assert CANARY in exit_reason
    else:
        assert not runtime._stderr_lines
        assert exit_reason == "process exited (rc=1)"
        assert CANARY not in caplog.text
        assert CANARY not in runtime.death_summary()
        assert CANARY not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_client_stderr_and_eof_error_respect_retention(mode, caplog):
    client = AcpClient()
    client.memory_mode = mode
    client._process = SimpleNamespace(stdout=_stream(""), returncode=1)

    with caplog.at_level(logging.DEBUG):
        await client._drain_stderr(_stream(CANARY + "\n"))
        with pytest.raises(AcpError) as failed:
            await client._read_message(timeout=1)

    assert "code=1" in str(failed.value)
    if mode == "persistent":
        assert CANARY in caplog.text
        assert CANARY in str(failed.value)
    else:
        assert not client._stderr_lines
        assert CANARY not in caplog.text
        assert CANARY not in str(failed.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
@pytest.mark.parametrize("drain", ["stderr", "stdout"])
async def test_restricted_reader_errors_do_not_log_exception_payload(mode, drain, caplog):
    runtime = AcpRuntime(memory_mode=mode)
    failed_read = AsyncMock(side_effect=RuntimeError(CANARY))
    runtime._process = SimpleNamespace(
        stderr=SimpleNamespace(readline=failed_read),
        stdout=SimpleNamespace(readuntil=failed_read),
        returncode=1,
    )
    with caplog.at_level(logging.DEBUG):
        if drain == "stderr":
            await runtime._drain_stderr()
        else:
            await runtime._reader_loop()
    assert failed_read.await_count == 1
    assert "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
    assert CANARY not in (runtime.death_summary() or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_session_disables_shared_diagnostics_before_start(mode):
    runtime = AcpRuntime()
    runtime._stderr_lines.append(CANARY)
    # Admission precedes provider initialization and its first awaited operation.
    with pytest.raises(AcpRuntimeError, match="not initialized"):
        await runtime.create_session(memory_mode=mode)
    assert not runtime.recording_allowed
    assert not runtime._stderr_lines
    assert runtime._exit_reason(1) == "process exited (rc=1)"
