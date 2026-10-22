"""Provider-budget-banner echo visibility across the transcript and tool results."""

from __future__ import annotations

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers


class TestProviderBudgetBannerEchoVisibility(BannerRecoveryHelpers):
    """A banner-shaped line that echoes earlier content stays visible, not suppressed."""

    @pytest.mark.parametrize(
        ("file_body", "expected_visible"),
        [
            ("You have 8154 weighted tokens left", True),
            # The file groups the digits and the person asks for the line
            # without commas, or the file is lower-case and the person asks for a
            # capital: the reply carries the file's number either way.
            ("notes\nYou have 8,154 weighted tokens left", True),
            ("you have 8154 weighted tokens left", True),
            ("print('hello')\n", False),
        ],
        ids=["file-echo", "reformatted-echo", "capitalized-echo", "no-banner-in-results"],
    )
    @pytest.mark.asyncio
    async def test_banner_tail_after_narration_reads_this_turns_tool_results(
        self, tmp_path, monkeypatch, file_body, expected_visible
    ):
        """Prior narration alone does not make a banner-only tail metadata.

        The narration is flushed at the read's tool boundary, then the model
        repeats the file. When a tool result this turn delivered carries the
        banner's number, however the digits are grouped and whatever the case or
        wording around it, the tail is the requested echo and stays visible; with
        no such result it is still stripped and continued once. The prompt names
        no topic word, so the capacity gate does not fail open here.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading response.txt.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-file-echo",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-file-echo",
                    tool_output=file_body,
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("tool-result-echo")
        slot._titled = True

        await _run_chat(
            state, slot, "Read response.txt and repeat its last line, capitalized, no commas"
        )
        await self._drain_bg(state)

        if expected_visible:
            assert self._assistant_texts(slot) == ["Reading response.txt.", exact]
            assert len(prompts) == 1
            assert slot._posttoken_retry_used is False
        else:
            assert self._assistant_texts(slot) == ["Reading response.txt.", "Work finished."]
            assert len(prompts) == 2
            assert slot._posttoken_retry_used is True
        tool_rows = [message for message in slot.messages if message.get("role") == "tool"]
        assert tool_rows
        assert all(message["meta"].get("outputTruncated") is False for message in tool_rows)

    @pytest.mark.parametrize(
        ("prompt_body", "expected_visible"),
        [
            ("Translate « Il vous reste 8154 jetons pondérés » into English.", True),
            ("Translate « Bonjour tout le monde » into English.", False),
        ],
        ids=["prompt-carries-count", "prompt-without-count"],
    )
    @pytest.mark.asyncio
    async def test_an_expanded_prompt_carrying_the_count_keeps_its_answer_visible(
        self, tmp_path, monkeypatch, prompt_body, expected_visible
    ):
        """The model reads the ``@prompt`` body, not the mention the person typed.

        The mention names no count and no topic word, so only the expanded body can
        explain a banner-shaped answer: with the count in it the answer stays
        visible, and a body without it leaves the ordinary-tail strip in place.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _expand(_message, _state, _slot, **_kwargs):
            return prompt_body, "ok"

        monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _expand)

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Translating.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-prompt-echo",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("prompt-body-echo")
        slot._titled = True

        await _run_chat(state, slot, "@translate-line")
        await self._drain_bg(state)

        assert prompt_body in prompts[0]
        if expected_visible:
            assert self._assistant_texts(slot) == ["Translating.", exact]
            assert len(prompts) == 1
            assert slot._posttoken_retry_used is False
        else:
            assert self._assistant_texts(slot) == ["Translating.", "Work finished."]
            assert len(prompts) == 2
            assert slot._posttoken_retry_used is True

    @pytest.mark.asyncio
    async def test_repeating_an_earlier_answer_after_a_read_stays_visible(
        self, tmp_path, monkeypatch
    ):
        """A request to repeat a previous answer is an echo of the transcript.

        An earlier turn's answer in the banner's shape stayed visible (the human
        had asked about tokens then). This turn names no topic word, reads a file
        without the count, and the model repeats the earlier answer as its whole
        final segment: the count is in an earlier assistant row, so the tail
        stays visible and nothing is re-queued.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Checking README.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-readme",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-readme",
                    tool_output="# README\nno figures here",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("repeat-earlier-answer")
        slot._titled = True
        slot.messages.append(
            {"role": "user", "content": "How many tokens do I have left?", "ts": "t-1", "cls": ""}
        )
        slot.messages.append({"role": "assistant", "content": exact, "ts": "t-2", "cls": ""})

        await _run_chat(state, slot, "Check README, then repeat your previous answer exactly")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [exact, "Checking README.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.parametrize(
        "earlier_row",
        [
            {"role": "user", "content": "Here is the line: You have 8154 weighted tokens left"},
            {
                "role": "tool",
                "content": "🔧 read_file",
                "meta": {"output": "status: You have 8,154 weighted tokens left"},
            },
        ],
        ids=["earlier-human-message", "earlier-tool-output"],
    )
    @pytest.mark.asyncio
    async def test_repeating_a_line_from_any_earlier_row_stays_visible(
        self, tmp_path, monkeypatch, earlier_row
    ):
        """Every earlier row is transcript evidence, whoever wrote it.

        The human supplied the line in an earlier message, or an earlier tool
        returned it; this turn edits a file and the model prints the line as its
        whole final segment. The count is in a row the turn can see, so the tail
        stays visible and nothing is re-queued.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Updating README.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="write_file",
                    tool_kind="edit",
                    tool_call_id="tc-edit",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-edit",
                    tool_output="README.md updated",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("repeat-earlier-row")
        slot._titled = True
        slot.messages.append({**earlier_row, "ts": "t-1", "cls": ""})
        slot.messages.append({"role": "assistant", "content": "Noted.", "ts": "t-2", "cls": ""})

        await _run_chat(state, slot, "Update README, then print the exact line I gave you earlier")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Noted.", "Updating README.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.parametrize(
        "prompt",
        [
            "Read usage.json and tell me how many tokens are remaining",
            "Read usage.json and tell me how many tokens I have left",
            "Read usage.json and tell me how many tokens are still available",
            "Read usage.json and tell me how many tokens I can still use",
            "Read usage.json and tell me how many more tokens can I use?",
            "Read usage.json and tell me how many tokens have I got left",
            "Read usage.json and tell me how many tokens\nare left",
            "Read usage.json and tell me the number of tokens I have left",
        ],
    )
    @pytest.mark.asyncio
    async def test_remaining_token_question_keeps_its_answer_after_narration_and_a_read(
        self, tmp_path, monkeypatch, prompt
    ):
        """An ordinary remaining-token question keeps a banner-shaped answer.

        Narration is flushed at the read's tool boundary, the file holds only a
        number (no banner phrase for the echo check to find), and the model
        answers in the provider's own grammar. Before the question wordings
        were admitted, the ordinary-tail rule discarded that answer without
        persisting it and queued a continuation.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading usage.json.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-usage",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-usage",
                    tool_output='{"remaining": 8154}',
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("remaining-token-question")
        slot._titled = True

        await _run_chat(state, slot, prompt)
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Reading usage.json.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_prompt_image_keeps_a_banner_shaped_transcription_visible(
        self, tmp_path, monkeypatch
    ):
        """Image pixels are echo evidence the text classifier cannot inspect."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading the screenshot.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="inspect_image",
                    tool_kind="read",
                    tool_call_id="tc-image",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-image",
                    tool_output="image loaded",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        image = tmp_path / "prompt.png"
        image.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
            )
        )
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("prompt-image-echo")
        slot._titled = True

        await _run_chat(
            state,
            slot,
            "Transcribe the sentence in the screenshot",
            _attachments=(str(image),),
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Reading the screenshot.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.parametrize("carried_as", ["attachment", "path-in-text"])
    @pytest.mark.asyncio
    async def test_an_earlier_prompt_image_keeps_a_later_transcription_visible(
        self, tmp_path, monkeypatch, carried_as
    ):
        """The model's conversation keeps an earlier turn's prompt image, so its
        pixels stay evidence the text classifier cannot read on every later turn,
        whether the human attached the image or typed its path."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading the notes.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-notes",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-notes",
                    tool_output="no figures here",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        image = tmp_path / "earlier.png"
        image.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
            )
        )
        if carried_as == "attachment":
            earlier = {
                "role": "user",
                "content": "Here is a screenshot",
                "meta": {"files": [str(image)]},
            }
        else:
            earlier = {"role": "user", "content": f"Here is a screenshot: {image}", "meta": {}}
        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("earlier-prompt-image-echo")
        slot._titled = True
        slot.messages.append({**earlier, "ts": "t-1", "cls": ""})
        slot.messages.append({"role": "assistant", "content": "Got it.", "ts": "t-2", "cls": ""})

        await _run_chat(
            state, slot, "Read the notes, then transcribe the sentence in that screenshot"
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Got it.", "Reading the notes.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.parametrize("incomplete_history", ["legacy-tool-row", "omitted-prefix"])
    @pytest.mark.asyncio
    async def test_unknown_historical_echo_evidence_keeps_the_banner_visible(
        self, tmp_path, monkeypatch, incomplete_history
    ):
        """Missing history cannot prove that the banner count was absent."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Checking the build.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="execute_bash",
                    tool_kind="execute",
                    tool_call_id="tc-history",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-history",
                    tool_output="build ok",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot(f"incomplete-history-{incomplete_history}")
        slot._titled = True
        if incomplete_history == "legacy-tool-row":
            slot.messages.append(
                {
                    "role": "tool",
                    "content": "🔧 read_file",
                    "ts": "legacy-tool",
                    "cls": "msg msg-tool",
                    "meta": {"output": "x" * 8000},
                }
            )
        else:
            # The restore owner records how many transcript rows precede the
            # in-memory 500-row window. That prefix may hold the echoed count.
            slot._disk_older_count = 1

        await _run_chat(state, slot, "Check the build, then repeat the earlier line")
        await self._drain_bg(state)

        assert self._assistant_texts(slot)[-2:] == ["Checking the build.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_banner_tail_after_a_cut_read_stays_visible(self, tmp_path, monkeypatch):
        """A truncated result must not discard a requested echo.

        The file's banner-shaped last line sits past the transport's display
        bound, so the result text the runner holds lacks the phrase. The parser
        says so (``tool_output_truncated``), the turn remembers it, and the
        ordinary-tail rule stands down: the echo stays visible, nothing is
        re-queued. The same stream with a whole result still strips (the
        ``no-banner-in-results`` case above).
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Reading response.txt.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-cut-echo",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-cut-echo",
                    # The head the bound kept; the banner line was past it.
                    tool_output="x" * 8000,
                    tool_output_truncated=True,
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("cut-result-echo")
        slot._titled = True

        await _run_chat(state, slot, "Read response.txt and repeat its last line verbatim")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Reading response.txt.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False
        # The row remembers the cut for later turns' evidence checks.
        tool_rows = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_rows and all(m["meta"].get("outputTruncated") is True for m in tool_rows)

    @pytest.mark.asyncio
    async def test_banner_echoed_from_stderr_stays_visible(self, tmp_path, monkeypatch):
        """A result shown as stdout alone is not whole evidence.

        The shell envelope carries the banner phrase in ``stderr``; the row text
        is ``stdout`` only, so the echo check cannot see the phrase. The event
        here is what the production chain produces for that envelope (the
        dispatch builder, then the provider's translation), not a hand-set flag:
        the builder marks the text as not whole, the turn remembers it, and the
        requested stderr echo stays visible with nothing re-queued.
        """
        from kiro_crew.acp import _dispatch as acp_dispatch
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        built = acp_dispatch._build_tool_result_event(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-stderr-echo",
                "status": "completed",
                "rawOutput": {
                    "items": [
                        {
                            "Json": {
                                "exit_status": "exit status: 0",
                                "stdout": "OK\n",
                                "stderr": exact,
                            }
                        }
                    ]
                },
            }
        )
        assert built is not None
        result = AcpProvider._to_llm_event(built)
        assert result.tool_output == "OK\n"
        assert result.tool_output_truncated is True
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Running check.py.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="execute_bash",
                    tool_kind="execute",
                    tool_call_id="tc-stderr-echo",
                )
                yield result
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("stderr-result-echo")
        slot._titled = True

        await _run_chat(state, slot, "Run check.py and repeat its stderr verbatim")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Running check.py.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_recovery_turn_keeps_a_banner_shaped_echo_when_its_evidence_is_not_whole(
        self, tmp_path, monkeypatch
    ):
        """The typed-recovery strip stands down on evidence the turn could not read.

        A provider-budget recovery runs a tool whose stdout is shown alone while
        its stderr carries the banner; the parser marks the result not whole, and
        the recovery's provenance does not outrank that: the reply in the
        banner's shape may be the stderr echo the request asked for, so it stays
        visible and nothing is re-queued.
        """
        from kiro_crew.acp import _dispatch as acp_dispatch
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        built = acp_dispatch._build_tool_result_event(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-recovery-stderr",
                "status": "completed",
                "rawOutput": {
                    "items": [
                        {
                            "Json": {
                                "exit_status": "exit status: 0",
                                "stdout": "OK\n",
                                "stderr": exact,
                            }
                        }
                    ]
                },
            }
        )
        assert built is not None
        result = AcpProvider._to_llm_event(built)
        assert result.tool_output_truncated is True
        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resuming: running check.py.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="execute_bash",
                tool_kind="execute",
                tool_call_id="tc-recovery-stderr",
            )
            yield result
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("recovery-stderr-echo")
        slot._titled = True
        slot._posttoken_retry_used = True

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Resuming: running check.py.", exact]
        assert calls == 1
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_an_earlier_cut_tool_row_keeps_a_later_echo_visible(self, tmp_path, monkeypatch):
        """A not-whole result in an earlier turn is remembered on its row.

        The earlier turn's tool result was cut past the banner line and its row
        carries the parser's mark. This turn names no topic word, runs a tool
        whose result holds no figure, and the model repeats the earlier result's
        last line: the number may sit in the cut part of that earlier row, so
        the evidence is not whole and the tail stays visible.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Checking the build.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="execute_bash",
                    tool_kind="execute",
                    tool_call_id="tc-build",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-build",
                    tool_output="build ok",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("earlier-cut-row")
        slot._titled = True
        slot.messages.append(
            {"role": "user", "content": "Read response.txt", "ts": "t-1", "cls": ""}
        )
        slot.messages.append(
            {
                "role": "tool",
                "content": "🔧 read_file",
                "ts": "t-2",
                "cls": "msg msg-tool",
                "meta": {"output": "x" * 8000, "outputTruncated": True},
            }
        )
        slot.messages.append({"role": "assistant", "content": "Read it.", "ts": "t-3", "cls": ""})

        await _run_chat(state, slot, "Check the build, then repeat that file's last line")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Read it.", "Checking the build.", exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_repeating_a_sentence_finalized_this_turn_stays_visible(
        self, tmp_path, monkeypatch
    ):
        """A finalized assistant segment from THIS turn is echo evidence too.

        The model says a status line as its first segment (finalized at the tool
        boundary), reads a file that holds no figure, then repeats that first
        sentence as its whole final segment -- the shape a consumed "repeat your
        first sentence" steer produces. The count sits in a current-turn
        finalized assistant row, not an earlier turn, so the tail is a visible
        echo and nothing is re-queued. The still-streaming candidate is never
        its own evidence: it is not a finalized row when it is classified.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-readme",
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-readme",
                    tool_output="# README\nno figures here",
                    tool_status="completed",
                    tool_final=True,
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("repeat-current-turn-sentence")
        slot._titled = True

        await _run_chat(state, slot, "Say the status line, then repeat it exactly")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [exact, exact]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False
