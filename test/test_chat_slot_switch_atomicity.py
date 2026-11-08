"""Concurrency tests for the slot model/workspace switch handlers.

The agent and effort switch handlers serialize their mutate-then-reset
sections under ``slot._lock``; the model and workspace handlers ran the same
shape unlocked, so two racing switches could each commit and reset against
the other's half-applied state, and a mid-turn model switch fell through to
the reset fallback and tore down the in-flight turn for any programmatic
caller. These tests pin the lock serialization, the in-lock re-checks, and
the mid-turn 409 (clones of the concurrency template in
``test_chat_slot_reasoning_effort.py``).

The switches on a linked-session slot are pinned in
``test_chat_slot_switch_atomicity_linked_session.py``, and the alias, project and
bulk switches in ``test_chat_slot_switch_atomicity_alias_and_bulk.py``. The app
and state builders they share live in ``chat_slot_switch_atomicity_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_switch_atomicity_helpers import _MODEL_A, _MODEL_B, _make_app, _mock_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.state import _ChatSlot

MOD = "kiro_crew.dashboard.chat_handlers"


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
    bind_private_session_store(key, slot.memory_store)
    state.conversation_log.update_metadata(
        key, {"agent": slot.agent, "memory_store": slot.memory_store}
    )
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
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            # A retry cannot gradually mutate the slot or erase the permanent pin.
            for _ in range(2):
                response = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": target}
                )
                assert response.status == 409
                result = await response.json()
                assert result["code"] == "member_session_pinned"
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
        async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
            response = await client.post(
                f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}
            )
            assert response.status == 409
            assert (await response.json())["code"] == "member_session_pinned"
        assert slot.agent == "writer"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_member_reset_stays_available(self, private_switch_state):
        state, slot, key = private_switch_state
        with patch(f"{MOD}.warm_project_agent_names", new_callable=AsyncMock):
            async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
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
            "kiro_crew.execution_context.read_session_execution",
            side_effect=ValueError("invalid binding"),
        ):
            async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
                response = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}
                )
                assert response.status == 503
                assert (await response.json())["code"] == "member_binding_unavailable"
        assert slot.agent == "writer"
        state.sessions.reset.assert_not_awaited()


class TestSlotModelSwitchAtomicity:
    @pytest.mark.asyncio
    async def test_same_value_pick_during_refusal_fallback_takes_live_path(self):
        # The pin equals the DISPLAYED primary while a refusal fallback is
        # serving the wire, so "nothing to switch" is false: the early return
        # must not be taken, or the pick bumps the generation, the restore
        # probe drops its record, and the session is stranded on the fallback.
        # With no live provider the live path lands on the reset — the
        # observable that the switch actually ran.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot._refusal_fallback_primary = _MODEL_A
        slot._refusal_fallback_candidate = _MODEL_B
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            assert resp.status == 200
            state.sessions.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_same_value_pick_without_fallback_still_short_circuits(self):
        # The companion guard: with NO fallback state the same-value pick keeps
        # its cheap path — generation bump, no reset, no session teardown.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        gen_before = slot._model_pick_gen
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            assert resp.status == 200
            assert slot._model_pick_gen == gen_before + 1
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_value_pick_stamps_the_shared_epoch(self):
        # The slot-local bump is invisible across aliases: another slot driving
        # the SAME wire session compares the shared client's epoch at restore
        # time, so a same-value pick that skips the stamp is silently undone by
        # that slot's refusal-fallback restore. The short-circuit must stamp
        # the shared epoch exactly as the live-switch path does.
        class _EpochClient:
            def __init__(self):
                self._explicit_pick_epoch = 0

        inner = _EpochClient()
        provider = MagicMock()
        provider.client = inner
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_A})
            assert resp.status == 200
            assert inner._explicit_pick_epoch == 1, (
                "a same-value pick is still an explicit pick: the shared epoch "
                "must move so an alias's restore respects it"
            )
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_epoch_stamped_when_effort_reapply_fails_after_landed_switch(self):
        # set_model LANDED on the shared wire session — the model changed for
        # every alias — then the effort reapply failed and the handler fell
        # back toward reset. The epoch stamp must precede the reapply: a
        # sibling slot's refusal restore can already observe the landed pick
        # live, and an unstamped epoch lets that restore overwrite the user's
        # explicit choice with its recorded primary.
        from kiro_crew.providers.acp import AcpProvider

        class _EpochClient:
            def __init__(self):
                self._explicit_pick_epoch = 0
                self.set_model = AsyncMock()

        inner = _EpochClient()
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        slot.reasoning_effort = "high"
        provider = MagicMock(spec=AcpProvider)
        provider.is_claude_backend = False
        provider.has_active_turn.return_value = False
        provider.client = inner
        provider.supports_effort = MagicMock(return_value=True)
        provider.change_effort = AsyncMock(side_effect=RuntimeError("reapply failed"))
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            assert inner._explicit_pick_epoch == 1, (
                "the epoch must stamp as soon as set_model lands — an effort "
                "reapply failure must not skip it"
            )

    @pytest.mark.asyncio
    async def test_mid_turn_switch_is_committed_and_deferred_without_reset(self):
        # _try_live_model_switch declines a mid-turn live switch, and the old
        # unlocked handler then fell through to the reset — tearing down the
        # in-flight turn mid-stream. The handler takes neither path: the pick
        # is committed on the slot, marked for the turn boundary, and answered
        # deferred; the live session is untouched and nothing is reset.
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
            assert resp.status == 200
            assert data["deferred"] is True
            assert slot.model == _MODEL_B
            assert slot._deferred_model_switch_key
            provider.client.set_model.assert_not_awaited()
            state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cold_start_turn_defers_via_slot_running(self):
        # A first message can be INSIDE the multi-second provider.start() when
        # the switch arrives: no session is registered yet, so the provider
        # pre-check sees nothing — but slot.running is set at dispatch, so the
        # handler still defers instead of resetting a session the
        # cold-starting turn is about to register on the old model; the
        # boundary moves that session once it is idle.
        slot = _ChatSlot("test")
        slot.model = _MODEL_A
        running_task = MagicMock()
        running_task.done.return_value = False
        slot.task = running_task
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/model", json={"model": _MODEL_B})
            data = await resp.json()
            assert resp.status == 200
            assert data["deferred"] is True
            assert slot.model == _MODEL_B
            assert slot._deferred_model_switch_key
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
        # The teardown-raise path must NOT bypass the rebind guard: a slot
        # rebound while the raising
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
    async def test_active_turn_is_refused_before_the_commit(self):
        # The reset path calls
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
        # slot.running is set at dispatch, before
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
        # The unlocked periodic flush may have
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
        # slot.project has lock-free writers (the
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
        # The teardown-raise path must NOT bypass the rebind guard: a slot
        # rebound while the raising
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
