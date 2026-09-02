"""Tests for the llm_helpers module — shared LLM interaction utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.acp.types import STOP_REASON_CANCELLED, TurnUsage
from kiro_crew.llm_helpers import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    TURN_FALLBACK_ATTR,
    FallbackState,
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    first_advertised_fallback,
    next_fallback_candidate,
    parse_llm_json,
    parse_llm_json_list,
    probe_fallback_restore,
    record_interaction_event,
    save_conversation_turn,
    stream_and_collect,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent


class TestFirstAdvertisedFallback:
    """Unit tests for the shared model-fallback helper."""

    def test_skips_rejected_and_auto(self) -> None:
        result = first_advertised_fallback(["auto", "claude-opus-5", "claude-sonnet-5"], "auto")
        assert result == "claude-opus-5"

    def test_skips_rejected_concrete(self) -> None:
        result = first_advertised_fallback(["claude-opus-5", "claude-sonnet-5"], "claude-opus-5")
        assert result == "claude-sonnet-5"

    def test_returns_none_when_empty(self) -> None:
        assert first_advertised_fallback([], "auto") is None

    def test_returns_none_when_only_auto_and_rejected(self) -> None:
        assert first_advertised_fallback(["auto"], "auto") is None

    def test_none_rejected(self) -> None:
        result = first_advertised_fallback(["claude-opus-5"], None)
        assert result == "claude-opus-5"

    def test_case_insensitive(self) -> None:
        result = first_advertised_fallback(["Auto", "Claude-Opus-5"], "auto")
        assert result == "Claude-Opus-5"


class TestParseLlmJson:
    def test_valid_json(self) -> None:
        assert parse_llm_json('{"key": "value"}') == {"key": "value"}

    def test_json_with_fences(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        assert parse_llm_json(text) == {"key": "value"}

    def test_json_with_plain_fences(self) -> None:
        text = '```\n{"key": "value"}\n```'
        assert parse_llm_json(text) == {"key": "value"}

    def test_empty_string(self) -> None:
        assert parse_llm_json("") is None

    def test_whitespace_only(self) -> None:
        assert parse_llm_json("   \n  ") is None

    def test_invalid_json(self) -> None:
        assert parse_llm_json("not json") is None

    def test_returns_none_for_list(self) -> None:
        assert parse_llm_json("[1, 2, 3]") is None

    def test_returns_none_for_string(self) -> None:
        assert parse_llm_json('"just a string"') is None

    def test_nested_fences(self) -> None:
        text = '```json\n{"code": "```"}\n```'
        # Should handle gracefully — the inner ``` gets split
        result = parse_llm_json(text)
        # May or may not parse, but should not raise
        assert result is None or isinstance(result, dict)

    def test_whitespace_around_json(self) -> None:
        text = '  \n  {"a": 1}  \n  '
        assert parse_llm_json(text) == {"a": 1}

    def test_leading_prose_before_json(self) -> None:
        # CC's chatty/un-scoped background session may prepend prose. The parser
        # must still extract the JSON object — otherwise consolidation silently
        # no-ops under the Claude Code provider (kiro's no-tools lite agent emits
        # bare JSON, CC does not).
        text = 'Sure! Here is the consolidated memory:\n{"key": "value"}'
        assert parse_llm_json(text) == {"key": "value"}

    def test_trailing_prose_after_json(self) -> None:
        text = '{"key": "value"}\nLet me know if you need anything else.'
        assert parse_llm_json(text) == {"key": "value"}

    def test_prose_then_fenced_json(self) -> None:
        text = 'Here you go:\n```json\n{"key": "value"}\n```'
        assert parse_llm_json(text) == {"key": "value"}

    def test_nested_object_with_surrounding_prose(self) -> None:
        text = 'Result:\n{"a": {"b": [1, 2]}, "c": "x"}\nDone.'
        assert parse_llm_json(text) == {"a": {"b": [1, 2]}, "c": "x"}

    def test_brace_inside_string_not_confused(self) -> None:
        text = 'Note:\n{"msg": "use {curly} braces"}\nthanks'
        assert parse_llm_json(text) == {"msg": "use {curly} braces"}

    def test_stray_structural_brace_in_prose_skipped(self) -> None:
        # A non-JSON brace span in the preamble must NOT defeat extraction of
        # the real trailing JSON (the first-match-only scanner regressed here).
        assert parse_llm_json('Use {placeholder} then: {"a": 1}') == {"a": 1}
        assert parse_llm_json('schema is {field: value}. Here:\n{"prefs": ["x"]}') == {
            "prefs": ["x"]
        }

    def test_dict_request_does_not_dig_into_array(self) -> None:
        # dict expected but only an array-of-objects present → None, NOT the
        # nested object dug out of the array.
        assert parse_llm_json('here [1, {"a": 2}] done') is None


class TestParseLlmJsonList:
    def test_valid_list(self) -> None:
        assert parse_llm_json_list('[{"title": "a"}]') == [{"title": "a"}]

    def test_list_with_fences(self) -> None:
        text = '```json\n[{"title": "a"}]\n```'
        assert parse_llm_json_list(text) == [{"title": "a"}]

    def test_empty_string(self) -> None:
        assert parse_llm_json_list("") is None

    def test_returns_none_for_dict(self) -> None:
        assert parse_llm_json_list('{"key": "value"}') is None

    def test_invalid_json(self) -> None:
        assert parse_llm_json_list("not json") is None


class TestSaveConversationTurn:
    def test_saves_user_and_assistant(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "world")
        assert log.append.call_count == 2
        log.append.assert_any_call(
            "key1", "user", "hello", source_thread=None, source_user=None, agent=None
        )
        log.append.assert_any_call(
            "key1", "assistant", "world", source_thread=None, source_user=None
        )

    def test_saves_with_provenance(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "world", source_thread="t1", source_user="u1")
        log.append.assert_any_call(
            "key1", "user", "hello", source_thread="t1", source_user="u1", agent=None
        )
        log.append.assert_any_call(
            "key1", "assistant", "world", source_thread="t1", source_user="u1"
        )

    def test_skips_empty_assistant(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "")
        assert log.append.call_count == 1
        log.append.assert_called_once_with(
            "key1", "user", "hello", source_thread=None, source_user=None, agent=None
        )

    def test_saves_with_agent(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "world", agent="ops")
        log.append.assert_any_call(
            "key1", "user", "hello", source_thread=None, source_user=None, agent="ops"
        )


class TestToolApprovalPolicy:
    def test_enum_values(self) -> None:
        assert ToolApprovalPolicy.AUTO_APPROVE.value == "auto_approve"
        assert ToolApprovalPolicy.REJECT_ALL.value == "reject_all"
        assert ToolApprovalPolicy.HOOK_BASED.value == "hook_based"


# ── Prompt-busy retry tests ──


def _make_provider(events=None, error=None):
    """Create a mock LLMProvider that yields events or raises."""
    provider = AsyncMock()
    provider.cancel = AsyncMock()
    provider.shutdown = AsyncMock()

    async def _stream(msg):
        if error:
            raise error
        for e in events or []:
            yield e

    provider.stream = _stream
    return provider


class TestStreamAndCollectPromptBusy:
    @pytest.mark.asyncio
    async def test_retries_on_prompt_busy_then_succeeds(self) -> None:
        """First call raises 'already in progress', second succeeds."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok"
        assert call_count == 2
        provider.cancel.assert_awaited_once()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shuts_down_provider_after_retries_exhausted(self) -> None:
        """After all retries fail, provider.shutdown() is called."""
        provider = _make_provider(error=AcpError("already in progress"))

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(PromptBusyExhaustedError),
        ):
            await stream_and_collect(provider, "test")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_formatted_prompt_busy_still_retries(self) -> None:
        """A FORMATTED prompt-busy must take the busy arm, not fall through.

        Regression guard. The shared-runtime raise path routes through
        _format_acp_error, which rewrites the backend's "prompt already in
        progress" into user-facing prose carrying none of that substring. When
        this check was string-only, such an error skipped BOTH busy arms
        (cancel+retry and PromptBusyExhaustedError) and surfaced as a generic
        failure — leaving the wedged parent session un-reset for every
        unattended caller (workflows/agent_pool, handlers/side, the
        subagent-completion injector).
        """
        from kiro_crew.acp.client import _format_acp_error

        formatted = _format_acp_error(
            {"code": -32603, "message": "Internal error", "data": "Prompt already in progress"}
        )
        # Precondition: the marker the old check relied on really is gone.
        assert "already in progress" not in formatted

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpPromptBusy(formatted)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok"
        assert call_count == 2
        provider.cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_formatted_prompt_busy_exhaustion_still_raises_typed(self) -> None:
        """The exhaustion arm must also fire for a formatted prompt-busy."""
        from kiro_crew.acp.client import _format_acp_error

        formatted = _format_acp_error(
            {"code": -32603, "message": "Internal error", "data": "Prompt already in progress"}
        )
        provider = _make_provider(error=AcpPromptBusy(formatted))

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(PromptBusyExhaustedError),
        ):
            await stream_and_collect(provider, "test")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_busy_error_raises_immediately(self) -> None:
        """Non-busy AcpError is not retried."""
        provider = _make_provider(error=AcpError("some other error"))

        with pytest.raises(AcpError, match="some other error"):
            await stream_and_collect(provider, "test")

        provider.cancel.assert_not_awaited()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_stream_no_retry(self) -> None:
        """Normal stream completes without retry."""
        provider = _make_provider(
            events=[
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )

        result = await stream_and_collect(provider, "test")

        assert result == "hello"
        provider.cancel.assert_not_awaited()


class TestStreamAndCollectRawCompletion:
    @pytest.mark.asyncio
    async def test_callback_receives_the_raw_complete_event(self) -> None:
        """Callers needing terminal evidence must see the provider event itself."""
        complete = LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason=STOP_REASON_CANCELLED,
            usage=TurnUsage(input_tokens=12, output_tokens=3),
        )
        provider = _make_provider(
            events=[LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"), complete]
        )
        observed: list[LLMEvent] = []

        result = await stream_and_collect(provider, "test", on_complete=observed.append)

        assert result == "partial"
        assert observed == [complete]

    @pytest.mark.asyncio
    async def test_stream_exhaustion_does_not_invent_completion(self) -> None:
        """A provider iterator ending without EVENT_COMPLETE is not a completed turn."""
        provider = _make_provider(events=[LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial")])
        observed: list[LLMEvent] = []

        result = await stream_and_collect(provider, "test", on_complete=observed.append)

        assert result == "partial"
        assert observed == []


# ── Transient backend (5xx / throttle / stream-reset) retry tests ──


class TestTransientErrorClassifier:
    """_is_transient_acp_error: retry server-side hiccups, fail fast on auth."""

    def test_internal_server_error_is_transient(self) -> None:
        from kiro_crew.llm_helpers import _is_transient_acp_error

        assert _is_transient_acp_error(
            "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
        )

    def test_throttle_and_unavailable_are_transient(self) -> None:
        from kiro_crew.llm_helpers import _is_transient_acp_error

        assert _is_transient_acp_error("Bedrock is throttling requests")
        assert _is_transient_acp_error("ServiceUnavailableException")
        assert _is_transient_acp_error("Model 'x' is unavailable on Bedrock right now")
        assert _is_transient_acp_error("connection reset by peer")

    def test_dispatch_failure_is_transient(self) -> None:
        from kiro_crew.llm_helpers import _is_transient_acp_error

        # AWS SDK connector-level I/O failure (conn/DNS/TLS drop) — retryable.
        # Uses the exact shapes seen in history-consolidation ACP errors.
        assert _is_transient_acp_error(
            "ACP error: {'code': -32603, 'message': 'Internal error', 'data': "
            "'Encountered an error in the response stream: An unknown error "
            "occurred: dispatch failure'}"
        )
        # Rust DispatchFailure variant (unspaced, from the response stream).
        assert _is_transient_acp_error(
            "CodewhispererChatResponseStream(DispatchFailure(DispatchFailure { "
            "source: ConnectorError { kind: Io } }))"
        )

    def test_auth_and_validation_are_not_transient(self) -> None:
        from kiro_crew.llm_helpers import _is_transient_acp_error

        # These must fail fast — a retry cannot fix them.
        assert not _is_transient_acp_error(
            "Bedrock authentication failed. Run 'ada credentials update'"
        )
        assert not _is_transient_acp_error("AccessDeniedException")
        assert not _is_transient_acp_error("ExpiredTokenException")
        assert not _is_transient_acp_error("ValidationException: bad input")
        assert not _is_transient_acp_error("Prompt error: some unknown thing")


class TestAcpErrorIsTransient:
    """acp_error_is_transient prefers the structured AcpError.transient flag and
    falls back to the string classifier."""

    def test_flag_true_wins_over_nontransient_message(self) -> None:
        from kiro_crew.llm_helpers import acp_error_is_transient

        # Flag is authoritative: a terminal-looking message is still retried.
        assert acp_error_is_transient(AcpError("ValidationException", transient=True))

    def test_flag_false_wins_over_transient_message(self) -> None:
        from kiro_crew.llm_helpers import acp_error_is_transient

        # Flag is authoritative: a transient-looking message still fails fast.
        assert not acp_error_is_transient(AcpError("ServiceUnavailableException", transient=False))

    def test_unflagged_5xx_message_falls_back_to_string(self) -> None:
        from kiro_crew.llm_helpers import acp_error_is_transient

        # The regression: _format_acp_error's friendly 5xx string is
        # now recognised by the string fallback even with no flag set.
        msg = (
            "The model backend hit a transient error (HTTP 5xx). This is usually "
            "momentary — retry in a moment. If it keeps happening, switch to a "
            "different model in the picker."
        )
        assert acp_error_is_transient(AcpError(msg))  # transient defaults to None

    def test_plain_exception_uses_string_fallback(self) -> None:
        from kiro_crew.llm_helpers import acp_error_is_transient

        # Non-AcpError (no .transient attr) → string classifier.
        assert acp_error_is_transient(RuntimeError("ServiceUnavailableException"))
        assert not acp_error_is_transient(RuntimeError("AccessDeniedException"))


class TestStreamAndCollectTransient:
    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @pytest.mark.asyncio
    async def test_retries_transient_then_succeeds(self) -> None:
        """Two transient failures, then success — recovered, no shutdown."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok-result"
        assert call_count == 3
        # Transient retries do NOT cancel (no in-flight turn) and never shutdown.
        provider.cancel.assert_not_awaited()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_exhausts_budget_then_raises(self) -> None:
        """Persistent transient failure raises AFTER exhausting the retry budget.

        Asserts the call count so this proves the retry loop actually ran
        (initial attempt + _TRANSIENT_RETRIES); without it, a skipped retry
        path would still pass on the first raise.
        """
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test")

        assert call_count == _TRANSIENT_RETRIES + 1
        provider.cancel.assert_not_awaited()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auth_error_fails_fast_no_retry(self) -> None:
        """Auth failure is NOT transient — raises on the first call, no retry."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._AUTH)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test")

        assert call_count == 1  # no retry
        provider.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_response_not_retried(self) -> None:
        """A transient error AFTER tokens have streamed must NOT be retried —
        re-running would duplicate the already-emitted output."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            # Emit a token first, THEN fail transiently mid-stream.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
            raise AcpError(self._TRANSIENT)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test")

        # No retry once a partial response was emitted — exactly one attempt.
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_transient_false_disables_retry(self) -> None:
        """retry_transient=False makes a transient error fail fast (for callers
        that own an outer retry loop and must not be double-retried)."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", retry_transient=False)

        assert call_count == 1  # opt-out → no inner retry


class TestStreamAndCollectModelFallback:
    """Model-rejection reactive fallback (e.g. "auto" on GovCloud)."""

    @pytest.mark.asyncio
    async def test_model_rejected_retries_with_fallback(self) -> None:
        """When 'auto' is rejected with an advertised list, retry once with
        the first advertised model after calling set_model."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                exc = AcpError("model 'auto' not available")
                exc.rejected_model = "auto"
                exc.advertised = ["claude-opus-5", "claude-sonnet-5"]
                raise exc
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="success")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.set_model = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", model_fallback=True)

        assert result == "success"
        assert call_count == 2
        provider.set_model.assert_awaited_once_with("claude-opus-5")

    @pytest.mark.asyncio
    async def test_model_rejected_default_propagates_no_swap(self) -> None:
        """Without model_fallback=True, a rejected model propagates and is NOT
        silently swapped — an interactive user pick must surface the error."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            exc = AcpError("model 'auto' not available")
            exc.rejected_model = "auto"
            exc.advertised = ["claude-opus-5", "claude-sonnet-5"]
            raise exc
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.set_model = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test")

        assert call_count == 1  # no retry
        provider.set_model.assert_not_awaited()  # no silent swap

    @pytest.mark.asyncio
    async def test_model_rejected_no_advertised_propagates(self) -> None:
        """When 'auto' is rejected but no advertised list, propagate."""

        async def _stream(msg):
            exc = AcpError("model 'auto' not available")
            exc.rejected_model = "auto"
            exc.advertised = []
            raise exc
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", model_fallback=True)

    @pytest.mark.asyncio
    async def test_model_fallback_only_once(self) -> None:
        """Fallback retries only once — a second rejection propagates."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            exc = AcpError("model rejected")
            exc.rejected_model = "auto"
            exc.advertised = ["claude-opus-5"]
            raise exc
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.set_model = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", model_fallback=True)

        # First call triggers fallback, second call propagates.
        assert call_count == 2


class TestNextFallbackCandidate:
    """Unit matrix for the throttle-fallback candidate selector."""

    _ADV = ["claude-opus-5", "claude-opus-4.8", "claude-opus-4.7"]

    def test_first_usable_candidate(self) -> None:
        chain = ["claude-opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-fable-5", self._ADV) == "claude-opus-5"

    def test_skips_active_model(self) -> None:
        chain = ["claude-opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-opus-5", self._ADV) == "claude-opus-4.8"

    def test_skips_active_model_case_insensitive(self) -> None:
        chain = ["Claude-Opus-5", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "claude-opus-5", self._ADV) == "claude-opus-4.8"

    def test_skips_unadvertised_ids(self) -> None:
        chain = ["not-served-model", "claude-opus-4.8"]
        assert next_fallback_candidate(chain, "x", self._ADV) == "claude-opus-4.8"

    def test_empty_chain_returns_none(self) -> None:
        assert next_fallback_candidate([], "x", self._ADV) is None

    def test_exhausted_chain_returns_none(self) -> None:
        assert next_fallback_candidate(["not-served"], "x", self._ADV) is None

    def test_skips_garbage_entries(self) -> None:
        chain = ["", "  ", None, 42, "claude-opus-5"]  # type: ignore[list-item]
        assert next_fallback_candidate(chain, "x", self._ADV) == "claude-opus-5"

    def test_auto_is_a_candidate_when_advertised(self) -> None:
        # "auto" (the backend's availability-aware routing) is the default
        # fallback — it must be selectable like any other advertised id.
        adv = [*self._ADV, "auto"]
        assert next_fallback_candidate(["auto"], "claude-fable-5", adv) == "auto"

    def test_auto_skipped_when_not_advertised(self) -> None:
        # The fallthrough: a partition that does not serve "auto" skips it
        # (the original error then surfaces) rather than sending a no-op swap.
        assert next_fallback_candidate(["auto"], "claude-fable-5", self._ADV) is None

    def test_fails_open_on_empty_advertised(self) -> None:
        # Entitlement unknown is not entitlement denied (model_is_unusable's
        # stance); the substitute set_model re-validates against the live list.
        assert next_fallback_candidate(["some-model"], "x", []) == "some-model"
        assert next_fallback_candidate(["some-model"], "x", None) == "some-model"


class TestConfiguredFallbackChain:
    """agent.fallback_model -> walk-order derivation (the one shared derivation)."""

    def _chain_for(self, value: str) -> tuple[str, ...]:
        from kiro_crew.llm_helpers import configured_fallback_chain

        cfg = MagicMock()
        cfg.agent.fallback_model = value
        with patch("kiro_crew.llm_helpers.KiroCrewConfig") as kc:
            kc.load.return_value = cfg
            return configured_fallback_chain()

    def test_empty_disables(self) -> None:
        # ROLLBACK PIN: fallback_model "" == pre-feature behavior everywhere.
        assert self._chain_for("") == ()

    def test_auto_default_yields_auto_only(self) -> None:
        assert self._chain_for("auto") == ("auto",)

    def test_concrete_id_yields_id_then_auto(self) -> None:
        # Fallthrough order: selected -> auto (-> backend default via
        # set_model's own resolve when auto is unserved).
        assert self._chain_for("claude-opus-4.8") == ("claude-opus-4.8", "auto")

    def test_load_failure_disables(self) -> None:
        from kiro_crew.llm_helpers import configured_fallback_chain

        with patch("kiro_crew.llm_helpers.KiroCrewConfig") as kc:
            kc.load.side_effect = RuntimeError("boom")
            assert configured_fallback_chain() == ()


class TestSetModelWitness:
    """A non-raising set_model that did not move the model is a NO-OP, not a
    transition — never published (walk) and never treated as restored (probe).
    """

    def _provider(self, model: str) -> MagicMock:
        provider = MagicMock()
        provider._model = model
        provider.served_model = None
        provider.available_models = None
        provider.set_model = AsyncMock()  # no side effect: _model never moves
        setattr(provider, TURN_FALLBACK_ATTR, None)
        return provider

    @pytest.mark.asyncio
    async def test_unwitnessed_swap_is_not_published(self) -> None:
        # set_model("claude-opus-4.8") silently no-ops (resolve collapsed it):
        # the walk must skip the candidate instead of publishing a marker for
        # a model that never took over.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider("primary-model")
        fb = FallbackState(chain=("claude-opus-4.8",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand is None
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_witnessed_swap_is_published(self) -> None:
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider("primary-model")

        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        fb = FallbackState(chain=("claude-opus-4.8",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand == "claude-opus-4.8"
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_noop_restore_keeps_the_marker(self) -> None:
        # An "auto" primary that resolves to "" restores nothing: the session
        # is still on the fallback, so the marker must survive for the next
        # probe (clearing it re-opens the backfill permanent-pin door).
        from kiro_crew.llm_helpers import probe_fallback_restore

        provider = self._provider("fallback-model")
        setattr(provider, TURN_FALLBACK_ATTR, ("auto", "fallback-model"))
        await probe_fallback_restore(provider, surface="test")
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("auto", "fallback-model")

    @pytest.mark.asyncio
    async def test_witnessed_restore_clears_the_marker(self) -> None:
        from kiro_crew.llm_helpers import probe_fallback_restore

        provider = self._provider("fallback-model")

        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fallback-model"))
        await probe_fallback_restore(provider, surface="test")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None


class TestProviderFallbackActive:
    """The shared usage-attribution guard reads the sticky marker."""

    def test_true_while_marker_present(self) -> None:
        from kiro_crew.llm_helpers import provider_fallback_active

        provider = MagicMock()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fallback-model"))
        assert provider_fallback_active(provider) is True

    def test_false_without_marker_or_malformed(self) -> None:
        from kiro_crew.llm_helpers import provider_fallback_active

        provider = MagicMock(spec=[])  # no marker attribute at all
        assert provider_fallback_active(provider) is False
        provider2 = MagicMock()
        setattr(provider2, TURN_FALLBACK_ATTR, "not-a-tuple")
        assert provider_fallback_active(provider2) is False
        provider3 = MagicMock()
        setattr(provider3, TURN_FALLBACK_ATTR, ("only-one",))
        assert provider_fallback_active(provider3) is False


class TestAdvanceFallbackCandidateAutoPrimary:
    """The shared walk seeds an empty active-model read as "auto".

    ``provider_active_model`` deliberately filters the ``"auto"`` sentinel, so
    an auto-routed session reads as ``""``. Left unseeded, that empty primary
    (a) let the dashboard's stale-clear arm skip the slot heal — a temporary
    fallback became a silent permanent pin — and (b) allowed an auto->auto
    no-op swap announced by a lying notice card.
    """

    def _provider(self) -> MagicMock:
        provider = MagicMock()
        provider._model = "auto"
        provider.served_model = None  # not a str -> ignored by the reader
        provider.available_models = None  # advertised unknown -> fail-open

        # Successful set_model syncs _model (real-provider behavior) so the
        # witness observes the switch.
        async def _move(model_id: str) -> None:
            provider._model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        # No surviving marker: the getattr must yield a non-tuple.
        setattr(provider, TURN_FALLBACK_ATTR, None)
        return provider

    @pytest.mark.asyncio
    async def test_auto_chain_on_auto_session_is_exhausted_not_noop(self) -> None:
        # chain ("auto",) on an auto-routed session: nothing to fall back to —
        # the walk must exhaust (original error surfaces), never "swap" to the
        # model already serving.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        fb = FallbackState(chain=("auto",))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand is None
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concrete_candidate_seeds_auto_primary(self) -> None:
        # chain (id, "auto") on an auto-routed session: the concrete candidate
        # applies, and the recorded primary is "auto" — the restore probe then
        # re-enters auto routing instead of tripping the empty-primary arm.
        from kiro_crew.llm_helpers import FallbackState, advance_fallback_candidate

        provider = self._provider()
        fb = FallbackState(chain=("claude-opus-4.8", "auto"))
        cand = await advance_fallback_candidate(provider, fb, surface="test")
        assert cand == "claude-opus-4.8"
        assert fb.primary == "auto"
        marker = getattr(provider, TURN_FALLBACK_ATTR)
        assert marker == ("auto", "claude-opus-4.8")


class TestFallbackState:
    """Chain-walk state: monotonic position, no candidate revisited."""

    def test_walks_in_order_and_exhausts(self) -> None:
        st = FallbackState(("m1", "m2", "m3"))
        adv = ["m1", "m2", "m3"]
        assert st.next_candidate("active", adv) == "m1"
        assert st.next_candidate("active", adv) == "m2"
        assert st.next_candidate("active", adv) == "m3"
        assert st.next_candidate("active", adv) is None
        assert st.next_candidate("active", adv) is None  # stays exhausted

    def test_skip_advances_past_unusable(self) -> None:
        st = FallbackState(("active-model", "unadvertised", "m2"))
        assert st.next_candidate("active-model", ["active-model", "m2"]) == "m2"
        assert st.next_candidate("active-model", ["active-model", "m2"]) is None


class TestStreamAndCollectThrottleFallback:
    """Case 2.75: throttle-exhaustion fallback chain (agent.fallback_model)."""

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"
    _AUTH = "Bedrock authentication failed. Run 'ada credentials update'"

    @staticmethod
    def _provider(stream, advertised=("fb-1", "fb-2")):
        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = stream
        provider.available_models = MagicMock(return_value=[{"modelId": m} for m in advertised])
        provider.served_model = "primary-model"
        provider._model = "primary-model"

        # Mirror the real substitute set_model: a successful send syncs the
        # provider's model attrs (both AcpClient and AcpSessionProvider do).
        # The witness in advance_fallback_candidate/probe_fallback_restore
        # reads this to distinguish a real switch from a silent no-op.
        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_fallback_serves_turn_after_budget_exhaustion(self) -> None:
        """Same-model budget exhausts, the first advertised candidate is set
        and serves the turn; the sticky marker is published on the provider."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb-result"
        assert call_count == _TRANSIENT_RETRIES + 2
        provider.set_model.assert_awaited_once_with("fb-1")
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-1")

    @pytest.mark.asyncio
    async def test_two_attempts_per_candidate_then_advance(self) -> None:
        """A failing candidate gets exactly FALLBACK_CANDIDATE_ATTEMPTS
        attempts, then the chain advances to the next candidate."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0
        fail_until = _TRANSIENT_RETRIES + 1 + FALLBACK_CANDIDATE_ATTEMPTS

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= fail_until:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb2-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb2-result"
        # 4 same-model + 2 on fb-1 + 1 on fb-2 (success)
        assert call_count == fail_until + 1
        assert [c.args[0] for c in provider.set_model.await_args_list] == ["fb-1", "fb-2"]
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-2")

    @pytest.mark.asyncio
    async def test_chain_exhaustion_surfaces_original_error_class(self) -> None:
        """Every candidate fails: the ORIGINAL error class propagates, carrying
        the chain's story for the delivering surface."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        # 4 same-model + 2 per candidate × 2 candidates
        assert call_count == (_TRANSIENT_RETRIES + 1) + 2 * FALLBACK_CANDIDATE_ATTEMPTS
        story = getattr(ei.value, "_kc_fallback_story", "")
        assert "fb-1" in story and "fb-2" in story
        assert "primary-model" in story

    @pytest.mark.asyncio
    async def test_empty_chain_is_todays_behavior(self) -> None:
        """REGRESSION PIN: with no chain configured, behavior is byte-for-byte
        the pre-feature error surface — same attempt count, no set_model."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test")

        assert call_count == _TRANSIENT_RETRIES + 1
        provider.set_model.assert_not_awaited()
        assert not hasattr(ei.value, "_kc_fallback_story")

    @pytest.mark.asyncio
    async def test_non_transient_error_mid_chain_propagates_immediately(self) -> None:
        """An auth/validation error raised by a candidate fails fast — the
        chain is for transient throttles only."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            raise AcpError(self._AUTH)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError) as ei:
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert call_count == _TRANSIENT_RETRIES + 2  # one attempt on fb-1, then fatal
        assert "authentication" in str(ei.value)
        provider.set_model.assert_awaited_once_with("fb-1")

    @pytest.mark.asyncio
    async def test_partial_output_never_falls_back(self) -> None:
        """Streamed tokens block ALL retry paths, fallback included — a re-run
        would duplicate the already-emitted output."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
            raise AcpError(self._TRANSIENT)

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", fallback_models=["fb-1"])

        assert call_count == 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unadvertised_candidates_are_skipped(self) -> None:
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream, advertised=("fb-2",))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "ok"
        provider.set_model.assert_awaited_once_with("fb-2")

    @pytest.mark.asyncio
    async def test_retry_transient_false_disables_fallback(self) -> None:
        """A caller that owns the outer transient loop also owns fallback
        policy — the inner chain must not fire."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(
                provider, "test", retry_transient=False, fallback_models=["fb-1"]
            )

        assert call_count == 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_completed_tool_call_disables_the_chain(self) -> None:
        """REGRESSION (review finding): a tool call can complete an EXTERNAL
        MUTATION before any text streams. The same-model retry (Case 2,
        pre-existing semantics) still runs, but the fallback chain must NOT
        replay the original prompt — that would re-run the mutation once per
        candidate attempt. Any fired tool across any attempt disables the
        chain and the error surfaces as before the feature."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Tool fires (external mutation completes), then the backend
                # throttles before any text streams.
                yield LLMEvent(kind=EVENT_TOOL_CALL, title="mutate-thing")
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = self._provider(_stream)
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AcpError):
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        # Case 2's same-model budget still applied (pre-existing behavior),
        # but the chain never engaged: no set_model, no extra replays.
        assert call_count == _TRANSIENT_RETRIES + 1
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marker_seeded_primary_survives_second_walk(self) -> None:
        """REGRESSION (review finding): a session already sitting on a
        fallback (marker P->F1, restore failing) that exhausts again must
        keep P as the primary — never record F1 as the primary — and must
        not re-try the currently-failing F1."""
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count <= _TRANSIENT_RETRIES + 1:
                raise AcpError(self._TRANSIENT)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb2-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = self._provider(_stream)
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _set_model(model_id):
            if model_id == "true-primary":
                # The primary is still throttled: the entry restore probe fails.
                raise AcpError(self._TRANSIENT)
            # Successful switch syncs the model attrs (real-provider behavior)
            # so the walk witness observes it.
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_set_model)
        setattr(provider, TURN_FALLBACK_ATTR, ("true-primary", "fb-1"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        assert result == "fb2-result"
        # Restore probe tried the true primary; the walk then went straight to
        # fb-2 (skipping the currently-failing fb-1) and PRESERVED the true
        # primary in the marker — never fb-1.
        assert [c.args[0] for c in provider.set_model.await_args_list] == [
            "true-primary",
            "fb-2",
        ]
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("true-primary", "fb-2")


class TestProbeFallbackRestore:
    """Sticky-restore probe: one set_model(primary) at the next turn start."""

    @staticmethod
    def _provider(served="fb-1"):
        provider = AsyncMock()
        provider.served_model = served
        provider._model = served

        # Successful set_model syncs the model attrs (real-provider behavior);
        # the restore witness reads this to confirm the session moved.
        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_restores_primary_and_clears_marker(self) -> None:
        provider = self._provider(served="fb-1")
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        provider.set_model.assert_awaited_once_with("primary-model")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_failed_restore_keeps_fallback(self) -> None:
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock(side_effect=AcpError("throttled again"))
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        assert getattr(provider, TURN_FALLBACK_ATTR) == ("primary-model", "fb-1")

    @pytest.mark.asyncio
    async def test_stale_marker_cleared_without_touching_model(self) -> None:
        """The session moved off our fallback by other means (explicit user
        pick / reset): drop the marker, never override the newer choice."""
        provider = self._provider(served="user-picked-model")
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))
        await probe_fallback_restore(provider)
        provider.set_model.assert_not_awaited()
        assert getattr(provider, TURN_FALLBACK_ATTR) is None

    @pytest.mark.asyncio
    async def test_no_marker_is_a_noop(self) -> None:
        provider = self._provider()
        setattr(provider, TURN_FALLBACK_ATTR, None)
        await probe_fallback_restore(provider)
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_and_collect_probes_restore_at_entry(self) -> None:
        """A provider carrying the sticky marker gets one restore attempt
        BEFORE the turn streams."""

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _move(model_id):
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-model", "fb-1"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok"
        provider.set_model.assert_awaited_once_with("primary-model")
        assert getattr(provider, TURN_FALLBACK_ATTR) is None


class TestRecordInteractionEvent:
    """The shared per-interaction telemetry helper used by every surface."""

    def _install_stub(self, monkeypatch, record):
        import kiro_crew.platform as platform

        telemetry = MagicMock()
        telemetry.record_event = record
        ctx = MagicMock()
        ctx.telemetry = telemetry
        monkeypatch.setattr(platform, "current_context", lambda: ctx)
        return telemetry

    def test_records_metadata_payload(self, monkeypatch) -> None:
        calls: list = []
        self._install_stub(monkeypatch, lambda etype, data: calls.append((etype, data)))

        # After Kiro startup client._client is an AcpSessionProvider exposing a
        # ``model`` property (backed by _handle.model). Model the real shape.
        client = MagicMock()
        client._client.model = "test-model-id"

        record_interaction_event(client, "sess-1", "dashboard")

        assert calls == [
            (
                "interaction",
                {"session_key": "sess-1", "surface": "dashboard", "model": "test-model-id"},
            ),
        ]

    def test_reads_model_from_raw_client_model_attr(self, monkeypatch) -> None:
        """Pre-startup / raw AcpClient exposes the configured model on the
        ``_model`` attribute (no ``model`` property); the extraction falls back
        to it. Use a plain object so ``model`` genuinely doesn't exist."""
        calls: list = []
        self._install_stub(monkeypatch, lambda etype, data: calls.append((etype, data)))

        class _RawClient:
            _model = "raw-model-id"

        class _Provider:
            def __init__(self, inner):
                self._client = inner

        record_interaction_event(_Provider(_RawClient()), "sess-1", "slack")
        assert calls[0][1]["model"] == "raw-model-id"

    def test_missing_model_falls_back_to_empty_string(self, monkeypatch) -> None:
        calls: list = []
        self._install_stub(monkeypatch, lambda etype, data: calls.append((etype, data)))

        # A plain object with no _client/_model attributes.
        client = object()
        record_interaction_event(client, "sess-2", "slack")  # type: ignore[arg-type]

        assert calls[0][1] == {"session_key": "sess-2", "surface": "slack", "model": ""}

    def test_telemetry_failure_is_swallowed(self, monkeypatch) -> None:
        def _boom(etype, data):
            raise RuntimeError("sink down")

        self._install_stub(monkeypatch, _boom)

        # Must not raise — best-effort only.
        record_interaction_event(MagicMock(), "sess-3", "dashboard")


class TestFallbackRetryBudgetSingleBody:
    """#5447 item 2: the per-candidate retry budget lives in ONE place."""

    def test_no_active_candidate_never_retries(self) -> None:
        st = FallbackState(("m1",))
        assert st.should_retry_active() is False
        assert st.attempts == 0

    def test_consumes_attempts_up_to_the_budget(self) -> None:
        st = FallbackState(("m1",), active="m1", attempts=1)
        # attempts=1 is how advance_fallback_candidate seeds a fresh candidate.
        for expected in range(2, FALLBACK_CANDIDATE_ATTEMPTS + 1):
            assert st.should_retry_active() is True
            assert st.attempts == expected
        assert st.should_retry_active() is False
        assert st.attempts == FALLBACK_CANDIDATE_ATTEMPTS

    def test_dashboard_rewind_derives_from_the_same_constant(self) -> None:
        """The interactive ladder's counter rewind grants a fresh candidate
        exactly FALLBACK_CANDIDATE_ATTEMPTS - 1 further same-model passes —
        the same budget the unattended surfaces consume via
        should_retry_active."""
        from kiro_crew.llm_helpers import (
            TRANSIENT_RETRIES,
            fallback_rewound_transient_budget,
        )

        assert fallback_rewound_transient_budget() == TRANSIENT_RETRIES - (
            FALLBACK_CANDIDATE_ATTEMPTS - 1
        )


class TestFallbackExhaustionStory:
    """The chain-exhausted story is built in ONE place (FallbackState)."""

    def test_story_names_primary_and_every_walked_candidate(self) -> None:
        st = FallbackState(("a", "b"), primary="primary-m", walked=["a", "b"])
        assert st.exhaustion_story() == ("primary-m throttled; fallbacks a, b also unavailable")

    def test_unwalked_chain_has_no_story(self) -> None:
        """Nothing actually ran (all candidates skipped) ⇒ the error surfaces
        exactly as before the feature — no story."""
        assert FallbackState(("a",), primary="p").exhaustion_story() is None

    def test_unknown_primary_uses_placeholder(self) -> None:
        story = FallbackState(("a",), walked=["a"]).exhaustion_story()
        assert story is not None and story.startswith("the selected model throttled")


class TestFallbackStoryConsumer:
    """#5447 item 1: append_fallback_story is THE reader of the story attr."""

    def _exc_with_story(self, story: str = "primary-m throttled; fallbacks fb-1 also unavailable"):
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR

        exc = AcpError("Internal server error")
        setattr(exc, FALLBACK_STORY_ATTR, story)
        return exc

    def test_appends_story_to_terminal_error_text(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        out = append_fallback_story("AcpError: Internal server error", self._exc_with_story())
        assert out == (
            "AcpError: Internal server error "
            "[primary-m throttled; fallbacks fb-1 also unavailable]"
        )

    def test_no_story_returns_text_byte_for_byte(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("AcpError: boom", AcpError("boom")) == "AcpError: boom"

    def test_empty_text_returns_bare_story(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("", self._exc_with_story("s")) == "s"

    def test_non_string_story_attr_is_ignored(self) -> None:
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, ["not", "a", "string"])
        assert append_fallback_story("t", exc) == "t"

    def test_stream_and_collect_sets_the_attr_this_reads(self) -> None:
        """DRIFT PIN: the producer (Case 2.75) and this consumer agree on the
        attribute — fallback_story_of reads back what the walk attached."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, fallback_story_of

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "the story")
        assert fallback_story_of(exc) == "the story"
        assert fallback_story_of(AcpError("boom")) == ""


class TestAnnotateModelFallbackSharedBody:
    """#5447 item 4: one spelling of the fallback-served warning."""

    def test_prefixes_warning_from_marker(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))
        out = annotate_model_fallback("body text", provider)
        assert out.startswith("⚠️ Model 'primary-m' throttled")
        assert "fallback 'fb-m'" in out
        assert out.endswith("\n\nbody text")

    def test_empty_text_yields_bare_warning_line(self) -> None:
        """The sub-agent call site can pass an empty result — the warning
        stands alone rather than trailing a blank separator."""
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))
        out = annotate_model_fallback("", provider)
        assert out.startswith("⚠️ Model 'primary-m' throttled")
        assert "\n\n" not in out

    def test_no_marker_and_malformed_marker_are_noops(self) -> None:
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        assert annotate_model_fallback("text", SimpleNamespace()) == "text"
        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("only-one",))
        assert annotate_model_fallback("text", provider) == "text"


class TestProbeFallbackRestoreSlotSeams:
    """#5447 item 3: the parameter seams the dashboard's slot probe wraps.

    The slot probe (chat_runner._probe_fallback_restore_for_slot_locked) is a
    thin adapter over this single body; these tests pin the seams it depends
    on: slot-held state, the extra staleness flag, the replacement clear, and
    the pre-clear heal hook.
    """

    @staticmethod
    def _provider(served: str = "fb-1"):
        provider = AsyncMock()
        provider.served_model = served
        provider._model = served

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)
        return provider

    @pytest.mark.asyncio
    async def test_state_override_restores_and_runs_hooks_in_order(self) -> None:
        provider = self._provider(served="fb-1")
        order: list[str] = []
        await probe_fallback_restore(
            provider,
            surface="dashboard",
            state=("primary-model", "fb-1"),
            clear=lambda: order.append("clear"),
            on_restored=lambda: order.append("heal"),
            log_suffix=", slot=s1",
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        # The heal must see the pre-clear state: heal strictly before clear.
        assert order == ["heal", "clear"]

    @pytest.mark.asyncio
    async def test_stale_flag_clears_without_restoring(self) -> None:
        """The dashboard's explicit-pick generation check rides in as `stale`:
        an explicit pick must never be overridden by a restore."""
        provider = self._provider(served="fb-1")
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            stale=True,
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_unknown_primary_clears_without_restoring(self) -> None:
        provider = self._provider(served="fb-1")
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("", "fb-1"),
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_empty_candidate_state_is_a_noop(self) -> None:
        """Matches the slot probe's no-active-fallback early return: nothing
        restored, nothing cleared."""
        provider = self._provider()
        cleared: list[bool] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", ""),
            clear=lambda: cleared.append(True),
        )
        provider.set_model.assert_not_awaited()
        assert cleared == []

    @pytest.mark.asyncio
    async def test_failed_restore_keeps_state_and_skips_hooks(self) -> None:
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock(side_effect=AcpError("throttled again"))
        called: list[str] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=lambda: called.append("clear"),
            on_restored=lambda: called.append("heal"),
        )
        assert called == []

    @pytest.mark.asyncio
    async def test_silent_noop_restore_keeps_state_and_skips_hooks(self) -> None:
        """A non-raising set_model that did not move the session (witness
        check) must keep the sticky state — clearing would let the backfill
        pin the fallback permanently."""
        provider = self._provider(served="fb-1")
        provider.set_model = AsyncMock()  # returns without syncing the model
        called: list[str] = []
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=lambda: called.append("clear"),
            on_restored=lambda: called.append("heal"),
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        assert called == []


class TestCase275RoutesThroughSharedBudgetBody:
    """DRIFT PIN (#5447 item 2): Case 2.75 must consult should_retry_active.

    Mutation check in reverse: forcing the shared body to refuse retries
    changes this surface's attempt count — proof the budget is not re-encoded
    locally. The sub-agent ladder has the mirror pin in
    test_subagent_turn_resilience.py.
    """

    _TRANSIENT = "Prompt error: {'message': 'Internal error: API Error: Internal server error'}"

    @pytest.mark.asyncio
    async def test_forcing_the_shared_body_to_refuse_drops_candidate_retries(self) -> None:
        from kiro_crew.llm_helpers import _TRANSIENT_RETRIES

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            raise AcpError(self._TRANSIENT)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream
        provider.available_models = MagicMock(
            return_value=[{"modelId": "fb-1"}, {"modelId": "fb-2"}]
        )
        provider.served_model = "primary-model"
        provider._model = "primary-model"

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(FallbackState, "should_retry_active", return_value=False),
            pytest.raises(AcpError),
        ):
            await stream_and_collect(provider, "test", fallback_models=["fb-1", "fb-2"])

        # Budget refused ⇒ each candidate gets only the single post-advance
        # attempt: 4 same-model + 1 per candidate (vs 2 each normally).
        assert call_count == (_TRANSIENT_RETRIES + 1) + 2


class TestFallbackStorySafety:
    """Round-2 review pins: the story is redacted+capped CENTRALLY, and the
    consumers/annotator never raise."""

    def test_story_is_capped_centrally(self) -> None:
        """Walked ids originate in config (advertised check fails open), so an
        arbitrarily long story must be bounded before riding any surface."""
        from kiro_crew.llm_helpers import (
            _FALLBACK_STORY_CAP,
            FALLBACK_STORY_ATTR,
            fallback_story_of,
        )

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "m" * (_FALLBACK_STORY_CAP * 3))
        assert len(fallback_story_of(exc)) == _FALLBACK_STORY_CAP

    def test_story_is_redacted_centrally(self) -> None:
        """A credential-shaped token in the story never reaches a consumer —
        heartbeat logs the story with no redaction of its own."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, fallback_story_of

        exc = AcpError("boom")
        setattr(
            exc,
            FALLBACK_STORY_ATTR,
            "AKIAIOSFODNN7EXAMPLE throttled; fallbacks fb-1 also unavailable",
        )
        story = fallback_story_of(exc)
        assert "AKIAIOSFODNN7EXAMPLE" not in story
        assert "also unavailable" in story

    def test_story_read_survives_hostile_exception_attr(self) -> None:
        """The attribute read itself is guarded — a raising property on an
        exotic exception type must not break the failure handler reading it."""
        from kiro_crew.llm_helpers import append_fallback_story

        class _HostileExc(Exception):
            @property
            def _kc_fallback_story(self):  # FALLBACK_STORY_ATTR
                raise RuntimeError("hostile attr")

        assert append_fallback_story("t", _HostileExc()) == "t"

    def test_annotate_never_raises_on_hostile_marker(self) -> None:
        """An annotation failure must not turn a successful run into a failed
        one — the un-annotated text comes back instead (the sub-agent call
        site relies on this; its old inline guard is gone)."""
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import annotate_model_fallback

        class _Boom:
            def __str__(self) -> str:  # str(primary) raises inside the helper
                raise RuntimeError("hostile id")

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, (_Boom(), "fb-m"))
        assert annotate_model_fallback("the result", provider) == "the result"

    @pytest.mark.asyncio
    async def test_probe_survives_raising_hooks(self) -> None:
        """probe_fallback_restore promises 'never raises' — caller-supplied
        heal/clear hooks are contained, and a failed heal does not block the
        clear (the two writes are one logical record)."""
        provider = AsyncMock()
        provider.served_model = "fb-1"
        provider._model = "fb-1"

        async def _move(model_id: str) -> None:
            provider._model = model_id
            provider.served_model = model_id

        provider.set_model = AsyncMock(side_effect=_move)

        cleared: list[bool] = []

        def _bad_heal() -> None:
            raise RuntimeError("heal boom")

        def _clear() -> None:
            cleared.append(True)

        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=_clear,
            on_restored=_bad_heal,
        )
        provider.set_model.assert_awaited_once_with("primary-model")
        assert cleared == [True]

    @pytest.mark.asyncio
    async def test_probe_survives_raising_clear_on_stale_path(self) -> None:
        provider = AsyncMock()
        provider.served_model = "other-model"
        provider._model = "other-model"
        provider.set_model = AsyncMock()

        def _bad_clear() -> None:
            raise RuntimeError("clear boom")

        # Moved-off session: the stale path runs the (raising) clear — must
        # not propagate.
        await probe_fallback_restore(
            provider,
            state=("primary-model", "fb-1"),
            clear=_bad_clear,
        )
        provider.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_malformed_state_is_a_noop(self) -> None:
        provider = AsyncMock()
        provider.set_model = AsyncMock()
        await probe_fallback_restore(provider, state=("only-one",))  # type: ignore[arg-type]
        provider.set_model.assert_not_awaited()

    def test_rewound_budget_is_clamped_at_zero(self) -> None:
        """A future FALLBACK_CANDIDATE_ATTEMPTS above TRANSIENT_RETRIES + 1
        cannot be expressed by the counter encoding — it clamps to zero (the
        full same-model budget) instead of going negative."""
        import kiro_crew.llm_helpers as lh

        with patch.object(lh, "FALLBACK_CANDIDATE_ATTEMPTS", lh.TRANSIENT_RETRIES + 5):
            assert lh.fallback_rewound_transient_budget() == 0

    def test_budgeted_append_keeps_the_story_over_the_error_tail(self) -> None:
        """With a budget, the ERROR text is trimmed to leave the story room —
        a verbose exception must never push the walk out of the composite —
        and the total stays within the budget."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-1 also unavailable")
        out = append_fallback_story("E" * 5000, exc, budget=2000)
        assert len(out) <= 2000
        assert out.endswith("[primary-m throttled; fallbacks fb-1 also unavailable]")

    def test_budgeted_append_without_story_just_caps(self) -> None:
        from kiro_crew.llm_helpers import append_fallback_story

        assert append_fallback_story("E" * 5000, AcpError("x"), budget=2000) == "E" * 2000

    def test_negative_and_zero_budgets_mean_empty_not_unbounded(self) -> None:
        """A negative slice drops characters from the END — a negative budget
        must tighten the bound to nothing, never loosen it."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "s" * 50)
        assert append_fallback_story("E" * 100, exc, budget=0) == ""
        assert append_fallback_story("E" * 100, exc, budget=-5) == ""
        assert append_fallback_story("E" * 100, AcpError("x"), budget=-5) == ""

    def test_oversized_story_cannot_evict_the_error_text(self) -> None:
        """The error text keeps a floor of half the budget even when the story
        alone fills the whole budget (possible when a caller's cap equals
        _FALLBACK_STORY_CAP): past the floor the story tail truncates, the
        error never vanishes from the alert."""
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR, append_fallback_story

        exc = AcpError("boom")
        setattr(exc, FALLBACK_STORY_ATTR, "s" * 500)
        out = append_fallback_story("E" * 400, exc, budget=500)
        assert len(out) == 500
        assert out.startswith("E" * 250)  # error floor = budget // 2
        assert "sss" in out  # the story is still present, truncated

    @pytest.mark.asyncio
    async def test_probe_never_raises_on_hostile_marker_read(self) -> None:
        """The marker property itself can raise (exotic providers) — the
        probe skips instead of propagating."""

        class _HostileProvider:
            served_model = "fb-1"
            _model = "fb-1"

            @property
            def _kc_active_fallback(self):  # TURN_FALLBACK_ATTR
                raise RuntimeError("hostile marker")

        await probe_fallback_restore(_HostileProvider())  # must not raise

    @pytest.mark.asyncio
    async def test_probe_never_raises_on_hostile_candidate_str(self) -> None:
        class _Boom:
            def __str__(self) -> str:
                raise RuntimeError("hostile id")

            def __bool__(self) -> bool:
                return True

        provider = AsyncMock()
        provider.served_model = "fb-1"
        provider._model = "fb-1"
        provider.set_model = AsyncMock()
        await probe_fallback_restore(provider, state=("primary-model", _Boom()))
        provider.set_model.assert_not_awaited()
