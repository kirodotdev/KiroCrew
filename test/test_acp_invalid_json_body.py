"""The claude adapter's "request body is not valid JSON" refusal is structural.

Claude Code builds the model request from the whole conversation, so once its
history holds something the model service cannot parse, every later turn is
refused with the same 400 at the same column::

    {'code': -32603,
     'message': 'Internal error: API Error: 400 The request body is not valid '
                'JSON: invalid escape at line 1 column 829692',
     'data': {'errorKind': 'unknown'}}

Retrying, switching models and ``/compact`` all re-send that history. The frame
must therefore be terminal AND structural (so a self-driving loop stops re-firing
it), and the user must be told the one thing that helps: a fresh session.
"""

from __future__ import annotations

from kiro_crew.acp.client import (
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
    classify_provider_error,
)
from kiro_crew.acp.transport_errors import PROVIDER_ERROR_MALFORMED_REQUEST

_SENTENCE = (
    "Internal error: API Error: 400 The request body is not valid JSON: "
    "invalid escape at line 1 column 829692"
)
_INVALID_BODY = {"code": -32603, "message": _SENTENCE, "data": {"errorKind": "unknown"}}


def _raised(error: dict) -> AcpError:
    try:
        _raise_acp_error(error, backend="claude")
    except AcpError as exc:
        return exc
    raise AssertionError("_raise_acp_error did not raise")


def test_the_refusal_is_terminal_and_structural() -> None:
    err = _raised(_INVALID_BODY)
    assert _is_transient_raw_error(_INVALID_BODY) is False
    assert err.transient is False
    assert err.structural_terminal is True


def test_the_user_is_told_to_start_a_fresh_session() -> None:
    out = _format_acp_error(_INVALID_BODY)
    assert "Start a fresh session." in out
    assert "not valid JSON" in out
    # The raw frame shape is gone, and nothing suggests a fix that re-sends the history.
    assert "errorKind" not in out
    assert "/compact" not in out
    assert "retry in a moment" not in out.lower()


def test_the_provider_classifier_names_it_a_malformed_request() -> None:
    verdict = classify_provider_error(_SENTENCE, data="{'errorKind': 'unknown'}")
    assert verdict.kind == PROVIDER_ERROR_MALFORMED_REQUEST
    assert verdict.retryable is False


def test_an_echo_later_in_the_message_does_not_match() -> None:
    """Anchored: a retryable 500 that merely quotes the sentence stays retryable."""
    echo = {
        "code": -32603,
        "message": "Internal error: API Error: 503 upstream said "
        "The request body is not valid JSON",
        "data": {"errorKind": "unknown"},
    }
    assert _is_transient_raw_error(echo) is True
    assert _raised(echo).structural_terminal is False


def test_the_sentence_in_data_alone_does_not_match() -> None:
    """The adapter puts it in ``message``; a ``data`` echo is someone else's text."""
    echo = {
        "code": -32603,
        "message": "Internal error",
        "data": "tool output: The request body is not valid JSON",
    }
    assert _raised(echo).structural_terminal is False
