"""Tests for POST /api/chat/slots/{slot}/continue.

The frontend decides whether to OFFER Continue (it holds the transcript locally).
This endpoint is the authority that AUTHORIZES it: the client's view is a lagging
WS snapshot, so a press landing as a turn starts — or a second browser tab acting
on a stale cache — must be refused here rather than dispatching a duplicate turn
against one slot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import _is_interrupted, api_chat_slot_continue
from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    # Explicit "this slot has no background children". Left as a MagicMock the
    # sub-agent guard would see a truthy running list and refuse EVERY test, and
    # left off the spec entirely it would be skipped silently — either way the
    # other guards would stop being what the tests actually exercise.
    state.subagents = None
    return state


@pytest.fixture
def _patched(monkeypatch):
    """Neutralize SEL, the readiness latch, and the real turn dispatcher."""
    mock_sel = MagicMock()
    mock_sel.log_tool_invocation = MagicMock()
    started = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch(
            "kiro_crew.dashboard.chat_handlers.reject_if_kiro_unverified",
            AsyncMock(return_value=None),
        ),
        patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", started),
    ):
        yield started


class TestIsInterrupted:
    """The predicate mirrors `selectContinuable` in website/src/store/chatSlice.ts."""

    def test_empty_transcript_is_not_interrupted(self):
        assert _is_interrupted(_ChatSlot("s")) is False

    def test_trailing_user_row_is_interrupted(self):
        # Gateway restarted mid-turn: the task died and nothing was appended.
        slot = _ChatSlot("s")
        slot.append("user", "do the thing", "msg msg-u")
        assert _is_interrupted(slot) is True

    def test_clean_completion_is_not_interrupted(self):
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "all done", "msg msg-a")
        assert _is_interrupted(slot) is False

    def test_error_after_assistant_is_interrupted(self):
        # Streamed partway then died — shape-identical to a clean completion
        # except for the trailing error row.
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "starting…", "msg msg-a")
        slot.append("error", "⟳ Connection lost — please retry.", "msg msg-err")
        assert _is_interrupted(slot) is True

    def test_superseded_error_is_not_interrupted(self):
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.append("error", "boom", "msg msg-err")
        slot.append("user", "again", "msg msg-u")
        slot.append("assistant", "done", "msg msg-a")
        assert _is_interrupted(slot) is False

    def test_compaction_notice_is_not_the_floor(self):
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "Auto-compacted at 80%.", "msg msg-a", meta={"kind": "compaction"})
        assert _is_interrupted(slot) is True


class TestChatSlotContinue:
    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404_with_code(self, _patched):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/missing/continue")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_running_slot_is_refused(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.task = MagicMock()
        slot.task.done = MagicMock(return_value=False)
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_running"

    @pytest.mark.asyncio
    async def test_queued_message_is_refused(self, _patched):
        # The runner is about to pick the thread up on its own; resuming here
        # would double-fire the turn.
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.queue_append("next one")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_queue_pending"

    @pytest.mark.asyncio
    async def test_settled_conversation_is_allowed_as_a_plain_continue(self, _patched):
        # A clean completion is NOT refused: os._exit on a force-quit runs no
        # cleanup, so a killed turn writes no error row and reads exactly like
        # this. Refusing this shape is what left a force-quit with no way back.
        # The wording is what changes, not the availability.
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "all done", "msg msg-a")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200
        entry = slot._queue[0]
        assert entry["content"].startswith("[Continue — requested by the user]")
        # Must NOT tell a model that finished cleanly it was interrupted — that
        # sends it hunting for half-done work that does not exist.
        assert "was interrupted before it finished" not in entry["content"]
        assert "pressed Continue" in entry["content"]

    @pytest.mark.asyncio
    async def test_running_subagents_are_refused(self, _patched):
        # slot.running is False here — the PARENT turn ends while its children
        # keep going — so no other guard catches this. A synthetic recovery entry
        # satisfies is_system_injection_item, so the queue's hold_users gate would
        # drain it straight through and interleave a parent turn with its own
        # children's completion injections.
        slot = _ChatSlot("s")
        slot.append("user", "spawn some agents", "msg msg-u")
        slot.append("assistant", "Spawned 2 agents, waiting…", "msg msg-a")
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["agent-1"])
        state.subagents._queued_depth = MagicMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"
        # Refused on the RUNNING child alone, with an empty queue.
        state.subagents.running_agents_for.assert_called_with("dashboard:s")
        assert not slot._queue
        _patched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subagent_probe_failure_fails_closed(self, _patched):
        # running_agents_for returning None is the probe FAILING, not "no
        # children". Treating the two alike would dispatch the very interleaved
        # turn the guard exists to prevent.
        slot = _ChatSlot("s")
        slot.append("user", "spawn some agents", "msg msg-u")
        slot.append("assistant", "Spawned 2 agents, waiting…", "msg msg-a")
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=None)
        state.subagents._queued_depth = MagicMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_queued_subagents_are_refused(self, _patched):
        # A spawn that hit the concurrency/stagger gate is deliberately NOT in
        # `_agents` (SubagentInfo.queued), so running_agents_for returns [] while
        # a child is still pending — and it WILL start on its own and write
        # concurrently with the turn this endpoint would dispatch.
        slot = _ChatSlot("s")
        slot.append("user", "spawn a wave", "msg msg-u")
        slot.append("assistant", "Spawned 8 agents.", "msg msg-a")
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        state.subagents._queued_depth = MagicMock(return_value=3)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"
        assert not slot._queue
        _patched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_born_slot_probes_its_linked_session_key(self, _patched):
        # A slot born on a channel carries the channel key, and its children
        # register under THAT. Probing "dashboard:<key>" would match nothing and
        # wave the continuation straight through.
        slot = _ChatSlot("s")
        slot.linked_session_key = "slack:1700000000.123456"
        slot.append("user", "spawn some agents", "msg msg-u")
        slot.append("assistant", "Spawned 2 agents.", "msg msg-a")
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["agent-1"])
        state.subagents._queued_depth = MagicMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"
        state.subagents.running_agents_for.assert_called_with("slack:1700000000.123456")

    @pytest.mark.asyncio
    async def test_unreadable_queue_fails_closed(self, _patched):
        # An exploding queue probe is UNKNOWN children, not zero children.
        slot = _ChatSlot("s")
        slot.append("user", "spawn a wave", "msg msg-u")
        slot.append("assistant", "Spawned some agents.", "msg msg-a")
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        state.subagents._queued_depth = MagicMock(side_effect=RuntimeError("boom"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"

    @pytest.mark.asyncio
    async def test_inflight_result_delivery_is_refused(self, _patched):
        # The last child can finish — emptying BOTH probes — while its completion
        # injection is still landing. A turn started in that window interleaves
        # with the injection and corrupts transcript order. The runner's own
        # synthesis gate pairs these two conditions for the same reason.
        slot = _ChatSlot("s")
        slot.append("user", "spawn some agents", "msg msg-u")
        slot.append("assistant", "Spawned 2 agents.", "msg msg-a")
        slot._subagent_deliveries_inflight = 1
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        state.subagents._queued_depth = MagicMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"
        assert not slot._queue
        _patched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finished_subagents_do_not_block(self, _patched):
        # The guard must not be a permanent veto on any slot that ever spawned:
        # no running children, an empty queue AND no delivery in flight is a slot
        # whose children are genuinely done.
        slot = _ChatSlot("s")
        slot.append("user", "spawn some agents", "msg msg-u")
        slot.append("assistant", "All 2 agents reported back.", "msg msg-a")
        assert slot._subagent_deliveries_inflight == 0
        state = _mock_state(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        state.subagents._queued_depth = MagicMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_brand_new_session_is_refused(self, _patched):
        state = _mock_state(_ChatSlot("s"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_empty"

    @pytest.mark.asyncio
    async def test_scaffolding_only_transcript_is_refused(self, _patched):
        # A compaction notice is assistant-ROLE but not conversation: a
        # continuation queued here would reach the model with nothing under it.
        slot = _ChatSlot("s")
        slot.append("assistant", "Auto-compacted at 80%.", "msg msg-a", meta={"kind": "compaction"})
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_empty"

    @pytest.mark.asyncio
    async def test_interrupted_turn_queues_the_continuation_and_dispatches(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "do the thing", "msg msg-u")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        # Dispatch goes through the runner's own dequeue path so the row lands as
        # an `inject` (folding into RecoveryCard) rather than a user bubble.
        _patched.assert_awaited_once()
        assert len(slot._queue) == 1
        entry = slot._queue[0]
        assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
        assert entry["content"].startswith("[Continue — requested by the user]")
        # An unanswered user row IS visible evidence, so this arm keeps the
        # resume wording.
        assert "was interrupted before it finished" in entry["content"]

    @pytest.mark.asyncio
    async def test_trailing_error_keeps_the_resume_wording(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "do the thing", "msg msg-u")
        slot.append("assistant", "starting…", "msg msg-a")
        slot.append("error", "⟳ Connection lost — please retry.", "msg msg-e")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200
        assert "was interrupted before it finished" in slot._queue[0]["content"]

    @pytest.mark.asyncio
    async def test_app_token_cannot_continue_a_foreign_slot(self, _patched):
        # Not a read: resuming dispatches an agent turn that runs tools and writes
        # to the repo, so an app token must not reach a slot it does not own. The
        # response is the same indistinguishable 404 as the send path, so it cannot
        # be used to probe which foreign slots exist.
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot._app = "other-app"
        state = _mock_state(slot)
        app = _make_app(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "attacker-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_app_token_can_continue_its_own_slot(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot._app = "my-app"
        state = _mock_state(slot)
        app = _make_app(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_mid_plan_orchestration_is_refused(self, _patched):
        # An autopilot plan reads `running` False BETWEEN stages, so `running`
        # alone would let Continue dispatch concurrently with the next stage.
        slot = _ChatSlot("s")
        slot.append("user", "hi", "msg msg-u")
        slot._in_stage_execution = True
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_orchestrating"
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_continuation_never_claims_prior_work_exists(self, _patched):
        # The runner's POSTTOKEN continuation asserts "the work already done
        # above ... is preserved". On a zero-output interruption that is false, so
        # the manual continuation must not reuse that wording.
        slot = _ChatSlot("s")
        slot.append("user", "first ever prompt", "msg msg-u")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/s/continue")
        body = slot._queue[0]["content"]
        assert "already done above" not in body
        assert "if nothing was done yet" in body
