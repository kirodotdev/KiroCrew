"""Death attribution on the session-handle side.

A runtime killed under a live turn surfaces to that turn only as a poison
sentinel (None) on the session queue; the AcpProcessDied raised from the
frame wait therefore carried no hint of WHO killed the runtime or WHY.
``AcpSessionHandle._died`` closes that gap by appending the runtime's
``death_summary()`` (reason + returncode + stderr tail, composed at
``_mark_dead`` time) to the raise message.

Field motivation: three unattributed runtime deaths in five days — two
subagent runtimes (Sep 6) and one under a live cron turn (Sep 11) — were
undiagnosable from the bare "Runtime process died during prompt" alone.
"""

import asyncio

import pytest

from kiro_crew.acp.session_handle import AcpProcessDied, AcpSessionHandle


class _DeadRuntime:
    """Minimal runtime double exposing only what _died consults."""

    def __init__(self, summary: str | None) -> None:
        self._summary = summary
        self.pid = None
        self.is_alive = lambda: False

    def death_summary(self) -> str | None:
        return self._summary

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


class _LegacyRuntime:
    """Runtime double WITHOUT death_summary — older doubles and any
    out-of-tree AcpRuntimeProtocol implementation predating the method."""

    def __init__(self) -> None:
        self.pid = None
        self.is_alive = lambda: False

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


def _handle(runtime) -> AcpSessionHandle:
    return AcpSessionHandle("sA", asyncio.Queue(), runtime)


def test_died_appends_runtime_death_summary():
    h = _handle(_DeadRuntime("killed (warm mint teardown) [returncode=None] stderr_tail: <none>"))
    exc = h._died("Runtime process died during prompt")
    assert isinstance(exc, AcpProcessDied)
    assert str(exc) == (
        "Runtime process died during prompt — "
        "killed (warm mint teardown) [returncode=None] stderr_tail: <none>"
    )


def test_died_stays_bare_when_summary_is_none():
    """A runtime that died without _mark_dead composing a summary (or that
    reports None) must not grow a dangling ' — ' suffix."""
    exc = _handle(_DeadRuntime(None))._died("Runtime process died during prompt")
    assert str(exc) == "Runtime process died during prompt"


def test_died_degrades_on_runtime_without_death_summary():
    """getattr-guarded: a double or legacy protocol implementation lacking
    death_summary yields the bare message instead of AttributeError — the
    diagnostic is additive, never a new failure mode on the death path."""
    exc = _handle(_LegacyRuntime())._died("Runtime died while waiting for compaction")
    assert str(exc) == "Runtime died while waiting for compaction"


@pytest.mark.asyncio
async def test_response_wait_raise_carries_attribution():
    """End-to-end through a real wait path: a poisoned queue while waiting
    for a JSON-RPC response raises AcpProcessDied WITH the runtime's death
    summary attached."""
    q: asyncio.Queue = asyncio.Queue()
    rt = _DeadRuntime("killed (background runtime reap) [returncode=None] stderr_tail: <none>")
    h = AcpSessionHandle("sA", q, rt)
    q.put_nowait(None)  # poison sentinel: runtime died

    with pytest.raises(AcpProcessDied) as exc_info:
        await asyncio.wait_for(h._wait_for_response(1, timeout=2.0), timeout=5.0)

    assert "background runtime reap" in str(exc_info.value)
