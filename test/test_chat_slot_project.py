"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import _session_work_dir
from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_PROBE_KEY = "slot:probe"

# Spawn sites that still state the raw project field, listed so the sweep below reports a
# NEW one. A reply-thread turn allocates under ``thread_session_key(slot.key, mid)``, a
# session of its own, so it is not on the path this change fixes and carrying it here would
# be a second behaviour change in a module this diff does not touch. The sweep compares
# EXACTLY, so fixing a listed site fails until it is removed from this list.
_OUT_OF_SCOPE_RAW_PROJECT_SITES = ["dashboard/chat_threads.py:1006"]


def _cleared_cwd(session_key: str = _PROBE_KEY) -> str:
    """The directory a cleared slot must resolve to: the session's own default.

    Taken from the symbol the provider factory binds, so the assertions below cannot pass
    against a path that merely looks right.
    """
    return str(_session_work_dir(session_key))


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


def _saveable_slot(tmp_path, monkeypatch):
    """A slot on a real ``DashboardState`` whose metadata line can be written and read back."""
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello")
    return state, slot


def _save(state, slot) -> None:
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    # closed=False: a closed session is skipped by rehydrate, so it could never round-trip.
    _save_slot_to_history(state, slot, closed=False)


def _restore(state):
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    return _rehydrate_slot_from_history(state, "s1")


class TestProjectClearedMarker:
    """``project_cleared`` distinguishes a REMOVED project from one never set.

    ``project=""`` carries both states, and only the removal invalidates a stored cwd, so
    the marker is what a consumer reads. These cover the two production writers and the
    round-trip through disk -- a marker no path writes, or one that dies at restart, leaves
    the distinction unreachable however carefully a consumer reads it.
    """

    @pytest.mark.asyncio
    async def test_clearing_over_http_records_the_clear(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project == ""
        assert slot.project_cleared is True
        assert await slot.resolved_cwd(_PROBE_KEY) == _cleared_cwd()

    @pytest.mark.asyncio
    async def test_clearing_over_http_schedules_the_clear_for_disk(self, tmp_path):
        """A 200 to the clear must not leave it in memory only.

        The periodic flush writes a slot's metadata line only while ``_dirty`` is set, and a
        clear arrives between turns, where nothing else on this path sets it -- so unmarked, a
        crash before the next save restores the project the user removed, and the slot they
        watched go blank comes back bound to it.
        """
        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        slot._dirty = False
        assert slot._dirty is False, "the slot starts marked, so the assertion below is vacuous"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_clearing_an_already_empty_project_arms_a_reset(self):
        """An empty project is not an unbound session, so this clear must tear one down.

        A slot created by sending a message carries no project, and ``cwd=None`` does not
        block warm-pool reuse (``cwd_blocks_pool`` in session_allocation), so its first
        turn takes a pooled child already rooted in the pool's own workspace. The clear
        moves only the marker, and a reset armed on the project TEXT alone would arm
        nothing -- while the live provider stays bound to that workspace and keeps
        answering, because ``_get_or_create_impl`` returns an existing session whatever
        cwd is requested. Relative writes then keep landing there with nothing to signal
        it, so the marker transition has to count as the directory change it is.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = False
        slot._dirty = False
        state = _mock_state(slot)
        state.sessions.get_provider = MagicMock(return_value=MagicMock())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key == effective_session_key(slot), (
            "the clear left the live session bound to the directory it was running in, so "
            "the next turn reuses that provider and writes keep landing in it"
        )
        assert slot._dirty is True, (
            "the marker moved while the project text stood still, so a flush keyed on the text "
            "alone would leave this clear in memory only"
        )

    @pytest.mark.asyncio
    async def test_clearing_an_already_cleared_project_stays_a_no_op(self):
        """Re-clearing moves nothing, so it must not cost a cold start.

        Neither the project nor the marker changes, so the reset stays unarmed -- otherwise
        every repeated clear tears down a session for no reason.
        """
        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = True
        slot._dirty = False
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
            assert resp.status == 200
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key is None
        assert slot._dirty is False, (
            "a request that moved neither the project nor the marker still queued a metadata "
            "write, so every repeated clear costs a flush"
        )

    @pytest.mark.asyncio
    async def test_the_clear_directive_arms_a_reset_on_an_already_empty_project(self):
        """The in-turn ``/project clear`` directive is the second writer of the marker.

        It reaches the same slot by another route, so it must agree with the endpoint
        above. Its own guard was the project text, which is empty on a send-created slot,
        so this is the same unbinding the endpoint missed -- reachable without ever
        touching the HTTP route.
        """
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.dashboard.session_directive_apply import _set_project

        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = False
        state = _mock_state(slot)
        await _set_project(state, slot, {"clear": True})
        assert slot.project_cleared is True
        assert slot._pending_reset_history_key == effective_session_key(slot), (
            "the directive recorded the clear but left the live session bound to the "
            "directory it was running in"
        )

    @pytest.mark.asyncio
    async def test_the_clear_directive_stays_a_no_op_when_already_cleared(self):
        """The directive's other half: re-clearing moves nothing and must cost no cold start.

        Without this, the transition arm above would widen into "arm on every clear" and
        every repeated directive would tear a session down.
        """
        from kiro_crew.dashboard.session_directive_apply import _set_project

        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = True
        state = _mock_state(slot)
        await _set_project(state, slot, {"clear": True})
        assert slot.project_cleared is True
        assert (
            slot._pending_reset_history_key is None
        ), "a directive that moved neither the project nor the marker still armed a reset"

    @pytest.mark.asyncio
    async def test_naming_a_project_over_http_retracts_the_clear(self, tmp_path):
        """The retraction half. Left set, the marker would outlive the state it describes and
        :meth:`resolved_cwd` -- which reads the marker BEFORE the project -- would keep
        binding the default workspace for a slot that now has a directory."""
        slot = _ChatSlot("test")
        slot.project_cleared = True
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(tmp_path)}
                )
                assert resp.status == 200
        assert slot.project_cleared is False
        assert await slot.resolved_cwd(_PROBE_KEY) == str(tmp_path)

    @pytest.mark.asyncio
    async def test_the_directive_clear_records_the_clear(self, tmp_path):
        """The OTHER production writer. The two clear paths are reached differently -- the
        directive in-process, the endpoint over loopback HTTP -- so covering one says nothing
        about the other."""
        from kiro_crew.dashboard.session_directive_apply import _set_project

        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot)
        await _set_project(state, slot, {"clear": True})
        assert slot.project == ""
        assert slot.project_cleared is True
        assert await slot.resolved_cwd(_PROBE_KEY) == _cleared_cwd()

    @pytest.mark.asyncio
    async def test_the_marker_survives_a_restart_and_outranks_the_stale_project(
        self, tmp_path, monkeypatch
    ):
        """The reason the marker has to reach disk at all.

        The metadata upsert cannot DELETE a key, so a slot cleared after a project was written
        comes back carrying BOTH keys. Rehydrating ``project`` alone hands that directory back
        as though it were still chosen -- which is the resurrection the marker exists to stop.
        Driven through the real save and the real restore, because an in-memory assertion
        cannot tell a persisted marker from one that merely never left the process.
        """
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        _save(state, slot)
        # Cleared AFTER the project reached disk: the line now carries both keys.
        slot.project_cleared = True
        _save(state, slot)
        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("project") == str(tmp_path), (
            "the stale project key is gone from the line, so this no longer reproduces the "
            "condition the marker exists for"
        )
        assert meta.get("project_cleared") is True

        del state._slots["s1"]
        restored = _restore(state)
        assert restored is not None
        assert restored.project_cleared is True, "the clear did not survive the restore"
        assert restored.project == "", (
            f"the restored slot's raw project is {restored.project!r}; a reader taking the "
            f"field rather than resolved_cwd spawns its turn in the removed directory"
        )
        resolved = await restored.resolved_cwd(_PROBE_KEY)
        assert resolved == _cleared_cwd(), (
            f"the restored slot resolves to {resolved!r}; the stale project key won over "
            f"the marker, so the cleared directory is live again"
        )

    @pytest.mark.asyncio
    async def test_a_restored_slot_with_no_marker_keeps_its_project(self, tmp_path, monkeypatch):
        """Negative control for the restore above: the same path must NOT report a clear for a
        slot that never had one, or every restored slot would read as cleared and the
        assertion above would pass for the wrong reason."""
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        _save(state, slot)
        assert (
            state.conversation_log._read_metadata("dashboard:s1").get("project_cleared") is not True
        )

        del state._slots["s1"]
        restored = _restore(state)
        assert restored is not None
        assert restored.project_cleared is False
        assert await restored.resolved_cwd(_PROBE_KEY) == str(tmp_path)

    def test_the_empty_window_merge_persists_the_clear(self, tmp_path, monkeypatch):
        """The other writer of the metadata line, reached when the window holds no new rows.

        A clear arrives between turns, so the save that follows it often has nothing to
        append and the empty-window merge is the only writer the marker ever sees. Skipping
        it there loses the clear on a restart inside that gap -- and because the upsert cannot
        delete the stale ``project``, the slot comes back pointing at the directory the user
        removed. The field is written unconditionally here, like the sibling clearable
        ``memory_store``, so ``test_session_control``'s slot-owned drift guard pins it.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project = str(tmp_path)
        slot.drain()
        # The merge only ever updates a record that exists, so the line has to be
        # materialized by a full save before the empty window can reconcile it.
        _save_slot_to_history(state, slot, closed=False, force=True)

        slot.project_cleared = True
        slot.messages.clear()
        _save_slot_to_history(state, slot, closed=False, force=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("project") == str(tmp_path), (
            "the stale project key is absent, so this no longer reproduces the condition that "
            "makes losing the marker a resurrection rather than a no-op"
        )
        assert meta.get("project_cleared") is True, (
            "the empty-window merge dropped the clear; a restart in the gap before the next "
            "row restores the stale project as though it were still chosen"
        )

    def test_naming_a_project_again_erases_the_marker_on_disk(self, tmp_path, monkeypatch):
        """Ownership, proven at the line rather than at the set: a carried key would survive
        the retraction and the slot would come back cleared after the user chose a directory."""
        state, slot = _saveable_slot(tmp_path, monkeypatch)
        slot.project_cleared = True
        slot.drain()
        _save(state, slot)
        assert state.conversation_log._read_metadata("dashboard:s1").get("project_cleared") is True

        slot.project = str(tmp_path)
        slot.project_cleared = False
        _save(state, slot)
        assert (
            state.conversation_log._read_metadata("dashboard:s1").get("project_cleared") is not True
        )

    def test_the_marker_is_slot_owned_so_absence_retracts_it(self):
        """Owned, not carried. ``carry_unowned_metadata`` forwards an unowned key forever, so
        a carried marker would make the clear un-erasable: naming a project again would write
        no key and the forwarded ``true`` would keep the slot reading as cleared."""
        from kiro_crew.history import SLOT_OWNED_META_KEYS, carry_unowned_metadata

        assert "project_cleared" in SLOT_OWNED_META_KEYS
        rebuilt = {"project": "/now/chosen"}
        merged = carry_unowned_metadata(rebuilt, {"project_cleared": True}, SLOT_OWNED_META_KEYS)
        assert "project_cleared" not in merged


class TestTheBindingItselfRetractsTheMarker:
    """Enforced by the ``project`` setter rather than by each writer remembering the pairing.

    Ten call sites across seven modules bind a project. One that forgets to retract leaves the
    slot resolving to the default workspace, so its side turns spawn there instead of in the
    directory just chosen -- the same wrong-tree harm this marker exists to prevent, and
    silent. The cases above cover the writers that exist today; these cover the enforcement
    point, which is what a writer added later inherits.
    """

    @pytest.mark.asyncio
    async def test_binding_a_project_retracts_a_standing_clear(self, tmp_path):
        slot = _ChatSlot("test")
        slot.project_cleared = True

        slot.project = str(tmp_path)

        assert slot.project_cleared is False
        assert await slot.resolved_cwd(_PROBE_KEY) == str(tmp_path)

    @pytest.mark.asyncio
    async def test_emptying_the_project_leaves_the_marker_to_its_writer(self):
        """Negative control, and the reason the retraction is keyed on a TRUTHY write: the clear
        paths empty the project and then state the marker, so retracting on an empty write would
        erase the very clear being recorded."""
        slot = _ChatSlot("test")
        slot.project_cleared = True

        slot.project = ""

        assert slot.project_cleared is True
        assert await slot.resolved_cwd(_PROBE_KEY) == _cleared_cwd()

    def test_a_field_named_at_runtime_is_bound_through_the_same_setter(self, tmp_path):
        """The slot-control handler writes whichever field the request names, so the binding
        arrives without ``project`` appearing in the source at all -- a shape no writer-by-writer
        pairing can cover."""
        slot = _ChatSlot("test")
        slot.project_cleared = True
        field = "project"

        setattr(slot, field, str(tmp_path))

        assert slot.project_cleared is False

    def test_no_writer_retracts_the_marker_behind_the_setter(self):
        """A writer that retracts on its own answers "has a project" for a slot that has none.

        The binding a writer assigns is not always truthy -- an agent with no workspace
        preference and no folder, or ``default_project_dir`` on a missing workspace directory,
        both resolve to empty -- so an unconditional retraction beside the assignment states
        the opposite of the clear it just walked past. The setter is keyed on a truthy write
        and is therefore the only place the retraction is correct for every binding.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        setter_and_initial_default = {
            src / "dashboard" / "state_parts" / "chat_slot.py",
            src / "dashboard" / "state_parts" / "slot_session.py",
        }
        offenders: list[str] = []
        for path in src.rglob("*.py"):
            if path in setter_and_initial_default:
                continue
            # Pinned: an unspecified encoding decodes with the locale's, and the
            # Windows runner's cp1252 has no mapping for the high bytes many of these
            # files carry, so the sweep raises instead of reporting offenders.
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "project_cleared = False" in stripped:
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these writers retract the cleared marker themselves instead of leaving it to the "
            f"``project`` setter, which only retracts for a truthy binding: {offenders}"
        )

    def test_no_hydration_path_restores_a_project_the_marker_retired(self):
        """A restore that pairs the stale ``project`` with the marker resurrects it anyway.

        Only :meth:`resolved_cwd` consults the marker, so a consumer reading the field
        directly spawns its turn in the removed directory -- and a metadata upsert cannot
        delete a key, so every cleared slot's line carries both. Each hydration path
        therefore has to decide between them, which is what the ``elif`` spelling records.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # See the sweep above for why the encoding is pinned.
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if line.strip() != 'slot.project = meta["project"]':
                    continue
                guard = next(
                    (
                        prior.strip()
                        for prior in reversed(lines[: number - 1])
                        if prior.strip() and not prior.strip().startswith("#")
                    ),
                    "",
                )
                if not guard.startswith("elif "):
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these hydration paths restore the persisted project without first deciding "
            f"whether the cleared marker retired it: {offenders}"
        )


class TestEverySpawnSiteResolvesTheClaim:
    """A cleared project and one never chosen are both empty, so no spawn site may read the
    raw field: only the slot's own resolver separates them."""

    @pytest.mark.asyncio
    async def test_a_cleared_slot_resolves_to_the_session_default(self):
        slot = _ChatSlot("test")
        slot.project_cleared = True

        resolved = await slot.resolved_cwd("slot:cleared")

        assert resolved == str(_session_work_dir("slot:cleared")), (
            "a cleared slot has to name the SAME directory the provider factory binds "
            "for this session, or the turn compares against one no provider uses"
        )
        # The point of resolving at all: what reaches the spawn is not falsy, so it
        # cannot land on the factory's "no cwd stated" branch.
        assert resolved

    @pytest.mark.asyncio
    async def test_a_chosen_directory_and_a_never_set_slot_pass_through(self):
        chosen = _ChatSlot("chosen")
        chosen.project = "/picked/by/user"
        assert await chosen.resolved_cwd("slot:set") == "/picked/by/user"

        # None states no requirement, which is what keeps the warm pool usable for a
        # slot that never had a project. Resolving it would bind every such slot.
        never = _ChatSlot("never")
        assert await never.resolved_cwd("slot:never") is None

    @pytest.mark.asyncio
    async def test_the_marker_outranks_a_project_left_standing_beside_it(self):
        """The ordering inside the resolver, which no caller can get wrong any more.

        A restored line carries both keys, so a resolver reading the project first would
        hand back the removed directory. Set here directly rather than through the setter,
        which retracts the marker -- this is the on-disk pairing, not a reachable write.
        """
        slot = _ChatSlot("test")
        slot._project = "/removed/by/user"
        slot.project_cleared = True

        assert await slot.resolved_cwd("slot:both") == str(_session_work_dir("slot:both"))

    def test_no_spawn_site_hands_over_the_raw_project_field(self):
        """The spawns read the slot's resolver, not ``slot.project``.

        :meth:`resolved_cwd` binds a concrete directory for a cleared slot where the raw
        field answers an empty string, and the two are indistinguishable to a spawn: both
        are falsy. A site passing the field cannot tell "the user removed this directory"
        from "no directory was ever chosen", and only the first must refuse the stored cwd.

        Walked as an AST rather than matched per line, because the coalesce and the
        spawn need not share one: ``chat_threads`` bound ``project = slot.project or
        None`` and passed ``cwd=project`` seventy lines further down, which a
        line-scoped regex cannot see and which therefore read clean while that site
        bypassed the resolver. So a local carrying the field counts as the field.

        Matched on the ATTRIBUTE ACCESS under any receiver: a site holding the slot in
        another variable reintroduces the collapse. Two bounds keep it from flagging
        correct code. The fallback must be a CONSTANT, because that is the shape that
        destroys the distinction -- ``preparation.project or effective_cwd`` falls back
        to another directory and loses nothing. And the call must be the allocation
        entry point, because a crew-log record also takes a ``cwd=`` and is reporting
        the project rather than binding a directory.
        """
        import ast
        import pathlib

        SPAWN = "get_or_create"

        def _destroys_the_distinction(node: ast.expr | None) -> bool:
            """``<recv>.project``, bare or coalesced with a constant."""
            if isinstance(node, ast.Attribute) and node.attr == "project":
                return True
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                head, *rest = node.values
                return _destroys_the_distinction(head) and all(
                    isinstance(value, ast.Constant) for value in rest
                )
            return False

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # Encoding pinned for the same reason as the sweep above.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            tainted: set[str] = set()
            for node in ast.walk(tree):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                if targets and _destroys_the_distinction(getattr(node, "value", None)):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            tainted.add(target.id)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                name = callee.attr if isinstance(callee, ast.Attribute) else None
                if name is None and isinstance(callee, ast.Name):
                    name = callee.id
                if name != SPAWN:
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "cwd":
                        continue
                    carried = isinstance(keyword.value, ast.Name) and keyword.value.id in tainted
                    if _destroys_the_distinction(keyword.value) or carried:
                        offenders.append(f"{path.relative_to(src)}:{keyword.value.lineno}")

        assert offenders == _OUT_OF_SCOPE_RAW_PROJECT_SITES, (
            "these spawn sites state the raw project field as their cwd, so a cleared "
            f"slot is indistinguishable from one that never had a project: {offenders}. "
            "A site that is FIXED must also be dropped from "
            "_OUT_OF_SCOPE_RAW_PROJECT_SITES, which is why this compares exactly."
        )

    def test_no_spawn_module_rolls_its_own_branch_on_the_marker(self):
        """One resolver decides a cwd from the marker, and it lives on the slot.

        The defect this change fixes WAS a falsy-coalesce (``slot.project or None``). A
        module that reads ``project_cleared`` and writes its own ``if`` to pick a directory
        rebuilds that hazard privately: its branch passes every behavioural test until the
        two spellings disagree, and then one spawn site binds the removed directory while
        the rest do not. Reading the marker is legitimate elsewhere -- the codec hydrates
        it, the handlers roll it back, forks carry it -- so this is keyed on modules that
        SPAWN.

        ``state.py`` is excepted because it is where the resolver lives.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            # Encoding pinned for the same reason as the sweeps above.
            text = path.read_text(encoding="utf-8")
            if path.name == "state.py" or "get_or_create(" not in text:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith("#") and "project_cleared" in stripped:
                    offenders.append(f"{path.relative_to(src)}:{number}")

        assert offenders == [], (
            "these modules spawn sessions AND read the cleared marker, so a second rule for "
            f"picking a directory can diverge from the slot's resolver: {offenders}"
        )
