"""Programmatic slot creation must publish the active-slot set.

The defect these pin: ``get_or_create_slot`` inserted the new slot into
``state._slots`` and broadcast to websockets, but never published the updated
key set to ``SessionManager.set_active_dashboard_slots`` — only the HTTP slot
endpoints did (via ``_sync_dashboard_slots``). Slots created programmatically
(auto-research campaign workers, cron/workflow inject, the task runner, spec
builder) never pass through those endpoints, so ``_active_dashboard_slots``
stayed stale and the idle sweep's orphan branch reaped their LIVE sessions as
"slot gone".

Observed in production (gateway.log, 2026-08-08): an actively running research
campaign's session ``dashboard:research-9057ccf5`` was expired as orphaned on
three consecutive sweeps (16:54:47, 17:04:53, 17:14:56) while its autonudge
loop was mid-campaign. The first reap's ``reset()`` released the companion
subagent runtime, killing subagent c6f44344 mid-prompt with
``AcpProcessDied("Runtime process died during prompt")``.

The busy-guard (see test_session_idle_busy_guard.py) does not cover this: an
autonudge-driven session is legitimately idle BETWEEN cycles (the semaphore is
released when a turn ends), which is exactly when the sweep caught it.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


def _make_state(sessions):
    """A dashboard state real enough to run the REAL get_or_create_slot."""
    from kiro_crew.dashboard.state import DashboardState

    st = DashboardState.__new__(DashboardState)
    st._slots = {}
    st._slot_counter = 0
    st.sessions = sessions
    st.push_slots_update = MagicMock()
    st._broadcast_chat_message = MagicMock()
    st._restricted_keys = set()
    st._ephemeral_keys = set()
    st._slack_to_slot = {}
    return st


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


class TestSlotCreatePublishesActiveSet:
    def test_programmatic_slot_creation_publishes_its_session_key(self):
        """The auto-research path: get_or_create_slot(name=...) with no HTTP endpoint."""
        sessions = MagicMock()
        sessions.get_slack_link.return_value = ("", "")
        st = _make_state(sessions)

        st.get_or_create_slot(name="research-abc123", app="auto-research")

        assert sessions.set_active_dashboard_slots.called, (
            "slot creation must publish the active-slot set to SessionManager"
        )
        published = sessions.set_active_dashboard_slots.call_args[0][0]
        assert "dashboard:research-abc123" in published

    def test_returning_an_existing_slot_does_not_republish(self):
        """The early return for existing slots leaves the (correct) set alone."""
        sessions = MagicMock()
        sessions.get_slack_link.return_value = ("", "")
        st = _make_state(sessions)
        st.get_or_create_slot(name="research-abc123")
        sessions.set_active_dashboard_slots.reset_mock()

        st.get_or_create_slot(name="research-abc123")

        assert not sessions.set_active_dashboard_slots.called

    def test_slot_creation_survives_a_missing_session_manager(self):
        """Test-constructed states have sessions=None; creation must not raise."""
        st = _make_state(None)

        slot = st.get_or_create_slot(name="research-abc123")

        assert slot is not None
        assert "research-abc123" in st._slots

    def test_slot_creation_survives_a_sync_failure(self):
        """A publish failure must never break slot creation itself."""
        sessions = MagicMock()
        sessions.get_slack_link.return_value = ("", "")
        sessions.set_active_dashboard_slots.side_effect = RuntimeError("boom")
        st = _make_state(sessions)

        slot = st.get_or_create_slot(name="research-abc123")

        assert slot is not None
        assert "research-abc123" in st._slots


class TestWorkerSessionSurvivesOrphanSweep:
    @pytest.mark.asyncio
    async def test_idle_worker_session_is_not_reaped_as_an_orphan(self):
        """The regression, end to end against a real SessionManager.

        An autonudge worker is idle between cycles (permit released), so the
        busy-guard does not protect it — only membership in the active-slot
        set does. Before the fix this test fails: the sweep reaps the session
        as "slot gone".
        """
        cfg = KiroCrewConfig()
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        st = _make_state(mgr)

        # The stale state the bug depends on: some earlier HTTP-path sync
        # published a set that predates the worker slot.
        mgr.set_active_dashboard_slots({"dashboard:tab1"})

        # The campaign body: session runs turns, then idles between cycles.
        await mgr.get_or_create("dashboard:research-abc123")
        mgr.release("dashboard:research-abc123")

        # The programmatic slot creation (auto_research handlers.py).
        st.get_or_create_slot(name="research-abc123", app="auto-research")

        # Orphan branch ignores the clock — pass a timeout no session reaches.
        await mgr._expire_idle(9999)

        assert "dashboard:research-abc123" in mgr._sessions, (
            "a live worker session was reaped as an orphaned tab"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_genuinely_orphaned_session_is_still_reaped(self):
        """The fix must not disable the orphan sweep it corrects."""
        cfg = KiroCrewConfig()
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        await mgr.get_or_create("dashboard:closed-tab")
        mgr.release("dashboard:closed-tab")
        async with mgr._lock:
            mgr._sessions["dashboard:closed-tab"].last_used = time.monotonic()
        mgr.set_active_dashboard_slots(set())  # tab really is gone

        await mgr._expire_idle(9999)

        assert "dashboard:closed-tab" not in mgr._sessions
        await mgr.close_all()
