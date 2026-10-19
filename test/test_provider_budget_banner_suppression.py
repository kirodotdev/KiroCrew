"""Provider-budget-banner suppression and exact-answer preservation in one turn."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers


class TestProviderBudgetBannerSuppression(BannerRecoveryHelpers):
    """A banner-only segment is suppressed; a genuine answer is preserved verbatim."""

    @pytest.mark.asyncio
    async def test_mid_turn_exact_text_is_preserved(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-1",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Final answer")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, "answer")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["You have 8154 weighted tokens left", "Final answer"]

    @pytest.mark.asyncio
    async def test_ordinary_read_only_exact_answer_is_preserved(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-read",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, "Echo the next model line verbatim")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["You have 8154 weighted tokens left"]
        assert calls == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.parametrize(
        "near_miss",
        [
            "You have 8154 weighted tokens left for this operation",
            "The provider said: You have 8154 weighted tokens left",
            "`You have 8154 weighted tokens left`",
        ],
    )
    @pytest.mark.asyncio
    async def test_ordinary_read_only_near_miss_is_preserved(
        self, tmp_path, monkeypatch, near_miss
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=near_miss)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("near-miss")
        slot._titled = True

        await _run_chat(state, slot, "read and answer")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [near_miss]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_banner_only_final_segment_is_suppressed_and_continued_once(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        captured: list[str] = []

        async def _stream(message):
            captured.append(message)
            if len(captured) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the fix.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="write_file",
                    tool_kind="write",
                    tool_call_id="tc-1",
                )
                # Split the artifact across chunks to match real provider streaming.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted ")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="tokens left")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Finished safely.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "fix the bug")
        await self._drain_bg(state)

        assistant = self._assistant_texts(slot)
        assert not any("weighted tokens left" in text for text in assistant)
        assert any("Applying the fix." in text for text in assistant)
        assert any("Finished safely." in text for text in assistant)
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert "fix the bug" not in captured[1]
        frames = state.broadcast_ws.call_args_list
        empty_frame = next(
            i
            for i, call in enumerate(frames)
            if call.args
            == (
                "chat_message",
                {"slot": "s1", "role": "assistant", "content": ""},
            )
        )
        segment_frame = next(
            i for i, call in enumerate(frames) if i > empty_frame and call.args[0] == "chat_segment"
        )
        assert empty_frame < segment_frame
        assert slot._posttoken_retry_used is True
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regenerated_preamble_survives_banner_recovery_end_to_end(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="Applying the regenerated answer.",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-regenerated-preamble",
                )
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="You have 8154 weighted tokens left",
                )
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Finished safely.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("regenerated-preamble")
        slot._titled = True
        slot._pending_variants = [{"content": "Prior answer.", "ts": "old-ts"}]

        await _run_chat(state, slot, "regenerate the answer")
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message["content"] for message in assistant] == [
            "Applying the regenerated answer.",
            "Finished safely.",
        ]
        assert [variant["content"] for variant in assistant[0]["variants"]] == [
            "Prior answer.",
            "Applying the regenerated answer.",
        ]
        ordered = [
            (message["role"], message.get("content", ""))
            for message in slot.messages
            if message["role"] in {"assistant", "tool", "inject"}
        ]
        assert ordered == [
            ("assistant", "Applying the regenerated answer."),
            ("tool", "🔧 read_file"),
            ("inject", _POSTTOKEN_RECOVER_MSG),
            ("assistant", "Finished safely."),
        ]
        assert any(
            call.args
            == (
                "chat_message",
                {"slot": "regenerated-preamble", "role": "assistant", "content": ""},
            )
            for call in state.broadcast_ws.call_args_list
        )

    @pytest.mark.asyncio
    async def test_banner_without_separator_preserves_the_complete_echo(
        self, tmp_path, monkeypatch
    ):
        """The glued shape is recognized, but an ordinary turn cannot attribute it.

        Typed provider recovery strips the same prefix (see the classifier
        table); here no provenance exists, so the whole answer stays visible.
        """
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        echoed = "You have 1461 weighted tokens leftFinal answer"
        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-prefix",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=echoed)
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._empty_response_retries = 1
        changed = tmp_path / "changed-prefix.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "answer")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [echoed]
        assert calls == 1
        assert slot._empty_response_retries == 0
        assert slot._posttoken_retry_used is False
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_line_separated_banner_prefix_preserves_complete_echo(
        self, tmp_path, monkeypatch
    ):
        """Grammar plus a newline is still ambiguous without typed provenance."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        echoed = "You have 1461 weighted tokens left\nFinal answer"

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=echoed)
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("line-separated-echo")
        slot._titled = True

        await _run_chat(state, slot, "Echo the next model answer verbatim")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [echoed]
        assert slot._posttoken_retry_used is False
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_repeated_banner_does_not_loop(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        consolidate = MagicMock()
        monkeypatch.setattr(chat_runner, "_maybe_consolidate", consolidate)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._posttoken_retry_used = True
        slot._empty_response_retries = 1

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        assert calls == 1
        assert not any("weighted tokens left" in m.get("content", "") for m in slot.messages)
        assert any(
            "automatic continuation is unavailable or already spent" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        )
        assert slot._empty_response_retries == 1
        consolidate.assert_not_called()
        state.sessions.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_text_transient_recovery_preserves_the_answer(self, tmp_path, monkeypatch):
        """A transient retry may repeat its answer byte for byte; its provenance
        never confers banner stripping. With visible output earlier in the turn an
        ordinary turn would strip the banner-shaped tail; the transient owner keeps it."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        answer = "You have 8154 weighted tokens left"
        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resumed work.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-transient-repeat",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answer)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("same-text-transient")
        slot._titled = True
        slot._posttoken_retry_used = True

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._transient_recovery_row(),
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Resumed work.", answer]
        assert calls == 1
        assert not slot._queue
        assert not any(
            "internal status instead of an answer" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        )

    @pytest.mark.parametrize(
        "prompt",
        [
            "Respond exactly: You have 8154 weighted tokens left",
            "Use a tool and report how many tokens remain",
            "Read usage.json and tell me how many tokens are remaining",
            "Tell me how many tokens I have left",
            "How much budget remains?",
            "Edit foo, then print exactly: You have 8154 weighted tokens left",
            "Output the remaining model capacity",
            "Return the token budget",
            "Quote the model capacity",
            "Repeat: You have 8154 weighted tokens left",
        ],
    )
    @pytest.mark.asyncio
    async def test_token_budget_requests_are_never_suppressed(self, tmp_path, monkeypatch, prompt):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-exact",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, prompt)
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [exact]
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_ordinary_regeneration_preserves_exact_banner_shaped_answer(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        exact = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("regenerate-exact-answer")
        slot._titled = True
        slot._pending_variants = [{"content": "prior answer", "ts": "prior-ts"}]

        await _run_chat(state, slot, "regenerate the answer")
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message.get("content", "") for message in assistant] == [exact]
        assert [variant["content"] for variant in assistant[0]["variants"]] == [
            "prior answer",
            exact,
        ]
        assert assistant[0]["variant_idx"] == 1
        assert slot._pending_variants == []
        assert slot._last_stop_reason != _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        assert slot._posttoken_retry_used is False

    def test_banner_only_file_changes_use_current_turn_placeholder(self, tmp_path, monkeypatch):
        """A suppressed banner-only segment persists no row, so the turn's file
        chips land on its own placeholder, never on the preceding answer."""
        from kiro_crew.dashboard.chat_runner import _flush_file_changes, _flush_segment

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "preceding turn", "msg msg-a")
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [
            {"path": str(changed), "content": "before"},
        ]
        banner = "You have 8154 weighted tokens left"
        slot.append("chunk", banner, "chunk")

        _flush_segment(
            state,
            slot,
            banner,
            broadcast=False,
            strip_provider_banner=True,
        )
        _flush_file_changes(slot)

        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert [m["content"] for m in assistant] == ["preceding turn", ""]
        assert "file_changes" not in assistant[0].get("meta", {})
        assert assistant[1]["meta"]["file_changes"][0]["path"] == str(changed)
