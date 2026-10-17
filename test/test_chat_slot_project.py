"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # No live session and no in-flight reservation by default: the arm guard
    # reads both get_provider() and session_keys(), so a realistic empty
    # default keeps the "do not arm" cases honest. Tests that exercise an armed
    # path override these explicitly.
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.session_keys = MagicMock(return_value=frozenset())
    state.sessions.resumable_hint = MagicMock(return_value=False)
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


class TestChatSlotProject:
    @pytest.mark.asyncio
    async def test_set_project(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["project"] == str(tmp_path)
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_clear_project(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": ""},
            )
            assert resp.status == 200
            assert slot.project == ""

    @pytest.mark.asyncio
    async def test_nonexistent_dir_returns_400(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": "/nonexistent_xyz_123"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sensitive_path_returns_403(self, tmp_path):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 403

    @pytest.mark.asyncio
    async def test_data_home_overlap_returns_actionable_400(self, tmp_path, monkeypatch):
        """Pre-flight: a workspace containing the voice runtime is refused
        at the endpoint with the actionable message, before any session spawn."""
        import kiro_crew.sandbox as sandbox_mod

        # The pre-flight is darwin-gated to match the spawn-time guards it
        # mirrors, so pin the platform for the refusal path.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": str(tmp_path)},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "workspace_overlaps_data_home"
            assert "protected voice runtime" in data["error"]
            # The guard message embeds paths with !r, so on Windows the
            # backslashes are repr-escaped — assert the repr form, which is the
            # exact token the formatter emits on every platform.
            assert repr(str(runtime)) in data["error"]
            assert "Pick a project subdirectory" in data["error"]
            assert slot.project != str(tmp_path)

    @pytest.mark.asyncio
    async def test_can_change_mid_session(self, tmp_path):
        """Unlike workspace, project can be changed after messages are sent."""
        slot = _ChatSlot("test")
        slot.total_messages = 5
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                assert slot.project == str(tmp_path)

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/project",
                json={"project": "/tmp"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_change_defers_session_reset(self, tmp_path):
        """Endpoint sets the deferred-reset flag instead of resetting inline,
        because an inline reset would killpg the MCP-core child that called it.
        chat_runner consumes the flag so the next message picks up the new CWD.

        A session is registered under the slot's key, so the reset is owed:
        get_or_create would otherwise reuse that stale-CWD session for a turn."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        # A live session under the slot's key — it carries the old CWD, so the
        # reset must arm.
        state.sessions.get_provider = MagicMock(return_value=object())
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        # Reset is deferred — endpoint must NOT call it inline.
        state.sessions.reset.assert_not_awaited()
        # Flag is set on the slot so chat_runner can consume it at the turn boundary.
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_change_with_no_live_session_does_not_arm_reset(self, tmp_path):
        """Setting the project on a slot with NO registered session and
        no eager spawn in flight must NOT arm the deferred reset.

        get_or_create cold-starts the session already rooted at the new project,
        so an armed reset would tear that already-correct session down at the
        first turn's end for nothing — and with tool_search on, the resume pays
        a full chat-text replay that drops every tool result from turn 1. This
        mirrors the guard api_chat_slot_model already applies to its own
        deferred reset."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        # No session registered under the slot's key.
        state.sessions.get_provider = MagicMock(return_value=None)
        # Eager spawn disabled in tests (live.snapshot() is None), so
        # schedule_eager_spawn returns None and no spawn is in flight.
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
                assert slot.project == str(tmp_path)
        # No teardown, and critically no armed flag to tear down the cold start.
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_change_with_only_scheduled_eager_spawn_does_not_arm_reset(self, tmp_path):
        """A freshly scheduled eager spawn roots the session at the NEW project
        (scheduling cancels any prior eager task), so it never carries the old
        CWD and must NOT by itself arm the reset.

        With no registered provider and no turn in flight, arming would leave a
        flag that tears the eager spawn's correctly rooted session down at the
        first turn's end for nothing. The scheduled task is in flight (not
        done()), yet the reset stays disarmed.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.sessions.get_provider = MagicMock(return_value=None)
        pending = asyncio.Future()  # a task that is not done()
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch(
                "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
                return_value=pending,
            ),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None
        pending.cancel()

    @pytest.mark.asyncio
    async def test_change_with_turn_in_flight_arms_reset(self, tmp_path):
        """When no session is registered yet and no eager spawn is in flight,
        but a turn is already running (or its admission is reserved), that
        turn's get_or_create captured the old project before this change and
        will register a session rooted at the old CWD. get_provider cannot see
        it — nothing enters the session map until provider.start() returns — so
        the reset must still arm on the running signal.

        schedule_eager_spawn stands down while a turn runs, so the eager operand
        is None here and only slot.running keeps the reset owed.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.sessions.get_provider = MagicMock(return_value=None)
        # Admission reserved for an in-flight turn: slot.running is True with no
        # registered provider and no eager spawn.
        slot._turn_admission_reserved = True
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch(
                "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_change_with_cold_start_reserved_under_key_arms_reset(self, tmp_path):
        """A cold start reserved under the slot's key must arm the reset even
        when no provider is registered and this slot is not running.

        A channel-linked turn (Slack/Discord) resuming on
        effective_session_key(slot) passes no cwd, so it resolves the stored old
        CWD and will register a session rooted there. Nothing is in the session
        map yet (get_provider is None) and this slot's own ``running`` is False,
        but the allocation reservation is visible through session_keys(). The
        reset must stay owed, or the old-CWD session is reused and the project
        change is silently dropped.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.sessions.get_provider = MagicMock(return_value=None)
        # No turn on THIS slot (slot.running is False), but a cold start is
        # reserved under the key — session_keys() includes reservations.
        state.sessions.session_keys = MagicMock(return_value=frozenset({"dashboard:test"}))
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch(
                "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_change_with_resumable_but_not_live_session_arms_reset(self, tmp_path):
        """A key with a resumable entry but no live session or reservation must
        arm the reset.

        A channel-linked slot whose session was idle-reaped, or lost in a
        gateway restart, keeps its resume sid and old cwd on disk while nothing
        is live (get_provider None), nothing is reserved (session_keys empty)
        and this slot is not running. The next channel turn resumes that session
        in the old project and later dashboard turns reuse it, so the reset must
        stay owed. resumable_hint() reports the on-disk entry.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.session_keys = MagicMock(return_value=frozenset())
        # Idle-reaped / restart-lost: resumable on disk, nothing live.
        state.sessions.resumable_hint = MagicMock(return_value=True)
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch(
                "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:test"

    @pytest.mark.asyncio
    async def test_unchanged_does_not_set_pending_reset(self, tmp_path):
        """No-op when project doesn't change: no inline reset and no flag set."""
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_pick_waits_for_a_preparing_turn(self, tmp_path):
        """A pick that lands while a turn is preparing must not move the
        binding that turn captured. The turn's dispatch holds
        ``slot_switch_session_lock`` from its binding capture to its session
        registration; the route takes the same lock, so the write waits until
        the turn has registered instead of failing it with "conversation
        binding changed during preparation"."""
        import asyncio

        from kiro_crew.llm_helpers import slot_switch_session_lock
        from kiro_crew.testing.wait import until_parked

        slot = _ChatSlot("test")
        slot.project = "/start-of-turn"
        state = _mock_state(slot)
        # The preparing turn registers its old-CWD session before the parked
        # pick resumes, so by the time the arm guard runs the key is live and
        # the reset is owed.
        state.sessions.get_provider = MagicMock(return_value=object())
        dispatch_lock = slot_switch_session_lock("dashboard:test")
        with (
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
            patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                # Stand in for a turn that has captured its binding and is
                # still registering its session.
                await dispatch_lock.acquire()
                try:
                    pick = asyncio.create_task(
                        client.post(
                            "/api/chat/slots/test/project",
                            json={"project": str(tmp_path)},
                        )
                    )
                    # Barrier before the absence assertions: the request is
                    # observed parked on the session lock, so the negatives
                    # cannot pass merely because it has not run yet.
                    await until_parked(dispatch_lock, timeout=10)
                    assert not pick.done()
                    assert slot.project == "/start-of-turn"
                    assert slot._pending_reset_history_key is None
                finally:
                    dispatch_lock.release()
                resp = await asyncio.wait_for(pick, timeout=30)
                assert resp.status == 200
        assert slot.project == str(tmp_path)
        assert slot._pending_reset_history_key == "dashboard:test"


class TestFolderProjectDirOverlapPreflight:
    """The folder ``project_dir`` write path is the third
    user-driven project chokepoint — it must refuse a data-home overlap at the
    moment of choice with the SAME message as the endpoint and set_project.
    The check lives in ``_folder_project_overlap_denied`` (run off-loop
    by the create/update handlers), NOT in ``_validate_project_dir``, which the
    slot-create read path re-runs against stored values."""

    def _pin_runtime(self, tmp_path, monkeypatch):
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "data" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(
            sandbox_mod,
            "_voice_runtime_sandbox_paths",
            lambda: (str(runtime),),
        )
        return runtime

    def test_folder_overlap_denied_with_guard_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        runtime = self._pin_runtime(tmp_path, monkeypatch)
        err = _folder_project_overlap_denied(str(tmp_path))
        assert err is not None
        # Byte-identical family: same formatter as endpoint + spawn guard.
        assert "protected voice runtime" in err
        assert repr(str(runtime)) in err
        assert "Pick a project subdirectory" in err

    def test_folder_overlap_check_accepts_non_overlapping_dir(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        self._pin_runtime(tmp_path, monkeypatch)
        clean = tmp_path / "clean"
        clean.mkdir()
        assert _folder_project_overlap_denied(str(clean)) is None
