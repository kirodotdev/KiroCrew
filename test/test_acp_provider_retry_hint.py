"""The claude-agent-acp seam's generic upstream 500 must be classified transient.

``_is_transient_raw_error`` decides retry-eligibility by regex-matching provider
prose, and every pattern in the family was written against Kiro/Bedrock wordings.
The ``claude`` ACP backend (``agent.acp_backend="claude"``, a baseline-selectable
backend that spawns ``@agentclientprotocol/claude-agent-acp``) words its generic
upstream 500 differently, and every pattern missed it for a different reason::

    {'code': -32603,
     'message': 'Internal error: API Error: The system encountered an unexpected '
                'error during processing. Try your request again.',
     'data': {'errorKind': 'unknown'}}

No named exception (``Internal error``, not ``InternalServerError``), no HTTP
status token, not a throttle, no model name — the provider's retry hint is the
ONLY transient signal the frame carries, and ``_RE_5XX_HINT`` wanted the Bedrock
spelling ``please try again``. So ``AcpError.transient`` was ``False``, the
backoff + re-prompt ladder never engaged, and a momentary blip reached the user
as a terminal error card while classified-retry telemetry recorded nothing.

The regression this must NOT cause: the same backend emits a DETERMINISTIC
sibling with the identical ``errorKind: 'unknown'`` —
``'Internal error: API Error: 400 Input is too long.'`` — which can never succeed
on retry. It stays terminal because it carries no retry hint and ``400`` is
outside ``_RE_5XX_STATUS``'s ``50[0234]|529`` set. That is why the fix is one
token on the narrow hint alternation rather than a broader ``errorKind`` rule.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import (
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
)
from kiro_crew.llm_helpers import acp_error_is_transient

# The reported frame, verbatim.
_GENERIC_500 = {
    "code": -32603,
    "message": (
        "Internal error: API Error: The system encountered an unexpected error "
        "during processing. Try your request again."
    ),
    "data": {"errorKind": "unknown"},
}

# Its deterministic sibling: same code, same errorKind, no retry hint.
_INPUT_TOO_LONG = {
    "code": -32603,
    "message": "Internal error: API Error: 400 Input is too long.",
    "data": {"errorKind": "unknown"},
}

# A third shape from the same seam, kept here so the auth guard is exercised
# alongside the two above rather than only in the Bedrock-worded tests.
_AUTH_KIND = {
    "code": -32603,
    "message": "Internal error: API Error: 403 Forbidden",
    "data": {"errorKind": "authentication_failed"},
}


def _raised(error: dict) -> AcpError:
    """The AcpError ``_raise_acp_error`` produces for *error*.

    Asserting through the real raise helper rather than the predicate alone is
    what proves the verdict actually reaches the retry layer: the ladder reads
    ``AcpError.transient`` via ``llm_helpers.acp_error_is_transient``, which
    trusts the structured flag and never consults the string markers.
    """
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


class TestGenericUpstream500IsRetryable:
    def test_the_frame_classifies_transient(self):
        assert _is_transient_raw_error(_GENERIC_500) is True

    def test_the_verdict_reaches_the_retry_layer(self):
        """The ladder reads the structured flag, so that is what must flip."""
        assert acp_error_is_transient(_raised(_GENERIC_500)) is True

    def test_the_user_sees_the_transient_message_not_the_raw_shape(self):
        """One edit fixes both halves: the hint alternation is shared by the
        formatter and the classifier by design, so the friendly text and the
        retryable verdict cannot drift apart."""
        out = _format_acp_error(_GENERIC_500)
        assert "transient error (HTTP 5xx)" in out
        assert "retry in a moment" in out.lower()
        # The raw provider shape is gone from the user-facing string.
        assert "errorKind" not in out
        assert "unexpected error during processing" not in out


class TestTheDeterministicSiblingStaysTerminal:
    """Retrying a 400 burns the retry budget and can never succeed."""

    def test_input_too_long_is_not_transient(self):
        assert _is_transient_raw_error(_INPUT_TOO_LONG) is False

    def test_input_too_long_carries_a_terminal_verdict(self):
        assert acp_error_is_transient(_raised(_INPUT_TOO_LONG)) is False

    def test_input_too_long_is_never_told_to_retry(self):
        out = _format_acp_error(_INPUT_TOO_LONG)
        assert "transient" not in out.lower()
        assert "retry in a moment" not in out.lower()
        # The provider's own sentence survives, which is the actionable part.
        assert "Input is too long" in out

    def test_the_auth_kind_stays_terminal(self):
        assert _is_transient_raw_error(_AUTH_KIND) is False
        assert "transient" not in _format_acp_error(_AUTH_KIND).lower()


class TestTheHintAlternationKeepsItsExistingScope:
    def test_the_bedrock_spelling_still_classifies(self):
        """Adding a token must not cost the wording that was already matched."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Service hiccup, please try again.",
        }
        assert _is_transient_raw_error(err) is True

    def test_the_response_stream_envelope_is_still_not_a_hint(self):
        """kiro-cli wraps EVERY mid-stream failure as "Encountered an error in
        the response stream: <real cause>". Matching the envelope makes this
        branch a catch-all that discards the real cause, so it was deliberately
        removed; a hint token added later must not smuggle it back."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Encountered an error in the response stream: Input is too long",
        }
        assert _is_transient_raw_error(err) is False

    @pytest.mark.parametrize(
        "wording",
        [
            "Try your request again.",
            "try your request again",
            "TRY YOUR REQUEST AGAIN",
        ],
    )
    def test_the_hint_matches_case_insensitively(self, wording):
        assert _is_transient_raw_error({"code": -32603, "message": "", "data": wording}) is True
