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

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
from kiro_crew.dashboard.chat_handlers import _slot_switch_session_lock
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# Valid registry aliases the model guard accepts (tests are exempt from the
# hardcoded-model-literal gate; these mirror the ids the existing model-switch
# tests use).
_MODEL_A = "claude-opus-4.8"
_MODEL_B = "gpt-5.6-sol"


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


def _mock_state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_context_usage = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # No live AcpProvider by default → the model handler takes the reset path.
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


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
        # A teardown that RAISES (#8598): SessionManager.reset pops the
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
        # The idle-decline RETRY can raise too (#8598): first reset declined
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
        # error. The helper re-raises (pre-#8598 semantics) instead of
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
        # review finding on the #8598 fix): a slot rebound while the raising
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
    async def test_message_guard_is_checked_inside_the_lock(self):
        # The total_messages guard is a check-then-act across the reset
        # await: an unlocked read could pass while a serialized predecessor
        # was still running, then mutate a slot whose conversation had
        # started in the meantime. Checked inside the lock, a message that
        # lands while the request waits makes it answer 409 and touch
        # nothing.
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
            assert resp.status == 409
            assert slot.workspace == "old-ws"
            assert slot.project == "/workspace/old-ws"
            state.sessions.reset.assert_not_awaited()

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
        busy.has_active_turn.return_value = True
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
        live.has_active_turn.return_value = True
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
        state = _mock_state(slot, provider=newborn)
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
        # A teardown that RAISES (#8598): the workspace/project pair is
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
        # The idle-decline RETRY can raise too (#8598): first reset declined
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
        # review finding on the #8598 fix): a slot rebound while the raising
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
        # a session the slot no longer runs on: commit nothing, reset nothing,
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
        # The session torn down is no longer the slot's: the commit — the one
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
    async def test_rebind_during_project_save_rolls_back_and_answers_409(
        self, tmp_path, monkeypatch
    ):
        # A binding that lands while the recent-project save awaits means the
        # deferred-reset flag would name a session the slot no longer runs on
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
