"""Regression tests for provider-only token-budget banners in dashboard chat."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state


class TestProviderBudgetBannerRecovery:
    """A backend-only context-budget reminder must not become chat content."""

    @pytest.mark.parametrize(
        ("raw", "expected", "removed"),
        [
            # The four shapes observed in a real provider transcript: two
            # banner-only messages, and two banners fused to the reply's first
            # word with no separator.
            ("You have 8136 weighted tokens left", "", True),
            ("You have 3762 weighted tokens left", "", True),
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "Two checks remain",
                True,
            ),
            (
                "You have 2811 weighted tokens leftThe summary follows",
                "The summary follows",
                True,
            ),
            # Plain decimal counts have no grouping requirement.
            ("You have 0 weighted tokens left", "", True),
            ("You have 9 weighted tokens left", "", True),
            ("You have 999 weighted tokens left", "", True),
            ("You have 1000 weighted tokens left", "", True),
            ("You have 1234567 weighted tokens left", "", True),
            # Grouped counts require one to three leading digits and exact
            # three-digit groups after every comma.
            ("You have 1,000 weighted tokens left", "", True),
            ("You have 12,345 weighted tokens left", "", True),
            ("You have 123,456 weighted tokens left", "", True),
            ("You have 1,234,567 weighted tokens left", "", True),
            ("  You have 8,154 weighted tokens left.\nFinal answer", "Final answer", True),
            # The glued form needs an uppercase letter directly after "left".
            # A lowercase continuation is a different word, punctuation before
            # the glued text is not the observed shape, and whitespace before a
            # word is ordinary prose.
            (
                "You have 1461 weighted tokens lefttwo tasks remain",
                "You have 1461 weighted tokens lefttwo tasks remain",
                False,
            ),
            (
                "You have 1461 weighted tokens leftover from the run",
                "You have 1461 weighted tokens leftover from the run",
                False,
            ),
            (
                "You have 1461 weighted tokens left.Done",
                "You have 1461 weighted tokens left.Done",
                False,
            ),
            (
                "You have 1461 weighted tokens left Two tasks remain",
                "You have 1461 weighted tokens left Two tasks remain",
                False,
            ),
            # Malformed grouping is ordinary model text, not provider metadata.
            ("You have 1,,2 weighted tokens left", "You have 1,,2 weighted tokens left", False),
            ("You have ,123 weighted tokens left", "You have ,123 weighted tokens left", False),
            ("You have 1,00 weighted tokens left", "You have 1,00 weighted tokens left", False),
            ("You have 12,34 weighted tokens left", "You have 12,34 weighted tokens left", False),
            (
                "You have 1234,567 weighted tokens left",
                "You have 1234,567 weighted tokens left",
                False,
            ),
            (
                "You have 1,2345 weighted tokens left",
                "You have 1,2345 weighted tokens left",
                False,
            ),
            (
                "You have 1,234,56 weighted tokens left",
                "You have 1,234,56 weighted tokens left",
                False,
            ),
            ("You have weighted tokens left", "You have weighted tokens left", False),
            # Quoted, embedded, suffixed and mid-text mentions are not leading
            # banners.
            (
                "The provider said: You have 8154 weighted tokens left",
                "The provider said: You have 8154 weighted tokens left",
                False,
            ),
            (
                "The provider said: You have 8154 weighted tokens leftDone",
                "The provider said: You have 8154 weighted tokens leftDone",
                False,
            ),
            (
                "You have 8154 weighted tokens left for this operation",
                "You have 8154 weighted tokens left for this operation",
                False,
            ),
            ("`You have 8154 weighted tokens left`", "`You have 8154 weighted tokens left`", False),
            (
                '"You have 8154 weighted tokens leftDone"',
                '"You have 8154 weighted tokens leftDone"',
                False,
            ),
            (
                "Done.\nYou have 8154 weighted tokens left",
                "Done.\nYou have 8154 weighted tokens left",
                False,
            ),
        ],
    )
    def test_strip_is_narrow(self, raw, expected, removed):
        from kiro_crew.dashboard.chat_utils import strip_provider_budget_banner

        assert strip_provider_budget_banner(raw) == (expected, removed)

    @pytest.mark.parametrize(
        (
            "message",
            "is_provider_recovery",
            "is_transient_recovery",
            "expected",
        ),
        [
            # Explicit provider-capacity wording fails open for every ordinary
            # interactive turn, regardless of file activity.
            ("What is the model capacity?", False, False, False),
            ("Report model capacity", False, False, False),
            ("Use a tool and report how many tokens remain", False, False, False),
            ("Tell me how many tokens are left", False, False, False),
            ("How much budget remains?", False, False, False),
            ("How much budget is available?", False, False, False),
            (
                "Edit foo, then print exactly: You have 8154 weighted tokens left",
                False,
                False,
                False,
            ),
            ("Print exactly: You have 8154 weighted tokens left", False, False, False),
            ("Return the remaining model capacity", False, False, False),
            ("Quote the token budget", False, False, False),
            ("Document the token budget setting", False, False, False),
            # Generic capacity, unrelated token nouns, and qualified business
            # budgets are not provider-capacity requests. A bare `budget`
            # alternative would make every one of these fail open.
            ("Report the remaining capacity", False, False, True),
            ("Fix the context window bug", False, False, True),
            ("Fix the token authentication bug", False, False, True),
            ("Rotate the API token after the edit", False, False, True),
            # Token counts that are not a remainder: the question names a
            # tokenizer or usage figure, not the provider's remaining budget.
            ("Count how many tokens are in this file", False, False, True),
            ("How many tokens did the prompt use?", False, False, True),
            ("How many API tokens have we issued?", False, False, True),
            ("How much project budget remains?", False, False, True),
            ("How much financial budget remains?", False, False, True),
            ("Update the project budget table", False, False, True),
            ("Finish the remaining tasks", False, False, True),
            ("Ordinary read-only answer", False, False, True),
            # A capacity request the session's own human wrote fails open on a
            # typed provider recovery too: the check runs before that strip. The
            # recovery's own continuation text never reaches this argument (the
            # runner passes only human-written text), so no human text strips.
            ("How much budget remains?", True, False, False),
            ("Print exactly: You have 8154 weighted tokens left", True, False, False),
            ("", True, False, True),
            ("Report the remaining capacity", True, False, True),
            # Ordinary transient recovery must preserve a repeated answer.
            ("Ordinary answer", False, True, False),
        ],
    )
    def test_capacity_topic_gate_reads_human_authority_on_every_turn_kind(
        self,
        message,
        is_provider_recovery,
        is_transient_recovery,
        expected,
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            message,
            is_provider_recovery=is_provider_recovery,
            is_transient_recovery=is_transient_recovery,
            # Give ordinary non-capacity rows independent artifact evidence;
            # explicit capacity wording must still fail open against it.
            prior_visible_output=expected,
        )
        assert artifact is expected
        assert classified == ("" if expected else exact)

    @pytest.mark.parametrize(
        ("message", "kept"),
        [
            # Ordinary remaining-token wordings: an optional subject ("I have",
            # "do we have", "you have") and the verb in any shape. Each of these
            # lost its answer while the alternative admitted only "remain" and
            # "are left|available".
            ("Read usage.json and tell me how many tokens are remaining", True),
            ("Tell me how many tokens I have left", True),
            ("How many tokens do I have left?", True),
            ("How many tokens do we have remaining?", True),
            ("How many tokens remaining?", True),
            ("How many tokens are available?", True),
            # Modifiers between the verb and the remainder word, or before "have".
            ("Read usage.json and tell me how many tokens are still available", True),
            ("How many tokens are still left?", True),
            ("How many tokens do I still have left?", True),
            ("How many tokens do we currently have remaining?", True),
            ("How many tokens are now left over?", True),
            # The ability form: a modal and the asker as subject, in either order.
            ("Read usage.json and tell me how many tokens I can still use", True),
            ("How many tokens can I still use?", True),
            ("How many tokens could we spend?", True),
            ("How many tokens may you still consume?", True),
            ("How many tokens are still usable?", True),
            ("How much budget can I still use?", True),
            ("How much budget do we have left?", True),
            # A qualifier between "many" and "tokens", and the inverted auxiliary.
            ("Read usage.json and tell me how many more tokens can I use?", True),
            ("How many tokens have I got left?", True),
            ("How many more tokens do I have left?", True),
            ("How many extra tokens can we still use?", True),
            ("How many tokens have we got remaining?", True),
            ("How much budget have I got left?", True),
            # Whitespace is folded before the search: a wrapped or double-spaced
            # question is the same question.
            ("Read usage.json and tell me how many tokens\nare left", True),
            ("How many  tokens are\tremaining?", True),
            ("how many\ntokens\ndo I have left", True),
            # A quantity phrase in place of "how many".
            ("Read usage.json and tell me the number of tokens I have left", True),
            ("What number of tokens remain?", True),
            ("Tell me the amount of tokens I can still use", True),
            ("The number of remaining tokens, please", True),
            ("Report the number of tokens per request", False),
            ("What is the number of tokens the prompt used?", False),
            # Token counts that are not a remainder name a tokenizer or usage
            # figure, not the provider's remaining budget: suppression stands.
            ("Count how many tokens are in this file", False),
            ("How many tokens did the prompt use?", False),
            ("How many API tokens have we issued?", False),
            ("How many tokens does this sentence have?", False),
            ("How many tokens are in the vocabulary?", False),
            ("How many tokens could this model use per call?", False),
            ("How many tokens can a tweet use?", False),
            ("How many tokens did we use yesterday?", False),
            ("How many API tokens have we got in the vault?", False),
            ("How many more tokens did the second prompt use?", False),
        ],
    )
    def test_remaining_token_questions_keep_a_banner_only_tail_after_visible_output(
        self, message, kept
    ):
        """The capacity gate must win against the ordinary-tail evidence.

        With prior visible output a banner-only tail is otherwise metadata, so
        this is the row where a missed wording loses the user's answer.
        """
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        classified, artifact = classify_provider_budget_banner(
            exact,
            message,
            is_provider_recovery=False,
            is_transient_recovery=False,
            prior_visible_output=True,
        )
        assert artifact is (not kept)
        assert classified == (exact if kept else "")

    @pytest.mark.parametrize(
        ("is_provider_recovery", "tool_results_complete", "expected_text", "artifact"),
        [
            # Ordinary tail after visible output: whole evidence strips, a cut
            # result keeps the tail as an echo the check could not rule out.
            (False, True, "", True),
            (False, False, "You have 8154 weighted tokens left", False),
            # Typed recovery owns the prefix by provenance; the echo evidence's
            # completeness does not change that.
            (True, True, "", True),
            (True, False, "", True),
        ],
        ids=["ordinary-whole", "ordinary-cut", "recovery-whole", "recovery-cut"],
    )
    def test_a_cut_tool_result_keeps_an_ordinary_banner_only_tail_visible(
        self, is_provider_recovery, tool_results_complete, expected_text, artifact
    ):
        """A result the transport cut cannot prove the phrase absent."""
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        exact = "You have 8154 weighted tokens left"
        assert classify_provider_budget_banner(
            exact,
            "Read response.txt and repeat its last line verbatim",
            is_provider_recovery=is_provider_recovery,
            is_transient_recovery=False,
            prior_visible_output=True,
            # The visible head of the result lacks the phrase either way.
            tool_results=["print('hello')\n"],
            tool_results_complete=tool_results_complete,
        ) == (expected_text, artifact)

    @pytest.mark.parametrize(
        (
            "text",
            "message",
            "provider_recovery",
            "transient_recovery",
            "prior_visible",
            "expected_text",
            "artifact",
        ),
        [
            # Exact banner-only ordinary output is ambiguous. Preserve it even
            # when the user used an unforeseen echo verb rather than adding that
            # verb to a wording allowlist.
            (
                "You have 8154 weighted tokens left",
                "Echo the next model line verbatim",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # A prior visible answer gives independent evidence that the exact
            # final tail is provider metadata.
            (
                "You have 8154 weighted tokens left",
                "finish the task",
                False,
                False,
                True,
                "",
                True,
            ),
            # A line boundary proves the banner grammar, not who authored it.
            # Ordinary output therefore preserves the exact echoed answer.
            (
                "You have 8154 weighted tokens left\nFinal answer",
                "finish the task",
                False,
                False,
                False,
                "You have 8154 weighted tokens left\nFinal answer",
                False,
            ),
            # The glued shape is recognized grammar too, and the same authority
            # rule applies: an ordinary turn keeps the bytes it cannot attribute.
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "finish the task",
                False,
                False,
                True,
                "You have 1461 weighted tokens leftTwo checks remain",
                False,
            ),
            # Typed provider recovery is the causal opposite: the controller
            # minted ownership after observing a prior provider artifact, so it
            # may remove only the banner prefix and preserve the semantic tail,
            # with a line break or glued to the first word.
            (
                "You have 8154 weighted tokens left\nFinal answer",
                "",
                True,
                False,
                False,
                "Final answer",
                True,
            ),
            (
                "You have 2811 weighted tokens leftThe summary follows",
                "",
                True,
                False,
                False,
                "The summary follows",
                True,
            ),
            # Capacity requests, quoted prose, transient recovery, and near
            # misses stay visible.
            (
                "You have 8154 weighted tokens left",
                "What is the model capacity?",
                False,
                False,
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "`You have 8154 weighted tokens left`",
                "quote the line",
                False,
                False,
                True,
                "`You have 8154 weighted tokens left`",
                False,
            ),
            (
                "You have 8154 weighted tokens left",
                "",
                False,
                True,
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 1461 weighted tokens leftTwo checks remain",
                "",
                False,
                True,
                False,
                "You have 1461 weighted tokens leftTwo checks remain",
                False,
            ),
            (
                "You have 8154 weighted tokens left for this operation",
                "finish the task",
                False,
                False,
                True,
                "You have 8154 weighted tokens left for this operation",
                False,
            ),
            # An explicit capacity echo stays visible with no other evidence.
            (
                "You have 8154 weighted tokens left",
                "Print exactly: You have 8154 weighted tokens left",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # Typed provider recovery is host-created specifically to recover a
            # prior provider artifact, so a repeated exact banner remains owned.
            (
                "You have 8154 weighted tokens left",
                "",
                True,
                False,
                False,
                "",
                True,
            ),
            # Regeneration context is not artifact provenance either.
            (
                "You have 8154 weighted tokens left",
                "regenerate",
                False,
                False,
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
        ],
    )
    def test_classifier_uses_content_and_structural_provenance(
        self,
        text,
        message,
        provider_recovery,
        transient_recovery,
        prior_visible,
        expected_text,
        artifact,
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        assert classify_provider_budget_banner(
            text,
            message,
            is_provider_recovery=provider_recovery,
            is_transient_recovery=transient_recovery,
            prior_visible_output=prior_visible,
        ) == (expected_text, artifact)

    @pytest.mark.parametrize(
        ("text", "tool_results", "provider_recovery", "expected_text", "artifact"),
        [
            # A tool result this turn delivered carries the banner's phrase, so
            # the tail may be the echo the user asked for: it stays visible.
            (
                "You have 8154 weighted tokens left",
                ["You have 8154 weighted tokens left"],
                False,
                "You have 8154 weighted tokens left",
                False,
            ),
            # The phrase anywhere in a result counts, with the echo's own trailing
            # period and a grouped count.
            (
                "You have 8,154 weighted tokens left.",
                ["notes\nYou have 8,154 weighted tokens left\nmore notes"],
                False,
                "You have 8,154 weighted tokens left.",
                False,
            ),
            # The check runs before the provider-recovery strip as well.
            (
                "You have 8154 weighted tokens left",
                ["You have 8154 weighted tokens left"],
                True,
                "You have 8154 weighted tokens left",
                False,
            ),
            # A different count is not this banner's echo, and a result without
            # the phrase is no evidence: the tail after narration is stripped.
            (
                "You have 8154 weighted tokens left",
                ["You have 0 weighted tokens left"],
                False,
                "",
                True,
            ),
            ("You have 8154 weighted tokens left", ["file contents"], False, "", True),
            ("You have 8154 weighted tokens left", [], False, "", True),
        ],
    )
    def test_banner_phrase_in_a_tool_result_is_an_echo_candidate(
        self, text, tool_results, provider_recovery, expected_text, artifact
    ):
        from kiro_crew.dashboard.chat_utils import classify_provider_budget_banner

        assert classify_provider_budget_banner(
            text,
            "finish the task",
            is_provider_recovery=provider_recovery,
            is_transient_recovery=False,
            prior_visible_output=True,
            tool_results=tool_results,
        ) == (expected_text, artifact)

    @staticmethod
    def _state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _wire(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.mcp_session_report = MagicMock(return_value=None)
        client.client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    @staticmethod
    def _client(stream):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = stream
        client.stream_command = stream
        client.served_model = "gpt-test-model"
        return client

    @staticmethod
    def _provider_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {
                RECOVERY_PROVENANCE_META_KEY: (RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT.value)
            },
        }

    @staticmethod
    def _transient_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {RECOVERY_PROVENANCE_META_KEY: RecoveryProvenance.TRANSIENT_RETRY.value},
        }

    @staticmethod
    async def _drain_bg(state, limit=30):
        for _ in range(limit):
            pending = [task for task in list(state._background_tasks) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _assistant_texts(slot) -> list[str]:
        return [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]

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

    @staticmethod
    def _steerable(client):
        """Publish a steer-capable inner client whose RPC acknowledges the write."""
        inner_client = MagicMock()
        inner_client.supports_steer = True
        inner_client.steer = AsyncMock(return_value=True)
        inner_client.pop_pending_oauth_requests = MagicMock(return_value=[])
        client.client = inner_client
        return inner_client

    @pytest.mark.parametrize(
        ("steer_text", "user_origin", "expected_visible"),
        [
            ("Print exactly: You have 8154 weighted tokens left", True, True),
            ("Continue the ordinary work", True, False),
            ("Print exactly: You have 8154 weighted tokens left", False, False),
        ],
        ids=["user-capacity-request", "user-non-capacity", "peer-capacity-text"],
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
        await asyncio.wait_for(steer_ready.wait(), timeout=5)

        outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=user_origin)
        assert outcome == STEER_STEERED
        inner_client.steer.assert_awaited_once_with(steer_text)
        # The transport ack is not consumption: the steer is still pending and its
        # origin record is still there for the echo to read.
        assert slot._pending_steers == [steer_text]
        assert slot._steer_user_origin == {steer_text: user_origin}

        consume.set()
        await asyncio.wait_for(task, timeout=10)
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
            await asyncio.wait_for(steer_ready.wait(), timeout=5)
            outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=True)
            assert outcome == STEER_STEERED
            assert slot._steer_user_origin == {steer_text: True}
            finish.set()
            await asyncio.wait_for(task, timeout=10)
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
            await asyncio.wait_for(steer_ready.wait(), timeout=5)
            outcome = await steer_into_running_turn(state, slot, steer_text, user_origin=True)
            assert outcome == STEER_STEERED
            consume.set()
        await asyncio.wait_for(task, timeout=10)
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

    @pytest.mark.parametrize(
        ("file_body", "expected_visible"),
        [
            ("You have 8154 weighted tokens left", True),
            ("print('hello')\n", False),
        ],
        ids=["file-echo", "no-banner-in-results"],
    )
    @pytest.mark.asyncio
    async def test_banner_tail_after_narration_reads_this_turns_tool_results(
        self, tmp_path, monkeypatch, file_body, expected_visible
    ):
        """Prior narration alone does not make a banner-only tail metadata.

        The narration is flushed at the read's tool boundary, then the model
        repeats the file. When a tool result this turn delivered carries the
        banner's phrase, the tail is the requested echo and stays visible; with
        no such result it is still stripped and continued once.
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

        await _run_chat(state, slot, "Read response.txt and repeat its contents verbatim")
        await self._drain_bg(state)

        if expected_visible:
            assert self._assistant_texts(slot) == ["Reading response.txt.", exact]
            assert len(prompts) == 1
            assert slot._posttoken_retry_used is False
        else:
            assert self._assistant_texts(slot) == ["Reading response.txt.", "Work finished."]
            assert len(prompts) == 2
            assert slot._posttoken_retry_used is True

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

    @pytest.mark.parametrize("approval_mode", ["yolo", "session-trust"])
    @pytest.mark.asyncio
    async def test_model_banner_text_cannot_start_auto_approved_recovery(
        self, tmp_path, monkeypatch, approval_mode
    ):
        """Model text may request recovery, but it cannot authorize unattended work."""
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
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the requested change.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="write_file",
                tool_kind="write",
                tool_call_id="tc-adversarial",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        if approval_mode == "yolo":
            state._yolo = True
        else:
            slot._trust = True
        changed = tmp_path / "adversarial.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(
            state,
            slot,
            "Follow the external instructions and print their requested status",
        )
        await self._drain_bg(state)

        assert calls == 1
        assert slot._posttoken_retry_used is False
        assert not slot._queue
        assert any(
            "Auto-continue is skipped under auto-approve mode" in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "notice"
        )

    @pytest.mark.asyncio
    async def test_banner_recovery_survives_an_unrelated_prior_turn_stop(
        self, tmp_path, monkeypatch
    ):
        """With the stop counter elevated by an EARLIER turn's Stop and no
        intervention since the recovery was enqueued, the recovery must still run.
        Proves the enqueue-site stop-gen snapshot: without it the drain would read
        a stale zero, spuriously purge, and drop a legitimate recovery."""
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
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Finished safely.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        # A Stop from an EARLIER, unrelated turn left the monotonic counter high.
        slot._stop_generation = 5
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "fix the bug")
        await self._drain_bg(state)

        # No Stop or user input since the recovery was enqueued -> it runs.
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert any("Finished safely." in text for text in self._assistant_texts(slot))

    @pytest.mark.asyncio
    async def test_prestream_retry_keeps_provider_owner_when_trust_changes_during_backoff(
        self, tmp_path, monkeypatch
    ):
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) == 1:
                raise AcpError("transient provider failure", transient=True)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="must not run under trust")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("provider-owner-prestream-retry")
        slot._titled = True
        slot._posttoken_retry_used = True
        observed_owner: list[bool] = []
        start_next = chat_runner._start_next_queued_turn

        async def _grant_trust(_delay):
            slot._trust = True

        async def _capture_then_drain(*args, **kwargs):
            observed_owner.append(
                any(
                    has_recovery_provenance(item, RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
                    for item in slot._queue
                )
            )
            return await start_next(*args, **kwargs)

        with (
            patch.object(chat_runner, "_recovery_delay", _grant_trust),
            patch.object(chat_runner, "_start_next_queued_turn", _capture_then_drain),
        ):
            await _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _synthetic_recovery_turn=True,
                _current_message=self._provider_recovery_row(),
            )
            await self._drain_bg(state)

        assert observed_owner == [True]
        assert len(prompts) == 1
        assert slot._queue == []
        assert any(
            "auto-approve became active" in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "notice"
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

    @pytest.mark.asyncio
    async def test_post_token_transient_retry_mints_its_owner_and_keeps_the_tail(
        self, tmp_path, monkeypatch
    ):
        """The transient owner is reachable from the production retry, not only
        from a hand-built row. A turn streams output and then hits a transient
        5xx. The post-token one-shot queues the CONTINUE under
        ``RecoveryProvenance.TRANSIENT_RETRY``, the drain copies the tag onto the
        replayed row, and the retry turn it owns keeps a banner-shaped tail after
        visible output, where an ordinary turn strips that tail and queues a
        recovery. Nothing in this test writes the tag by hand."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        answer = "You have 8154 weighted tokens left"
        calls = 0
        captured: list = []

        async def _no_backoff(_delay):
            return None

        async def _stream(message):
            nonlocal calls
            calls += 1
            captured.append(message)
            if calls == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Partial work.")
                raise AcpError(
                    "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
                )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Resumed work.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-posttoken-retry",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answer)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("posttoken-transient-owner")
        slot._titled = True

        with patch.object(chat_runner, "_recovery_delay", _no_backoff):
            await _run_chat(state, slot, "Continue the report")
            await self._drain_bg(state)

        # The production one-shot re-prompted the live session exactly once with
        # the fixed continuation (its marker line is neutralised on the way out,
        # so the instruction body is what the client sees).
        assert calls == 2
        assert "Continue from where it stopped" in captured[1]
        assert "Continue the report" not in captured[1]
        injected = [m for m in slot.messages if m.get("role") == "inject"]
        assert [m.get("content") for m in injected] == [_POSTTOKEN_RECOVER_MSG]
        # The replayed row carries the owner the retry path minted, and not the
        # provider-budget owner that shares its text.
        assert has_recovery_provenance(injected[0], RecoveryProvenance.TRANSIENT_RETRY)
        assert not has_recovery_provenance(injected[0], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
        # The owned retry keeps its banner-shaped tail; nothing was stripped and no
        # recovery was queued for it.
        assert self._assistant_texts(slot) == ["Partial work.", "Resumed work.", answer]
        assert not slot._queue
        assert not any(
            "internal status instead of an answer" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        )
        assert slot._posttoken_retry_used is True

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

    @pytest.mark.asyncio
    async def test_banner_recovery_precedes_stop_hook_without_replacing_visible_variant(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )
        from kiro_crew.dashboard.state import HOOK_CONTINUATION_RECOVERY_PREFIX
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the regenerated answer.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-regenerate-artifact",
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text="You have 8154 weighted tokens left",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        state._hook_store = MagicMock()
        state._hook_store.fire = AsyncMock(
            return_value=[
                SimpleNamespace(
                    exit_code=0,
                    stdout='{"decision":"block","reason":"run the gate"}',
                    stderr="",
                    hook_name="stop-gate",
                )
            ]
        )
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("banner-stop-hook")
        slot._titled = True
        slot._pending_variants = [{"content": "prior answer", "ts": "old-ts"}]
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        # Keep the queue observable instead of letting the finally drain dispatch it.
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "regenerate the answer")

        queued = [item["content"] for item in slot._queue]
        assert queued[0] == _POSTTOKEN_RECOVER_MSG
        assert queued[1] == f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nrun the gate"
        assert has_recovery_provenance(slot._queue[0], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
        assert not has_recovery_provenance(
            slot._queue[1], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT
        )
        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message["content"] for message in assistant] == ["Applying the regenerated answer."]
        assert [variant["content"] for variant in assistant[0]["variants"]] == [
            "prior answer",
            "Applying the regenerated answer.",
        ]

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
