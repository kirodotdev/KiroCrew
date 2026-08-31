"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project, api_chat_slot_workspace
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


class TestWorkspaceSwitchAdvancesTheGeneration:
    """A workspace switch commits a new project, so it must advance the generation.

    The retry site prefers a live arm over the cwd its caller stated, which is correct
    while the arm is the newest statement about the project. A switch that commits a new
    project without advancing the generation leaves the earlier arm live, so it outranks
    the selection and the first turn's relative writes land in the project the user left.
    """

    @staticmethod
    def _factory(seen: list):
        def factory(session_key=None, agent=None, channel_id=None, cwd=None, **kwargs):
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.shutdown = AsyncMock()
            provider.cwd = cwd if cwd else "/unset"
            provider.context_usage_pct = MagicMock(return_value=0.0)
            provider.is_alive = MagicMock(return_value=True)
            provider.is_process_alive = MagicMock(return_value=True)
            provider.has_active_turn = MagicMock(return_value=False)
            provider.runtime_info = MagicMock(return_value=(None, None))
            seen.append(provider)
            return provider

        return factory

    @pytest.mark.asyncio
    async def test_the_retried_turn_binds_the_newly_selected_workspace(self, tmp_path):
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.session import SessionManager

        beta = tmp_path / "beta"
        gamma = tmp_path / "gamma"
        for d in (beta, gamma):
            d.mkdir()

        seen: list = []
        mgr = SessionManager(KiroCrewConfig(), provider_factory=self._factory(seen))
        slot = _ChatSlot("test")
        slot.project = str(beta)
        state = _mock_state(slot)
        state.sessions = mgr

        # The key the slot's turns actually run on, which is what the handler advances.
        session_key = effective_session_key(slot)
        # The eager spawn for beta armed the key; nothing has satisfied it yet.
        mgr.mark_retire_on_next_claim(session_key, str(beta))

        with (
            patch(
                "kiro_crew.dashboard.chat_handlers._reset_slot_session_or_warn",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "kiro_crew.dashboard.chat_handlers.default_project_dir",
                new=MagicMock(return_value=str(gamma)),
            ),
            patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", new=AsyncMock()),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/workspace", json={"workspace": "gamma-ws"}
                )
                assert resp.status == 200, await resp.text()

        assert slot.project == str(gamma), "precondition: the switch committed gamma"

        # The first turn after the switch. It states gamma; the retry site must not
        # substitute the arm's beta for it.
        provider, _, _ = await mgr.get_or_create(session_key, cwd=str(gamma))
        assert provider.cwd == str(gamma), (
            "the turn after a workspace switch must bind the selected project; binding "
            f"{provider.cwd!r} writes into the workspace the switch left"
        )
        await mgr.close_all()


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
        """#7392 pre-flight: a workspace containing the voice runtime is refused
        at the endpoint with the actionable message, before any session spawn."""
        import kiro_crew.sandbox as sandbox_mod

        # The pre-flight is darwin-gated to match the spawn-time guards it
        # mirrors (review round 1), so pin the platform for the refusal path.
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
            # The guard message embeds paths with !r (#7407), so on Windows the
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
        chat_runner consumes the flag so the next message picks up the new CWD."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
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
    async def test_the_arm_is_raised_before_the_endpoint_returns(self, tmp_path):
        """The arm cannot wait for the deferred consumer -- there is a window before it.

        The RESET is deferred because this endpoint is reachable over loopback from inside
        the kiro-cli process group, so an inline teardown would killpg the caller. The ARM
        is not: it is in-memory bookkeeping. Leaving it to `_consume_pending_reset` puts it
        behind the eager task's 1.5s debounce, and a channel turn arriving in that window
        states NO cwd -- so the claim-time directory check cannot fire and the arm is the
        only thing that would refuse the pre-change session. Raised here, the window is
        closed by the time the caller gets its 200.
        """
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1234567890.123456"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200

        # Nothing has consumed the deferral yet -- no eager task was allowed to run.
        assert slot._pending_reset_history_key == "slack:1234567890.123456"
        state.sessions.mark_retire_on_next_claim.assert_called_once_with(
            "slack:1234567890.123456", str(tmp_path)
        )

    @pytest.mark.asyncio
    async def test_channel_linked_slot_defers_reset_on_its_channel_session(self, tmp_path):
        """A channel-born slot runs its turns on the channel's own session, so the
        deferred teardown has to name THAT session. The ``dashboard:`` prefix is
        unconditional, so deriving the key from the slot key instead would name a
        nonexistent ``dashboard:slack:<ts>``, the teardown would miss the live
        session, and a later turn would reuse it with the pre-change directory."""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1234567890.123456"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(tmp_path)},
                )
                assert resp.status == 200
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "slack:1234567890.123456"
        assert "dashboard:" not in slot._pending_reset_history_key

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


class TestFolderProjectDirOverlapPreflight:
    """#7392 review round 3: the folder ``project_dir`` write path is the third
    user-driven project chokepoint — it must refuse a data-home overlap at the
    moment of choice with the SAME message as the endpoint and set_project.
    Round 4: the check lives in ``_folder_project_overlap_denied`` (run off-loop
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
        # Byte-identical family: same formatter as endpoint + spawn guard (#7407).
        assert "protected voice runtime" in err
        assert repr(str(runtime)) in err
        assert "Pick a project subdirectory" in err

    def test_folder_overlap_check_accepts_non_overlapping_dir(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_folders import _folder_project_overlap_denied

        self._pin_runtime(tmp_path, monkeypatch)
        clean = tmp_path / "clean"
        clean.mkdir()
        assert _folder_project_overlap_denied(str(clean)) is None
