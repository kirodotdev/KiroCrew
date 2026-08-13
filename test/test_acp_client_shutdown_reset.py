"""``AcpClient.shutdown()`` must reset its state even when the kill fails.

``shutdown`` awaited ``_kill_process(force=True)`` and then called
``_reset_state()`` sequentially, so any exception out of the kill skipped the
reset entirely.

``_kill_process`` has several exits: four ``run_in_executor`` awaits (child
scan, record capture, escaped-child sweep) that are not individually guarded,
``subprocess_executor()`` refusing new work once the loop is tearing down, and
``asyncio.CancelledError`` -- a ``BaseException`` -- arriving mid-await, which
is precisely what a shutdown produces.

Nothing retries. Every caller treats ``shutdown`` as terminal and drops the
client right after: ``AcpWorker`` (``knowledge/llm_pool.py``) and
``_shutdown_quietly`` (``connections/mint.py``) both ``except Exception``, log,
and set their reference to ``None``. So a skipped reset is permanent.

The effect asserted here is the one with a security shape: for the claude
backend ``_reset_state`` deletes ``.claude/settings.local.json``, which exists
only to carry ``bypassPermissions`` for the live session. These tests use a real
work directory and a real file rather than asserting a mock was called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient


def _claude_client(tmp_path):
    """A client whose reset has one plainly observable effect on disk."""
    client = AcpClient(work_dir=tmp_path)
    # `_is_claude` is a read-only property over the backend seam.
    client._acp_backend = ACP_BACKEND_CLAUDE
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"permissions": "bypassPermissions"}', encoding="utf-8")
    # No live child: the reset's PID bookkeeping is not what these pin.
    client._process = None
    client._pid = None
    client._child_pids = {}
    return client, settings


@pytest.mark.asyncio
async def test_a_cancelled_kill_still_resets_the_client(tmp_path):
    """Cancellation is the shutdown case, and it must not skip the reset."""
    client, settings = _claude_client(tmp_path)
    client._kill_process = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.shutdown()

    assert not settings.exists(), (
        "settings.local.json outlived the session it granted bypassPermissions "
        "for; no caller retries shutdown, so nothing else removes it"
    )
    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_failing_kill_still_resets_the_client(tmp_path):
    """The executor refusing work during teardown looks like this."""
    client, settings = _claude_client(tmp_path)
    client._kill_process = AsyncMock(
        side_effect=RuntimeError("cannot schedule new futures after shutdown")
    )

    with pytest.raises(RuntimeError):
        await client.shutdown()

    assert not settings.exists()
    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_clean_shutdown_still_resets(tmp_path):
    """Control: the path that already worked must keep working."""
    client, settings = _claude_client(tmp_path)
    client._kill_process = AsyncMock()

    await client.shutdown()

    assert not settings.exists()
    assert client._session_id is None
