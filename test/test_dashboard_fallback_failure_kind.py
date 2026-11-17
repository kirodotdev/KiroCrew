"""The interactive model fallback reads what spent the retry ladder.

The fallback swap (``agent.fallback_model``) runs once the same-model retries are
spent on a transient error. Two kinds of transient error need more than "swap and
say the model is throttled":

- Kiro's per-user request-rate limit (HTTP 429, ``USER_REQUEST_RATE_EXCEEDED``) is
  shared by every model, so a swap cannot clear it. The turn ends on the limit's
  own error row, with no swap and no fallback notice.
- A ladder spent on server errors (5xx) did not throttle anything, so the notice
  names server errors.

A genuine model throttle keeps today's swap and "is throttled" notice.

The errors are built by the real frame parser, so the tags the dashboard reads
are the ones a live frame produces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from test_dashboard_chat import TestRunChatModelFallback as _FallbackSuite

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.transport_errors import _raise_acp_error

_USER_RATE_FRAME = {
    "code": -32603,
    "message": "You're sending requests faster than your rate limit allows.",
    "data": {
        "errorType": "ClientThrottleError",
        "throttleReason": "USER_REQUEST_RATE_EXCEEDED",
        "retryErrorType": "THROTTLING",
    },
}
_MODEL_THROTTLE_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": "ThrottlingException: Rate exceeded",
}
_SERVER_ERROR_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": "API Error: 500 Internal server error",
}

# A transient that is neither a throttle nor a 5xx: a credential not yet propagated.
_CREDENTIAL_DELAY_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": "UnrecognizedClientException: The security token included in the request is invalid.",
}


def _no_backoff(monkeypatch) -> None:
    """Zero the retry and fallback backoffs at the dashboard's own seams, so the
    ladder runs at once without rebinding the stdlib sleep for the whole worker."""
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "_recovery_delay", AsyncMock())
    monkeypatch.setattr(chat_runner, "transient_retry_delay", lambda *_a, **_k: 0.0)


def _raised(frame: dict) -> AcpError:
    try:
        _raise_acp_error(frame, backend="kas")
    except AcpError as exc:
        return exc
    raise AssertionError("the frame raised nothing")


def test_the_frame_parser_tags_each_kind():
    user = _raised(_USER_RATE_FRAME)
    assert (user.transient, user.throttle, user.user_rate_limit) == (True, True, True)
    model = _raised(_MODEL_THROTTLE_FRAME)
    assert (model.transient, model.throttle, model.user_rate_limit) == (True, True, False)
    server = _raised(_SERVER_ERROR_FRAME)
    assert (server.transient, server.throttle, server.user_rate_limit) == (True, False, False)
    assert (server.server_error, model.server_error, user.server_error) == (True, False, False)
    delay = _raised(_CREDENTIAL_DELAY_FRAME)
    assert (delay.transient, delay.throttle, delay.server_error) == (True, False, False)


class TestFallbackReadsTheFailure:
    _make_state = staticmethod(_FallbackSuite._make_state)
    _wire_sessions = staticmethod(_FallbackSuite._wire_sessions)
    _drain_bg = staticmethod(_FallbackSuite._drain_bg)
    _client = staticmethod(_FallbackSuite._client)

    async def _run(self, tmp_path, monkeypatch, frame):
        """Fail every primary attempt with *frame*; the fallback candidate answers."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model",),
        )
        calls = 0

        async def _stream(msg):
            nonlocal calls
            calls += 1
            if calls <= TRANSIENT_RETRIES + 1:
                raise _raised(frame)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        _no_backoff(monkeypatch)
        await _run_chat(state, slot, "hello")
        await self._drain_bg(state)
        return slot, client

    @staticmethod
    def _rows(slot, role):
        return [m for m in slot.messages if m.get("role") == role]

    @pytest.mark.asyncio
    async def test_a_per_user_rate_limit_ends_the_turn_without_a_swap(self, tmp_path, monkeypatch):
        slot, client = await self._run(tmp_path, monkeypatch, _USER_RATE_FRAME)
        client.set_model.assert_not_awaited()
        assert slot._active_fallback_model == ""
        assert not any(
            (m.get("meta") or {}).get("notice") == "model_throttled_fallback"
            for m in self._rows(slot, "notice")
        )
        errors = [m["content"] for m in self._rows(slot, "error")]
        assert any("Switching to a different model will not help" in t for t in errors), errors

    @pytest.mark.asyncio
    async def test_a_ladder_spent_on_server_errors_names_server_errors(self, tmp_path, monkeypatch):
        slot, client = await self._run(tmp_path, monkeypatch, _SERVER_ERROR_FRAME)
        client.set_model.assert_awaited_once_with("fallback-model")
        notices = self._rows(slot, "notice")
        assert [m["meta"]["notice"] for m in notices] == ["model_server_error_fallback"]
        text = notices[0]["content"]
        assert "keeps returning server errors" in text and "throttled" not in text
        assert notices[0]["meta"]["notice_args"] == {
            "primary": "claude-test-model",
            "fallback": "fallback-model",
        }

    @pytest.mark.asyncio
    async def test_a_model_throttle_still_swaps_and_says_throttled(self, tmp_path, monkeypatch):
        slot, client = await self._run(tmp_path, monkeypatch, _MODEL_THROTTLE_FRAME)
        client.set_model.assert_awaited_once_with("fallback-model")
        notices = self._rows(slot, "notice")
        assert [m["meta"]["notice"] for m in notices] == ["model_throttled_fallback"]
        assert "is throttled" in notices[0]["content"]

    @pytest.mark.asyncio
    async def test_a_transient_that_is_not_a_5xx_is_not_called_a_server_error(
        self, tmp_path, monkeypatch
    ):
        slot, client = await self._run(tmp_path, monkeypatch, _CREDENTIAL_DELAY_FRAME)
        client.set_model.assert_awaited_once_with("fallback-model")
        notices = self._rows(slot, "notice")
        assert [m["meta"]["notice"] for m in notices] == ["model_throttled_fallback"]

    @pytest.mark.asyncio
    async def test_a_later_candidates_5xx_is_not_named_as_the_selected_models(
        self, tmp_path, monkeypatch
    ):
        """The selected model throttles, the first fallback then returns 5xx: every
        swap notice names the selected model, so each says what that model did."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.llm_helpers import TRANSIENT_RETRIES
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
            lambda: ("fallback-model", "second-model"),
        )
        client = None

        async def _stream(msg):
            model = client._model
            if model == "claude-test-model":
                raise _raised(_MODEL_THROTTLE_FRAME)
            if model == "fallback-model":
                raise _raised(_SERVER_ERROR_FRAME)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="fb-result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream, advertised=("fallback-model", "second-model"))
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        _no_backoff(monkeypatch)
        await _run_chat(state, slot, "hello")
        await self._drain_bg(state)

        assert TRANSIENT_RETRIES > 0
        assert [c.args[0] for c in client.set_model.await_args_list][:2] == [
            "fallback-model",
            "second-model",
        ]
        notices = self._rows(slot, "notice")
        assert [m["meta"]["notice"] for m in notices] == ["model_throttled_fallback"] * 2
        assert not any("server errors" in m["content"] for m in notices)
