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
from kiro_crew.messaging.queue_drain import (
    QUEUED_CHANNEL_KEY,
    QUEUED_OWNER_KEY,
    entries_queued_by,
    register_drain,
    reset_drains,
)
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS


async def _no_events(_command: str):
    if False:  # pragma: no cover - makes this an async generator
        yield None


def _factory(order: list[str]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        if "fail-start" in order:
            raise RuntimeError("successor could not start")
        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
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
        self.child = SimpleNamespace(
            id="a1", parent_session_key=parent, conversation_key="", _stop_origin=""
        )
        self.live = {"subagent:a1"} if shared else set()
        self.order = order
        self.cancelled: list[tuple[str, ...]] = []
        #: Each ordinary cancel reports "stopped" home, as the real manager does.
        self.stopped_reports: list[tuple[str, str]] = []

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

    async def cancel(self, agent_id: str) -> bool:
        self.order.append("stop")
        self.stopped_reports.append((agent_id, self.child._stop_origin))
        self.finish()
        return True

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


@pytest.fixture
def slack_drain():
    """A stand-in Slack drain that dispatches by dequeuing, as the real one does."""
    delivered: list[tuple[str, str]] = []
    holder: dict[str, SessionManager] = {}

    async def _drain(session_key: str) -> None:
        while (entry := holder["mgr"].dequeue(session_key)) is not None:
            delivered.append((entry[0], entry[1]))

    register_drain("slack", _drain)
    yield holder, delivered
    reset_drains()


async def _restart(mgr, key, runs, *messages: str, before_restart=None) -> str:
    """Queue *messages* on the held session, then let the restart run."""
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 95.0))
    await _settle()
    for i, text in enumerate(messages, 1):
        owner = {QUEUED_OWNER_KEY: "alice" if i == 1 else "bob"}
        assert mgr.enqueue(key, f"ts-{i}", text, **{QUEUED_CHANNEL_KEY: "slack"}, **owner)
    if before_restart is not None:
        before_restart()
    runs.finish()
    return await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_message_queued_during_the_wait_is_delivered_after_the_restart(slack_drain):
    """The restart wakes the channel drain, so the message runs with no other turn."""
    holder, delivered = slack_drain
    mgr, key, runs, _order, _notices = await _setup()
    holder["mgr"] = mgr

    assert await _restart(mgr, key, runs, "hello while waiting") == "recycled"
    await _settle()

    assert delivered == [("ts-1", "hello while waiting")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_message_queued_during_the_old_shutdown_is_delivered(slack_drain):
    """The drain reads the queue after the lease goes, not before the teardown."""
    holder, delivered = slack_drain
    mgr, key, runs, _order, _notices = await _setup()
    holder["mgr"] = mgr
    mgr._sessions[key].provider.shutdown = AsyncMock(
        side_effect=lambda: mgr.enqueue(key, "ts-late", "late", **{QUEUED_CHANNEL_KEY: "slack"})
    )

    assert await _restart(mgr, key, runs) == "recycled"
    await _settle()

    assert delivered == [("ts-late", "late")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_failing_old_shutdown_still_wakes_the_drain(slack_drain):
    holder, delivered = slack_drain
    mgr, key, runs, _order, _notices = await _setup()
    holder["mgr"] = mgr
    mgr._sessions[key].provider.shutdown = AsyncMock(side_effect=RuntimeError("stuck"))

    with pytest.raises(RuntimeError, match="stuck"):
        await _restart(mgr, key, runs, "hello while waiting")
    await _settle()

    assert delivered == [("ts-1", "hello while waiting")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_new_message_queues_behind_the_carried_one():
    """The carried messages sit in the successor's real queue, ahead of newcomers."""
    mgr, key, runs, _order, _notices = await _setup()
    session = mgr._sessions[key]
    session.requested_model = "model-pinned"
    await _restart(mgr, key, runs, "hello while waiting")

    assert mgr._sessions[key] is not session
    assert mgr._sessions[key].requested_model == "model-pinned"
    assert mgr.enqueue(key, "ts-9", "after the restart", force=True)
    assert mgr.dequeue(key)[:2] == ("ts-1", "hello while waiting")
    assert mgr.dequeue(key) == ("ts-9", "after the restart", {})
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_refused_teardown_keeps_the_carried_message():
    mgr, key, runs, _order, _notices = await _setup()
    await _restart(mgr, key, runs, "keep me")
    await mgr.get_or_create("dashboard:chat-1")  # a turn holds the successor

    assert not await mgr.discard_conversation(key, skip_if_busy=True)
    mgr.release(key)
    assert mgr.dequeue(key)[:2] == ("ts-1", "keep me")
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_superseded_restart_carries_nothing():
    """Another teardown already removed the session and discarded its queue."""
    mgr, key, runs, _order, _notices = await _setup()

    await _restart(mgr, key, runs, "discarded", before_restart=lambda: mgr._sessions.pop(key))

    assert not mgr.has_session(key)
    assert mgr.dequeue(key) is None
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_pump_can_put_a_carried_message_back():
    mgr, key, runs, _order, _notices = await _setup()
    await _restart(mgr, key, runs, "set aside")

    ts, text, kwargs = mgr.dequeue(key)
    assert mgr.enqueue(key, ts, text, force=True, **kwargs)
    assert mgr.dequeue(key)[:2] == ("ts-1", "set aside")
    await mgr.close_all()


@pytest.mark.asyncio
async def test_stop_sees_and_clears_the_carried_messages():
    """Slack's !stop clears only when a session exists; the successor is one."""
    mgr, key, runs, _order, _notices = await _setup()
    await _restart(mgr, key, runs, "alice's", "bob's")

    assert mgr.has_session(key)
    mgr.clear_queue(key, entries_queued_by("alice"))
    assert mgr.cancel_queued(key, "ts-2")
    assert mgr.dequeue(key) is None
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_successor_that_fails_to_start_keeps_the_old_session(slack_drain):
    holder, delivered = slack_drain
    mgr, key, runs, order, _notices = await _setup()
    session = mgr._sessions[key]
    holder["mgr"] = mgr

    result = await _restart(
        mgr, key, runs, "keep me", before_restart=lambda: order.append("fail-start")
    )

    assert result == "failed"
    assert mgr._sessions[key] is session
    assert "shutdown" not in order
    assert key in mgr._compaction_state.cooldown_until
    assert mgr._session_map._data[key]["sid"] == "sid-parent"
    # Nothing else drains the kept queue, so the failure wakes it too.
    await _settle()
    assert delivered == [("ts-1", "keep me")]
    order.remove("fail-start")
    await mgr.close_all()


@pytest.mark.parametrize("fail_start", [False, True])
@pytest.mark.asyncio
async def test_a_restart_records_one_recycled_end(monkeypatch, fail_start):
    """The old session's end is recorded once; a failed start re-opens its record."""
    import kiro_crew.session_compaction as sc

    mgr, key, runs, order, _notices = await _setup()
    calls: list[tuple[str, str]] = []

    async def _ended(k, *, end_reason):
        calls.append(("end", end_reason))

    async def _started(k):
        calls.append(("start", ""))

    monkeypatch.setattr(sc, "record_session_ended", _ended)
    monkeypatch.setattr(sc, "record_session_started", _started)
    await _restart(
        mgr, key, runs, before_restart=(lambda: order.append("fail-start")) if fail_start else None
    )

    ends = [c for c in calls if c[0] == "end"]
    assert ends == [("end", sc.END_REASON_RECYCLED)]
    assert calls[-1] == (("start", "") if fail_start else ("end", sc.END_REASON_RECYCLED))
    if fail_start:
        order.remove("fail-start")
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cancelled_successor_start_keeps_the_old_session(monkeypatch):
    """A cancel is not an Exception; the marker and resume id must still come back."""
    mgr, key, runs, _order, _notices = await _setup()
    session = mgr._sessions[key]

    async def _cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(mgr, "get_or_create", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _restart(mgr, key, runs)

    assert key not in mgr._recycling
    assert mgr._sessions[key] is session
    assert mgr._session_map._data[key]["sid"] == "sid-parent"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_failed_start_leaves_a_racing_successors_resume_pointer(monkeypatch):
    mgr, key, runs, _order, _notices = await _setup()

    async def _racer_then_fail(*_args, **_kwargs):
        mgr._sessions[key] = SimpleNamespace(queue=[], cancelled=set())
        mgr._session_map.set(key, "sid-racer")
        raise RuntimeError("successor could not start")

    monkeypatch.setattr(mgr, "get_or_create", _racer_then_fail)
    assert await _restart(mgr, key, runs) == "failed"

    assert mgr._session_map._data[key]["sid"] == "sid-racer"
    mgr._sessions.pop(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_uncompactable_restart_also_waits_for_a_shared_sub_agent():
    """A backend with no compaction restarts at the threshold; that must hold too."""
    mgr, key, runs, order, notices = await _setup()
    session = mgr._sessions[key]

    task = asyncio.ensure_future(mgr._compaction._recycle_unmanaged(key, session, 95.0))
    await _settle()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert order == [], "the restart must not fire while the sub-agent runs"
    assert notices == [(False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS)]

    runs.finish()
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    assert order == ["shutdown"]
    assert runs.cancelled == []
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
    # Deterministic order: the report is parked on the held turn's lease before
    # the restart runs, so it cannot race the successor's start.
    async with asyncio.timeout(5):
        while not session.semaphore._waiters:
            await asyncio.sleep(0.01)
    assert not report.done(), "the report waits while the hold keeps the turn"

    runs.finish()
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    provider, _is_new, _ = await asyncio.wait_for(report, timeout=5)
    assert provider is not session.provider
    assert mgr.get_provider(key) is provider
    # Exactly one process ends: the old one. The successor is the one start the
    # restart makes and the report joins it, so no duplicate start is shut down.
    assert order == ["shutdown"]
    mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_restart_stops_the_sub_agent_with_a_stopped_report_at_the_timeout():
    """The chat lives on after the restart, so the stop must report home.

    The parent-end teardown would mark the run out of delivery instead.
    """
    mgr, key, runs, order, _notices = await _setup(wait_secs=0.05)
    session = mgr._sessions[key]

    assert await asyncio.wait_for(mgr._compact_in_place(key, session, 95.0), 5) == "recycled"

    assert runs.stopped_reports == [("a1", "stopped at a failed compaction's restart")]
    assert runs.cancelled == [], "the parent-end teardown drops the report"
    assert order == ["stop", "shutdown"], "sub-agents stop before the process goes"
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
