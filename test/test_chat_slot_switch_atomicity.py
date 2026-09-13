"""Concurrency tests for the slot model/workspace switch handlers.

The agent and effort switch handlers serialize their mutate-then-reset
sections under ``slot._lock``; the model and workspace handlers ran the same
shape unlocked, so two racing switches could each commit and reset against
the other's half-applied state, and a mid-turn model switch fell through to
the reset fallback and tore down the in-flight turn for any programmatic
caller. These tests pin the lock serialization, the in-lock re-checks, and
the mid-turn 409 (clones of the concurrency template in
``test_chat_slot_reasoning_effort.py``).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.sections import ResolvedBindings
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
from kiro_crew.dashboard.chat_handlers import _slot_switch_session_lock
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

MOD = "kiro_crew.dashboard.chat_handlers"

# Valid registry aliases the model guard accepts (tests are exempt from the
# hardcoded-model-literal gate; these mirror the ids the existing model-switch
# tests use).
_MODEL_A = "claude-opus-4.8"
_MODEL_B = "gpt-5.6-sol"


def _bindings_for(**overrides) -> ResolvedBindings:
    """A real `ResolvedBindings`, so a field added to it arrives with its default here.

    The handler reads several of these fields inside its own ``try``, where a missing attribute
    is swallowed -- the commit compare-and-set is then skipped and the arm carries the project
    the switch was leaving, which reads as a behaviour regression rather than a stale stub.
    """
    return ResolvedBindings(
        **{
            "workspace_dir": Path("/ws"),
            "memory_store_name": DEFAULT_MEMORY_STORE,
            "effective_memory_config": {},
            "kiro_agent": "ka",
            **overrides,
        }
    )


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: token_auth middleware sets request["app"] on every
    # authenticated path ("" = dashboard user); the bulk handler fails closed
    # without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _boom_cfg():
    raise RuntimeError("config unreadable")


def _mock_state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_context_usage = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # Async because it resolves a cleared project off-thread; a plain MagicMock returns a
    # non-awaitable here and the handler answers 500 instead of exercising the switch.
    state.sessions.note_project_change = AsyncMock()
    # Resolve-only helper: async, and it must hand back a real path string because the arm
    # sites record `slot.project or <this>`.
    state.sessions.resolve_arm_cwd = AsyncMock(
        side_effect=lambda key, cwd: cwd or "/workspace/_default"
    )
    # No live AcpProvider by default → the model handler takes the reset path.
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


@pytest.fixture
def private_switch_state():
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    for name in ("writer", "reviewer"):
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, name)
    cfg.save()
    slot = _ChatSlot("private-switch")
    slot.agent = "writer"
    slot.memory_store = cfg.agents["writer"].memory_store
    slot.workspace = "original-workspace"
    slot.project = "original-project"
    state = _mock_state(slot)
    state.sessions.reset.return_value = True
    state.conversation_log = ConversationLog()
    key = effective_session_key(slot)
    state.conversation_log.update_metadata(
        key, {"agent": slot.agent, "memory_store": slot.memory_store}
    )
    bind_private_session_store(key, slot.memory_store)
    return state, slot, key


class TestPrivateChatMemberSwitch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["reviewer", "default", ""])
    async def test_cross_member_switch_refuses_before_any_mutation(
        self, private_switch_state, target
    ):
        from kiro_crew.member_memory_auth import read_private_session_store

        state, slot, key = private_switch_state
        before = (slot.agent, slot.memory_store, slot.workspace, slot.project)
        metadata = await asyncio.to_thread(state.conversation_log.get_metadata, key)
        async with TestClient(TestServer(_make_app(state))) as client:
            # A retry cannot gradually mutate the slot or erase the permanent pin.
            for _ in range(2):
                response = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": target}
                )
                assert response.status == 409
                result = await response.json()
                assert result["code"] == "private_memory_session_pinned"
                assert "Start a new conversation" in result["error"]
        assert (slot.agent, slot.memory_store, slot.workspace, slot.project) == before
        assert await asyncio.to_thread(state.conversation_log.get_metadata, key) == metadata
        assert await asyncio.to_thread(read_private_session_store, key) == before[1]
        state.sessions.reset.assert_not_awaited()
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_linked_session_pin_is_checked_instead_of_the_slot_key(
        self, private_switch_state
    ):
        state, slot, key = private_switch_state
        slot.linked_session_key = key
        slot.key = "linked-alias"
        state._slots = {slot.key: slot}
        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}
            )
            assert response.status == 409
            assert (await response.json())["code"] == "private_memory_session_pinned"
        assert slot.agent == "writer"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_member_reset_stays_available(self, private_switch_state):
        state, slot, key = private_switch_state
        with patch(f"{MOD}.warm_project_agent_names", new_callable=AsyncMock):
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": "writer"}
                )
                assert response.status == 200
        state.sessions.reset.assert_awaited_once()
        assert state.sessions.reset.await_args.args[0] == key
        assert slot.agent == "writer"

    @pytest.mark.asyncio
    async def test_unreadable_pin_refuses_without_reset(self, private_switch_state):
        state, slot, _ = private_switch_state
        with patch(
            "kiro_crew.member_memory_auth.read_private_session_store",
            side_effect=ValueError("invalid binding"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}
                )
                assert response.status == 503
                assert (await response.json())["code"] == "private_memory_binding_unavailable"
        assert slot.agent == "writer"
        state.sessions.reset.assert_not_awaited()


class TestSlotAgentSwitchArmOrdering:
    @pytest.mark.asyncio
    async def test_new_agent_is_not_visible_before_the_arm_is_raised(self, monkeypatch):
        """A claim landing mid-resolve must not see the new agent with no arm.

        The agent switch resolves bindings behind an await. If the slot publishes the new
        agent before that await, a cwd-less channel claim acquiring in the window reads
        the new agent off the slot while no retirement arm has been recorded yet, so it
        reuses the OLD session and runs the turn under the stale agent and project.
        """
        slot = _ChatSlot(key="chat-1", agent="old-agent")
        slot.project = ""
        state = _mock_state(slot)
        armed: list = []
        state.sessions.mark_retire_on_next_claim = MagicMock(
            side_effect=lambda key, cwd, agent=None: armed.append(agent)
        )

        observed: dict = {}

        async def _warm_and_observe(project, **kwargs):
            # This is the suspension point. Whatever a competing claim could read, it
            # reads HERE.
            observed["agent"] = str(slot.agent)
            observed["armed"] = list(armed)

        monkeypatch.setattr(
            chat_handlers, "warm_project_agent_names", _warm_and_observe, raising=False
        )

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/chat/slots/chat-1/agent", json={"agent": "new-agent"})

        assert observed, "the resolve never ran, so the window was never observed"
        assert not (observed["agent"] == "new-agent" and not observed["armed"]), (
            "the switch published the new agent while the arm was still unraised, so a "
            "claim in this window reuses the old session under the new agent"
        )
        await asyncio.sleep(0)


class TestSlotModelSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_mid_turn_switch_answers_409_without_reset(self):
        # _try_live_model_switch declines a mid-turn live switch, and the old
        # unlocked handler then fell through to the reset — tearing down the
        # in-flight turn mid-stream. The handler must answer busy instead:
        # no live switch, no reset, slot model untouched.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = True
        provider.client = MagicMock()
        provider.client.set_model = AsyncMock()
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            provider.client.set_model.assert_not_awaited()
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_start_turn_answers_409_via_slot_running(self):
        # A first message can be INSIDE the multi-second provider.start() when
        # the switch arrives: no session is registered yet, so the provider
        # pre-check sees nothing — but slot.running is set at dispatch, so the
        # handler still answers 409 instead of committing a model the
        # cold-starting session did not capture.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        running_task = MagicMock()
        running_task.done.return_value = False
        slot.task = running_task
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_starting_during_live_switch_answers_409_before_reset(self):
        # _try_live_model_switch's provider RPCs take seconds; a send can start
        # (and post an ask_question card) in that window. _reset_slot_session
        # clears pending waits BEFORE its atomic decline, so entering it busy
        # would falsely reject that turn's cards even though the reset itself
        # declines. The handler re-checks busyness in a no-await window
        # immediately before the reset: busy → rollback + 409, reset NEVER
        # entered.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        provider.has_active_turn.return_value = False
        provider.client = MagicMock()

        running_task = MagicMock()
        running_task.done.return_value = False

        async def _set_model_starts_a_send(*args, **kwargs):
            # A send dispatches while the live switch's RPC is in flight.
            slot.task = running_task
            raise RuntimeError("wire hiccup")  # live switch fails -> reset path

        provider.client.set_model = AsyncMock(side_effect=_set_model_starts_a_send)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            # The invariant under test: the reset (and its pending-wait
            # clearing) is never entered while the slot is busy.
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_turn_selecting_its_own_agent_mid_switch_is_not_overwritten(self):
        """The agent commit joins its siblings' compare-and-set instead of clobbering.

        `slot.workspace` and `slot.project` commit only when they still hold the value read
        before the awaits. The agent did not: it committed unconditionally, so a turn starting
        inside those awaits -- which selects its own agent -- had that pick erased by the
        commit, and erased again by the rollback. There is no recovery once overwritten.
        """
        slot = _ChatSlot("test")
        slot.agent = "agent-before"
        state = _mock_state(slot)

        async def _turn_picks_its_own_agent(*args, **kwargs):
            # A turn dispatches while the arm's directory is being resolved and binds the
            # agent it was started with, writing without this handler's lock.
            slot.agent = "agent-the-turn-picked"
            return ""

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_turn_picks_its_own_agent)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "agent-asked"})
            body = await resp.text()

        assert resp.status == 409, f"got {resp.status}: {body[:220]}"
        assert '"code": "turn_in_flight"' in body or '"code":"turn_in_flight"' in body, (
            "the refusal must be the concurrent-pick one, not another 409 that would make this "
            f"control vacuous: {body[:220]}"
        )
        assert slot.agent == "agent-the-turn-picked", (
            "the switch overwrote an agent the running turn selected, and no rollback can "
            f"recover it: {slot.agent!r}"
        )

    @pytest.mark.asyncio
    async def test_a_project_set_mid_clear_is_not_overwritten(self, tmp_path):
        """The project commit re-reads the value it resolved against.

        `api_chat_slot_project` resolves the arm's directory through an await, then committed
        unconditionally. An in-turn `set_project` directive writes without this lock, so a
        change made inside that window was overwritten by a commit computed from the project
        it replaced.
        """
        before = tmp_path / "before"
        asked = tmp_path / "asked"
        by_turn = tmp_path / "by-turn"
        for d in (before, asked, by_turn):
            d.mkdir()
        slot = _ChatSlot("test")
        slot.project = str(before)
        state = _mock_state(slot)

        async def _directive_sets_a_new_project(*args, **kwargs):
            slot.project = str(by_turn)
            return ""

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_directive_sets_a_new_project)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": str(asked)})
            body = await resp.text()

        assert resp.status == 409, f"got {resp.status}: {body[:200]}"
        expected = str(by_turn)
        assert (
            slot.project == expected
        ), f"the clear overwrote a project the running turn set: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_turn_on_target_model_during_live_switch_succeeds(self):
        # The counterpart to the 409 above: set_model LANDED, then the effort
        # reapply failed as a turn started. The pre-reset busy re-check sees
        # the turn, but the live session already serves the target — rolling
        # back would publish the old model while the turn streams under the
        # new one. Success, no rollback, reset never entered.
        from kiro_crew import model_registry
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.reasoning_effort = "high"
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        # Pre-check idle, _try_live_model_switch's own check idle (so
        # set_model runs), then the pre-reset re-check sees the raced turn.
        provider.has_active_turn.side_effect = [False, False, True]
        provider.served_model = model_registry.to_acp_id(_MODEL_B)
        provider.client = MagicMock()
        provider.client.set_model = AsyncMock()
        provider.supports_effort = MagicMock(return_value=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("turn raced the push"))
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot.model == _MODEL_B
            provider.client.set_model.assert_awaited_once()
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_on_auto_during_live_switch_succeeds_via_raw_served_model(self):
        # Same chain as above with Auto as the target. AcpProvider.served_model
        # collapses the "auto" sentinel to "" (the fallback canary's
        # invariant), so the filtered read can never equal the "auto" wire id
        # — the handler must read the session client's raw served id instead,
        # or a landed switch to Auto rolls back to the old model while the
        # live session runs Auto.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.reasoning_effort = "high"
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        provider.available_models = MagicMock(return_value=[{"modelId": "auto"}])
        provider.has_active_turn.side_effect = [False, False, True]
        provider.served_model = ""  # filtered: "auto" -> ""
        provider.client = MagicMock()
        provider.client.served_model = "auto"  # raw, unfiltered
        provider.client.set_model = AsyncMock()
        provider.supports_effort = MagicMock(return_value=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("turn raced the push"))
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": ""})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot.model == ""
            provider.client.set_model.assert_awaited_once_with("auto")
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_on_other_model_during_auto_switch_still_fails_closed(self):
        # The raw read is scoped to the Auto wire id only: when the raw served
        # id is something else, the switch to Auto did not land and the
        # fail-closed rollback + 409 stands.
        from kiro_crew import model_registry
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.reasoning_effort = "high"
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        provider.available_models = MagicMock(return_value=[{"modelId": "auto"}])
        provider.has_active_turn.side_effect = [False, False, True]
        provider.served_model = model_registry.to_acp_id(_MODEL_A)
        provider.client = MagicMock()
        provider.client.served_model = model_registry.to_acp_id(_MODEL_A)
        provider.client.set_model = AsyncMock(side_effect=RuntimeError("set_model failed"))
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": ""})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_declined_busy_rolls_back_and_answers_409(self):
        # A turn can start even after the in-lock has_active_turn pre-check
        # (message dispatch does not take slot._lock), so the reset fallback
        # runs with skip_if_busy=True and its atomic decline is
        # authoritative: when the pre-commit session (same provider object)
        # declined and is mid-turn, the committed model is rolled back, the
        # response is the same 409 the pre-check gives, and the in-flight
        # turn survives.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-check AND the last-instant pre-reset re-check (so
        # the handler proceeds into the reset), mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, False, True]
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}

    @pytest.mark.asyncio
    async def test_reset_declined_idle_old_session_retries_once(self):
        # The slipped-in turn can FINISH before the post-decline re-read: the
        # declined reset left a live idle session on the OLD model, and
        # reporting success would leave that stale process alive under the
        # new slot.model. The handler retries the reset once (the reload
        # handler's template for this exact race) and succeeds.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        stale = MagicMock(spec=LLMProvider)
        stale.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=stale)
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 200
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_second_time_fails_closed_to_409(self):
        # An idle live session declined the reset twice (another turn is
        # genuinely racing the retry): the handler must fail closed — roll
        # back the commit and answer 409 — never report success over a live
        # session whose model it cannot prove.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        stale = MagicMock(spec=LLMProvider)
        stale.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=stale)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_post_commit_session_fails_closed(self):
        # No session existed at the pre-check; the decline came from a session
        # registered AFTER the commit. Registration time proves nothing about
        # which model the session captured (dispatch reads slot.model at its
        # call site but registers only after a multi-second provider.start()),
        # so the handler fails CLOSED: rollback + 409, never a silent success
        # over a live session that may be running the old model.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        newborn = MagicMock(spec=LLMProvider)
        newborn.has_active_turn.return_value = True
        # Pre-check, the last-instant pre-reset re-check, and the reset
        # helper's pre-await identity snapshot all see no provider; the
        # post-decline re-read sees the session a slipped-in send registered
        # after the commit.
        state.sessions.get_provider = MagicMock(side_effect=[None, None, None, newborn])
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_live_session_already_on_target_succeeds(self):
        # The partially-applied live switch: set_model landed, the effort
        # reapply failed, and the consistency reset declined because a new
        # turn started. The live session's backend-resolved model already
        # equals the requested wire id, so slot.model is TRUTHFUL — rolling
        # back would report the old model while the turn runs the new one.
        # Success, no rollback, no second reset.
        from kiro_crew import model_registry
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        live = MagicMock(spec=AcpProvider)
        live.is_claude_backend = False
        live.served_model = model_registry.to_acp_id(_MODEL_B)
        live.has_active_turn.return_value = False
        live.client = MagicMock()
        live.client.set_model = AsyncMock()
        # set_model lands, then the effort reapply fails → went_live False →
        # the handler takes the consistency-reset fallback, which declines.
        slot.reasoning_effort = "high"
        live.supports_effort = MagicMock(return_value=True)
        live.change_effort = AsyncMock(side_effect=RuntimeError("effort push failed"))
        state = _mock_state(slot, provider=live)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot.model == _MODEL_B
            # set_model actually landed and the declined reset was accepted as
            # final: exactly one reset attempt, no retry, no rollback.
            live.client.set_model.assert_awaited_once()
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_no_live_provider_succeeds(self):
        # A declined reset with NO live registered provider is the legitimate
        # success case: nothing to tear down, the next message cold-starts
        # under the new model. Exactly one reset attempt, no 409.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 200
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_raise_answers_200_with_warning_and_pushes(self):
        # A teardown that RAISES: SessionManager.reset pops the
        # session before its shutdown can fail, so the switch is COMMITTED
        # regardless — the handler must answer 200 with the committed model
        # plus an advisory warning and still push the slots update. The old
        # unwrapped await propagated a 500 that never reached
        # push_slots_update, stranding every connected client on the OLD
        # value while the slot already carried the new one.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("shutdown boom"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["model"] == _MODEL_B
            assert data["warning"] == "old session teardown incomplete"
            assert slot.model == _MODEL_B
            state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_retry_raise_answers_200_with_warning_and_pushes(self):
        # The idle-decline RETRY can raise too: first reset declined
        # (idle live session), the retry's teardown throws. Same
        # committed-switch answer as the first attempt — 200 + warning +
        # slots push, no rollback to the old model.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        stale = MagicMock(spec=LLMProvider)
        stale.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=stale)
        calls = {"n": 0}

        async def _decline_then_pop_and_raise(*_a, **_k):
            if calls["n"] == 0:
                calls["n"] += 1
                return False
            # The retry pops the session BEFORE its shutdown raises, so the
            # helper's post-pop probe sees no registered provider.
            state.sessions.get_provider = MagicMock(return_value=None)
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_decline_then_pop_and_raise)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["model"] == _MODEL_B
            assert data["warning"] == "old session teardown incomplete"
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_count == 2
            state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_raise_before_pop_propagates(self):
        # A raise with the session STILL REGISTERED came before the pop: the
        # old session survives on the old model, so a 200 would be the false
        # success the decline ladders treat as worse than any retryable
        # error. The helper re-raises instead of
        # answering a committed-switch success it cannot vouch for.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        alive = MagicMock(spec=LLMProvider)
        alive.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=alive)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("pre-pop boom"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert resp.status == 500
            state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_raise_with_successor_session_still_succeeds(self):
        # A concurrent send can register a SUCCESSOR session for the same key
        # after the pop and before the old session's shutdown raises (server
        # GPT lane finding on 295817e70): the probe compares instance
        # IDENTITY, so a different registered provider is NOT the unpopped
        # old session — the switch is committed, the successor cold-started
        # from the committed bindings, and the answer is 200 + warning.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        old = MagicMock(spec=LLMProvider)
        old.has_active_turn.return_value = False
        state = _mock_state(slot, provider=old)

        async def _pop_register_successor_and_raise(*_a, **_k):
            successor = MagicMock(spec=LLMProvider)
            successor.has_active_turn.return_value = False
            state.sessions.get_provider = MagicMock(return_value=successor)
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_pop_register_successor_and_raise)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["model"] == _MODEL_B
            assert data["warning"] == "old session teardown incomplete"
            assert slot.model == _MODEL_B
            state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebind_during_raising_reset_rolls_back_to_409(self):
        # The teardown-raise path must NOT bypass the rebind guard (GPT
        # review finding): a slot rebound while the raising
        # reset awaited answers the same rollback + 409 as any other rebind —
        # never a 200 that advertises the committed model over a newly bound
        # session that never saw the switch.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)

        async def _rebind_and_raise(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_rebind_and_raise)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.model == _MODEL_A

    @pytest.mark.asyncio
    async def test_attached_subagents_refuse_the_reset_and_roll_back(self):
        # The reset tears down the runtime attached children run on, so an
        # idle parent with children (running, queued, or mid-delivery) answers
        # the reload handler's 409 instead of discarding their work — and the
        # already-committed model rolls back with its pick generation.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        gen_before = slot._model_pick_gen
        state = _mock_state(slot, provider=None)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-1"]
        state.subagents._queued_depth.return_value = 0
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "slot_subagents_running"
            assert slot.model == _MODEL_A
            assert slot._model_pick_gen == gen_before
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_switch_waits_for_slot_lock(self):
        # The mutate-then-reset section runs under slot._lock, same as the
        # agent/effort handlers: while another actor holds the lock, a model
        # switch must neither commit nor reset.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
                )
                # Let the request reach (and block on) the slot lock.
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_racing_switches_serialize_instead_of_interleaving(self):
        # Two racing switches to DIFFERENT targets: the second must not
        # commit its model while the first's reset await is still in flight
        # (unlocked, it did — each then reset against the other's
        # half-applied session). Serialized, each reset observes exactly the
        # model its own request committed.
        slot = _ChatSlot("test")
        slot.model = ""
        state = _mock_state(slot)

        seen_at_reset: list[str] = []
        first_reset_started = asyncio.Event()
        release_first_reset = asyncio.Event()

        async def _reset(*args, **kwargs):
            seen_at_reset.append(slot.model)
            if len(seen_at_reset) == 1:
                first_reset_started.set()
                await release_first_reset.wait()
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            )
            await first_reset_started.wait()
            second = asyncio.create_task(
                client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            )
            # Let the second request reach (and block on) the slot lock, then
            # release the first request's reset.
            await asyncio.sleep(0.05)
            # The serialization under test: the second switch has NOT
            # committed while the first's reset is still in flight.
            assert slot.model == _MODEL_A
            release_first_reset.set()
            resp1 = await first
            resp2 = await second
            assert resp1.status == 200
            assert resp2.status == 200
            assert seen_at_reset == [_MODEL_A, _MODEL_B]
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_same_target_successor_noops_under_lock(self):
        # Two clients pick the SAME target; the second is queued behind the
        # first's in-flight reset. The no-op check is re-run INSIDE the lock,
        # so the successor observes the predecessor's committed value and
        # answers OK without tearing down the session the predecessor just
        # set up — one reset total.
        slot = _ChatSlot("test")
        slot.model = ""
        state = _mock_state(slot)

        first_reset_started = asyncio.Event()
        release_first_reset = asyncio.Event()
        calls = {"n": 0}

        async def _reset(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                first_reset_started.set()
                await release_first_reset.wait()
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            )
            await first_reset_started.wait()
            second = asyncio.create_task(
                client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            )
            await asyncio.sleep(0.05)
            release_first_reset.set()
            resp1 = await first
            resp2 = await second
            assert resp1.status == 200
            assert resp2.status == 200
            assert slot.model == _MODEL_A
            assert calls["n"] == 1


class TestSlotWorkspaceSwitchAtomicity:
    @pytest.fixture(autouse=True)
    def _stub_project_dir(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: f"/workspace/{ws}",
        )

    @pytest.mark.asyncio
    async def test_switch_waits_for_slot_lock(self):
        # The workspace switch mutates the same workspace/project fields the
        # agent handler compare-and-sets under slot._lock, so it must take
        # the same lock: while another actor holds it, the switch neither
        # mutates nor resets.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
                )
                await asyncio.sleep(0.05)
                assert slot.workspace == "old-ws"
                assert slot.project == "/workspace/old-ws"
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_landing_during_the_lock_wait_still_switches(self):
        # There is no total_messages refusal, so a message that lands
        # while this request waits on the slot lock does not turn the switch
        # into a 409: the switch commits and the session is reset, exactly as
        # it would have with no message in flight.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
                )
                # Let the request reach (and block on) the slot lock, then
                # start the conversation before releasing it.
                await asyncio.sleep(0.05)
                slot.total_messages = 1
            resp = await task
            assert resp.status == 200
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_busy_rolls_back_and_answers_409(self):
        # A first send can slip in between the total_messages guard and the
        # reset (message dispatch does not take slot._lock), so the reset runs
        # with skip_if_busy=True: on an atomic decline the committed
        # workspace/project pair is rolled back, the response is 409, and the
        # slipped-in turn survives.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit check, mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.workspace == "old-ws"
            assert slot.project == "/workspace/old-ws"
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}

    @pytest.mark.asyncio
    async def test_a_failed_resolution_leaves_no_project_armed(self):
        """Nothing may be armed by a switch that answers 503.

        The recording ran BEFORE the resolution that can raise, so an unavailable workspace root
        left the arm naming the project this request then refused to commit -- and the next claim
        followed it into the rejected workspace. The resolve goes first, so a failure arms nothing.
        """
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=OSError("root unreadable"))
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 503
            state.sessions.note_project_change.assert_not_awaited()
            assert slot.workspace == "old-ws"

    @pytest.mark.asyncio
    async def test_minting_a_slot_supersedes_a_previous_occupants_arm(self, tmp_path):
        """The supersede must be wired to slot CREATION, not just available on the manager.

        Closing a tab runs ``remove``, which preserves the arm on purpose, so the arm outlives
        the slot. Nothing else can then drop it: the next occupant of that name is a different
        slot, and an arm naming the old project silently redirects its relative writes. Reuse
        of a live slot returns earlier, which keeps the retry target ``remove`` preserves it
        for.
        """
        state = _make_state(tmp_path / "sessions")
        superseded: list[str] = []
        state.sessions.supersede_arm_for_new_slot = lambda key: superseded.append(key)

        first = state.get_or_create_slot("chat-9")
        assert superseded == ["dashboard:chat-9"], (
            "minting a slot did not supersede the key's arm, so a previous occupant's "
            f"armed project survives into it; calls seen: {superseded}"
        )

        superseded.clear()
        again = state.get_or_create_slot("chat-9")
        assert again is first
        assert superseded == [], (
            "reusing a live slot must NOT drop the arm -- it is still owed to a start the "
            "cleanup evicted, whose own frame carries only the pre-change directory"
        )

    @pytest.mark.asyncio
    async def test_a_project_written_during_the_awaits_is_not_erased(self):
        """The commit must not overwrite a project another writer chose mid-await.

        The arm recording and its cleared-default resolution both run BEFORE the fields
        commit, and `slot.project` has unlocked writers -- the in-turn `_set_project`
        directive among them. An unconditional assignment after those awaits therefore
        discards the project the user selected during the window, with nothing to signal it.
        The commit is compare-and-set, and when it loses the arm is re-pointed at the value
        that survived so the next claim binds what the slot actually carries.
        """
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()

        async def _writer_lands_mid_await(key, cwd):
            # Stands in for the unlocked in-turn writer landing inside this await window.
            slot.project = "/writer/chose-this"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_writer_lands_mid_await)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert slot.project == "/writer/chose-this", (
                "the switch overwrote a project written during its own await window; the "
                f"user's selection is gone with nothing to signal it; got {slot.project!r}"
            )
            armed = [c.args[1] for c in state.sessions.note_project_change.await_args_list]
            assert armed[-1] == "/writer/chose-this", (
                "when the compare-and-set loses, the arm must be re-pointed at the project "
                f"that survived, or the next claim binds a directory the slot left; got {armed}"
            )

    @pytest.mark.asyncio
    async def test_a_rebind_transfers_an_arm_cwd_resolved_for_the_live_key(self, monkeypatch):
        """A followed arm must name the LIVE key's directory, not the abandoned key's.

        With no configured project the arm carries the CLEARED cwd, which is resolved for
        the key read before the awaits. If a channel/cron link reassigns the slot in that
        window, transferring that same cwd arms the live key at a directory no live-key
        provider binds: the live session is evicted and its won-race retry rebinds into a
        scratch dir shared with the abandoned key's own sessions.
        """
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = ""
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        monkeypatch.setattr(chat_handlers, "default_project_dir", lambda ws: "")

        async def _link_lands_mid_await(key, cwd):
            slot.linked_session_key = "slack:live-1"
            return f"/workspace/{key}"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_link_lands_mid_await)
        state.sessions.transfer_retire_arm = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            # A rebind mid-switch answers `session_rebound` by design; the arm still had to
            # follow the slot before that refusal, and this pins WHERE it points.
            assert resp.status == 409
            assert (
                state.sessions.transfer_retire_arm.call_args is not None
            ), "the slot rebound during the resolve but no arm was transferred"
            live_key, armed_cwd = state.sessions.transfer_retire_arm.call_args.args[1:3]
            assert live_key == "slack:live-1"
            assert armed_cwd == "/workspace/slack:live-1", (
                "the arm was transferred to the live key carrying the ABANDONED key's "
                f"directory, so no live-key provider binds it; got {armed_cwd!r}"
            )

    @pytest.mark.asyncio
    async def test_active_turn_is_refused_before_the_commit(self):
        # GPT review finding: the reset path calls
        # _unblock_pending_waits BEFORE SessionManager.reset's atomic busy
        # decline, so a turn parked on a pending approval had that approval
        # rejected and only then got a 409. The model handler's early refusal
        # is copied here: a live turn at the pre-commit check answers 409 with
        # nothing committed, nothing unblocked and no reset attempted.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.return_value = True
        state = _mock_state(slot, provider=busy)
        with patch(f"{MOD}._unblock_pending_waits") as unblock:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/test/workspace", json={"workspace": "new-ws"}
                )
                data = await resp.json()
        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        assert slot.workspace == "old-ws"
        assert slot.project == "/workspace/old-ws"
        unblock.assert_not_called()
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_starting_turn_is_refused_via_slot_running(self):
        # Opus review finding: slot.running is set at dispatch, before
        # the multi-second provider.start() registers a session, so a
        # cold-starting turn is invisible to get_provider. Without this check
        # the switch reported success while that turn ran on the OLD project.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        # `running` is a property over slot.task: a pending future stands in
        # for the dispatched-but-not-yet-registered turn.
        slot.task = asyncio.get_running_loop().create_future()
        state = _mock_state(slot)  # no provider registered yet
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        assert slot.workspace == "old-ws"
        state.sessions.reset.assert_not_awaited()
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_busy_rollback_remarks_the_slot_dirty(self):
        # GPT review finding: the unlocked periodic flush may have
        # written the PROVISIONAL bindings to disk during the reset await, so
        # a 409 rollback must re-mark the slot dirty or the rejected switch
        # survives a restart. Simulate the flush having cleared the flag
        # mid-transaction.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        slot.total_messages = 3
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit check, mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)

        async def _flush_then_decline(*_a, **_k):
            slot._dirty = False  # the periodic flush ran with the new bindings
            return False

        state.sessions.reset = AsyncMock(side_effect=_flush_then_decline)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 409
            assert slot.workspace == "old-ws"
            assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_rollback_spares_a_concurrent_project_write(self):
        # Opus review finding: slot.project has lock-free writers (the
        # in-turn set_project directive) that can land during the reset await.
        # The rollback is identity-scoped -- it unwinds only THIS request's
        # commit token -- so a concurrent write of a different project stands
        # while the workspace (untouched by that writer) is rolled back.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit check, mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)

        async def _concurrent_set_project(*_a, **_k):
            slot.project = "/elsewhere/picked-by-turn"
            return False

        state.sessions.reset = AsyncMock(side_effect=_concurrent_set_project)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 409
            assert slot.workspace == "old-ws"
            assert slot.project == "/elsewhere/picked-by-turn"

    @pytest.mark.asyncio
    async def test_rollback_unwinds_a_same_text_own_commit(self):
        # The identity test distinguishes this handler's own token from a
        # concurrent write of the SAME text: with no concurrent writer, the
        # committed project (same text as the token) IS rolled back.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit check, mid-turn at the post-decline
        # re-read: the turn slipped into the reset's own entry window.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 409
            assert slot.project == "/workspace/old-ws"

    @pytest.mark.asyncio
    async def test_a_rejected_switch_does_not_erase_a_project_written_mid_await(self):
        """The ROLLBACK must not clobber the writer either -- same class as the commit.

        A compare-and-set commit that loses leaves the slot carrying the writer's project,
        so restoring `prior_project` on the refusal path erases exactly what the CAS just
        protected. Both fields unwind only while this request still owns them.
        """
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit refusal, mid-turn at the post-decline re-read: the turn
        # slipped in during the reset, which is the only path that reaches the rollback.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        state.conversation_log = MagicMock()

        async def _writer_lands_mid_await(key, cwd):
            slot.project = "/writer/chose-this"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_writer_lands_mid_await)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 409
            assert slot.project == "/writer/chose-this", (
                "the refusal rolled back over a project this request never committed; got "
                f"{slot.project!r}"
            )

    @pytest.mark.asyncio
    async def test_a_rejected_switch_repoints_the_arm_with_an_already_resolved_path(self):
        """The rollback must not be the thing that resolves a cleared directory.

        With an EMPTY prior project the re-point passed the cleared sentinel, and the recorder
        resolves that off-thread through the workspace root's stat/realpath -- which raises on
        an unavailable root. On a rollback path that raise escapes as a 500 AFTER the slot
        fields were restored, while the retirement arm still names the REJECTED project, so the
        next claim binds the workspace this request just refused. Resolving in the guarded
        pre-commit window instead means every rollback re-points with a concrete directory.
        """
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        # The cleared case: this is what made the rollback resolve, and re-raise, on the
        # one path that cannot afford to. The MARKER is what distinguishes it from a slot
        # that was never scoped, which states no directory and so arms none.
        slot.project = ""
        slot.project_cleared = True
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit refusal, mid-turn at the post-decline re-read: only the
        # slipped-in turn reaches the rollback where the re-point happens.
        busy.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.workspace == "old-ws"
            repointed = state.sessions.note_project_change.await_args_list[-1].args[1]
            assert repointed == "/workspace/_default", (
                "the rollback must re-point with a directory already resolved before the "
                f"commit; passing the cleared sentinel resolves on this path and can raise a "
                f"500 with the rejected project still armed; got {repointed!r}"
            )

    @pytest.mark.asyncio
    async def test_a_failed_arm_record_leaves_the_workspace_switch_uncommitted(self):
        """The resolve can raise, so it must run BEFORE the fields move, not after.

        Resolving a cleared default goes through `workspace_root()`, whose mkdir raises on an
        unavailable root. Resolving after the commit makes that a 500 with the new workspace
        already on the slot -- a half-applied switch the caller cannot see or undo. Ordering
        every resolve first makes the failure a clean, retryable 503.

        The record itself cannot raise: it is handed an already-resolved cwd, which is what
        lets it publish synchronously after the commit without reopening the window where a
        claim reads an arm the compare-and-set has not finalized.
        """
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=OSError("root unreadable"))
        recorded: list[str] = []
        state.sessions.note_project_change = AsyncMock(
            side_effect=lambda key, cwd: recorded.append(cwd)
        )
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 503
            assert data["code"] == "workspace_unavailable"
            assert slot.workspace == "old-ws"
            assert slot.project == "/workspace/old-ws"
            state.sessions.reset.assert_not_awaited()
        assert (
            not recorded
        ), f"a project change was recorded behind the 503, arming a rejected switch; {recorded}"

    @pytest.mark.asyncio
    async def test_reset_declined_idle_session_retries_once(self):
        # An idle live session declined the first reset (a slipped-in first
        # send finished before the re-read): the handler retries once and
        # succeeds, so the stale process never survives under the new
        # bindings.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        idle = MagicMock(spec=LLMProvider)
        idle.has_active_turn.return_value = False
        state = _mock_state(slot, provider=idle)
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_live_session_on_new_bindings_succeeds(self):
        # A first send slipped in AFTER the commit, captured the committed new
        # project, and its session declined the reset. The live session's
        # actual cwd equals the committed project, so slot state is TRUTHFUL —
        # rolling back would advertise the old workspace while the live
        # process runs the new one. Success, no rollback, no teardown.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        live = MagicMock(spec=AcpProvider)
        live.cwd = "/workspace/new-ws"
        live.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=live)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_post_commit_session_fails_closed(self):
        # Pre-check era saw no session; the decline came from a session
        # registered after the commit with a turn in flight. Fail closed:
        # both fields rolled back, 409.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        newborn = MagicMock(spec=LLMProvider)
        newborn.has_active_turn.return_value = True
        state = _mock_state(slot)
        # Pre-commit check and the reset helper's identity snapshot see no
        # provider; the post-decline re-read sees the session a slipped-in
        # send registered after the commit.
        state.sessions.get_provider = MagicMock(side_effect=[None, None, newborn])
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.workspace == "old-ws"
            assert slot.project == "/workspace/old-ws"
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_second_time_fails_closed_to_409(self):
        # Two declined resets from an idle live session: exactly two
        # attempts, both fields rolled back, 409 — never success over a live
        # session whose bindings cannot be proven.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        idle = MagicMock(spec=LLMProvider)
        idle.has_active_turn.return_value = False
        state = _mock_state(slot, provider=idle)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "turn_in_flight"
            assert slot.workspace == "old-ws"
            assert slot.project == "/workspace/old-ws"
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_no_live_provider_succeeds(self):
        # A declined reset with NO live registered provider is the legitimate
        # success case: nothing to tear down, the next message cold-starts
        # under the new bindings. Exactly one reset attempt.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_bindings_visible_during_reset(self):
        # Commit-before-reset ordering per the agent-handler template: a send
        # landing while the reset await is in flight cold-starts a session
        # from the slot's CURRENT bindings, so the new workspace/project pair
        # must already be committed when the reset runs.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        seen_during_reset: list[tuple[str, str]] = []

        async def _observe(*args, **kwargs):
            seen_during_reset.append((slot.workspace, slot.project))
            return True

        state.sessions.reset = AsyncMock(side_effect=_observe)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert seen_during_reset == [("new-ws", "/workspace/new-ws")]

    @pytest.mark.asyncio
    async def test_reset_raise_answers_200_with_warning_and_pushes(self):
        # A teardown that RAISES: the workspace/project pair is
        # committed before the reset and SessionManager.reset pops the
        # session before its shutdown can fail, so the handler must answer
        # 200 with the committed workspace plus an advisory warning and still
        # push the slots update — never a 500 that strands clients on the old
        # bindings.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("shutdown boom"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["workspace"] == "new-ws"
            assert data["warning"] == "old session teardown incomplete"
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_retry_raise_answers_200_with_warning_and_pushes(self):
        # The idle-decline RETRY can raise too: first reset declined
        # (idle live session), the retry's teardown throws. Same
        # committed-switch answer — 200 + warning + slots push, no rollback
        # to the old bindings.
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        idle = MagicMock(spec=LLMProvider)
        idle.has_active_turn.return_value = False
        state = _mock_state(slot, provider=idle)
        calls = {"n": 0}

        async def _decline_then_pop_and_raise(*_a, **_k):
            if calls["n"] == 0:
                calls["n"] += 1
                return False
            # The retry pops the session BEFORE its shutdown raises, so the
            # helper's post-pop probe sees no registered provider.
            state.sessions.get_provider = MagicMock(return_value=None)
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_decline_then_pop_and_raise)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["workspace"] == "new-ws"
            assert data["warning"] == "old session teardown incomplete"
            assert slot.workspace == "new-ws"
            assert slot.project == "/workspace/new-ws"
            assert state.sessions.reset.await_count == 2
            state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebind_during_raising_reset_rolls_back_to_409(self):
        # The teardown-raise path must NOT bypass the rebind guard (GPT
        # review finding): a slot rebound while the raising
        # reset awaited answers the same rollback + 409 as any other rebind.
        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot, provider=None)

        async def _rebind_and_raise(*_a, **_k):
            slot.linked_session_key = "cron:job-1"
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_rebind_and_raise)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert (slot.workspace, slot.project) == ("old-ws", "/workspace/old-ws")


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
    async def test_a_switch_onto_an_empty_project_records_the_clear(self):
        """A workspace whose project dir resolves to "" must leave the slot CLEARED, not unset.

        `default_project_dir` answers "" for a missing or sensitive root. Committing that empty
        project without recording the clear makes the slot indistinguishable from one that never
        had a project, so its next claim states no cwd, keeps the stored-cwd resume override, and
        binds the directory the switch was meant to leave.
        """
        from kiro_crew.config.paths import CWD_CLEARED

        slot = _ChatSlot("test")
        slot.project = "/workspace/before"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)

        with patch.object(chat_handlers, "default_project_dir", return_value=""):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post("/api/chat/slots/test/workspace", json={"workspace": "other"})

        assert slot.project == ""
        assert slot.claim_cwd == CWD_CLEARED, (
            "the switched slot states no cwd, so a claim keeps the resume override and binds "
            f"the previous project; claim_cwd={slot.claim_cwd!r}"
        )

    @pytest.mark.asyncio
    async def test_a_concurrent_clear_does_not_arm_the_workspace_it_rejected(self):
        """A clear that wins the compare-and-set must not leave this request's project armed.

        The fallback used when the slot ends with no project has to be a CLEARED resolution.
        Seeding it from the candidate workspace means a clear landing during the resolve is
        answered with an arm naming the very directory it just rejected, so the next claim
        binds there.
        """
        slot = _ChatSlot("test")
        slot.project = "/workspace/before"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        armed: list[str] = []
        state.sessions.transfer_retire_arm = MagicMock(
            side_effect=lambda frm, to, cwd: armed.append(cwd)
        )

        async def _clear_wins_during_resolve(key, cwd):
            slot.project = ""
            return "/defaults/resolved"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_clear_wins_during_resolve)

        with patch.object(chat_handlers, "default_project_dir", return_value="/workspace/other"):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post("/api/chat/slots/test/workspace", json={"workspace": "other"})

        assert armed, "no arm was transferred, so this test is not exercising the fallback"
        assert "/workspace/other" not in armed, (
            "the arm names the workspace the concurrent clear rejected, so the next claim binds "
            f"a project the slot is not on; armed={armed}"
        )

    @pytest.mark.asyncio
    async def test_an_empty_project_arm_resolves_for_the_key_it_lands_on(self):
        """The per-session default differs per key, so it must follow the transfer.

        With no project the arm names the per-session default. Resolving that for the key the
        request started on and then transferring it to the key the slot rebound to arms a
        directory no live-key provider binds, so the winning claim is evicted and its retry
        binds a scratch directory instead of the session's own default.
        """
        slot = _ChatSlot("test")
        slot.project = ""
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        armed: list[tuple[str, str]] = []
        state.sessions.transfer_retire_arm = MagicMock(
            side_effect=lambda frm, to, cwd: armed.append((to, cwd))
        )

        async def _default_per_key(key, cwd):
            # The rebind lands inside the resolve, as an in-turn link does.
            slot.linked_session_key = "slack:live-1"
            return f"/defaults/{key}"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_default_per_key)

        with patch.object(chat_handlers, "default_project_dir", return_value=""):
            async with TestClient(TestServer(_make_app(state))) as client:
                await client.post("/api/chat/slots/test/workspace", json={"workspace": "other"})

        assert armed, "no arm was transferred, so this test is not exercising the empty arm"
        mismatched = [(to, cwd) for to, cwd in armed if cwd != f"/defaults/{to}"]
        assert not mismatched, (
            "an arm names a per-session default resolved for a different key than the one it "
            f"landed on, so no provider on that key binds it; mismatched={mismatched}"
        )

    @pytest.mark.asyncio
    async def test_the_arm_names_the_project_that_won_the_compare_and_set(self):
        """The arm must publish AFTER the project is final, not before.

        The workspace switch resolves off-thread, and an in-turn `set_project` directive can
        land in that window -- which is exactly what the compare-and-set below exists to
        preserve. Publishing the arm first leaves a claim following a directory the CAS then
        declines to keep, so relative writes run outside the project the user selected.
        """
        slot = _ChatSlot("test")
        slot.project = "/workspace/before"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)

        async def _in_turn_writer_wins(key, cwd):
            # The racing in-turn directive, landing inside the resolve the switch awaits.
            slot.project = "/workspace/picked-by-user"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_in_turn_writer_wins)
        # Each publish is recorded with the project LIVE at that instant: a final-state check
        # cannot see a transient exposure a later re-point repairs.
        publishes: list[tuple[str, str, str]] = []
        state.sessions.note_project_change = AsyncMock(
            side_effect=lambda key, cwd: publishes.append(("note", cwd, slot.project))
        )
        state.sessions.transfer_retire_arm = MagicMock(
            side_effect=lambda frm, to, cwd: publishes.append(("transfer", cwd, slot.project))
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/test/workspace", json={"workspace": "other"})

        assert publishes, "nothing was published, so this test is not exercising the arm"
        disagreed = [p for p in publishes if p[1] != p[2]]
        assert not disagreed, (
            "an arm was published naming a project the slot was not on, so a claim in that "
            f"window binds a directory the compare-and-set declines to keep; {disagreed}"
        )

    @pytest.mark.asyncio
    async def test_a_denied_rebind_unwinds_the_published_agent_switch(self):
        """A computed denial must be honored, not discarded.

        The binding triple commits BEFORE the reset so a send landing in the teardown sees
        the new bindings. Discarding the denial lets the request carry on under them: the
        caller's agent stays published on a session it has no claim on until a later check
        unwinds it, and the answer it finally gets is a 409 naming the rebind rather than the
        gate's own indistinguishable refusal.
        """
        slot = _ChatSlot("test")
        slot._app = "owner-app"
        slot.agent = "kirocrew"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()

        async def _link_lands_mid_resolve(key, cwd):
            slot.linked_session_key = "slack:foreign-1"
            return f"/workspace/{key}"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_link_lands_mid_resolve)

        async with TestClient(TestServer(_make_app_as(state, "owner-app"))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "other"})

        assert resp.status == 404, f"the denial was not returned to the caller: {resp.status}"
        assert slot.agent == "kirocrew", (
            "the agent switch stayed published after the rebind was denied, so a caller "
            f"with no claim on the live session repointed it; slot.agent={slot.agent!r}"
        )

    @pytest.mark.asyncio
    async def test_a_rebind_to_an_unauthorized_session_arms_neither_key(self):
        """An app caller must not arm a session its own gate would refuse.

        The gate clears the key read inside the lock. A concurrent cron/channel link can
        then rebind the slot, and transferring the arm onto that key would repoint the
        bindings of a conversation this caller has no claim on -- its next claim would run
        under them. On denial the source arm is retracted so no arm survives on either key.
        """
        slot = _ChatSlot("test")
        slot._app = "owner-app"
        slot.agent = "kirocrew"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.sessions.supersede_arm_for_new_slot = MagicMock()
        state.conversation_log = MagicMock()

        async def _link_lands_mid_resolve(key, cwd):
            slot.linked_session_key = "slack:foreign-1"
            return f"/workspace/{key}"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_link_lands_mid_resolve)

        async with TestClient(TestServer(_make_app_as(state, "owner-app"))) as client:
            await client.post("/api/chat/slots/test/agent", json={"agent": "other"})

        assert state.sessions.transfer_retire_arm.call_args is None, (
            "the arm was transferred onto a session the caller's own gate refuses, so its "
            "next claim runs under bindings this caller had no claim to set"
        )
        retracted = [c.args[0] for c in state.sessions.supersede_arm_for_new_slot.call_args_list]
        assert "dashboard:test" not in retracted, (
            "the denied switch spent an arm it never wrote -- a project arm resident before "
            f"the request -- leaving a cwd-less claim on the stale project; retracted={retracted}"
        )

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
                await asyncio.sleep(0.05)
                slot.linked_session_key = "cron:job-1"
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
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            assert slot.agent == "new-agent"
            assert state.sessions.reset.await_args.args[0] == "slack:123.456"
            meta_call = state.conversation_log.update_metadata.call_args
            assert meta_call.args[0] == "dashboard:test"

    @pytest.mark.asyncio
    async def test_agent_switch_arms_the_project_that_survives_the_switch(self, monkeypatch):
        """A project that survives the switch is what the arm carries, not the default.

        The fallback is resolved unconditionally (an unlocked concurrent clear can empty
        `slot.project` after any gate on the candidate projects), so the resolve happening is
        not the question -- what matters is that the arm still prefers the live project and
        never records a directory the slot is not on.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/Users/alice/proj"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            assert state.sessions.mark_retire_on_next_claim.call_args.args[1] == "/Users/alice/proj"

    @pytest.mark.asyncio
    async def test_an_agent_switch_leaves_an_unscoped_slot_unscoped(self, monkeypatch):
        """A slot that never had a project must not come out of a switch reading as CLEARED.

        `project_cleared` is what stops a slot resuming its conversation and sends its claim
        past the warm pool. Deriving it from "the new project is empty" marks a slot the user
        never scoped, so an agent switch alone would silently cost that slot its history and a
        cold start -- and the arm would name the per-session default rather than stating no
        directory, so the next turn's relative writes land outside whatever it was resuming.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = ""
        slot.project_cleared = False
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200

        assert (
            slot.project_cleared is False
        ), "the switch converted a slot that was never scoped into an explicitly cleared one"
        assert (
            state.sessions.mark_retire_on_next_claim.call_args.args[1] is None
        ), "the arm named the cleared default for a slot that states no directory"

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_switch_moves_the_arm_to_the_live_key(self, monkeypatch):
        """The arm must guard the key the slot ENDS on, not the one captured before the awaits.

        `session_key` is read once before the resolve/reset awaits, and `linked_session_key` is
        assigned outside `slot._lock`, so a channel link landing mid-transaction leaves the arm
        on an abandoned key. The claim gate is per-key with no cross-key fallback, so the live
        key is then unguarded and the channel reuses the temporary agent and CWD -- writing in
        the project this request refused.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/Users/alice/proj"
        from kiro_crew.providers.base import LLMProvider

        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit probe so the switch reaches the arm, busy at the re-probe
        # after the awaits so the rollback -- the path that arms the stale key -- runs.
        busy.has_active_turn.side_effect = [False, True] + [True] * 8
        state = _mock_state(slot, provider=busy)
        state.conversation_log = MagicMock()
        state.sessions.reset = AsyncMock(return_value=True)

        async def _rebind_then_resolve(
            key, cwd
        ):  # A channel link landing during the resolve await, the way cron_inject does.
            slot.linked_session_key = "slack:999.111"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_rebind_then_resolve)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 409
        moved = state.sessions.transfer_retire_arm.call_args
        assert moved is not None, (
            "the arm was never re-pointed after the rebind, so it still guards the abandoned "
            "key while the live one accepts the refused agent and project"
        )
        assert moved.args[0] == "dashboard:test"
        assert (
            moved.args[1] == "slack:999.111"
        ), f"the arm must move to the key the slot now runs on; moved to {moved.args[1]!r}"

    @pytest.mark.asyncio
    async def test_a_workspace_rollback_moves_the_arm_to_the_live_key(self):
        """The workspace handler's rollback paths must arm the key the slot ENDS on.

        `session_key` is read once before the cleared resolve, and `linked_session_key` is
        assigned outside `slot._lock`, so a channel link landing in that await leaves every
        rollback arming an abandoned key. The claim gate is per-key with no cross-key
        fallback, so later channel turns on the live key reuse the rejected binding -- the
        agent handler already follows the rebind; these paths did not.
        """
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.workspace = "old-ws"
        # Cleared, so the resolve actually awaits: a non-empty project short-circuits it.
        slot.project = ""
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.side_effect = [False, True] + [True] * 8
        state = _mock_state(slot, provider=busy)
        state.conversation_log = MagicMock()
        state.sessions.reset = AsyncMock(return_value=False)

        async def _rebind_then_resolve(key, cwd):
            # A channel link landing during the resolve await, the way cron_inject does.
            slot.linked_session_key = "slack:777.222"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_rebind_then_resolve)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/workspace", json={"workspace": "new-ws"})
            assert resp.status == 409
        moved = state.sessions.transfer_retire_arm.call_args
        assert moved is not None, (
            "the rejected workspace switch left the arm on the abandoned key, so the live "
            "session keeps the binding this request refused"
        )
        assert moved.args[1] == "slack:777.222", (
            "the arm must move to the key the slot now runs on; moved to " f"{moved.args[1]!r}"
        )

    @pytest.mark.asyncio
    async def test_a_rejected_switch_arms_the_configured_default_agent(self, monkeypatch):
        """An EMPTY restored agent must arm `config.default_agent`, not the built-in name.

        The empty selection runs the configured default, so a rollback that falls back to
        the hardcoded `"kirocrew"` arms an agent the session will never run: the retirement
        retry then starts the built-in while the binding names the operator's own default,
        and the session comes up on the wrong agent. The commit-side arm already prefers
        `default_alias`; the rollback did not.
        """
        resolved = SimpleNamespace(workspace="ws", memory_store="ms", kiro_agent="target")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name=None, project=None: resolved,
        )
        cfg = MagicMock()
        cfg.default_agent = "operators-own-default"
        cfg.agents = {}
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        # EMPTY prior agent: this is what makes the restored value falsy on the rollback.
        slot.agent = ""
        slot.project = "/Users/alice/proj"
        busy = MagicMock(spec=LLMProvider)
        # Idle at the pre-commit probe so the switch commits, busy at the re-probe so the
        # rollback -- the only path carrying the fallback -- actually runs.
        busy.has_active_turn.side_effect = [False, True] + [True] * 8
        state = _mock_state(slot, provider=busy)
        state.conversation_log = MagicMock()
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": ""})
            assert resp.status == 409
        armed = state.sessions.mark_retire_on_next_claim.call_args
        assert armed is not None, "the rollback never armed an agent"
        assert armed.kwargs["agent"] == "operators-own-default", (
            "the rollback armed the built-in default instead of the configured one, so the "
            f"retirement retry starts a different agent than the session runs; got "
            f"{armed.kwargs['agent']!r}"
        )

    @pytest.mark.asyncio
    async def test_a_named_switch_rollback_arms_the_configured_default_agent(self, monkeypatch):
        """`default_alias` must be populated for a NAMED switch too, not just an empty one.

        It was assigned only in the empty-selection branch, so a switch that NAMES an agent
        left it "" -- and a rollback restoring an empty prior agent (a slot that was running
        the configured default) then armed the built-in literal. The retirement retry starts
        a different agent than the session will run, on a security-class path.
        """
        resolved = SimpleNamespace(workspace="ws", memory_store="ms", kiro_agent="target")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name=None, project=None: resolved,
        )
        cfg = MagicMock()
        cfg.default_agent = "operators-own-default"
        cfg.agents = {"picked-agent": object()}
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        # Empty prior agent: the slot was running the CONFIGURED default before the switch.
        slot.agent = ""
        slot.project = "/Users/alice/proj"
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.side_effect = [False, True] + [True] * 8
        state = _mock_state(slot, provider=busy)
        state.conversation_log = MagicMock()
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            # NAMES an agent, so the empty-selection branch never runs.
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "picked-agent"})
            assert resp.status == 409
        armed = state.sessions.mark_retire_on_next_claim.call_args
        assert armed is not None, "the rollback never armed an agent"
        assert armed.kwargs["agent"] == "operators-own-default", (
            "a NAMED switch left default_alias empty, so the rollback armed the built-in "
            f"instead of the configured default; got {armed.kwargs['agent']!r}"
        )

    @pytest.mark.asyncio
    async def test_the_identity_arm_records_the_alias_not_a_resolved_snapshot(self, monkeypatch):
        """The arm must name the ALIAS, whose target is resolved fresh at every consume.

        `slot.agent` is an alias and the config maps it to a runtime agent. Recording the
        RESOLVED target freezes that mapping for the arm's whole lifetime, so an alias
        re-pointed by a config edit during the window feeds the retirement retry the OLD
        target -- and the retry then replaces a correctly-resolved session with one running
        the wrong agent. Naming the alias keeps the arm stable and defers resolution.
        """
        resolved = SimpleNamespace(
            workspace="ws", memory_store="ms", kiro_agent="old-runtime-target"
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name=None, project=None: resolved,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: MagicMock()
        )
        slot = _ChatSlot("test")
        slot.agent = "old-alias"
        slot.project = "/Users/alice/proj"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-alias"})
            assert resp.status == 200
        armed = state.sessions.mark_retire_on_next_claim.call_args.kwargs["agent"]
        assert armed == "new-alias", (
            "the arm froze a resolved target instead of the alias; a config re-point during "
            f"the arm window then hands the retry a stale agent. armed={armed!r}"
        )

    @pytest.mark.asyncio
    async def test_agent_switch_answers_503_when_the_workspace_is_unavailable(self, monkeypatch):
        """A cleared slot whose default root cannot be resolved gets a controlled error.

        The resolution is the only filesystem work on this path and it is reached before
        the commit, so a raise must answer a retryable 503 rather than escape as a 500 --
        and the slot must be left exactly as the request found it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot, provider=None)
        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=OSError("root unreadable"))
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 503
            assert data["code"] == "workspace_unavailable"
            assert slot.agent == "old-agent"
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unavailable_workspace_answers_503_without_publishing_or_arming(
        self, monkeypatch
    ):
        """The resolve runs BEFORE the commit, so its failure has nothing to undo.

        Resolving after publishing `slot.agent` required an unwind, and that unwind armed
        the retirement -- which re-resolves the same cleared value SYNCHRONOUSLY, the very
        resolution that had just failed. The raise escaped the handler, so an unavailable
        workspace root answered an uncaught 500 instead of the retryable 503, and the arm
        that was supposed to protect the recovery was what crashed it. Resolving first
        removes the window instead of compensating for it: nothing is published, so there
        is no session to protect and no arm to raise.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        published: list[str] = []

        async def _fail_resolve(key, cwd):
            published.append(slot.agent)
            raise OSError("root unreadable")

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_fail_resolve)
        # The arm resolves its cwd synchronously, so on the old ordering it raised HERE.
        state.sessions.mark_retire_on_next_claim = MagicMock(side_effect=OSError("root unreadable"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 503, (
                "an unavailable workspace root must stay a retryable 503 -- a 500 here means "
                "the recovery path re-ran the resolution that had already failed"
            )
            assert (await resp.json())["code"] == "workspace_unavailable"

        assert published == ["old-agent"], (
            "the resolve must run BEFORE the agent is published, so a failure leaves the "
            f"slot exactly as the request found it; the resolve saw {published}"
        )
        assert slot.agent == "old-agent"
        state.sessions.mark_retire_on_next_claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_agent_selection_arms_the_defaults_alias(self, monkeypatch):
        """An empty selection runs the DEFAULT agent, so the arm must name THAT alias.

        The arm degraded to a literal here, which the correctly-resolved claim never matches,
        so the retry re-pointed onto the literal and ran an identity the user never chose.
        Any client can send an empty selection and the regex gate lets it through. The arm
        names the default ALIAS rather than its resolved target, because a target frozen at
        arm time survives a config re-point and feeds the retry the old agent.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load",
            MagicMock(return_value=MagicMock(agents={}, default_agent="house-default")),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            MagicMock(return_value=_bindings_for(kiro_agent="claude-code", workspace_dir=None)),
        )
        slot = _ChatSlot("test")
        slot.agent = ""
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": ""})
            assert resp.status == 200
            armed_agent = state.sessions.mark_retire_on_next_claim.call_args.kwargs["agent"]
            assert armed_agent == "house-default", (
                "an empty selection must arm the DEFAULT agent's ALIAS; a literal fallback is "
                f"the mismatch that refuses the real claim, and a resolved target freezes the "
                f"mapping the retry then re-points onto; got {armed_agent!r}"
            )

    @pytest.mark.asyncio
    async def test_agent_switch_still_arms_the_resolved_default_when_cleared(self, monkeypatch):
        """Positive control for the two tests above.

        A guard that skipped every resolution, or refused them all, would pass those two
        and silently arm an empty target -- which is the stale-binding class the arm exists
        to remove. A cleared slot must still resolve, and arm the resolved directory.
        """
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom_cfg)
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        # Cleared, not merely unscoped: without this the fixture models a slot that never had a
        # project, which states no directory and is the case the sibling test above pins.
        slot.project_cleared = True
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            state.sessions.resolve_arm_cwd.assert_awaited()
            armed = state.sessions.mark_retire_on_next_claim.call_args.args[1]
            assert armed == "/workspace/_default"

    @pytest.mark.asyncio
    async def test_agent_switch_never_arms_the_project_it_just_left(self, monkeypatch):
        """A workspace with NO default project must not arm the OLD directory.

        ``default_project_dir`` answers "" when the workspace directory is missing or
        sensitive, so the committed post-switch project is legitimately empty. The arm's
        fallback then decides what the next cwd-less claim binds, and resolving it from the
        PRE-switch project made that the directory being abandoned -- a silent bind to the
        old repository, with relative writes landing there and nothing to recover from.
        The correct fallback for an empty project is the CLEARED per-session default.
        """
        cfg = MagicMock()
        cfg.agents = {"new-agent": MagicMock()}
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda *a, **kw: _bindings_for(kiro_agent="ka", workspace_dir="/ws/empty"),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir", lambda *a: "empty-ws"
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names", lambda *a: frozenset()
        )
        # The workspace has no default project: this is the condition under test.
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.default_project_dir", lambda *a: "")
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/Users/alice/OLD-project"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            armed = state.sessions.mark_retire_on_next_claim.call_args.args[1]
            assert armed != "/Users/alice/OLD-project"
            assert armed == "/workspace/_default"

    @pytest.mark.asyncio
    async def test_a_concurrent_clear_during_the_awaits_still_arms_a_resolved_path(
        self, monkeypatch
    ):
        """An unlocked clear landing mid-request must not hand `""` to the SYNCHRONOUS arm.

        `slot.project` has writers that take no lock -- the in-turn set_project directive sets
        it to `""` -- so a clear can land during this handler's resolution awaits. The arm reads
        `slot.project or <fallback>` and runs synchronously inside the commit window, so an
        unresolved fallback means `mark_retire_on_next_claim` receives the empty string and
        resolves it itself: a mkdir and realpath of the workspace root, on the event loop, which
        that method's own contract forbids. Gating the resolve on the two candidate projects
        could not see this write, so the resolve is unconditional.
        """
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/Users/alice/proj"
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()

        async def _clear_mid_flight(*_a, **_kw):
            slot.project = ""
            # A real clear sets BOTH fields; emptying only the project models a slot that was
            # never scoped, which must NOT arm the cleared default.
            slot.project_cleared = True

        cfg = MagicMock()
        cfg.agents = {"new-agent": MagicMock()}
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda *a, **kw: _bindings_for(kiro_agent="ka", workspace_dir="/ws/x"),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir", lambda *a: "ws-x"
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names", lambda *a: frozenset()
        )
        # A real post-switch project, so the fallback is reached ONLY because the concurrent
        # clear made the commit's compare-and-set lose and left `slot.project` empty.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir", lambda *a: "/ws/x/proj"
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", _clear_mid_flight
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            assert slot.project == "", "precondition: the concurrent clear must have survived"
            armed = state.sessions.mark_retire_on_next_claim.call_args.args[1]
            assert armed == "/workspace/_default"
            assert armed != ""

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
            # The cleared-project resolution mkdirs and realpaths the workspace root, so
            # an unauthorized caller must reach no filesystem work en route to its 404.
            state.sessions.resolve_arm_cwd.assert_not_awaited()

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
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.agent == "old-agent"
            state.conversation_log.update_metadata.assert_not_called()

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
        state = _mock_state(slot)
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
        async with TestClient(TestServer(_make_app(state))) as client:
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
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        log = MagicMock()

        def _persist_and_rebind(_key, _meta):
            if not slot.linked_session_key:
                slot.linked_session_key = "cron:job-1"

        log.update_metadata = MagicMock(side_effect=_persist_and_rebind)
        state.conversation_log = log
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            assert slot.agent == "old-agent"
            # The restore wrote the rolled-back agent back into the
            # transcript metadata (last call).
            assert log.update_metadata.call_args.args[1] == {"agent": "old-agent"}

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
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            data = await resp.json()
            assert resp.status == 409
            assert data["code"] == "session_rebound"
            # The concurrent writer's value survives; only OUR commit unwinds.
            assert slot.agent == "new-agent"

    @pytest.mark.asyncio
    async def test_the_rollback_arms_the_preserved_agent_not_the_prior_one(self, monkeypatch):
        """A preserved concurrent write owns the slot, so the arm must name ITS identity.

        The rollback stands down on token identity, so an unlocked writer's agent
        survives this request's unwind -- and the rollback path is reached precisely
        BECAUSE a turn is in flight, which is exactly when an in-turn ``/agent``
        directive lands. The arm raised before the awaits still names the abandoned
        switch, so it must be re-pointed; re-pointing it at the PRIOR agent arms an
        identity the slot has left, so the next cwd-less claim is refused for
        the wrong agent and the retry re-points the session to an agent nobody
        selected. The registration matches on ``kiro_agent or slot.agent``, so the
        armed value has to be the preserved agent's RESOLVED target.
        """
        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names",
            lambda p: frozenset(),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "ws1",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: None,
        )
        targets = {
            "old-agent": "old-target",
            "hijack-agent": "hijack-target",
            "new-agent": "new-target",
        }
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None: MagicMock(
                kiro_agent=targets.get(str(name)), workspace_dir="/tmp/ws1"
            ),
        )
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        state = _mock_state(slot, provider=None)
        state.conversation_log = MagicMock()

        async def _hijack_then_rebind(*_a, **_k):
            # A DIFFERENT agent, so the rollback PRESERVES it instead of restoring.
            slot.agent = "hijack-agent"
            slot.linked_session_key = "cron:job-1"
            return True

        state.sessions.reset = AsyncMock(side_effect=_hijack_then_rebind)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
            assert resp.status == 409
        assert slot.agent == "hijack-agent"
        armed = list(state.sessions.mark_retire_on_next_claim.call_args_list)
        assert armed, "the rollback must re-point the arm it raised before the awaits"
        got = armed[-1].kwargs.get("agent")
        assert got == "hijack-agent", (
            "the arm must name the PRESERVED agent's ALIAS, not the prior agent and not a "
            f"resolved target frozen at arm time -- got {got!r}"
        )

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
            lambda cfg, name, project_dir=None: MagicMock(workspace_dir="/tmp/ws2"),
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
    async def test_rebind_during_the_save_transfers_the_arm_to_the_live_key(
        self, tmp_path, monkeypatch
    ):
        # The save awaits AFTER the arm, so a binding landing there leaves the arm and
        # pending key on the abandoned key while the key it now runs on is unarmed.
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        transfers: list = []
        state.sessions.transfer_retire_arm = MagicMock(
            side_effect=lambda frm, to, cwd: transfers.append((frm, to))
        )

        def _save_and_rebind(_project):
            slot.linked_session_key = "cron:job-1"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._save_recent_project", _save_and_rebind
        )
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": new_dir})
            assert resp.status == 200

        live = effective_session_key(slot)
        assert slot._pending_reset_history_key == live, (
            "the pending reset still names the abandoned key, so the reset lands on a "
            "session nobody is on while the live key keeps the old directory"
        )
        assert (
            transfers and transfers[-1][1] == live
        ), "the arm was left on the abandoned key, so the live key runs unarmed"

    @pytest.mark.asyncio
    async def test_arm_is_raised_before_the_recent_project_save_yields(self, tmp_path, monkeypatch):
        # The recent-project save is disk I/O, so it yields. A cwd-less claim acquiring
        # there reads the NEW project; with no arm up it reuses the old session.
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        armed: list = []
        state.sessions.mark_retire_on_next_claim = MagicMock(
            side_effect=lambda key, cwd, agent=None: armed.append(cwd)
        )
        observed: dict = {}

        def _save_and_observe(_project):
            # Whatever a competing claim could read, it reads HERE.
            observed["project"] = str(slot.project)
            observed["armed"] = list(armed)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._save_recent_project", _save_and_observe
        )
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/project", json={"project": new_dir})
            assert resp.status == 200

        assert observed, "the save never ran, so the window was never observed"
        assert not (observed["project"] == new_dir and not observed["armed"]), (
            "the slot advertised the new project while the arm was still unraised, so a "
            "cwd-less claim in this window reuses the old session under the old directory"
        )

    @pytest.mark.asyncio
    async def test_rebind_during_arm_resolve_answers_409_without_mutating(
        self, tmp_path, monkeypatch
    ):
        # Same intent at the window that now exists: the arm resolve is the only await
        # before the commit, so a binding landing there must leave the slot untouched.
        import os

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)

        def _resolve_and_rebind(key, cwd):
            slot.linked_session_key = "cron:job-1"
            return cwd or "/workspace/_default"

        state.sessions.resolve_arm_cwd = AsyncMock(side_effect=_resolve_and_rebind)
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


_LINKED_KEY = "slack:8442.001"


def _alias_state(slot_a: _ChatSlot, slot_b: _ChatSlot) -> DashboardState:
    """A state holding two alias slots that resolve onto ONE session."""
    state = _mock_state(slot_a)
    state._slots = {slot_a.key: slot_a, slot_b.key: slot_b}
    return state


class TestAliasSlotSwitchSerialization:
    """Two alias slots on ONE session must serialize against each other.

    ``effective_session_key`` folds every alias onto the session a slot's
    turns actually run on, so two slot names can address one live session
    (a channel- or cron-born slot carries the real key in
    ``linked_session_key``). The switch handlers serialize on ``slot._lock``
    and ``slot._model_pick_lock``, both created per ``_ChatSlot`` — so two
    switches arriving through DIFFERENT aliases take different locks and
    neither waits for the other: both commit, both reset the shared session,
    and the two slots' committed settings can disagree with each other and
    with the live provider. ``_autocompact_txn_locks`` already solved this
    class for its own endpoint by keying the lock on the shared resource
    rather than on the slot; these tests pin the same shape for the switch
    handlers.
    """

    @pytest.mark.asyncio
    async def test_racing_alias_model_switches_serialize(self):
        slot_a = _ChatSlot("alias-a")
        slot_b = _ChatSlot("alias-b")
        for _s in (slot_a, slot_b):
            _s.model = ""
            _s.linked_session_key = _LINKED_KEY
        state = _alias_state(slot_a, slot_b)

        seen_at_reset: list[tuple[str, str]] = []
        first_reset_started = asyncio.Event()
        release_first_reset = asyncio.Event()

        async def _reset(*args, **kwargs):
            seen_at_reset.append((slot_a.model, slot_b.model))
            if len(seen_at_reset) == 1:
                first_reset_started.set()
                await release_first_reset.wait()
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = asyncio.create_task(
                client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_A})
            )
            await first_reset_started.wait()
            second = asyncio.create_task(
                client.post("/api/chat/slots/alias-b/model", json={"model": _MODEL_B})
            )
            # Give the second request time to run as far as it can. Per-slot
            # locks are DISJOINT across aliases, so without a session-keyed
            # lock it runs to completion right here: it commits its model and
            # resets the very session the first request is still resetting.
            await asyncio.sleep(0.05)
            assert slot_b.model == ""
            assert state.sessions.reset.await_count == 1
            release_first_reset.set()
            resp1 = await first
            resp2 = await second
            assert resp1.status == 200
            assert resp2.status == 200
            # Each reset observed exactly the state its own request committed.
            assert seen_at_reset == [(_MODEL_A, ""), (_MODEL_A, _MODEL_B)]
            assert slot_a.model == _MODEL_A
            assert slot_b.model == _MODEL_B
            assert {c.args[0] for c in state.sessions.reset.await_args_list} == {_LINKED_KEY}

    # One pin per handler the sweep changed: with the session lock held by
    # another alias's in-flight switch, each handler must neither mutate nor
    # reset. Holding the lock externally is the same shape the per-slot
    # tests above use for slot._lock.

    @pytest.mark.asyncio
    async def test_agent_switch_waits_for_the_session_lock(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
        slot = _ChatSlot("alias-a")
        slot.agent = "old-agent"
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/agent", json={"agent": "new-agent"})
                )
                await asyncio.sleep(0.05)
                assert slot.agent == "old-agent"
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.agent == "new-agent"

    @pytest.mark.asyncio
    async def test_model_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_B})
                )
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_effort_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.reasoning_effort = ""
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        state.conversation_log = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/alias-a/reasoning-effort",
                        json={"reasoning_effort": "high"},
                    )
                )
                await asyncio.sleep(0.05)
                assert slot.reasoning_effort == ""
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_workspace_switch_waits_for_the_session_lock(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: f"/workspace/{ws}",
        )
        slot = _ChatSlot("alias-a")
        slot.workspace = "old-ws"
        slot.project = "/workspace/old-ws"
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/alias-a/workspace", json={"workspace": "new-ws"})
                )
                await asyncio.sleep(0.05)
                assert slot.workspace == "old-ws"
                state.sessions.reset.assert_not_awaited()
            resp = await task
            assert resp.status == 200
            assert slot.workspace == "new-ws"

    @pytest.mark.asyncio
    async def test_bulk_model_switch_waits_for_the_session_lock(self):
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["alias-a"]
            assert slot.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_a_rebind_while_queued_locks_the_new_session(self):
        # The window GPT 5.6 found on the first revision, closed structurally.
        # A rebind can land while a request queues on slot._lock, which is why
        # every handler resolves the key INSIDE that lock (pinned by
        # test_binding_that_lands_while_queued_on_the_lock_is_the_one_switched).
        # Had the session lock been keyed on any EARLIER read, the handler would
        # hold the lock for the OLD session while probing and resetting the new
        # one -- so an alias switching the new session would not be serialized
        # against it, and the post-await re-checks could not see it because they
        # compare against session_key, which is already the new key.
        # Entering the session lock AFTER the in-lock read makes the lock key
        # and the acted-on key the same value. This pins that: hold the NEW
        # session's lock, and the handler must wait for it.
        s2 = "slack:8442.999"
        s2_lock = _slot_switch_session_lock(s2)
        slot = _ChatSlot("alias-a")
        slot.model = _MODEL_A
        slot.linked_session_key = _LINKED_KEY
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            await s2_lock.acquire()
            try:
                async with slot._lock:
                    task = asyncio.create_task(
                        client.post("/api/chat/slots/alias-a/model", json={"model": _MODEL_B})
                    )
                    await asyncio.sleep(0.05)
                    # Rebind while it is queued on slot._lock.
                    slot.linked_session_key = s2
                # slot._lock is free now, so the handler resolves s2 and must
                # queue on L(s2) -- which this test holds. Keyed on the stale
                # pre-lock read it would instead sail through holding L(S1).
                await asyncio.sleep(0.05)
                assert not task.done()
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            finally:
                s2_lock.release()
            resp = await task
            assert resp.status == 200
            # The binding that landed is still the one switched.
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_args.args[0] == s2

    @pytest.mark.asyncio
    async def test_a_different_sessions_switch_is_not_blocked(self):
        # The complement, and the reason this is a session-keyed lock rather
        # than one global switch lock: a global lock would also stop the
        # collision above, by serializing every unrelated slot with it. Keyed
        # by session, a slot on a DIFFERENT session resolves to a different
        # lock and runs straight through while this one is held.
        other = _ChatSlot("other")
        other.model = _MODEL_A
        other.linked_session_key = "slack:9999.000"
        state = _mock_state(other, provider=None)
        state.sessions.reset = AsyncMock(return_value=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with _slot_switch_session_lock(_LINKED_KEY):
                # Completes WITHOUT the held lock being released. Bounded so a
                # regression to ONE global switch lock reddens here promptly
                # instead of hanging: the correct path takes milliseconds.
                resp = await asyncio.wait_for(
                    client.post("/api/chat/slots/other/model", json={"model": _MODEL_B}),
                    timeout=5,
                )
                assert resp.status == 200
                assert other.model == _MODEL_B
                state.sessions.reset.assert_awaited_once()


class TestSlotProjectSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_project_set_waits_for_slot_lock(self, tmp_path):
        # api_chat_slot_project is the one remaining live mutator of
        # slot.project outside the switch handlers: unlocked, its write could
        # land during a locked workspace switch's reset await and then be
        # erased by that switch's rollback. Serialized on the same lock, the
        # write queues until the switch completes.
        import os

        from kiro_crew.dashboard.chat import api_chat_slot_project

        slot = _ChatSlot("test")
        slot.project = "/workspace/old-ws"
        state = _mock_state(slot)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
        # A real directory on every OS: the endpoint realpaths and isdir-checks
        # the payload before the locked section this test pins.
        new_dir = os.path.realpath(str(tmp_path))
        async with TestClient(TestServer(app)) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/test/project", json={"project": new_dir})
                )
                # Let the request pass validation and block on the slot lock.
                await asyncio.sleep(0.05)
                assert slot.project == "/workspace/old-ws"
            resp = await task
            assert resp.status == 200
            assert slot.project == new_dir


class TestBulkModelSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_bulk_switch_waits_for_slot_lock(self):
        # The bulk handler acquires each slot's lock per-iteration, same lock
        # as the single-slot switch handlers: while another actor holds slot
        # A's lock, the bulk switch must neither commit nor reset A.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                # Let the request reach (and block on) the slot lock.
                await asyncio.sleep(0.05)
                assert slot.model == _MODEL_A
                state.sessions.reset.assert_not_awaited()
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slot_that_started_running_while_queued_is_skipped(self):
        # The skip_running pre-check is re-run INSIDE the lock: a slot that
        # became running while the bulk request waited on its lock must land
        # in skipped_running — not be reset mid-turn (the defect this PR
        # exists to prevent, on the bulk path).
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            async with slot._lock:
                task = asyncio.create_task(
                    client.post("/api/chat/slots/model", json={"model": _MODEL_B})
                )
                # Let the request pass the unlocked pre-check and block on the
                # lock, then start a turn before releasing it.
                await asyncio.sleep(0.05)
                running_task = MagicMock()
                running_task.done.return_value = False
                slot.task = running_task
            resp = await task
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_slot_already_on_target_is_unchanged_not_skipped(self):
        # Classification order inside the lock is equality FIRST: a running
        # slot that already uses the requested model is "unchanged", not
        # "skipped_running" — a running-check ahead of the equality check
        # would misreport it and imply work was left undone.
        slot = _ChatSlot("test")
        slot.model = _MODEL_B
        running_task = MagicMock()
        running_task.done.return_value = False
        slot.task = running_task
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["unchanged"] == ["test"]
            assert data["skipped_running"] == []
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_exception_is_isolated_per_slot(self):
        # The retry runs inside the per-slot failure-isolation try: a teardown
        # that raises on the retry classifies THAT slot as failed (model
        # untouched) and the remaining slots are still processed — never a
        # 500 aborting the whole bulk switch.
        from kiro_crew.providers.acp import AcpProvider

        slot_a = _ChatSlot("a")
        slot_a.model = _MODEL_A
        slot_b = _ChatSlot("b")
        slot_b.model = _MODEL_A
        idle = MagicMock(spec=AcpProvider)
        idle.has_active_turn.return_value = False
        state = _mock_state(slot_a, provider=idle)
        state._slots = {"a": slot_a, "b": slot_b}
        # Slot a: first reset declines, retry raises. Slot b: reset succeeds.
        state.sessions.reset = AsyncMock(side_effect=[False, RuntimeError("boom"), True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["failed"] == ["a"]
            assert data["switched"] == ["b"]
            assert slot_a.model == _MODEL_A
            assert slot_b.model == _MODEL_B

    @pytest.mark.asyncio
    async def test_reset_declined_no_live_provider_switches(self):
        # A declined reset with NO live registered provider commits: nothing
        # to tear down, the next message cold-starts under the new model.
        # Exactly one reset attempt, slot lands in switched.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_cold_start_lands_in_skipped_running(self):
        # A first send can slip into the reset await and still be INSIDE its
        # provider.start() when the decline is read: slot.running is set (at
        # dispatch) but get_provider sees nothing yet. Bulk commits AFTER the
        # reset, so that cold-starting session captured the OLD model —
        # committing here would report success over it. The handler re-reads
        # slot.running before the provider ladder and classifies the slot as
        # skipped_running with its model untouched.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)

        async def _decline_and_start_turn(*_a, **_k):
            running_task = MagicMock()
            running_task.done.return_value = False
            slot.task = running_task
            return False

        state.sessions.reset = AsyncMock(side_effect=_decline_and_start_turn)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_declined_idle_retry_switches(self):
        # An idle live session declined the first reset (its slipped-in turn
        # already finished). Bulk commits AFTER the reset, so that session is
        # on the old model: the handler retries once, and on success the slot
        # is switched — never left as a silent stale process.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = False
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["switched"] == ["test"]
            assert slot.model == _MODEL_B
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_twice_lands_in_skipped_running(self):
        # A second decline means another turn is genuinely racing the retry:
        # the slot lands in skipped_running with its model untouched.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = False
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_reset_declined_busy_lands_in_skipped_running(self):
        # A turn can start even after the in-lock checks (message dispatch
        # does not take slot._lock), so the reset runs with
        # skip_if_busy=skip_running and its atomic decline is authoritative:
        # the slot lands in skipped_running with its model untouched, and the
        # in-flight turn survives.
        from kiro_crew.providers.acp import AcpProvider

        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        provider = MagicMock(spec=AcpProvider)
        # Idle at the in-lock pre-check, busy by the time the decline is read.
        provider.has_active_turn.side_effect = [False, True]
        state = _mock_state(slot, provider=provider)
        state.sessions.reset = AsyncMock(return_value=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            assert state.sessions.reset.await_args.kwargs == {"skip_if_busy": True}

    @pytest.mark.asyncio
    async def test_live_turn_on_effective_session_is_skipped_before_the_reset(self):
        # slot.running only sees turns dispatched through this slot's task; a
        # channel-linked slot's turn runs under its linked key without setting
        # it. The last-instant has_active_turn re-check on the effective
        # session catches it BEFORE the reset, so _reset_slot_session's
        # unblock half never runs against the live turn's pending cards.
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
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slot_with_attached_subagents_is_skipped_even_when_forced(self):
        # The reset tears down the runtime attached children run on, so a
        # parent with children is skipped — even with skip_running=false,
        # which speaks to the parent's own turn, not to its children.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=None)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-1"]
        state.subagents._queued_depth.return_value = 0
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/model", json={"model": _MODEL_B, "skip_running": False}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["skipped_running"] == ["test"]
            assert data["switched"] == []
            assert slot.model == _MODEL_A
            state.sessions.reset.assert_not_awaited()


class TestTheSettleRefusesAnUnsettledKey:
    """A rebind after the LAST resolve must not arm the key the slot just left.

    The settle probes before each resolve. Without a verification after the final one, a rebind
    landing in that window is invisible: the caller arms the abandoned string key, and a later
    session under that same key reads an arm naming a directory chosen for a binding that is
    gone. Reported rather than assumed away -- an unsettled key publishes nothing.
    """

    @pytest.mark.asyncio
    async def test_a_rebind_after_the_final_pass_publishes_no_arm(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _settle_arm_target

        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)

        # Every probe reports a DIFFERENT key, so the passes can never settle -- which is the
        # shape of a rebind that keeps landing, including one after the last resolve.
        keys = iter(f"chat-{n}" for n in range(1, 40))
        with patch(
            "kiro_crew.dashboard.chat_runner.effective_session_key",
            side_effect=lambda _slot: next(keys),
        ):
            denied, key, cwd, settled = await _settle_arm_target(state, slot, "chat-0", "", None)

        assert denied is None
        assert settled is False, (
            "the settle reported success on a key that never stabilised, so its caller arms "
            "an abandoned key and a later session under that string reads the stale arm"
        )
        state.sessions.supersede_arm_for_new_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unsettled_resolve_keeps_an_arm_this_caller_did_not_write(self, tmp_path):
        """A caller holding no generation must not spend a project arm another producer owns.

        Retracting unconditionally erased that arm, and the next claim stating no directory --
        every channel turn states none -- was then served the superseded project's session with
        nothing left to retire it.
        """
        from kiro_crew.dashboard.chat_runner import _settle_arm_target

        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)

        keys = iter(f"chat-{n}" for n in range(1, 40))
        with patch(
            "kiro_crew.dashboard.chat_runner.effective_session_key",
            side_effect=lambda _slot: next(keys),
        ):
            await _settle_arm_target(state, slot, "chat-0", "", None, only_generation=None)

        state.sessions.supersede_arm_for_new_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unsettled_resolve_retracts_the_generation_this_caller_owns(self, tmp_path):
        """A producer unwinding its OWN arm still retracts it, scoped to its own generation."""
        from kiro_crew.dashboard.chat_runner import _settle_arm_target

        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)

        keys = iter(f"chat-{n}" for n in range(1, 40))
        with patch(
            "kiro_crew.dashboard.chat_runner.effective_session_key",
            side_effect=lambda _slot: next(keys),
        ):
            await _settle_arm_target(state, slot, "chat-0", "", None, only_generation=7)

        state.sessions.supersede_arm_for_new_slot.assert_called_once_with(
            "chat-0", only_generation=7
        )

    @pytest.mark.asyncio
    async def test_a_stable_key_still_settles(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _settle_arm_target

        slot = _ChatSlot("test")
        slot.project = str(tmp_path)
        state = _mock_state(slot, provider=None)

        denied, key, cwd, settled = await _settle_arm_target(
            state, slot, "chat-0", str(tmp_path), None
        )
        assert denied is None
        assert settled is True


class TestTheCommitToArmWindowHoldsByConstruction:
    """No ``await`` may separate a binding commit from the arm that protects it.

    The window is what makes the arm safe: a claim landing between the published triple and the
    arm reads the new bindings with nothing raised to retire the session bound to the old ones.
    Until now that was carried by a comment, so a later edit could open the window silently and
    every test would still pass. This reads the source instead, so the invariant fails at the
    seam that breaks it rather than in production.
    """

    COMMIT = "_CommitToken"
    ARMS = frozenset({"mark_retire_on_next_claim", "transfer_retire_arm", "note_project_change"})

    @staticmethod
    def _called_name(node: ast.AST) -> str | None:
        call = node.value if isinstance(node, ast.Await) else node
        if isinstance(call, ast.Expr):
            call = call.value
        if isinstance(call, ast.Await):
            call = call.value
        if isinstance(call, ast.Assign):
            call = call.value
        if isinstance(call, ast.Await):
            call = call.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None

    @classmethod
    def _commits_a_binding(cls, stmt: ast.stmt) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == cls.COMMIT
            for n in ast.walk(stmt)
        )

    @classmethod
    def _publishes_an_arm(cls, stmt: ast.stmt) -> bool:
        return cls._called_name(stmt) in cls.ARMS

    @classmethod
    def _violations(cls, source: str) -> list[str]:
        """Every block where an await separates a binding commit from a later arm."""
        found: list[str] = []
        for node in ast.walk(ast.parse(source)):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                commits = [i for i, s in enumerate(block) if cls._commits_a_binding(s)]
                arms = [i for i, s in enumerate(block) if cls._publishes_an_arm(s)]
                if not commits or not arms or max(arms) < min(commits):
                    continue
                for stmt in block[min(commits) : max(arms) + 1]:
                    if cls._publishes_an_arm(stmt):
                        continue
                    for inner in ast.walk(stmt):
                        if isinstance(inner, ast.Await):
                            name = cls._called_name(inner) or "<expr>"
                            found.append(f"line {inner.lineno}: await {name}")
        return found

    def test_no_await_separates_a_commit_from_its_arm(self):
        source = Path(chat_handlers.__file__).read_text(encoding="utf-8")
        assert self._violations(source) == [], (
            "an await was added between a published binding triple and the arm that protects "
            "it, so a claim in that window runs the new bindings with the old session resident"
        )

    def test_the_guard_fails_when_the_window_is_opened(self):
        """Positive control: the scan must reject the very edit the invariant forbids."""
        opened = (
            "async def h(state, slot):\n"
            "    slot.agent = _CommitToken('a')\n"
            "    await state.sessions.resolve_arm_cwd('k', '')\n"
            "    state.sessions.mark_retire_on_next_claim('k', None)\n"
        )
        assert self._violations(opened), "the scan cannot see an await inside the window"

        closed = (
            "async def h(state, slot):\n"
            "    slot.agent = _CommitToken('a')\n"
            "    state.sessions.mark_retire_on_next_claim('k', None)\n"
        )
        assert self._violations(closed) == [], "the scan flags a window that is actually closed"

    def test_the_scan_reads_utf8_on_every_platform(self):
        """Control: the module holds bytes cp1252 cannot decode, so the encoding is load-bearing.

        ``read_text()`` with no encoding takes the platform default -- cp1252 on Windows -- so
        this scan died there on a byte the source legitimately contains while every Linux shard
        passed. Reading the same file as cp1252 must still raise, otherwise this assertion would
        hold only because the host happens to be UTF-8 and the Windows break would return.
        """
        path = Path(chat_handlers.__file__)
        with pytest.raises(UnicodeDecodeError):
            path.read_text(encoding="cp1252")
        assert path.read_text(encoding="utf-8"), "the utf-8 read must succeed on every platform"

    def test_an_awaited_arm_is_not_itself_a_violation(self):
        """``note_project_change`` IS awaited, so the arm's own await must not read as the leak."""
        awaited_arm = (
            "async def h(state, slot):\n"
            "    slot.project = _CommitToken('p')\n"
            "    await state.sessions.note_project_change('k', '/p')\n"
        )
        assert self._violations(awaited_arm) == []


class TestTheAgentSwitchAuthorizesBeforeItPublishes:
    """The arm target must be settled and authorized BEFORE the binding triple is published.

    The resolve inside the settle awaits. If the triple is committed first, then for that whole
    window `slot.agent` names the new agent while the arm still names the key the slot left, so a
    turn claiming the rebound key runs an agent whose switch may still answer 409 and roll back --
    and `mark_retire_on_next_claim` states an in-flight turn is never retro-corrected. Rolling back
    afterwards does not close that window; only ordering the gate first does.
    """

    def test_the_settle_and_gate_precede_the_commit_triple(self):
        import inspect

        src = inspect.getsource(chat_handlers.api_chat_slot_agent)

        settle = src.find("await _settle_arm_target(")
        gate = src.find("if pre_commit_denied is not None:")
        publish = src.find("slot.agent = _CommitToken(agent_name)")

        assert settle != -1, "the pre-commit settle is gone"
        assert gate != -1, "the pre-commit refusal is gone"
        assert publish != -1, "the commit triple moved; re-point this control"
        assert settle < publish, (
            "the binding triple is published before the arm target is settled, so a turn on the "
            "rebound key can run an agent whose switch has not been authorized"
        )
        assert gate < publish, (
            "the rebind refusal runs after the commit, so an unauthorized agent is published "
            "first and only rolled back afterwards -- an in-flight turn already saw it"
        )

    def test_the_arm_transfer_follows_the_commit_with_no_await_between(self):
        """Commit and arm must share one suspension-free window."""
        import inspect

        src = inspect.getsource(chat_handlers.api_chat_slot_agent)
        publish = src.find("slot.agent = _CommitToken(agent_name)")
        transfer = src.find("state.sessions.transfer_retire_arm(")
        assert publish != -1 and transfer != -1
        between = src[publish:transfer]
        assert "await " not in between, (
            "an await sits between the commit and its arm transfer, so a claim can read the "
            f"published triple while the arm names the old key: {between[:160]!r}"
        )


class TestAnUnsettledKeyCommitsNoBinding:
    """The commit and the arm are one unit, so an unsettled key must publish neither.

    `_settle_arm_target` reports `arm_settled` False when the slot kept rebinding through
    every pass. The arm is then owed to a binding nobody is on, so the transfer is skipped --
    and committing the agent and project anyway publishes a new binding on the live key with
    no arm raised to protect it.
    """

    @pytest.mark.asyncio
    async def test_an_unsettled_settle_answers_409_and_leaves_the_agent_alone(self):
        slot = _ChatSlot("test")
        slot.agent = "old-agent"
        slot.project = "/workspace/old"
        state = _mock_state(slot)
        state.conversation_log = MagicMock()

        armed: list = []
        state.sessions.mark_retire_on_next_claim = MagicMock(
            side_effect=lambda *a, **k: armed.append((a, k)) or 1
        )

        # The settle exhausts its passes: denial None, but the key never settled.
        async def never_settles(*args, **kwargs):
            return None, "slack:moved-9999", "/workspace/old", False

        with (
            patch.object(chat_handlers, "_settle_arm_target", new=never_settles),
            patch.object(chat_handlers, "warm_project_agent_names", new=AsyncMock()),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})

        assert resp.status == 409, await resp.text()
        assert str(slot.agent) == "old-agent", (
            "the switch committed a new agent on a key that never settled, so the binding is "
            f"published with no arm to protect it; agent={slot.agent!r}"
        )
        assert (
            armed == []
        ), f"an arm was raised for an unsettled key, which nobody is on; armed={armed!r}"


class TestARejectedSwitchLeavesANeverScopedSlotUnscoped:
    """A rejected switch must not convert an UNSET project into an explicit clear.

    `slot.project or cleared_arm_cwd` collapsed two distinct prior states. A slot that
    was explicitly CLEARED states `CWD_CLEARED`, whose arm is the per-session default.
    A slot that was NEVER SCOPED states nothing, which is what keeps the warm pool and
    its stored-cwd resume override; arming the resolved default there binds the
    per-session scratch directory instead, so the next turn's relative writes land
    outside the directory the session was resuming.
    """

    @pytest.mark.asyncio
    async def test_a_never_scoped_slot_arms_no_directory_but_still_retires_the_agent(self):
        slot = _ChatSlot(key="chat-1", agent="old-agent")
        # NEVER SCOPED: no project, and no clear ever asked for either.
        slot.project = ""
        slot.project_cleared = False
        # The concurrent turn starts INSIDE the post-commit settle await, which is the
        # window the pre-commit guard cannot see and the rollback path exists for.
        running_task = MagicMock()
        running_task.done.return_value = False

        async def settle_then_turn_starts(*args, **kwargs):
            slot.task = running_task
            # The helper's own shape: denial, key, and whether the transfer settled.
            return None, None, True

        state = _mock_state(slot)
        state.conversation_log = MagicMock()

        # The directory a CLEARED slot would arm, and the one an unset slot must not.
        scratch = "/workspace/_default"
        armed: list = []

        def record(key, cwd, agent=None):
            armed.append({"key": key, "cwd": cwd, "agent": agent})
            return 1

        state.sessions.mark_retire_on_next_claim = MagicMock(side_effect=record)

        with (
            patch.object(chat_handlers, "warm_project_agent_names", new=AsyncMock()),
            patch.object(
                chat_handlers,
                "_settle_and_transfer_arm",
                new=AsyncMock(side_effect=settle_then_turn_starts),
            ),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/chat-1/agent", json={"agent": "new-agent"}
                )

        assert resp.status == 409, await resp.text()
        assert slot.project == "", "precondition: the rollback did not restore the unset project"
        assert not getattr(
            slot, "project_cleared", False
        ), "precondition: the rollback left the unset project marked as an explicit clear"
        assert (
            len(armed) >= 2
        ), f"precondition: the rejected switch raised no rollback arm; arms={armed!r}"

        arm = armed[-1]
        assert arm["cwd"] != scratch, (
            "the rollback armed the per-session scratch directory for a slot that was never "
            f"scoped, so the next turn writes relative paths there; armed={arm['cwd']!r}"
        )
        assert arm["cwd"] is None, (
            "an unset project states NO directory, so the arm must state none either; "
            f"armed={arm['cwd']!r}"
        )
        # Retirement itself must survive the fix: the agent still has to be retired.
        assert arm["agent"], (
            "the rollback lost agent retirement, so the next claim is served a session "
            "still running the rejected agent"
        )
