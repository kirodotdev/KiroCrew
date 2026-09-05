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
    api_chat_slot_model,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
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
