"""Slot switches on a slot whose turns run under a linked channel or cron session.

The switch handlers probe and reset the session under ``linked_session_key`` and
resolve that key inside the slot lock. A rebind that lands mid-switch rolls the
switch back with a 409, lands the slot in the bulk route's ``skipped_running``,
or, once a live effort push has persisted, commits with a warning. An App Kit
caller that owns the slot but not its linked session is refused.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_switch_atomicity_helpers import _MODEL_A, _MODEL_B, _make_app, _mock_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.testing.wait import await_until


def _make_app_as(state: DashboardState, app_name: str) -> web.Application:
    """Like _make_app but the caller is an App Kit token owning *app_name*."""

    @web.middleware
    async def app_marker(request, handler):
        request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[app_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


class TestLinkedSlotSessionKey:
    """A channel-/cron-born slot runs its turns under ``linked_session_key``.

    The switch handlers must probe and reset THAT session (the reload
    handler's rule), not the ``dashboard:<slot>`` spelling that names a
    session which never existed — otherwise the busy probe sees nothing and
    the reset "succeeds" against nothing while the live process keeps the old
    model. And slot ownership does not imply ownership of the linked session,
    so an app caller may not switch a channel thread's model.
    """

    @pytest.mark.asyncio
    async def test_model_switch_probes_and_resets_the_linked_session(self):
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 200
            assert slot.model == _MODEL_B
            probed = {c.args[0] for c in state.sessions.get_provider.call_args_list}
            assert probed == {"slack:123.456"}
            state.sessions.reset.assert_awaited_once()
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"

    @pytest.mark.asyncio
    async def test_model_switch_sees_the_linked_sessions_active_turn(self):
        # The busy probe now lands on the live linked session: an in-flight
        # channel turn answers 409 instead of a silent success over it.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.linked_session_key = "slack:123.456"
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = True
        state = _mock_state(slot, provider=None)
        state.sessions.get_provider = MagicMock(
            side_effect=lambda key: provider if key == "slack:123.456" else None
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_caller_cannot_switch_a_linked_sessions_model(self):
        # Owning the slot is not owning the channel session it is bound to:
        # denied as an indistinguishable 404, nothing mutated.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 404
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_caller_still_switches_its_own_unlinked_slot(self):
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot._app = "demo-app"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 200
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_bulk_switch_resets_the_linked_session_for_dashboard_users(self):
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"

    @pytest.mark.asyncio
    async def test_bulk_switch_skips_linked_slots_for_app_callers(self):
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == []
            assert data["skipped_running"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workspace_switch_resets_the_linked_session(self):
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "ws2"})
            assert resp.status == 200
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"

    @pytest.mark.asyncio
    async def test_binding_that_lands_while_queued_on_the_lock_is_the_one_switched(self):
        # The key is resolved INSIDE the lock: a slot that gets linked while
        # the request waits on slot._lock has its LINKED session probed and
        # reset, not the dashboard:<slot> key a pre-lock read would have named.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
                )
                # flake-ok: pre-existing 50 ms sleep that lets the racing request reach the held lock, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                slot.linked_session_key = "cron:job-1"
            # flake-ok: pre-existing await of a request task the test has already unblocked, moved verbatim; tracked for a de-flake pass
            resp = await task
            assert resp.status == 200
            assert slot.model == _MODEL_B
            probed = {c.args[0] for c in state.sessions.get_provider.call_args_list}
            assert probed == {"cron:job-1"}
            assert state.sessions.reset.await_args.args[0] == "cron:job-1"

    @pytest.mark.asyncio
    async def test_rebind_during_live_switch_rolls_back_and_answers_409(self):
        # A binding that lands DURING _try_live_model_switch's provider RPC
        # (after the key was resolved) means whatever set_model did landed on
        # a session the slot does not run on: commit nothing, reset nothing,
        # 409 so the retry resolves the current binding.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        provider.has_active_turn.return_value = False
        provider.client = MagicMock()

        async def _set_model_and_rebind(_wire):
            slot.linked_session_key = "cron:job-1"

        provider.client.set_model = AsyncMock(side_effect=_set_model_and_rebind)
        provider.supports_effort = MagicMock(return_value=False)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebind_during_reset_rolls_back_the_model_switch(self):
        # The same check after the reset await: the session torn down is no
        # longer the slot's, so the commit is rolled back and the caller
        # retries against the current binding.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)

        async def _reset_and_rebind(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            return False

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.model == _MODEL_A

    @pytest.mark.asyncio
    async def test_rebind_during_reset_lands_bulk_slot_in_skipped_running(self):
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)

        async def _reset_and_rebind(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A

    @pytest.mark.asyncio
    async def test_rebind_during_reset_rolls_back_the_workspace_switch(self):
        slot = _ChatSlot("test")
        prior_ws, prior_project = slot.workspace, slot.project
        state = _mock_state(slot, provider=None)

        async def _reset_and_rebind(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "ws2"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert (slot.workspace, slot.project) == (prior_ws, prior_project)

    @pytest.mark.asyncio
    async def test_agent_switch_resets_the_linked_session(self, monkeypatch):
        # The reset targets the linked session; the transcript-metadata write
        # stays HISTORY-keyed — it names the .jsonl the restart scan reads,
        # not the live session (the _cancel_target history-vs-session split).
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            assert slot.agent == "new-agent"
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"
            meta_call = state.conversation_log.update_metadata.call_args
            assert meta_call.args[0] == "dashboard:test"

    @pytest.mark.asyncio
    async def test_agent_switch_sees_the_linked_sessions_active_turn(self):
        # The busy probe lands on the live linked session: an in-flight
        # channel turn answers 409 instead of tearing the turn (or a
        # captured-identity session) down.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.linked_session_key = "slack:123.456"
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = True
        state = _mock_state(slot, provider=None)
        state.sessions.get_provider = MagicMock(
            side_effect=lambda key: provider if key == "slack:123.456" else None
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.agent == "old-agent"
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_caller_cannot_switch_a_linked_sessions_agent(self):
        # Owning the slot is not owning the channel session it is bound to:
        # denied as an indistinguishable 404, nothing mutated.
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 404
            assert slot.agent == "old-agent"
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebind_during_reset_rolls_back_the_agent_switch(self, monkeypatch):
        # The session torn down is not the slot's: the commit — the one
        # case the agent handler's no-rollback rule unwinds — is rolled back,
        # the metadata write never runs, and the caller retries against the
        # current binding.
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        async def _reset_and_rebind(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.agent == "old-agent"
            state.conversation_log.update_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebind_during_reset_restores_the_fallback_walk_it_reset(self, monkeypatch):
        # An agent switch resets the provider-scoped fallback walk (the pin
        # itself is kept). This is the one rollback path, and it must bring the
        # walk back with the agent: a 409 saying nothing changed cannot leave
        # the slot with a walk cleared for an agent it did not switch to.

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.model = _MODEL_A
        slot._fallback_candidate_idx = 2
        slot._fallback_walked = ["fallback-a"]
        slot._active_fallback_model = "fallback-a"
        slot._fallback_primary_model = _MODEL_A
        slot._fallback_slot_model = _MODEL_A
        slot._fallback_pick_gen = 3
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        seen_at_reset: dict[str, list[str]] = {}

        async def _reset_and_rebind(*_a, **_k):
            seen_at_reset["walk"] = list(slot._fallback_walked)
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"

        assert seen_at_reset["walk"] == [], "the commit must reset the walk before the reset"
        assert slot.agent == "old-agent"
        assert slot._fallback_candidate_idx == 2
        assert slot._fallback_walked == ["fallback-a"]
        assert slot._active_fallback_model == "fallback-a"
        assert slot._fallback_primary_model == _MODEL_A
        assert slot._fallback_slot_model == _MODEL_A
        assert slot._fallback_pick_gen == 3
        assert slot.model == _MODEL_A, "the pin is kept by design, rollback or not"

    @pytest.mark.asyncio
    async def test_effort_switch_probes_and_resets_the_linked_session(self):
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            assert resp.status == 200
            assert slot.reasoning_effort == "high"
            probed = {c.args[0] for c in state.sessions.get_provider.call_args_list}
            assert probed == {"slack:123.456"}
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"

    @pytest.mark.asyncio
    async def test_effort_switch_defers_on_the_linked_sessions_active_turn(self):
        # The live-effort probe lands on the linked session, so its active
        # turn takes the defer branch (commit now, live push next turn)
        # instead of a reset against a session that never existed.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:123.456"
        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort.return_value = True
        provider.has_active_turn.return_value = True
        state = _mock_state(slot, provider=None)
        state.sessions.get_provider = MagicMock(
            side_effect=lambda key: provider if key == "slack:123.456" else None
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["deferred"] is True
            assert slot.reasoning_effort == "high"
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_caller_cannot_switch_a_linked_sessions_effort(self):
        slot = _ChatSlot("test")
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            assert resp.status == 404
            assert slot.reasoning_effort == ""
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebind_during_reset_rolls_back_the_effort_switch(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)

        async def _reset_and_rebind(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_and_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_project_set_defers_the_reset_under_the_linked_session_key(self, tmp_path):
        # The deferred-reset flag carries the linked key (the key the app
        # gate authorized), so the chat_runner consumer tears down the
        # session the slot actually runs on — not dashboard:<slot>.
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        slot.linked_session_key = "slack:123.456"
        # A live session under the linked key — the reset arms only for a
        # session that can carry the old CWD (the arm guard), and this
        # test's whole point is which KEY that teardown targets.
        state = _mock_state(slot, provider=object())
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": new_dir})
            assert resp.status == 200
            assert slot.project == new_dir
            assert slot._pending_reset_history_key == "slack:123.456"

    @pytest.mark.asyncio
    async def test_app_caller_cannot_set_a_linked_sessions_project(self, tmp_path):
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot)
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": new_dir})
            assert resp.status == 404
            assert slot.project == "/workspace/old-ws"
            assert not slot._pending_reset_history_key

    @pytest.mark.asyncio
    async def test_agent_reset_declined_busy_rolls_back_and_answers_409(self, monkeypatch):
        # A turn can start after the last-instant re-check (message dispatch
        # does not take slot._lock): the reset runs with skip_if_busy=True and
        # its atomic decline is authoritative — the committed agent is rolled
        # back, the metadata write never runs, and the in-flight turn survives.
        from kiro_crew.providers.base import LLMProvider

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot)
        state.conversation_log = MagicMock()
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit check and the last-instant re-check (so the
        # handler proceeds into the reset), mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, False, True]
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.agent == "old-agent"
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}
            state.conversation_log.update_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_effort_reset_declined_busy_rolls_back_and_answers_409(self):
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        state = _mock_state(slot)
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-reset re-check, mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, True]
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.reasoning_effort == ""
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}

    @pytest.mark.asyncio
    async def test_rebind_during_metadata_persist_rolls_back_and_answers_409(self, monkeypatch):
        # The metadata write awaits AFTER the post-reset rebound guard, so a
        # binding landing there must be caught by a second re-validation —
        # otherwise the switch answers 200 while the linked session keeps the
        # old agent. The rollback also restores the transcript metadata the
        # write just persisted, so the 409's "nothing changed" is true.
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.agent_kind = "member"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        log = MagicMock()

        def _persist_and_rebind(_key, _meta):
            if not slot.linked_session_key:
                slot.linked_session_key = "cron:job-1"

        log.update_metadata = MagicMock(side_effect=_persist_and_rebind)
        state.conversation_log = log
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.agent == "old-agent"
            assert slot.agent_kind == "member"
            # The restore wrote the rolled-back agent back into the
            # transcript metadata (last call).
            assert log.update_metadata.call_args.args[1] == {
                "agent": "old-agent",
                "agent_kind": "member",
            }

    @pytest.mark.asyncio
    async def test_agent_and_kind_are_not_published_separately(self, monkeypatch):
        """A sender crossing resolution observes one complete selection."""
        entered_resolution = asyncio.Event()
        release_resolution = asyncio.Event()

        async def _blocked_warm(*_args, **_kwargs):
            entered_resolution.set()
            # flake-ok: pre-existing unbounded wait inside the stub until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_resolution.wait()

        cfg = MagicMock()
        cfg.agents = {}
        cfg.memory_stores = {}
        cfg.default_workspace = "default"
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", _blocked_warm
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda *_args, **_kwargs: MagicMock(
                workspace_dir="",
                memory_store_name="",
                selection_kind="template",
                requested_resolved=True,
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._record_explicit_agent_selection",
            AsyncMock(return_value=None),
        )
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.agent_kind = "member"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = None

        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            request = asyncio.create_task(
                client.post(
                    "/api/chat/slots/test/agent",
                    json={"agent": "new-agent", "agent_kind": "template"},
                )
            )
            # Lost-run bounds only: before the warm-up the handler does real
            # off-loop session-record reads, which a Windows runner can stretch
            # past any fixed second.
            await await_until(entered_resolution, what="the agent resolution warm-up")
            observed = (slot.agent, slot.agent_kind)
            release_resolution.set()
            response = await await_until(request, what="the agent switch response")

        assert response.status == 200, await response.text()
        assert observed == ("old-agent", "member")
        assert (slot.agent, slot.agent_kind) == ("new-agent", "template")

    @pytest.mark.asyncio
    async def test_concurrent_same_agent_write_survives_the_rollback(self, monkeypatch):
        # An unlocked writer (openai_compat / members / in-turn directive)
        # can write the SAME agent name during this handler's reset await and
        # dispatch on it. The rollback is gated on the write GENERATION, not
        # the value, so that concurrent write takes ownership and the
        # rollback stands down — a value compare-and-set would restore the
        # old agent over a dispatch already running the new one.
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        async def _reset_concurrent_write_and_rebind(*_a, **_k):
            # The concurrent same-value write, then the rebind that forces
            # this request onto its rollback path.
            slot.agent = "new-agent"
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_concurrent_write_and_rebind)
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            # The concurrent writer's value survives; only OUR commit unwinds.
            assert slot.agent == "new-agent"
            assert slot.agent_kind == ""

    @pytest.mark.asyncio
    async def test_concurrent_same_project_write_survives_the_rollback(self, monkeypatch):
        # The in-turn set_project directive can write the VERY project this
        # handler derived, during the reset await. The rollback is gated on
        # each field's commit-token identity, so that successful concurrent
        # write survives — a value compare-and-set would erase it back to the
        # pre-switch project.
        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: MagicMock(
                workspace_dir="/tmp/ws2",
                memory_store_name="",
                kiro_agent=name,
                selection_kind="template",
                resolved_alias="",
                requested_resolved=True,
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "ws2",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/derived",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names",
            lambda p: frozenset(),
        )
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        async def _reset_concurrent_project_write_and_rebind(*_a, **_k):
            # The concurrent same-value write (a plain str, new identity),
            # then the rebind that forces this request onto its rollback.
            slot.project = "/workspace/derived"
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset_concurrent_project_write_and_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            # The concurrent writer's project survives (a value CAS would
            # have restored /workspace/old-ws); our own agent commit unwinds.
            assert slot.project == "/workspace/derived"
            assert slot.agent == "old-agent"

    @pytest.mark.asyncio
    async def test_rebind_during_project_save_rolls_back_and_answers_409(
        self, tmp_path, monkeypatch
    ):
        # A binding that lands while the recent-project save awaits means the
        # deferred-reset flag would name a session the slot does not run on
        # (and the flag's consumer would tear down a session nobody is on
        # while the actual session keeps the old CWD): the commit is rolled
        # back, the flag stays unarmed, and the caller retries against the
        # current binding.
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)

        def _save_and_rebind(_project):
            slot.linked_session_key = "cron:job-1"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._save_recent_project", _save_and_rebind
        )
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": new_dir})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.project == "/workspace/old-ws"
            assert not slot._pending_reset_history_key

    @pytest.mark.asyncio
    async def test_app_caller_project_denied_before_path_probing(self):
        # The isdir/sensitive-path probes answer differently for existing vs
        # missing paths: an app caller that owns a linked slot must get the
        # indistinguishable 404 BEFORE any filesystem check, or the endpoint
        # is a filesystem existence oracle for unauthorized callers (a
        # missing path would leak as the probe's 400 instead).
        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        slot._app = "demo-app"
        slot.linked_session_key = "slack:123.456"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app_as(state, "demo-app"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project",
                json={"project": "/definitely/not/a/real/dir-8415"},
            )
            assert resp.status == 404
            assert slot.project == "/workspace/old-ws"

    @pytest.mark.asyncio
    async def test_rebind_during_live_effort_push_commits_with_warning(self):
        # change_effort persisted the per-model override before the rebind
        # was observable, so a 409 would claim a rollback that did not
        # happen: the slot value is committed (it is what the new binding's
        # next cold start reads) and the rebind is reported as a warning.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)
        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort.return_value = True
        provider.has_active_turn.return_value = False

        async def _push_and_rebind(_effort):
            slot.linked_session_key = "cron:job-1"
            return True

        provider.change_effort = AsyncMock(side_effect=_push_and_rebind)
        state.sessions.get_provider = MagicMock(return_value=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert "rebound" in data["warning"]
            assert slot.reasoning_effort == "high"
            state.sessions.reset.assert_not_awaited()
