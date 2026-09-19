"""The idle sweep must consult the attached-sub-agent probe before a reset.

``_rss_threshold_check`` already does: a free semaphore only proves the
parent's own turn is over, and sub-agents dispatched by that turn keep running
on the parent's runtime, so a reset discards their work. ``_expire_idle``
opened with the same semaphore check and reset without asking the probe, on
both its branches. The orphan branch is the reachable one: it ignores the
clock, so a parent whose tab closed while its background sub-agents were still
running was reaped on the very next sweep.

These tests install a probe that answers "attached" and pin that neither branch
resets the session, that a probe which raises keeps the session (fail closed,
as on the RSS path), and that a probe answering "none" still lets the sweep
expire the session so the guard does not turn into a leak.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


async def _idle_parent(cfg) -> SessionManager:
    """A parent whose own turn is over (permit released) and looks idle."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000
    return mgr


class TestIdleSweepAttachedSubagentGuard:
    @pytest.mark.asyncio
    async def test_idle_parent_with_attached_subagents_is_kept(self, cfg) -> None:
        mgr = await _idle_parent(cfg)
        asked: list[str] = []

        def probe(key: str) -> bool:
            asked.append(key)
            return True

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert asked == ["dashboard:tab1"], "the sweep never asked the probe"
        assert "dashboard:tab1" in mgr._sessions, "reaped a parent with sub-agents running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_orphaned_parent_with_attached_subagents_is_kept(self, cfg) -> None:
        """The orphan branch ignores the clock, so it needs the guard most."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab2"})  # tab1 looks orphaned
        mgr.set_subagent_probe(lambda key: True)

        await mgr._expire_idle(9999)

        assert (
            "dashboard:tab1" in mgr._sessions
        ), "reaped an orphaned parent with sub-agents running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_awaitable_probe_answer_is_awaited(self, cfg) -> None:
        """The dashboard installs a coroutine probe; its answer must be awaited, not bool()-ed."""
        mgr = await _idle_parent(cfg)

        async def probe(key: str) -> bool:
            return True

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_raising_probe_keeps_the_session(self, cfg) -> None:
        """A probe that cannot see the children is not a session with none."""
        mgr = await _idle_parent(cfg)

        def probe(key: str) -> bool:
            raise RuntimeError("task store unavailable")

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions, "a raising probe must fail closed"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_idle_parent_without_subagents_still_expires(self, cfg) -> None:
        """The guard must not turn the sweep into a leak."""
        mgr = await _idle_parent(cfg)
        mgr.set_subagent_probe(lambda key: False)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" not in mgr._sessions, "an idle parent with no children must expire"
        await mgr.close_all()
