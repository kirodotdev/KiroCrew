"""Tests for the llm_helpers module — shared LLM interaction utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    parse_llm_json,
    parse_llm_json_list,
    record_interaction_event,
    save_conversation_turn,
    stream_and_collect,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent


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
