"""A steer the owner types over an open approval card answers it with Reject.

Without that, the runner stays suspended on the approval future, the steer is
held for an inference boundary the turn never reaches, and the person who typed
a new instruction gets no reply until the card is clicked or Stop is pressed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
from kiro_crew.dashboard.steer_supersedes_approval import (
    SUPERSEDED_REJECTION_REASON,
    SUPERSEDED_SEL_OUTCOME,
    reject_approvals_superseded_by_steer,
)


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch("kiro_crew.sel.sel", return_value=mock_sel),
    ):
        yield mock_sel


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    slot._acp_client = client
    return slot


def _open_approval(slot, request_id="req-1"):
    fut = asyncio.get_running_loop().create_future()
    slot.register_approval(request_id, fut, {})
    return fut


def _resolved_frames(state):
    return [
        c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "approval_resolved"
    ]


class TestComposerSteerAnswersTheCard:
    @pytest.mark.asyncio
    async def test_owner_steer_rejects_the_open_approval(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        fut = _open_approval(slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "do X instead", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._acp_client.steer.assert_awaited_once_with("do X instead")
        assert fut.done() and fut.result() == "rejected"
        assert slot.take_rejection_reason("req-1", fut) == SUPERSEDED_REJECTION_REASON
        frames = _resolved_frames(state)
        assert len(frames) == 1
        assert frames[0]["id"] == "req-1"
        assert frames[0]["approved"] is False
        assert frames[0]["slot"] == "test"

    @pytest.mark.asyncio
    async def test_steer_lands_before_the_rejection(self, tmp_path, monkeypatch, _patch_sel):
        """The backend must hold the steer when the rejection wakes the runner."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        fut = _open_approval(slot)
        seen_pending_during_rpc: list[bool] = []

        async def _steer(_text):
            seen_pending_during_rpc.append(not fut.done())
            return True

        slot._acp_client.steer = AsyncMock(side_effect=_steer)
        outcome = await steer_into_running_turn(
            state, slot, "stop that", user_origin=True, owner_origin=True
        )

        assert outcome == STEER_STEERED
        assert seen_pending_during_rpc == [True]
        assert fut.result() == "rejected"


class TestOnlyTheOwnerAnswersTheCard:
    @pytest.mark.asyncio
    async def test_peer_steer_leaves_the_card_open(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_delivery.live.snapshot",
            lambda: MagicMock(agent=MagicMock(peer_steer_loss_recovery=True)),
        )
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client.steer_needs_loss_recovery = False
        fut = _open_approval(slot)

        outcome = await steer_into_running_turn(state, slot, "from a peer")

        assert outcome == STEER_STEERED
        assert not fut.done()
        assert _resolved_frames(state) == []
        fut.cancel()

    @pytest.mark.asyncio
    async def test_non_owner_person_steer_leaves_the_card_open(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A Shared Crew teammate types as a person but cannot decide approvals."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        fut = _open_approval(slot)

        outcome = await steer_into_running_turn(
            state, slot, "teammate here", user_origin=True, owner_origin=False
        )

        assert outcome == STEER_STEERED
        assert not fut.done()
        fut.cancel()


class TestRejectHelper:
    @pytest.mark.asyncio
    async def test_rejects_every_open_card_and_skips_answered_ones(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        open_a = _open_approval(slot, "a")
        open_b = _open_approval(slot, "b")
        answered = _open_approval(slot, "c")
        answered.set_result("approved")

        reject_approvals_superseded_by_steer(state, slot)

        assert open_a.result() == "rejected"
        assert open_b.result() == "rejected"
        assert answered.result() == "approved"
        assert sorted(f["id"] for f in _resolved_frames(state)) == ["a", "b"]
        outcomes = {c.kwargs["outcome"] for c in _patch_sel.log_tool_invocation.call_args_list}
        assert outcomes == {SUPERSEDED_SEL_OUTCOME}

    @pytest.mark.asyncio
    async def test_no_open_card_is_a_no_op(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")

        reject_approvals_superseded_by_steer(state, slot)
        assert _resolved_frames(state) == []

    @pytest.mark.asyncio
    async def test_settled_permission_pushes_the_board_once(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The runner skips its push for a row already resolved, so the helper pushes."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        state.push_slots_update = MagicMock()

        reject_approvals_superseded_by_steer(state, slot)
        state.push_slots_update.assert_not_called()

        _open_approval(slot, "a")
        _open_approval(slot, "b")
        reject_approvals_superseded_by_steer(state, slot)
        state.push_slots_update.assert_called_once_with()


def _open_elicitation(slot, request_id="ask-1"):
    """An open MCP question card, registered the way ``elicitation_prompt`` does it."""
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, "tool_call_id": "", "message": "Which branch?"}
    row = slot.append("elicitation", "Which branch?", json.dumps(meta))
    slot.register_approval(request_id, fut, row)
    return fut


def _open_permission_with_row(slot, request_id="req-1"):
    """An open permission card whose row carries an id, as ``permission_prompt`` registers it."""
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Run touch?", "{}")
    slot.register_approval(request_id, fut, row)
    return fut


class TestElicitationCardsAreCancelled:
    @pytest.mark.asyncio
    async def test_question_card_is_cancelled_not_declined(self, tmp_path, monkeypatch, _patch_sel):
        """Moving on is MCP's cancel; decline would tell the server the person refused."""
        from kiro_crew.acp.elicitation import ELICITATION_CANCEL

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        fut = _open_elicitation(slot)

        reject_approvals_superseded_by_steer(state, slot)

        assert fut.result() == ELICITATION_CANCEL
        # The elicitation runner owns its row, its frame and its audit line.
        assert _resolved_frames(state) == []
        assert slot.take_rejection_reason("ask-1", fut) == ""
        assert _patch_sel.log_tool_invocation.call_count == 0

    def test_cancel_reaches_the_server_as_cancel(self):
        """Pins the mapping the settlement relies on: ``cancel`` is not a decline."""
        from kiro_crew.acp.elicitation import ELICITATION_CANCEL
        from kiro_crew.dashboard.chat_turn.elicitation_prompt import read_elicitation_answer

        assert read_elicitation_answer(ELICITATION_CANCEL, {}) == (ELICITATION_CANCEL, None)

    @pytest.mark.asyncio
    async def test_mixed_cards_each_get_their_own_answer(self, tmp_path, monkeypatch, _patch_sel):
        from kiro_crew.acp.elicitation import ELICITATION_CANCEL

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        permission = _open_permission_with_row(slot, "req-1")
        question = _open_elicitation(slot, "ask-1")

        reject_approvals_superseded_by_steer(state, slot)

        assert permission.result() == "rejected"
        assert question.result() == ELICITATION_CANCEL
        assert [f["id"] for f in _resolved_frames(state)] == ["req-1"]
        assert _resolved_frames(state)[0].get("mid")


class TestOtherCardShapes:
    def test_deny_override_card_refuses_on_rejected(self):
        """An override card is answered by the same future; a steer must never grant it."""
        from kiro_crew.dashboard.deny_override import override_decision

        assert override_decision("rejected") == ("rejected", "")


class TestConsumedSteerLeavesLaterCardsAlone:
    @pytest.mark.asyncio
    async def test_steer_the_turn_already_read_rejects_nothing(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A card open after the agent read the steer was raised after it, not under it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        fut = _open_approval(slot)
        slot._steer_confirmed = set()

        async def _consumed(text):
            # What the settle path does when kiro-cli echoes steering_consumed.
            slot._pending_steers.remove(text)
            slot._steer_confirmed.add("did-1")
            return True

        slot._acp_client.steer = AsyncMock(side_effect=_consumed)
        outcome = await steer_into_running_turn(
            state, slot, "read already", user_origin=True, owner_origin=True, delivery_id="did-1"
        )

        assert outcome == STEER_STEERED
        assert not fut.done()
        assert _resolved_frames(state) == []
        fut.cancel()


class TestSteerRejectionCascadesThroughItsBatch:
    """One steer settles a whole batch, not only the card open when it was sent.

    Calls of one model response are prompted one at a time, so only the first card
    is open when the steer lands. The rest are refused by the batch rejection the
    first rejection starts (``slot._batch_rejected``), never by a second steer.
    """

    @pytest.mark.asyncio
    async def test_a_steer_rejection_cascades_through_the_rest_of_its_batch(
        self, tmp_path, _patch_sel
    ):
        from dashboard_approval_helpers import (
            _ANSWER_WAIT_SECS,
            _complete_event,
            _context_builder,
            _drain,
            _make_slot,
        )
        from dashboard_approval_helpers import _make_state as _make_approval_state
        from dashboard_approval_helpers import (
            _patch_stats,
            _permission_event,
            _set_stream,
        )

        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.state import parse_cls_meta
        from kiro_crew.testing.wait import async_wait_until

        state, client = _make_approval_state(tmp_path, context_builder=_context_builder())
        # Bounds a broken cascade: req-2 would wait out this window, expire as a host
        # decline and steer its notice, failing the attribution assert below instead
        # of parking the test on the attended window. The same bound the steer below
        # waits on, so a slow runner cannot expire req-1 before the steer settles it.
        state.approval_timeout_for = MagicMock(return_value=_ANSWER_WAIT_SECS)
        slot = _make_slot()
        events = []
        for i in (1, 2, 3):
            evt = _permission_event(title=f"tool_{i}")
            evt.request_id = f"req-{i}"
            evt.tool_call_id = f"tc-{i}"
            events.append(evt)
        _set_stream(client, [*events, _complete_event()])
        settled: list[list[str]] = []

        async def _steer_over_the_first_card() -> None:
            await async_wait_until(
                lambda: "req-1" in slot._approval_futures,
                describe=lambda: "the first card of the batch never opened",
            )
            settled.append([r for r, f in slot._approval_futures.items() if not f.done()])
            reject_approvals_superseded_by_steer(state, slot)

        steerer = asyncio.get_running_loop().create_task(_steer_over_the_first_card())
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(steerer)

        # The steer found exactly one card open; the batch rule refused the rest.
        assert settled == [["req-1"]]
        assert [c.args[0] for c in client.reject_tool.call_args_list] == [
            "req-1",
            "req-2",
            "req-3",
        ]
        cards = {
            (parse_cls_meta(row["cls"]) or {})
            .get("approval_id"): (parse_cls_meta(row["cls"]) or {})
            .get("resolved")
            for row in slot.messages
            if row.get("role") == "permission"
        }
        assert cards == {"req-1": "rejected", "req-2": "rejected", "req-3": "rejected"}
        # The person moved on, so the cascade keeps the person's attribution: no host
        # notice is steered for the cascaded remainder.
        client.steer.assert_not_called()
        assert slot._batch_rejected is False
