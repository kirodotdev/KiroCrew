"""An unsettled arm target may not leave a binding published.

``_settle_arm_target`` reports whether the key it resolved SETTLED, and retracts the source
arm when it did not. A caller that publishes anyway leaves a workspace or project visible on
the slot with no arm raised to carry it, so a later cwd-less claim binds the directory the
slot is leaving and every relative write in that turn lands in the wrong project. The caller
must therefore publish nothing and answer 409, and unwind whatever it had already committed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_project, api_chat_slot_workspace
from kiro_crew.dashboard.chat_utils import bind_linked_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def dashboard_auth_marker(request, handler):
        request["user"] = "dashboard"
        return await handler(request)

    app.middlewares.append(dashboard_auth_marker)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_context_usage = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.note_project_change = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.transfer_retire_arm = MagicMock()
    state.sessions.supersede_arm_for_new_slot = MagicMock()
    state.sessions.mark_retire_on_next_claim = MagicMock(return_value=1)
    state.conversation_log = MagicMock()
    return state


class TestAnUnsettledArmPublishesNoWorkspace:
    @pytest.mark.asyncio
    async def test_a_parked_rebind_during_the_settle_commits_nothing(self, monkeypatch):
        """The switch must not reach its reset, because nothing may be published first.

        A rebind arriving inside the settle's own region is PARKED, so the key the settle
        observed never moved and it reports unsettled while retracting the source arm. The
        binding pair is committed before the reset by design, so publishing on an unsettled
        key exposes a workspace and project no transfer will ever carry. The 409 alone does
        not discriminate -- the later rebind guard answers that too, once the parked key is
        applied -- so what this pins is that NOTHING was published and no session torn down.
        """
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        # Empty so the settle RESOLVES rather than short-circuiting on a stated project --
        # that resolve is the only await inside the region, so it is where a rebind can park.
        slot.project = ""
        slot.linked_session_key = "slack:1111.0001"
        state = _mock_state(slot)
        monkeypatch.setattr(chat_handlers, "default_project_dir", lambda ws: "/workspace/new-ws")

        async def _park_a_rebind(key, cwd):
            bind_linked_session_key(slot, "cron:job-9")
            return "/workspace/cleared"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_park_a_rebind)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            body = await resp.json()

        assert resp.status == 409
        assert body["code"] == "session_rebound"
        assert state.sessions.reset.await_count == 0, (
            "the switch tore down a session after an unsettled settle, so the bindings it "
            "published were visible with no arm raised to carry them"
        )
        assert (
            slot.workspace == "old-ws"
        ), f"the workspace was published on an unsettled key; got {slot.workspace!r}"
        assert (
            slot.project == ""
        ), f"the project was published on an unsettled key; got {slot.project!r}"


class TestAnUnsettledArmPublishesNoProject:
    @pytest.mark.asyncio
    async def test_an_unsettled_transfer_unwinds_the_committed_project(self, tmp_path):
        """The published project must be unwound, not merely left without its reset.

        This branch already retracts the deferred reset, which is what proves the key is
        gone -- and that is exactly why the project committed before the transfer may not
        stand: a cwd-less claim on the session the slot moved to would bind the directory
        the slot left. Re-marked dirty because the save above awaits, so the periodic flush
        may already have written the provisional project to disk.
        """
        new_project = tmp_path / "new"
        new_project.mkdir()
        slot = _ChatSlot("test")
        slot.project = "/old/project"
        slot.project_cleared = False
        slot.linked_session_key = "slack:1111.0001"
        slot._dirty = False
        state = _mock_state(slot)
        state.sessions.resolve_arm_cwd = AsyncMock(return_value="/workspace/cleared")

        # Runs in a worker thread, as the real save does, and rebinds the slot there: that
        # is the window the handler's own comment names as the one a rebind lands in.
        def _rebind_during_the_save(_project):
            slot.linked_session_key = "cron:job-9"

        # Unsettled, reported by the helper the handler calls: the branch under test is the
        # one the helper's own retraction leaves behind.
        async def _unsettled(*_a, **_k):
            return None, "cron:job-9", "/workspace/cleared", False

        with (
            patch.object(chat_handlers, "_save_recent_project", _rebind_during_the_save),
            patch.object(chat_handlers, "_settle_arm_target", AsyncMock(side_effect=_unsettled)),
            patch.object(chat_handlers, "schedule_eager_spawn", MagicMock()),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(new_project)}
                )
                body = await resp.json()

        assert resp.status == 409, (
            "an unsettled transfer answered success, so the caller believes the project "
            f"binding took; got {resp.status} {body}"
        )
        assert body["code"] == "session_rebound"
        assert slot.project == "/old/project", (
            "the project stayed published after the settle retracted its arm, so a cwd-less "
            f"claim binds the directory the slot left; got {slot.project!r}"
        )
        assert slot.project_cleared is False
        assert slot._pending_reset_history_key is None
        assert slot._dirty is True, (
            "the rollback was not re-marked dirty, so a flush that persisted the provisional "
            "project leaves the rejected value on disk across a restart"
        )

    @pytest.mark.asyncio
    async def test_a_cleared_slot_comes_back_cleared(self, tmp_path):
        """The marker is unwound WITH the project, or a cleared slot reads as never-set.

        The commit retires the marker as it publishes a project, so restoring the path alone
        leaves an empty project without the flag that distinguishes "deliberately cleared"
        from "never had one" -- and the resume then restores the project the user cleared.
        """
        new_project = tmp_path / "new"
        new_project.mkdir()
        slot = _ChatSlot("test")
        slot.project = ""
        slot.project_cleared = True
        slot.linked_session_key = "slack:1111.0001"
        state = _mock_state(slot)
        state.sessions.resolve_arm_cwd = AsyncMock(return_value="/workspace/cleared")

        def _rebind_during_the_save(_project):
            slot.linked_session_key = "cron:job-9"

        async def _unsettled(*_a, **_k):
            return None, "cron:job-9", "/workspace/cleared", False

        with (
            patch.object(chat_handlers, "_save_recent_project", _rebind_during_the_save),
            patch.object(chat_handlers, "_settle_arm_target", AsyncMock(side_effect=_unsettled)),
            patch.object(chat_handlers, "schedule_eager_spawn", MagicMock()),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/project", json={"project": str(new_project)}
                )

        assert resp.status == 409
        assert slot.project == ""
        assert slot.project_cleared is True, (
            "the cleared marker was not unwound with the project, so the empty project reads "
            "as never-set and the resume restores the one the user cleared"
        )
