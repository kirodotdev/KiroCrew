"""Tests for the heartbeat per-task timeout + session teardown (F3).

An unattended heartbeat turn can block on a human-approval wait with no human
present. Without a bound, a single non-allowlisted tool approval would freeze the
whole heartbeat subsystem up to the 2h approval window. ``_heartbeat_task`` wraps
``stream_and_collect`` in ``asyncio.wait_for(timeout=HEARTBEAT_TASK_TIMEOUT_SECS)``
and, on timeout, resets the heartbeat session (killing the lingering turn/process)
before releasing it, returning a graceful incomplete result instead of crashing.

Per-task ``finally`` only releases the per-key semaphore (NOT reset) — concurrent
``asyncio.gather``'d sibling tasks share ``HEARTBEAT_KEY`` and a per-task reset
would tear down a session a sibling is still using.  Cycle-end recycle is
handled separately by ``SessionManager.recycle_heartbeat`` (called once via
``HeartbeatService.on_cycle_end``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.slack.gateway as gw_mod
from kiro_crew.heartbeat import HEARTBEAT_TASK_TIMEOUT_SECS
from kiro_crew.session import HEARTBEAT_KEY


def _make_orchestrator():
    """GatewayOrchestrator with the minimal wiring _heartbeat_task touches."""
    orch = gw_mod.GatewayOrchestrator.__new__(gw_mod.GatewayOrchestrator)

    sessions = MagicMock()
    client = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.reset = AsyncMock()
    sessions.release = MagicMock()
    orch.sessions = sessions

    ctx_builder = MagicMock()
    ctx_builder.build_message = MagicMock(return_value=("full message", None))
    ctx_builder.hooks = MagicMock()
    ctx_builder.memory = MagicMock()
    orch.ctx_builder = ctx_builder

    orch.consolidator = None
    # Heartbeat now uses ``_heartbeat_approval`` (scoped allowlist) directly,
    # but the interactive helper is still patched on the orchestrator surface
    # for any test that wants to swap it out.
    orch._interactive_approval = MagicMock(return_value=AsyncMock())
    orch._heartbeat_approval = AsyncMock(return_value=True)
    orch._deliver_result = AsyncMock()
    return orch, sessions


async def _capture_task(orch):
    """Run _init_heartbeat with HeartbeatService.start stubbed; return on_task."""
    started = {}

    async def _fake_start(self):
        started["on_task"] = self._on_task

    orig_start = gw_mod.HeartbeatService.start
    gw_mod.HeartbeatService.start = _fake_start  # type: ignore[assignment]
    try:
        await orch._init_heartbeat()
    finally:
        gw_mod.HeartbeatService.start = orig_start  # type: ignore[assignment]
    return started["on_task"]


class TestHeartbeatTaskTimeout:
    @pytest.mark.asyncio()
    async def test_timeout_resets_and_releases_session(self, monkeypatch):
        orch, sessions = _make_orchestrator()

        # stream_and_collect hangs forever — simulates blocking on an approval
        # wait with no human present.
        async def _hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(gw_mod, "stream_and_collect", _hang)
        # Force the wait_for deadline to fire immediately instead of after 30 min.

        async def _fast_wait_for(awaitable, timeout):
            # Close the never-resolving coroutine to avoid "never awaited" noise
            # and raise the timeout the production code handles.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(gw_mod.asyncio, "wait_for", _fast_wait_for)

        on_task = await _capture_task(orch)

        # Should NOT raise — timeout is handled gracefully.
        result = await on_task("do a thing", "")

        # In-flight turn torn down via reset on the heartbeat key — once
        # in the except branch only; the finally branch no longer resets
        # (cycle-end recycle is handled by SessionManager.recycle_heartbeat,
        # invoked once after asyncio.gather completes — not per task).
        sessions.reset.assert_awaited_once_with(HEARTBEAT_KEY)
        # finally still ran: session released.
        sessions.release.assert_called_once_with(HEARTBEAT_KEY)
        # Graceful incomplete result mentions the deadline; loop not wedged.
        assert str(HEARTBEAT_TASK_TIMEOUT_SECS) in result
        assert "timed out" in result.lower()
        # No delivery suppression error — _deliver_result still invoked.
        orch._deliver_result.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_timeout_does_not_wedge_when_reset_fails(self, monkeypatch):
        """A failing reset is swallowed; release still runs so the loop continues."""
        orch, sessions = _make_orchestrator()
        sessions.reset = AsyncMock(side_effect=RuntimeError("reset boom"))

        async def _hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(gw_mod, "stream_and_collect", _hang)

        async def _fast_wait_for(awaitable, timeout):
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(gw_mod.asyncio, "wait_for", _fast_wait_for)

        on_task = await _capture_task(orch)
        result = await on_task("do a thing", "")

        # The except-branch reset raises; the exception is swallowed so the
        # loop continues and the graceful timeout result is still returned.
        # No second reset in finally (per-task reset removed — cycle-end
        # recycle is handled by SessionManager.recycle_heartbeat).
        sessions.reset.assert_awaited_once_with(HEARTBEAT_KEY)
        sessions.release.assert_called_once_with(HEARTBEAT_KEY)
        assert "timed out" in result.lower()

    @pytest.mark.asyncio()
    async def test_success_path_does_not_reset(self, monkeypatch):
        """A normal (non-hanging) turn never resets the session.

        Per-task reset was removed — concurrent asyncio.gather'd sibling tasks
        share HEARTBEAT_KEY and a per-task reset would tear down a session a
        sibling is still using.  Cycle-end recycle is handled by
        SessionManager.recycle_heartbeat (invoked once via
        HeartbeatService.on_cycle_end after gather completes).
        """
        orch, sessions = _make_orchestrator()

        async def _ok(*args, **kwargs):
            return "all good"

        monkeypatch.setattr(gw_mod, "stream_and_collect", _ok)

        on_task = await _capture_task(orch)
        result = await on_task("do a thing", "")

        # Success path takes neither the except-branch reset nor a finally
        # reset.  Only release runs.
        sessions.reset.assert_not_awaited()
        sessions.release.assert_called_once_with(HEARTBEAT_KEY)
        assert result == "all good"
