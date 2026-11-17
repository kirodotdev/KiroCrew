"""Merging a fork back into its parent: how a parent turn's outcome settles the card.

Split from ``test_chat_merge_back_merge.py``; the shared fixtures and helpers are
in ``chat_merge_back_helpers.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_merge_back_helpers import (  # noqa: F401
    _client,
    _draft,
    _merge,
    _run_turn,
    chats_fixture,
    model_fixture,
)

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.testing.wait import await_until


class TestMergeTurnOutcome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["error", "cancel"])
    async def test_an_uncompleted_turn_returns_merge_context_and_keeps_its_hold(
        self, chats, model, outcome
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "retry summary", draft)
        assert status == 200, body
        [context] = chats.parent._pending_context
        note_id = context["noteId"]

        await _run_turn(state, chats.parent, "continue", outcome=outcome)

        assert [entry["content"] for entry in chats.parent._pending_context] == ["retry summary"]
        assert note_id not in state._consumed_merge_cards
        state.flush_slot_now(chats.parent)
        assert state.conversation_log.get_metadata(key).get("deferred_notes")

    @staticmethod
    async def _merged_card(chats, summary: str) -> str:
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, summary, draft)
        assert status == 200, body
        [context] = chats.parent._pending_context
        return context["noteId"]

    @staticmethod
    def _kept_reply(*events_before_end):
        """A reply kiro-cli keeps: *events_before_end*, then a normal ``end_turn``."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def events():
            for event in events_before_end:
                yield event
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        return events

    @staticmethod
    def _text(text: str):
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)

    async def _assert_card_reached_the_model_once(
        self, chats, summary: str, note_id: str, first_turn: list[str]
    ) -> None:
        """The failed turn sent the frame; the next turn does not send it again."""
        state = chats.state
        assert first_turn[0].count(summary) == 1
        assert chats.parent._pending_context == []
        following = await _run_turn(state, chats.parent, "and then")
        assert summary not in following[0]
        state.flush_slot_now(chats.parent)
        key = slot_history_key(chats.parent)
        held = state.conversation_log.get_metadata(key).get("deferred_notes") or []
        held_ids = {entry.get("id") for entry in held}
        assert note_id not in held_ids
        assert not held_ids

    @pytest.mark.asyncio
    async def test_a_promise_only_turn_retires_the_card_its_prompt_carried(self, chats, model):
        """The continuation runs on the session that already holds the frame."""
        note_id = await self._merged_card(chats, "promise summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(self._text("I'll go ahead and run the gate now.")),
        )
        assert chats.parent._promise_only_retries == 1
        await self._assert_card_reached_the_model_once(chats, "promise summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_promise_only_turn_under_auto_approve_retires_the_card(self, chats, model):
        """No continuation is queued, so the next USER turn must not re-send the frame."""
        note_id = await self._merged_card(chats, "yolo promise summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(self._text("I'll go ahead and run the gate now.")),
            yolo=True,
        )
        assert any(
            "Auto-continue is skipped" in row.get("content", "")
            for row in chats.parent.messages
            if row.get("role") == "notice"
        )
        await self._assert_card_reached_the_model_once(
            chats, "yolo promise summary", note_id, first
        )

    @pytest.mark.asyncio
    async def test_a_leaked_tool_call_turn_retires_the_card_its_prompt_carried(self, chats, model):
        leak = (
            "call <" + 'invoke name="spawn_run"> <' + 'parameter name="task">reconcile</'
            "parameter> </" + "invoke>"
        )
        note_id = await self._merged_card(chats, "leak summary")
        first = await _run_turn(
            chats.state, chats.parent, "continue", events=self._kept_reply(self._text(leak))
        )
        assert any(
            "wrote a tool call as plain text" in row.get("content", "")
            for row in chats.parent.messages
            if row.get("role") == "notice"
        )
        await self._assert_card_reached_the_model_once(chats, "leak summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_turn_cut_by_a_completed_compaction_retires_the_card(self, chats, model):
        """The summarized session was built from a prompt that carried the frame."""
        from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, LLMEvent

        note_id = await self._merged_card(chats, "compaction summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(
                LLMEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
                LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"),
            ),
        )
        assert chats.parent._compaction_continue_retries == 1
        await self._assert_card_reached_the_model_once(chats, "compaction summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_stop_after_the_first_token_returns_the_card_for_the_next_turn(
        self, chats, model
    ):
        """kiro-cli discards a cancelled prompt, so the frame goes out once more, and only once."""
        state = chats.state
        note_id = await self._merged_card(chats, "stopped summary")
        streamed = asyncio.Event()

        async def events():
            yield self._text("partial")
            streamed.set()
            await await_until(asyncio.Future(), what="the cancel that ends this stub")

        stopped = await _run_turn(
            state, chats.parent, "continue", events=events, cancel_when=streamed
        )
        assert stopped[0].count("stopped summary") == 1
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards

        resent = await _run_turn(state, chats.parent, "continue")
        assert resent[0].count("stopped summary") == 1
        assert chats.parent._pending_context == []
        later = await _run_turn(state, chats.parent, "and then")
        assert "stopped summary" not in later[0]

    @pytest.mark.asyncio
    async def test_empty_response_give_up_returns_the_card_for_the_next_turn(self, chats, model):
        """An unanswered prompt is not a delivery even when recovery gives up."""
        state = chats.state
        note_id = await self._merged_card(chats, "empty response summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=self._kept_reply(),
            queued_events=self._kept_reply(),
        )

        assert len(prompts) == 3
        assert all(prompt.count("empty response summary") == 1 for prompt in prompts)
        # The queued verbatim replay and empty-response continuation are the
        # episode's second and third attempts.
        assert "[Empty response — automatic recovery]" not in prompts[1]
        assert "[Empty response — automatic recovery]" in prompts[2]

        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert chats.parent._inflight_merge_contexts == []
        assert note_id not in state._consumed_merge_cards
        hold = state.conversation_log._read_metadata(slot_history_key(chats.parent)).get(
            "deferred_notes"
        )
        assert [entry.get("id") for entry in hold] == [note_id]
        assert hold[0].get("consumed") is not True

        resent = await _run_turn(state, chats.parent, "continue")
        assert resent[0].count("empty response summary") == 1
        assert chats.parent._pending_context == []
        later = await _run_turn(state, chats.parent, "and then")
        assert "empty response summary" not in later[0]

    @pytest.mark.asyncio
    async def test_an_empty_continuation_after_productive_work_returns_the_card(self, chats, model):
        """A give-up reads only its own attempt, so the card comes back once."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        state = chats.state
        note_id = await self._merged_card(chats, "productive empty summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="read-0",
                ),
            ),
            queued_events=self._kept_reply(),
        )

        assert len(prompts) == 2
        assert prompts[0].count("productive empty summary") == 1
        assert prompts[1].count("productive empty summary") == 1
        assert "[Empty response — automatic recovery]" in prompts[1]
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        following = await _run_turn(state, chats.parent, "and then")
        assert following[0].count("productive empty summary") == 1
        later = await _run_turn(state, chats.parent, "and later")
        assert "productive empty summary" not in later[0]

    @pytest.mark.asyncio
    async def test_a_productive_empty_give_up_consumes_the_card(self, chats, model):
        """Work in the attempt that gives up answers its prompt, so the card is spent."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        def productive_empty_reply(call_id):
            return self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id=call_id,
                ),
            )

        state = chats.state
        note_id = await self._merged_card(chats, "productive give-up summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=productive_empty_reply("read-first"),
            queued_events=productive_empty_reply("read-second"),
        )

        assert len(prompts) == 2
        assert prompts[1].count("productive give-up summary") == 1
        assert chats.parent._pending_context == []
        assert state._consumed_merge_cards[note_id] == (
            slot_history_key(chats.parent),
            True,
        )
        following = await _run_turn(state, chats.parent, "and then")
        assert "productive give-up summary" not in following[0]

    @pytest.mark.asyncio
    async def test_stale_episode_productivity_does_not_consume_an_app_turn_card(self, chats, model):
        """A discarded recovery gives an unrelated synthetic turn no productivity."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        state = chats.state
        await _run_turn(
            state,
            chats.parent,
            "do some work",
            events=self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="read-stale",
                ),
            ),
        )
        assert chats.parent._empty_episode_productive is True
        assert chats.parent._queue
        # A hard Stop takes and clears the queued continuation before settling
        # it; clearing here reproduces that slot-state effect without stopping
        # the test's mocked provider session.
        chats.parent._queue.clear()

        note_id = await self._merged_card(chats, "app turn summary")
        prompts = await _run_turn(
            state,
            chats.parent,
            "[MCP app turn]",
            events=self._kept_reply(),
            synthetic_payload=True,
        )

        assert prompts[0].count("app turn summary") == 1
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert chats.parent._inflight_merge_contexts == []
        assert note_id not in state._consumed_merge_cards
