"""A failed compaction PAUSES while sub-agents share the parent's process.

The restart that follows a failed ``/compact`` shuts the parent's provider down, and
for a parent that owns its runtime that kills every session-sharing sub-agent on it.
These tests drive the failure with fakes only: a mock provider whose ``/compact``
reports no result, and a stand-in sub-agent manager.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat_compaction_notice import notice_text
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS


async def _no_events(_command: str):
    if False:  # pragma: no cover - makes this an async generator
        yield None


def _factory(order: list[str]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        # No status event and no dict result: the compaction fails.
        m.stream_command = MagicMock(side_effect=_no_events)
        m.shutdown = AsyncMock(side_effect=lambda: order.append("shutdown"))
        return m

    return factory


class _Runs:
    """The slice of ``SubagentManager`` the pause reads and ends."""

    def __init__(self, parent: str, order: list[str], *, shared: bool = True) -> None:
        self.child = SimpleNamespace(id="a1", parent_session_key=parent, conversation_key="")
        self.live = {"subagent:a1"} if shared else set()
        self.order = order
        self.cancelled: list[tuple[str, ...]] = []

    @property
    def running(self):
        return [self.child] if self.child is not None else []

    def has_live_shared_session(self, session_key: str) -> bool:
        return session_key in self.live

    def finish(self) -> None:
        self.child = None
        self.live.clear()

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
        # Reached only by close_all at the end of each test.
        return ()

    async def cancel_for_teardown(self, agent_ids, *, parent_session_key, verb=""):
        self.order.append("cancel")
        self.cancelled.append(tuple(agent_ids))
        return len(agent_ids)


async def _setup(wait_secs: float = 30.0, *, shared: bool = True):
    order: list[str] = []
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory(order))
    await mgr.get_or_create("dashboard:chat-1")
    key = mgr._fold_key("dashboard:chat-1")
    mgr.release(key)
    mgr._session_map.set(key, "sid-parent")
    runs = _Runs(key, order, shared=shared)
    mgr.set_child_teardown_handler(runs)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=wait_secs, cotenant_poll_secs=0.01
    )
    notices: list[tuple[bool, str]] = []

    async def _cb(key, pct, *, success, outcome="compacted"):
        notices.append((success, outcome))

    mgr.set_compact_callback(_cb)
    return mgr, key, runs, order, notices


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_restart_waits_for_a_shared_sub_agent_then_runs_when_it_finishes():
    mgr, key, runs, order, notices = await _setup()
    session = mgr._sessions[key]

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 95.0))
    await _settle()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert order == [], "the restart must not fire while the sub-agent runs"
    assert notices == [(False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS)]
    assert mgr._session_map._data.get(key, {}).get("sid") == "sid-parent"

    runs.finish()
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    assert order == ["shutdown"]
    assert runs.cancelled == []
    # The conversation is still there to come back to.
    provider, is_new, _ = await mgr.get_or_create("dashboard:chat-1")
    assert is_new and provider is not session.provider
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_report_waiting_on_the_parent_lands_on_the_fresh_session():
    """A finished child's report queues for the parent turn the hold is keeping.

    It must not be handed the process the restart is about to shut down.
    """
    mgr, key, runs, order, _notices = await _setup()
    session = mgr._sessions[key]

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 95.0))
    await _settle()
    report = asyncio.ensure_future(mgr.get_or_create("dashboard:chat-1"))
    await _settle()
    assert not report.done(), "the report waits while the hold keeps the turn"

    runs.finish()
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    provider, _is_new, _ = await asyncio.wait_for(report, timeout=5)
    assert provider is not session.provider
    assert order == ["shutdown"]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_restart_ends_the_sub_agent_through_its_teardown_at_the_timeout():
    mgr, key, runs, order, _notices = await _setup(wait_secs=0.05)
    session = mgr._sessions[key]

    assert await asyncio.wait_for(mgr._compact_in_place(key, session, 95.0), 5) == "recycled"

    assert runs.cancelled == [("a1",)]
    assert order == ["cancel", "shutdown"], "children end before the process goes"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_sub_agent_on_its_own_process_does_not_hold_the_restart():
    mgr, key, runs, order, notices = await _setup(shared=False)
    session = mgr._sessions[key]

    assert await asyncio.wait_for(mgr._compact_in_place(key, session, 95.0), 5) == "recycled"

    assert order == ["shutdown"]
    assert runs.cancelled == []
    assert (False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS) not in notices
    await mgr.close_all()


def test_a_channel_is_told_the_restart_is_waiting():
    text = notice_text("slack", 95.0, success=False, outcome=COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS)

    assert "waiting for its sub-agents" in text
    assert "cooldown" not in text
