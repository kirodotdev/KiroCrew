"""Advisor lifecycle integration: epochs join the parent's real boundaries.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``AdvisorService.notify_boundary(key, reason)`` starts a new observation
  epoch for epoch-scoped reasons (reset, compaction, history rewrite) and
  detaches the observer for terminal reasons (close, remove, transfer).
- The dashboard's single reset choke point (``_reset_slot_session`` -- the
  helper every agent/model/bulk/effort/workspace switch and reload routes
  through) notifies the advisor service.
- ``close_slot`` notifies with a terminal reason.
- ``dispose_all`` drops every observer (gateway shutdown).
- A disabled service ignores boundary notifications without error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.service import (
    BOUNDARY_CLOSE,
    BOUNDARY_COMPACTION,
    BOUNDARY_RESET,
    AdvisorService,
)


class TestServiceBoundarySemantics:
    def test_reset_boundary_begins_new_epoch(self):
        service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:a")
        observer.record_tool_result("read", "stale evidence")
        service.notify_boundary("dashboard:a", BOUNDARY_RESET)
        assert service.observer_count() == 1
        assert observer.drain_update() is None, "pending records must not cross"

    def test_compaction_boundary_begins_new_epoch(self):
        service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:a")
        observer.record_segment("pre-compaction text")
        service.notify_boundary("dashboard:a", BOUNDARY_COMPACTION)
        assert observer.drain_update() is None

    def test_close_boundary_detaches(self):
        service = AdvisorService(enabled=True, reviewer_available=True)
        service.attach("dashboard:a")
        service.notify_boundary("dashboard:a", BOUNDARY_CLOSE)
        assert service.observer_count() == 0

    def test_unknown_session_boundary_is_a_noop(self):
        service = AdvisorService(enabled=True, reviewer_available=True)
        service.notify_boundary("dashboard:ghost", BOUNDARY_RESET)
        assert service.observer_count() == 0

    def test_disabled_service_ignores_boundaries(self):
        service = AdvisorService(enabled=False)
        service.notify_boundary("dashboard:a", BOUNDARY_RESET)
        service.notify_boundary("dashboard:a", BOUNDARY_CLOSE)
        assert service.observer_count() == 0

    def test_dispose_all_drops_every_observer(self):
        service = AdvisorService(enabled=True, reviewer_available=True)
        service.attach("dashboard:a")
        service.attach("dashboard:b")
        service.dispose_all()
        assert service.observer_count() == 0


class TestDashboardChokePoints:
    """The real call sites notify the advisor service."""

    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        st = _make_state(tmp_path)
        st.broadcast_ws = MagicMock()
        return st

    @pytest.fixture
    def recording_service(self, monkeypatch):
        service = AdvisorService(enabled=True, reviewer_available=True)
        calls: list[tuple[str, str]] = []
        original = service.notify_boundary

        def recording(key: str, reason: str) -> None:
            calls.append((key, reason))
            original(key, reason)

        service.notify_boundary = recording  # type: ignore[method-assign]
        monkeypatch.setattr("kiro_crew.advisor.service.get_advisor_service", lambda: service)
        service.calls = calls  # type: ignore[attr-defined]
        return service

    @pytest.mark.asyncio
    async def test_reset_slot_session_notifies_reset_boundary(self, state, recording_service):
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        slot = state.get_or_create_slot("test")
        state.sessions.reset = AsyncMock(return_value=True)
        await _reset_slot_session(state, slot, "dashboard:test")
        assert ("dashboard:test", BOUNDARY_RESET) in recording_service.calls

    @pytest.mark.asyncio
    async def test_close_slot_notifies_close_boundary(self, state, recording_service):
        from kiro_crew.dashboard.chat_handlers import close_slot

        slot = state.get_or_create_slot("test")
        await close_slot(state, slot, "test")
        closes = [c for c in recording_service.calls if c[1] == BOUNDARY_CLOSE]
        assert closes, "close_slot must notify a terminal advisor boundary"

    @pytest.mark.asyncio
    async def test_successful_compaction_notifies_compaction_boundary(
        self, state, recording_service
    ):
        state.wire_session_compact_callback()
        callback = state.sessions.set_compact_callback.call_args[0][0]
        state.get_or_create_slot("test")
        await callback("dashboard:test", 87.0, success=True)
        assert ("dashboard:test", BOUNDARY_COMPACTION) in recording_service.calls

    @pytest.mark.asyncio
    async def test_failed_compaction_does_not_touch_the_epoch(self, state, recording_service):
        state.wire_session_compact_callback()
        callback = state.sessions.set_compact_callback.call_args[0][0]
        state.get_or_create_slot("test")
        await callback("dashboard:test", 87.0, success=False)
        compactions = [c for c in recording_service.calls if c[1] == BOUNDARY_COMPACTION]
        assert compactions == [], "a failed compact rewrote nothing"

    @pytest.mark.asyncio
    async def test_sibling_alias_survives_a_close(self, state, recording_service):
        """Two slots can front one session (a channel-stem slot and a dashboard
        tab linked to the same channel key). The observer is keyed per session,
        so a close boundary fired for an idle alias would drop the sibling's
        live observer mid-turn: the boundary fires only when the LAST slot
        fronting the session leaves."""
        from kiro_crew.dashboard.chat_handlers import close_slot

        a = state.get_or_create_slot("alias-a")
        b = state.get_or_create_slot("alias-b")
        a.linked_session_key = b.linked_session_key = "slack:1700000000.000100"
        await close_slot(state, a, "alias-a")
        closes = [c for c in recording_service.calls if c[1] == BOUNDARY_CLOSE]
        assert closes == [], "a sibling still fronts the session: no close boundary yet"
        await close_slot(state, b, "alias-b")
        closes = [c for c in recording_service.calls if c[1] == BOUNDARY_CLOSE]
        assert closes == [("slack:1700000000.000100", BOUNDARY_CLOSE)]


class TestPoolLifecycleWiring:
    """Round-4: the reap half of the pool must be reachable in production.

    A terminal boundary releases the parent's reviewer session, and gateway
    disposal shuts the pool down so the shared subprocess dies with the
    gateway instead of orphaning.
    """

    @pytest.mark.asyncio
    async def test_terminal_boundary_releases_the_pool_session(self):
        import asyncio  # noqa: F811

        service = AdvisorService(enabled=True, reviewer_available=True)

        class FakePool:
            def __init__(self):
                self.released = []

            async def release_session(self, key):
                self.released.append(key)

            async def shutdown(self):
                pass

        pool = FakePool()
        service.set_reviewer_pool(pool)
        service.attach("dashboard:a")
        service.notify_boundary("dashboard:a", "close")
        await asyncio.sleep(0)  # let the scheduled release run
        assert pool.released == ["dashboard:a"]

    @pytest.mark.asyncio
    async def test_dispose_all_schedules_pool_shutdown(self):
        import asyncio  # noqa: F811

        service = AdvisorService(enabled=True, reviewer_available=True)

        class FakePool:
            def __init__(self):
                self.shut = False

            async def release_session(self, key):
                pass

            async def shutdown(self):
                self.shut = True

        pool = FakePool()
        service.set_reviewer_pool(pool)
        service.dispose_all()
        await asyncio.sleep(0)
        assert pool.shut is True


class TestTerminalCloseReclaimsAllSessionState:
    """Round-26 (Opus): the terminal branch and dispose_all must reclaim
    EVERY per-session dict -- _last_review_at, _boundary_gen and
    _override_source otherwise grow for the gateway's lifetime."""

    def test_terminal_boundary_pops_all_dicts(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        key = "dashboard:leak"
        service.attach(key, override="on")
        service._last_review_at[key] = 1.0
        service._boundary_gen[key] = 3
        service.notify_boundary(key, "close")
        assert key not in service._observers
        assert key not in service._last_review_at
        assert key not in service._boundary_gen
        assert key not in service._override_source

    def test_dispose_all_clears_all_dicts(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        service.attach("dashboard:a", override="on")
        service._last_review_at["dashboard:a"] = 1.0
        service._boundary_gen["dashboard:a"] = 2
        service.dispose_all()
        assert not service._last_review_at
        assert not service._boundary_gen
        assert not service._override_source


class TestAdvisorConfigureIsPostBind:
    """Round-36: advisor configuration must not run inside aiohttp's
    on_startup (that runs BEFORE the listener binds). Both gateway
    entrypoints kick it strictly after ``_start_site`` returns, mirroring
    the connections-warm scavenge precedent."""

    def test_no_on_startup_hook_and_kick_in_both_entrypoints(self):
        import inspect

        from aiohttp import web

        from kiro_crew.dashboard import server

        src = inspect.getsource(server)
        baseline = web.Application()
        app = web.Application()
        server._register_advisor_hooks(app)
        assert len(app.on_startup) == len(
            baseline.on_startup
        ), "advisor must not configure pre-bind"
        assert len(app.on_cleanup) == len(baseline.on_cleanup) + 1
        assert hasattr(server, "_kick_advisor_configure")
        # both entrypoints call the kick after _start_site
        for entry in ("start_dashboard", "start_api_server"):
            fn = getattr(server, entry, None)
            if fn is None:
                continue
            body = inspect.getsource(fn)
            assert "_kick_advisor_configure(" in body, entry
            assert body.index("await _start_site(") < body.index("_kick_advisor_configure("), entry
        assert src.count("_register_advisor_hooks(app)") == 2, "dispose hook in BOTH entrypoints"


class TestDisabledSessionsLeaveNoRegistryResidue:
    """Round-38: under the default-off advisor every chat turn attaches and
    every close fires a terminal boundary -- neither may leave a per-session
    dict entry behind, or distinct closed sessions accumulate for the
    gateway's lifetime."""

    def test_disabled_attach_inserts_nothing(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=False)
        assert service.attach("dashboard:d1", override="inherit") is None
        assert "dashboard:d1" not in service._boundary_gen
        assert "dashboard:d1" not in service._override_source

    def test_terminal_boundary_reclaims_detached_state(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        service.attach("dashboard:d2", override="on")
        # opt out mid-session: observer detached, generation bumped
        assert service.attach("dashboard:d2", override="off") is None
        assert "dashboard:d2" in service._boundary_gen
        # the session then closes -- with NO observer present the terminal
        # boundary must still reclaim the detached session's state
        service.notify_boundary("dashboard:d2", "close")
        assert "dashboard:d2" not in service._boundary_gen
        assert "dashboard:d2" not in service._last_review_at
        assert "dashboard:d2" not in service._override_source

    def test_hard_kill_without_observer_inserts_nothing(self):
        import kiro_crew.advisor.service as service_mod
        from kiro_crew.advisor.service import AdvisorService, notify_hard_kill

        service = service_mod._service = AdvisorService(enabled=False)
        notify_hard_kill("dashboard:d3")
        assert "dashboard:d3" not in service._boundary_gen


class TestClearResetsAdvisorEpoch:
    """Round-39: `/clear` erases the conversation -- the observer's pending
    evidence must go with it, or the final pump resurfaces erased history as
    advice. The runner's EVENT_CLEAR_STATUS arm joins the boundary."""

    def test_clear_status_arm_notifies_boundary(self):
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        i = src.index("elif event.kind == EVENT_CLEAR_STATUS:")
        arm = src[i : i + 1500]
        assert "notify_boundary(" in arm and "BOUNDARY_CLEAR" in arm

    def test_clear_reason_is_epoch_scoped(self):
        from kiro_crew.advisor.service import BOUNDARY_CLEAR, AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        obs = service.attach("dashboard:c", override="on")
        obs.record_segment("erased history")
        service.notify_boundary("dashboard:c", BOUNDARY_CLEAR)
        assert service._observers.get("dashboard:c") is obs  # session survives
        assert obs.drain_update() is None  # evidence does not

    def test_clear_wipes_staged_and_peeked_context(self):
        """Round-40: `/clear` must drop preserved next-turn advice too --
        the conversation it describes is gone."""
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        i = src.index("elif event.kind == EVENT_CLEAR_STATUS:")
        arm = src[i : i + 2000]
        assert "clear_all_advisor_context(" in arm
        from unittest.mock import MagicMock

        from kiro_crew.advisor.delivery import clear_all_advisor_context

        slot = MagicMock()
        slot._advisor_pending_context = ["stale"]
        slot._advisor_peeked_context = ["stale"]
        slot._dirty = False
        clear_all_advisor_context(slot)
        assert slot._advisor_pending_context == []
        assert slot._advisor_peeked_context == []
        assert slot._dirty is True
