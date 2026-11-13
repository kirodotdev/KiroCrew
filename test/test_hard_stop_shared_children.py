"""A hard Stop of a parent turn stops the sub-agents that share its process, by name.

The force path resets the parent, and the reset shuts down the process its
session-sharing sub-agents run on. Without a stop of their own those runs died
as an unattributed runtime death ("killed (provider shutdown)"): a failure
tombstone, a failure stat and an error report for a run the user had just
stopped. The hard Stop's reset therefore cancels them between its pop and the
provider shutdown, with a stop origin that names the hard Stop.

Only the runs that share this parent's process are stopped. A run on a
dedicated process survives a recycle and delivers into the resumed
conversation, and a soft Stop leaves every run alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until

KEY = "dashboard:chat-b4"


class _Runs:
    """The parent-end teardown handler, reduced to what ``stop_turn`` reads."""

    def __init__(self, events: list, children: list[SimpleNamespace]) -> None:
        self._events = events
        self._children = children
        self.shared: set[str] = set()
        # A first stopper waits for its report to be delivered to the parent after
        # the record is final; held open here until the test lets it go.
        self.delivered = asyncio.Event()

    @property
    def running(self) -> list[SimpleNamespace]:
        return [c for c in self._children if not c.done]

    def has_live_shared_session(self, session_key: str) -> bool:
        return session_key in self.shared

    def snapshot_teardown_children(self, _parent: str) -> tuple[str, ...]:
        return ()

    async def cancel(self, agent_id: str) -> bool:
        child = next(c for c in self._children if c.id == agent_id)
        self._events.append(("cancel", agent_id, child._reap_reason, child._stop_origin))
        child.done = True
        await self.delivered.wait()
        return True

    async def cancel_for_teardown(self, ids, *, parent_session_key, verb=""):
        return len(ids)


class _EntryRecordingRuns(_Runs):
    """A teardown handler that captures each run's state when cancel starts."""

    def __init__(self, events: list, children: list[SimpleNamespace]) -> None:
        super().__init__(events, children)
        self.cancel_entries: list[tuple[str, str, bool]] = []

    async def cancel(self, agent_id: str) -> bool:
        child = next(c for c in self._children if c.id == agent_id)
        self.cancel_entries.append((agent_id, child._reap_reason, child._reap_started))
        child.done = True
        return True


def _child(agent_id: str, parent: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        parent_session_key=parent,
        conversation_key="",
        done=False,
        _reap_started=False,
        _reap_reason="",
        _stop_origin="",
    )


async def _setup(cfg, events: list):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await mgr.get_or_create(KEY)
    key = mgr._fold_key(KEY)
    mgr.release(key)
    provider.cancel = AsyncMock(return_value="acked")

    async def _shutdown() -> None:
        events.append(("shutdown",))

    provider.shutdown = AsyncMock(side_effect=_shutdown)

    async def _abort(_key, _session) -> None:
        events.append(("abort",))

    mgr._send_abort_for_session = _abort
    return mgr, key


@pytest.mark.asyncio
async def test_a_hard_stop_stops_its_shared_children_before_the_reset(cfg):
    events: list = []
    mgr, key = await _setup(cfg, events)
    shared = _child("a-shared", key)
    dedicated = _child("a-dedicated", key)
    other_parent = _child("a-other", "dashboard:chat-other")
    runs = _Runs(events, [shared, dedicated, other_parent])
    runs.shared = {"subagent:a-shared", "subagent:a-other"}
    mgr.set_child_teardown_handler(runs)

    # Returns once the record is final, without waiting for the report's
    # delivery, which needs the parent this Stop is replacing.
    outcome = await await_until(
        asyncio.ensure_future(mgr.stop_turn(KEY, force=True)), what="the hard Stop returning"
    )
    assert outcome == "hard"
    assert not runs.delivered.is_set()

    cancels = [e for e in events if e[0] == "cancel"]
    # Named as a user stop of the parent's turn, so the run's record is the
    # neutral "stopped" and not the runtime death the reset would cause.
    assert cancels == [("cancel", "a-shared", "user_stop", "stopped by a hard Stop of the parent")]
    # Stopped through its own teardown, before the reset kills the process.
    assert events.index(cancels[0]) < events.index(("shutdown",))
    assert not dedicated.done
    assert not other_parent.done
    runs.delivered.set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_reap_that_starts_before_shared_cancel_keeps_its_cause(cfg):
    events: list = []
    mgr, key = await _setup(cfg, events)
    reaping = _child("a-reaping", key)
    ordinary = _child("an-ordinary-stop", key)
    runs = _EntryRecordingRuns(events, [reaping, ordinary])
    mgr.set_child_teardown_handler(runs)

    async def _start_deadline_reap() -> None:
        reaping._reap_started = True

    reap = asyncio.create_task(_start_deadline_reap())
    await mgr._lifecycle._stop_shared_children(key, (reaping, ordinary))
    await await_until(reap, what="the deadline reap claiming the run")

    entries = {agent_id: (reason, started) for agent_id, reason, started in runs.cancel_entries}
    assert entries == {
        "a-reaping": ("", True),
        "an-ordinary-stop": ("user_stop", False),
    }
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_hard_stop_keeps_a_stop_origin_a_reap_already_wrote(cfg):
    events: list = []
    mgr, key = await _setup(cfg, events)
    reaping = _child("a-reaping", key)
    reaping._reap_started = True
    reaping._reap_reason = "reaped"
    reaping._stop_origin = "reaped after 900s (deadline)"
    runs = _Runs(events, [reaping])
    runs.shared = {"subagent:a-reaping"}
    mgr.set_child_teardown_handler(runs)

    assert await mgr.stop_turn(KEY, force=True) == "hard"

    # The reap in flight owns the record; the Stop joins it and renames nothing.
    assert [e for e in events if e[0] == "cancel"] == [
        ("cancel", "a-reaping", "reaped", "reaped after 900s (deadline)")
    ]
    runs.delivered.set()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_soft_stop_leaves_shared_children_running(cfg):
    events: list = []
    mgr, key = await _setup(cfg, events)
    shared = _child("a-shared", key)
    runs = _Runs(events, [shared])
    runs.shared = {"subagent:a-shared"}
    mgr.set_child_teardown_handler(runs)

    assert await mgr.stop_turn(KEY) == "soft"

    assert not shared.done
    assert [e for e in events if e[0] == "cancel"] == []
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_hard_stop_does_not_wait_for_a_reap_that_has_started(cfg, monkeypatch):
    """The wait ends once the reap has started, not when its teardown finishes.

    The hard Stop is the escape hatch for a parent process that has stopped
    responding, so a shared child's teardown on that process can hang. With the cap
    raised past the test's own bound, only the started reap can end the wait.
    """
    from kiro_crew.session_lifecycle_parts import turn_stop

    monkeypatch.setattr(turn_stop, "_SHARED_CHILD_STOP_WAIT_SECS", 3600.0)
    events: list = []
    mgr, key = await _setup(cfg, events)
    hung = _child("a-hung", key)
    release = asyncio.Event()

    class _HangingRuns(_Runs):
        async def cancel(self, agent_id: str) -> bool:
            hung._reap_started = True
            await release.wait()
            hung.done = True
            return True

    runs = _HangingRuns(events, [hung])
    runs.shared = {"subagent:a-hung"}
    mgr.set_child_teardown_handler(runs)

    outcome = await await_until(
        asyncio.ensure_future(mgr.stop_turn(KEY, force=True)), what="the hard Stop returning"
    )
    assert outcome == "hard"
    assert ("shutdown",) in events
    assert not hung.done
    release.set()
    await mgr.close_all()
