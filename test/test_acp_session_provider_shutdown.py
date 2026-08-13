"""``AcpSessionProvider.shutdown()`` must destroy the handle even when cancelled.

The shared-subagent arm cancels the in-flight turn and *then* destroys the
handle, sequentially:

```python
if self._handle.is_turn_active:
    try:
        await asyncio.wait_for(self._handle.cancel(), timeout=5.0)
    except Exception:
        logger.debug(...)
try:
    await self._handle.destroy()
```

``asyncio.CancelledError`` is a ``BaseException``, so it walks straight past
``except Exception`` and the destroy never runs. This is not hypothetical: the
session-restart path calls ``asyncio.wait_for(p.shutdown(), timeout=10)`` and
gathers those calls, so a timeout or a cancelled restart task delivers exactly
that cancellation into this coroutine.

``destroy()`` is where two documented invariants live — ``terminate_session``
evicts the session from the SHARED kiro-cli process, and the transcript unlink
is the only thing that removes ``~/.kiro/sessions/cli/{sid}.json(+.jsonl)``
("no separate cleanup call needed", per this method's own comment).

These drive the real provider with a real ``AcpSessionHandle`` and real
transcript files, and assert on the files rather than on a mock call. The only
seam is ``cancel()``, which is made suspendable so the cancellation lands where
production would deliver it. Event-driven; no sleeps.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_provider import AcpSessionProvider


def _handle_with_transcript(tmp_path, sid: str = "sid-provider"):
    """A real AcpSessionHandle carrying a real transcript, mid-turn."""
    from kiro_crew.acp.session_handle import AcpSessionHandle

    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = sid
    handle.keep_transcript = False
    runtime = MagicMock()
    runtime.is_alive.return_value = True
    runtime.terminate_session = AsyncMock()
    handle._runtime = runtime
    # Real state behind the real `is_turn_active` property: a turn is running.
    handle._turn_done = asyncio.Event()
    handle._cancelled = False

    sessions = tmp_path / "sessions" / "cli"
    sessions.mkdir(parents=True)
    files = [sessions / f"{sid}.json", sessions / f"{sid}.jsonl"]
    for f in files:
        f.write_text("{}", encoding="utf-8")
    return handle, runtime, sessions, files


@pytest.mark.asyncio
async def test_shutdown_destroys_the_handle_when_cancelled_mid_cancel(tmp_path, monkeypatch):
    """Cancel the shutdown while it is suspended in `handle.cancel()`."""
    handle, runtime, sessions, files = _handle_with_transcript(tmp_path)
    entered = asyncio.Event()

    async def _suspended_cancel() -> None:
        entered.set()
        await asyncio.Event().wait()  # never completes; the task is cancelled here

    handle.cancel = _suspended_cancel
    provider = AcpSessionProvider(handle, runtime, owns_runtime=False)

    monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions)

    task = asyncio.ensure_future(provider.shutdown())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [f for f in files if f.exists()] == [], (
        "shutdown() exited without destroying the handle, so this subagent's "
        "transcript is stranded — nothing else deletes it"
    )
    runtime.terminate_session.assert_awaited_once_with("sid-provider")


@pytest.mark.asyncio
async def test_a_cancel_that_merely_times_out_still_destroys(tmp_path, monkeypatch):
    """Control: the already-handled arm. `wait_for`'s TimeoutError is an
    ordinary Exception, so it was and stays swallowed, and destroy still runs."""
    handle, runtime, sessions, files = _handle_with_transcript(tmp_path, sid="sid-timeout")

    async def _slow_cancel() -> None:
        await asyncio.Event().wait()

    handle.cancel = _slow_cancel
    provider = AcpSessionProvider(handle, runtime, owns_runtime=False)
    monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions)
    monkeypatch.setattr(
        "kiro_crew.acp.session_provider.asyncio.wait_for",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )

    await provider.shutdown()

    assert [f for f in files if f.exists()] == []
    runtime.terminate_session.assert_awaited_once_with("sid-timeout")


@pytest.mark.asyncio
async def test_an_idle_session_still_destroys(tmp_path, monkeypatch):
    """Control: with no turn running, `cancel()` is skipped entirely."""
    handle, runtime, sessions, files = _handle_with_transcript(tmp_path, sid="sid-idle")
    handle._turn_done.set()  # no active turn
    handle.cancel = AsyncMock()
    provider = AcpSessionProvider(handle, runtime, owns_runtime=False)
    monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions)

    await provider.shutdown()

    handle.cancel.assert_not_awaited()
    assert [f for f in files if f.exists()] == []
