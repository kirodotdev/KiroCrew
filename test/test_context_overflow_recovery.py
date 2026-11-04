"""A turn refused as too long for the model ends the walk and names its fix.

The claude adapter answers an over-full conversation with

    {'code': -32603, 'message': 'Internal error: API Error: 400 Input is too long.',
     'data': {'errorKind': 'unknown'}}

The overflow is in the history the adapter re-sends on every turn, so every
retry, every Resume and every model in the fallback chain is refused the same
way. These tests pin the three things that follow from that:

- the frame is tagged a context overflow at raise time (``context_overflow``,
  ``structural_terminal``) and is never transient, so neither the same-model
  retry ladder nor the throttle fallback chain re-sends it;
- the user-facing text says why and names the fix, a fresh session;
- the dashboard's terminal row carries the ``context_overflow`` kind, which is
  what the error card reads to offer "Start a fresh session" instead of Resume.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.client import (
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
)
from kiro_crew.dashboard.chat_utils import CONTEXT_OVERFLOW_KIND
from kiro_crew.llm_helpers import (
    acp_error_is_transient,
    is_context_overflow_error,
    stream_and_collect,
)

_INPUT_TOO_LONG = {
    "code": -32603,
    "message": "Internal error: API Error: 400 Input is too long.",
    "data": {"errorKind": "unknown"},
}

_PROMPT_TOO_LONG = {
    "code": -32603,
    "message": "Internal error: Prompt is too long",
    "data": {"errorKind": "unknown"},
}

_KIRO_OVERFLOW = {
    "code": -32603,
    "message": "Internal error",
    "data": "context window overflowed",
}


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


_ADAPTER_FRAMES = [_INPUT_TOO_LONG, _PROMPT_TOO_LONG]


@pytest.mark.parametrize("frame", _ADAPTER_FRAMES)
class TestTheAdapterOverflowIsTagged:
    def test_it_is_structural_so_a_self_driving_loop_stops(self, frame):
        assert _raised(frame).structural_terminal is True


class TestTheTagReadsTheAdaptersOwnWording:
    def test_a_retry_hint_in_the_same_frame_cannot_rescue_it(self):
        frame = dict(_INPUT_TOO_LONG, data="Service hiccup, please try again.")
        assert _is_transient_raw_error(frame) is False

    def test_a_phrase_echo_deeper_in_another_message_is_not_an_overflow(self):
        """Anchored at the start of ``message``: an unrelated error that merely
        quotes the words further in (a tool's stderr, a user's own text) must
        not be retagged as a full conversation."""
        frame = {
            "code": -32603,
            "message": "Internal error: tool failed: grep said 'Input is too long'",
            "data": {"errorKind": "unknown"},
        }
        err = _raised(frame)
        assert getattr(err, "context_overflow", False) is False

    def test_the_kiro_cli_overflow_keeps_its_tag(self):
        assert _raised(_KIRO_OVERFLOW).context_overflow is True


class TestTheUserIsToldTheFix:
    def test_the_text_names_the_cause_and_the_fresh_session(self):
        out = _format_acp_error(_INPUT_TOO_LONG)
        assert "Input is too long" in out
        assert "images" in out
        assert "Start a fresh session" in out
        assert "switching models will not help" in out
        assert "retry in a moment" not in out.lower()
        assert "errorKind" not in out


class TestTheFallbackChainDoesNotWalk:
    @pytest.mark.asyncio
    async def test_a_configured_chain_is_never_tried(self):
        """With a chain configured, the overflow is sent ONCE and propagates:
        no same-model retry, no set_model onto a fallback candidate."""
        calls = 0

        async def _stream(msg):
            nonlocal calls
            calls += 1
            _raise_acp_error(_INPUT_TOO_LONG)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.set_model = AsyncMock()
        provider.stream = _stream

        # No sleep patch: a terminal verdict never reaches a backoff, so the
        # call count alone is the evidence.
        with pytest.raises(AcpError) as exc:
            await stream_and_collect(
                provider, "hi", fallback_models=("claude-opus-4.8", "claude-opus-4.7")
            )

        assert calls == 1
        provider.set_model.assert_not_awaited()
        assert exc.value.context_overflow is True


class TestTheDashboardRowCarriesTheKind:
    def test_terminal_meta_is_context_overflow(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta

        assert _terminal_error_meta(_raised(_INPUT_TOO_LONG)) == {"kind": CONTEXT_OVERFLOW_KIND}
        assert _terminal_error_meta(_raised(_KIRO_OVERFLOW)) == {"kind": CONTEXT_OVERFLOW_KIND}

    def test_an_untagged_error_keeps_a_plain_row(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta

        assert _terminal_error_meta(AcpError("boom")) is None

    def test_the_sign_in_fix_outranks_it(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta
        from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND

        err = _raised(_INPUT_TOO_LONG)
        err.auth_required = True
        assert _terminal_error_meta(err) == {"kind": AUTH_REQUIRED_KIND}


# The model service's byte cap, relayed by kiro-cli. Which JSON-RPC
# field carries the sentence is not captured, so both shapes are pinned.
_SIZE_SENTENCE = (
    "Request exceeds the maximum allowed size (Request ID: 0f8e2b7c-1d4a-4e55-9a31-"
    "6c2b9f0d7e11): {errorType: GenericValidationError, retryErrorType: CLIENT_ERROR}"
)

_SIZE_IN_DATA = {"code": -32603, "message": "Internal error", "data": _SIZE_SENTENCE}

_SIZE_IN_STREAM_ENVELOPE = {
    "code": -32603,
    "message": "Internal error",
    "data": f"Encountered an error in the response stream: {_SIZE_SENTENCE}",
}

_SIZE_IN_MESSAGE = {
    "code": -32603,
    "message": f"Internal error: {_SIZE_SENTENCE}",
    "data": {"errorKind": "unknown"},
}


_SIZE_AFTER_API_STATUS = {
    "code": -32603,
    "message": f"Internal error: API Error: 400 {_SIZE_SENTENCE}",
    "data": {"errorKind": "unknown"},
}


_SIZE_CAP_FRAMES = [
    _SIZE_IN_DATA,
    _SIZE_IN_STREAM_ENVELOPE,
    _SIZE_IN_MESSAGE,
    _SIZE_AFTER_API_STATUS,
]


@pytest.mark.parametrize("frame", _SIZE_CAP_FRAMES)
class TestTheRequestSizeCapIsAnOverflow:
    def test_it_is_structural_so_a_self_driving_loop_stops(self, frame):
        assert _raised(frame).structural_terminal is True

    def test_the_dashboard_row_offers_a_fresh_session(self, frame):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta

        assert _terminal_error_meta(_raised(frame)) == {"kind": CONTEXT_OVERFLOW_KIND}

    def test_the_text_fallback_reads_the_formatted_sentence(self, frame):
        assert is_context_overflow_error(AcpError(_format_acp_error(frame))) is True


_EVERY_OVERFLOW_FRAME = [
    *(
        pytest.param(f, id=f"TheAdapterOverflowIsTagged-frame{i}")
        for i, f in enumerate(_ADAPTER_FRAMES)
    ),
    *(
        pytest.param(f, id=f"TheRequestSizeCapIsAnOverflow-frame{i}")
        for i, f in enumerate(_SIZE_CAP_FRAMES)
    ),
]


@pytest.mark.parametrize("frame", _EVERY_OVERFLOW_FRAME)
def test_it_is_a_context_overflow(frame):
    err = _raised(frame)
    assert err.context_overflow is True
    assert is_context_overflow_error(err) is True


@pytest.mark.parametrize("frame", _EVERY_OVERFLOW_FRAME)
def test_it_is_never_transient(frame):
    assert _is_transient_raw_error(frame) is False
    assert acp_error_is_transient(_raised(frame)) is False


class TestTheRequestSizeCapReadsOnlyItsOwnSentence:
    def test_a_5xx_token_in_the_same_frame_cannot_rescue_it(self):
        frame = dict(_SIZE_IN_MESSAGE, data="HTTP 503 Service Unavailable, please try again.")
        assert _is_transient_raw_error(frame) is False

    @pytest.mark.asyncio
    async def test_it_is_sent_once_and_the_chain_is_never_tried(self):
        calls = 0

        async def _stream(msg):
            nonlocal calls
            calls += 1
            _raise_acp_error(_SIZE_IN_DATA)
            yield  # pragma: no cover

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.set_model = AsyncMock()
        provider.stream = _stream

        with pytest.raises(AcpError) as exc:
            await stream_and_collect(provider, "hi", fallback_models=("claude-opus-4.8",))

        assert calls == 1
        provider.set_model.assert_not_awaited()
        assert exc.value.context_overflow is True

    @pytest.mark.parametrize(
        "frame",
        [
            # The KAS route's missing-profile refusal: a
            # GenericValidation/ValidationException about something else entirely.
            {
                "code": -32603,
                "message": "Internal error",
                "data": "400 ValidationException: profileArn is required for this request. "
                "{errorType: GenericValidationError, retryErrorType: CLIENT_ERROR}",
            },
            {
                "code": -32603,
                "message": "Internal error: 400 ValidationException: profileArn is required",
                "data": {"errorKind": "unknown"},
            },
            # The words echoed further into another error (a tool's stderr, a
            # user's own text) are not the provider's refusal.
            {
                "code": -32603,
                "message": "Internal error",
                "data": "tool failed: upload.sh said 'Request exceeds the maximum allowed size'",
            },
            {
                "code": -32603,
                "message": "Internal error: user wrote: Request exceeds the maximum allowed size",
                "data": {"errorKind": "unknown"},
            },
        ],
    )
    def test_an_unrelated_validation_error_is_not_tagged(self, frame):
        err = _raised(frame)
        assert getattr(err, "context_overflow", False) is False
        assert getattr(err, "structural_terminal", False) is False
