"""Tests for POST /api/chat/slots/{slot}/project endpoint."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_project
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState, *, internal_auth: bool = False) -> web.Application:
    @web.middleware
    async def mark_internal(request: web.Request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[mark_internal] if internal_auth else [])
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
    async def test_assignment_superseded_during_recent_save_has_no_rollback_receipt(self, tmp_path):
        old = tmp_path / "old"
        requested = tmp_path / "requested"
        newer = tmp_path / "newer"
        for path in (old, requested, newer):
            path.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(old)
        state = _mock_state(slot)
        save_started = threading.Event()
        release_save = threading.Event()

        def save_recent(_project: str) -> None:
            if not save_started.is_set():
                save_started.set()
                assert release_save.wait(5)

        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project", save_recent):
            async with TestClient(TestServer(_make_app(state, internal_auth=True))) as client:
                assignment = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/test/project",
                        json={"project": str(requested), "return_previous": True},
                    )
                )
                assert await asyncio.to_thread(save_started.wait, 5)
                assigned_generation = slot._project_generation
                slot.project = str(newer)
                release_save.set()

                response = await assignment
                data = await response.json()
                assert response.status == 409
                assert data["code"] == "project_changed"
                assert "generation" not in data
                assert "previous_project" not in data

                stale = await client.post(
                    "/api/chat/slots/test/project",
                    json={
                        "project": str(old),
                        "expected_generation": assigned_generation,
                    },
                )
                assert (await stale.json())["applied"] is False

        assert slot.project == str(newer)

    @pytest.mark.asyncio
    async def test_conditional_restore_refuses_newer_generation(self, tmp_path):
        old = tmp_path / "old"
        first = tmp_path / "first"
        newer = tmp_path / "newer"
        for path in (old, first, newer):
            path.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(old)
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state, internal_auth=True))) as client:
                first_resp = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(first), "return_previous": True},
                )
                first_data = await first_resp.json()
                first_generation = first_data["generation"]
                assert first_data["previous_project"] == str(old)
                newer_resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(newer)}
                )
                newer_generation = (await newer_resp.json())["generation"]
                stale = await client.post(
                    "/api/chat/slots/test/project",
                    json={
                        "project": str(old),
                        "expected_generation": first_generation,
                    },
                )
                stale_data = await stale.json()
                assert stale_data["applied"] is False
                assert slot.project == str(newer)

                current = await client.post(
                    "/api/chat/slots/test/project",
                    json={
                        "project": str(old),
                        "expected_generation": newer_generation,
                    },
                )
                current_data = await current.json()
                assert current_data["applied"] is True
                assert slot.project == str(old)

    @pytest.mark.asyncio
    async def test_project_mutation_retry_replays_original_response(self, tmp_path):
        first = tmp_path / "first"
        newer = tmp_path / "newer"
        first.mkdir()
        newer.mkdir()
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        first_body = {"project": str(first), "mutation_id": "mutation-first"}
        with patch("kiro_crew.dashboard.chat_handlers._save_recent_project"):
            async with TestClient(TestServer(_make_app(state, internal_auth=True))) as client:
                initial = await client.post("/api/chat/slots/test/project", json=first_body)
                initial_data = await initial.json()
                await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(newer), "mutation_id": "mutation-newer"},
                )
                replay = await client.post("/api/chat/slots/test/project", json=first_body)
                assert await replay.json() == initial_data
                assert slot.project == str(newer)
                conflict = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(newer), "mutation_id": "mutation-first"},
                )
                assert conflict.status == 409
                assert (await conflict.json())["code"] == "mutation_conflict"

    @pytest.mark.asyncio
    async def test_conditional_restore_accepts_owner_but_predecessor_requires_internal_auth(
        self, tmp_path
    ):
        old = tmp_path / "old"
        new = tmp_path / "new"
        old.mkdir()
        new.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(old)
        state = _mock_state(slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            denied = await client.post(
                "/api/chat/slots/test/project",
                json={"project": str(new), "expected_generation": "generation"},
            )
        assert denied.status == 404

        with (
            patch(
                "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
                return_value=True,
            ),
            patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                assigned = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(new)}
                )
                generation = (await assigned.json())["generation"]
                restored = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(old), "expected_generation": generation},
                )
                restored_data = await restored.json()
                predecessor = await client.post(
                    "/api/chat/slots/test/project",
                    json={"project": str(new), "return_previous": True},
                )

        assert restored.status == 200
        assert restored_data["applied"] is True
        assert slot.project == str(old)
        assert predecessor.status == 404


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
