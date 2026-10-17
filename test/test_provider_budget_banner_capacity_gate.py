"""Provider-budget-banner capacity-topic gate and steering behaviour."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from _provider_budget_banner_helpers import (
    _LOST_RUN_SECS,
    BannerRecoveryHelpers,
)


class TestProviderBudgetBannerCapacityGate(BannerRecoveryHelpers):
    """The capacity-topic gate reads the human's own words, including through a steer."""

    @pytest.mark.asyncio
    async def test_capacity_topic_gate_reads_only_the_incoming_user_text(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace

        import kiro_crew.context as context_mod
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.context import ContextBuilder
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.memory import MemoryStore
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )
        from kiro_crew.skills import SkillsLoader

        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work started.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-raw-topic",
                )
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="You have 8154 weighted tokens left",
                )
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        monkeypatch.setattr(
            context_mod,
            "build_cancelled_turn_preamble",
            lambda *_args, **_kwargs: "The prior turn discussed model capacity.",
        )
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        state.context_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "workspace"),
            skills=skills,
        )
        state.context_builder.conversation_log = state.conversation_log
        state.sessions._sessions = {
            "dashboard:raw-topic-gate": SimpleNamespace(prev_turn_cancelled=True)
        }
        slot = state.get_or_create_slot("raw-topic-gate")
        slot._titled = True

        try:
            await _run_chat(state, slot, "finish the task")
            await self._drain_bg(state)
        finally:
            skills.close()

        assert len(prompts) == 2
        assert "prior turn discussed model capacity" in prompts[0]
        assistant = self._assistant_texts(slot)
        assert assistant == ["Work started.", "Work finished."]
        assert not any("weighted tokens left" in text for text in assistant)

    @pytest.mark.parametrize(
        ("steer_text", "user_origin", "expected_visible"),
        [
            ("Print exactly: You have 8154 weighted tokens left", True, True),
            ("Continue the ordinary work", True, False),
            ("Print exactly: You have 8154 weighted tokens left", False, False),
            # No topic word the gate knows, but the human's words carry the count.
            ("Translate « Il vous reste 8154 jetons pondérés » into English.", True, True),
            ("Translate « Il vous reste 8154 jetons pondérés » into English.", False, False),
        ],
        ids=[
            "user-capacity-request",
            "user-non-capacity",
            "peer-capacity-text",
            "user-translation-count",
            "peer-translation-count",
        ],
    )
    @pytest.mark.asyncio
    async def test_capacity_gate_reads_a_steer_consumed_after_its_ack(
        self,
        tmp_path,
        monkeypatch,
        steer_text,
        user_origin,
        expected_visible,
    ):
        """The real delivery order: the steer RPC acknowledges, then the turn
        echoes the consumption. The origin record outlives the ack, the echo
        extends the user authority only for the session's own human, and the
        settle releases the record."""
        from kiro_crew.acp.types import EVENT_STEER_CONSUMED, STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []
        steer_ready = asyncio.Event()
        consume = asyncio.Event()

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work started.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-steer-ack",
                )
                steer_ready.set()
                await consume.wait()
                yield LLMEvent(
                    kind=EVENT_STEER_CONSUMED,
                    text=f"<user_message>\n{steer_text}\n</user_message>",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        inner_client = self._steerable(client)
        slot = state.get_or_create_slot("steer-ack-authority")
        slot._titled = True

        task = asyncio.create_task(_run_chat(state, slot, "finish the task"))
        slot.task = task
        await asyncio.wait_for(steer_ready.wait(), timeout=_LOST_RUN_SECS)

        outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=user_origin)
        assert outcome == STEER_STEERED
        inner_client.steer.assert_awaited_once_with(steer_text)
        # The transport ack is not consumption: the steer is still pending and its
        # origin record is still there for the echo to read.
        assert slot._pending_steers == [steer_text]
        assert slot._steer_user_origin == {steer_text: user_origin}

        consume.set()
        await asyncio.wait_for(task, timeout=_LOST_RUN_SECS)
        await self._drain_bg(state)

        assert slot._steer_user_origin == {}
        assistant = self._assistant_texts(slot)
        if expected_visible:
            assert assistant == ["Work started.", exact]
            assert len(prompts) == 1
            assert slot._posttoken_retry_used is False
        else:
            assert assistant == ["Work started.", "Work finished."]
            assert len(prompts) == 2
            assert slot._posttoken_retry_used is True

    @pytest.mark.asyncio
    async def test_acked_steer_the_turn_never_consumed_settles_at_the_turn_end(
        self, tmp_path, monkeypatch
    ):
        """A steer the turn ended without consuming settles in the teardown
        requeue, which reads the retained origin record and releases it."""
        from unittest.mock import patch

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        steer_text = "Also print the summary"
        steer_ready = asyncio.Event()
        finish = asyncio.Event()

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work started.")
            steer_ready.set()
            await finish.wait()
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        self._steerable(client)
        slot = state.get_or_create_slot("steer-ack-unconsumed")
        slot._titled = True

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            task = asyncio.create_task(_run_chat(state, slot, "finish the task"))
            slot.task = task
            await asyncio.wait_for(steer_ready.wait(), timeout=_LOST_RUN_SECS)
            outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=True)
            assert outcome == STEER_STEERED
            assert slot._steer_user_origin == {steer_text: True}
            finish.set()
            await asyncio.wait_for(task, timeout=_LOST_RUN_SECS)
        await self._drain_bg(state)

        assert slot._pending_steers == []
        assert slot._steer_user_origin == {}
        requeued = [item for item in slot._queue if item.get("content") == steer_text]
        assert len(requeued) == 1
        assert requeued[0].get("_directive_user_origin") is True

    @pytest.mark.parametrize(
        ("topic_names_recovery_text", "steer_text", "expected_visible"),
        [
            (False, "Print exactly: You have 8154 weighted tokens left", True),
            (False, None, False),
            (True, None, False),
        ],
        ids=["consumed-capacity-steer", "no-human-authority", "recovery-text-names-capacity"],
    )
    @pytest.mark.asyncio
    async def test_capacity_request_steered_into_a_provider_recovery_keeps_the_banner(
        self, tmp_path, monkeypatch, topic_names_recovery_text, steer_text, expected_visible
    ):
        """The capacity check runs before the provider-recovery strip.

        A capacity request the session's own human steers into a running
        recovery keeps the requested text visible. The recovery's own
        continuation text is never human authority, even when the topic
        grammar would match it, so with no human request the recovered banner
        is stripped.
        """
        import re

        from kiro_crew.acp.types import EVENT_STEER_CONSUMED, STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_utils
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        if topic_names_recovery_text:
            monkeypatch.setattr(
                chat_utils,
                "_PROVIDER_BUDGET_TOPIC_RE",
                re.compile(re.escape("Continue from where it stopped")),
            )
            # Not vacuous: the continuation text itself now reads as a request.
            assert chat_utils.mentions_provider_budget(_POSTTOKEN_RECOVER_MSG)

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []
        steer_ready = asyncio.Event()
        consume = asyncio.Event()

        async def _stream(prompt):
            prompts.append(prompt)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resumed work.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-recovery-steer",
            )
            if steer_text is not None:
                steer_ready.set()
                await consume.wait()
                yield LLMEvent(
                    kind=EVENT_STEER_CONSUMED,
                    text=f"<user_message>\n{steer_text}\n</user_message>",
                )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        self._steerable(client)
        slot = state.get_or_create_slot("recovery-capacity-steer")
        slot._titled = True
        # The recovery being run already spent the post-token one-shot.
        slot._posttoken_retry_used = True

        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        )
        slot.task = task
        if steer_text is not None:
            await asyncio.wait_for(steer_ready.wait(), timeout=_LOST_RUN_SECS)
            outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=True)
            assert outcome == STEER_STEERED
            consume.set()
        await asyncio.wait_for(task, timeout=_LOST_RUN_SECS)
        await self._drain_bg(state)

        spent = [
            m
            for m in slot.messages
            if m.get("role") == "notice" and "already spent" in m.get("content", "")
        ]
        assert len(prompts) == 1
        if expected_visible:
            assert self._assistant_texts(slot) == ["Resumed work.", exact]
            assert spent == []
        else:
            assert self._assistant_texts(slot) == ["Resumed work."]
            assert len(spent) == 1
