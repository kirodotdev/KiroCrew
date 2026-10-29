"""A session closed while its in-place ``/compact`` runs is not a failed compaction.

Closing the session (a tab close, ``session_close``) pops it and shuts its provider
down. The ``/compact`` stream then dies with the process. That is the teardown, not
the harness failing to compact, so the compaction must end quietly: no failure
warning, no compaction counted, and no recycle, which would shut the same process
down a second time. A ``/compact`` that really fails on a live session
still warns, counts and recycles.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.metrics.events import CONTEXT_COMPACTIONS
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import async_wait_until, await_until

KEY = "dashboard:chat-1"
FAILED_WARNING = "in-place /compact failed"


class _NoRuns:
    """A child-teardown handler with no sub-agents, so nothing waits on cotenants."""

    @property
    def running(self):
        return []

    def has_live_shared_session(self, session_key: str) -> bool:
        return False

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
        return ()

    async def cancel_for_teardown(self, agent_ids, *, parent_session_key, verb=""):
        return 0


def _factory(shutdowns: list[int], streaming: list[asyncio.Event], *, die_at_once: bool):
    """Providers whose ``/compact`` dies when the process does, like a real harness."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        index = len(streaming)
        died = asyncio.Event()
        entered = asyncio.Event()
        streaming.append(entered)

        async def stream_command(_command: str):
            entered.set()
            if not die_at_once:
                await died.wait()
            raise RuntimeError("Runtime died while waiting for compaction")
            yield None  # pragma: no cover - makes this an async generator

        async def shutdown() -> None:
            shutdowns.append(index)
            died.set()

        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: not died.is_set()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.stream_command = MagicMock(side_effect=stream_command)
        m.shutdown = AsyncMock(side_effect=shutdown)
        return m

    return factory


async def _setup(monkeypatch, *, die_at_once: bool):
    shutdowns: list[int] = []
    streaming: list[asyncio.Event] = []
    counted: list[dict] = []
    monkeypatch.setattr(
        "kiro_crew.session_compaction.emit_counter",
        lambda name, labels: counted.append(labels) if name == CONTEXT_COMPACTIONS else None,
    )
    mgr = SessionManager(
        KiroCrewConfig(), provider_factory=_factory(shutdowns, streaming, die_at_once=die_at_once)
    )
    await mgr.get_or_create(KEY)
    key = mgr._fold_key(KEY)
    mgr.release(key)
    mgr.set_child_teardown_handler(_NoRuns())
    notices: list[tuple[bool, str]] = []

    async def _cb(key, pct, *, success, outcome="compacted"):
        notices.append((success, outcome))

    mgr.set_compact_callback(_cb)
    return mgr, key, shutdowns, streaming, counted, notices


@pytest.mark.asyncio
async def test_a_session_closed_during_compact_ends_quietly(monkeypatch, caplog):
    mgr, key, shutdowns, streaming, counted, notices = await _setup(monkeypatch, die_at_once=False)
    session = mgr._sessions[key]
    caplog.set_level(logging.DEBUG, logger="kiro_crew")

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 95.0))
    await async_wait_until(streaming[0].is_set, describe=lambda: "/compact is streaming")
    # The close pops the session and shuts its provider down; the /compact stream
    # dies with the process.
    await mgr.remove(KEY)

    assert await await_until(task, what="compact_in_place ended") == "absent"
    assert shutdowns == [0], "the closed session's process must be shut down exactly once"
    assert counted == [], "a close is not a compaction, failed or otherwise"
    assert notices == []
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and FAILED_WARNING in r.getMessage()
    ]
    assert key not in mgr._sessions, "nothing may start a successor for a closed session"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_real_compact_failure_still_warns_counts_and_recycles(monkeypatch, caplog):
    mgr, key, shutdowns, _streaming, counted, notices = await _setup(monkeypatch, die_at_once=True)
    session = mgr._sessions[key]
    caplog.set_level(logging.DEBUG, logger="kiro_crew")

    outcome = await await_until(
        asyncio.ensure_future(mgr._compact_in_place(key, session, 95.0)),
        what="compact_in_place ended",
    )

    assert outcome == "recycled"
    assert shutdowns == [0], "the failed session is recycled once"
    assert counted == [
        {"success": False}
    ], "a recycle after a failed /compact counts as a failed compaction"
    assert [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and FAILED_WARNING in r.getMessage()
    ]
    assert notices, "the user is told the compaction did not succeed"
    assert mgr._sessions[key] is not session, "a successor takes the key"
    await mgr.close_all()
