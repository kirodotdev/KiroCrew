"""A credential IAM has not propagated yet must be retried, not re-authenticated.

When several sessions cold-start at once (a burst of Slack messages), each mints
a fresh STS token and reaches Bedrock within milliseconds. Bedrock rejects some
of them with ``The security token included in the request is invalid`` — an
IAM-propagation delay, not a bad credential: the IDENTICAL token is accepted a
couple of seconds later. Measured ~60% turn failure on a 5-message burst.

Every auth signal in ``acp/client.py`` was terminal, and this rejection trips
three of them independently, so the fix has to hold at all three seams or the
burst still fails:

1. ``_is_transient_raw_error`` — ``_RE_AUTH`` matches the wrapping
   ``UnrecognizedClientException`` and returns terminal before the 5xx family is
   ever consulted, so ``AcpError.transient`` was ``False`` and the ladder never
   ran. ``_format_acp_error`` keys off the same patterns and must be reworded in
   step, or the two drift.
2. ``llm_helpers._is_transient_acp_error`` — its string fallback hard-excludes
   any message containing ``unrecognizedclient``, so a marker appended to
   ``_TRANSIENT_MARKERS`` alone would be unreachable. The check has to sit ABOVE
   the exclusion list.
3. ``is_auth_failure_output`` — the runtime latches any auth-shaped kiro-cli
   stderr into ``_saw_auth_failure``, which ``providers/acp.py`` converts into
   ``AcpAuthRequired`` ("non-retryable, skip the retry ladder"). A cold-start
   burst is exactly when that line appears on stderr, so without this seam the
   turn fails with "run kiro-cli login" no matter what the classifier says.

The carve-out is scoped to the ``security token ... is invalid`` WORDING, never
to the 403 status: a bare 401/403 is still a terminal session expiry, and
``... is expired`` is still terminal because no retry revives an expired token.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import (
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
    is_auth_failure_output,
)
from kiro_crew.llm_helpers import acp_error_is_transient, is_transient_backend_error

# The real rejection, as it arrives in a JSON-RPC error frame.
_NOT_YET_VALID = {
    "code": -32603,
    "message": "Internal error",
    "data": (
        "UnrecognizedClientException: The security token included in the request "
        "is invalid (HTTP 403)"
    ),
}

# Its terminal sibling: one word different, and no retry can fix it.
_EXPIRED = {
    "code": -32603,
    "message": "Internal error",
    "data": (
        "ExpiredTokenException: The security token included in the request is expired (HTTP 403)"
    ),
}

# Real kiro-cli stderr during a cold-start burst.
_STDERR_NOT_YET_VALID = (
    "An error occurred (UnrecognizedClientException) when calling the "
    "ConverseStream operation: The security token included in the request is invalid."
)


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


class TestTheRawClassifierRetriesIt:
    def test_not_yet_valid_is_transient(self):
        assert _is_transient_raw_error(_NOT_YET_VALID) is True

    def test_the_verdict_reaches_the_retry_layer(self):
        assert acp_error_is_transient(_raised(_NOT_YET_VALID)) is True

    def test_expired_stays_terminal(self):
        assert _is_transient_raw_error(_EXPIRED) is False
        assert acp_error_is_transient(_raised(_EXPIRED)) is False

    def test_invalid_or_expired_stays_terminal(self):
        """A combined rejection names an expiry, and that half is unfixable."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The security token included in the request is invalid or expired",
        }
        assert _is_transient_raw_error(err) is False


class TestTheFormatterTellsTheTruth:
    """The formatter and the classifier key off the same predicate, so a frame
    the ladder will retry must not also tell the operator to re-authenticate a
    credential that is already valid."""

    def test_not_yet_valid_advises_a_retry(self):
        out = _format_acp_error(_NOT_YET_VALID)
        assert "propagation" in out.lower()
        assert "retry in a moment" in out.lower()
        assert "authentication failed" not in out.lower()

    def test_expired_still_advises_re_authentication(self):
        out = _format_acp_error(_EXPIRED)
        assert "authentication failed" in out.lower()
        assert "propagation" not in out.lower()

    def test_the_message_names_the_permanently_invalid_key(self):
        """The same sentence arrives for a deleted/rotated/mistyped key, so the
        message must not assert the propagation diagnosis as the only cause."""
        out = _format_acp_error(
            {
                "code": -32603,
                "message": "Internal error",
                "data": (
                    "InvalidClientTokenId: The security token included in the "
                    "request is invalid (HTTP 403)"
                ),
            }
        ).lower()
        assert "usually" in out
        assert "refresh your aws credentials" in out


class TestTheStringFallbackAgrees:
    """``_is_transient_acp_error`` sees only a formatted (or legacy raw) string,
    and its auth exclusion list would claim this rejection before any marker."""

    def test_the_raw_provider_sentence_classifies(self):
        assert is_transient_backend_error(_STDERR_NOT_YET_VALID) is True

    def test_the_formatted_message_classifies(self):
        assert is_transient_backend_error(_format_acp_error(_NOT_YET_VALID)) is True

    def test_the_expired_sentence_does_not_classify(self):
        assert (
            is_transient_backend_error(
                "ExpiredTokenException: The security token included in the request is expired"
            )
            is False
        )


class TestTheStderrLatchDoesNotFire:
    """Latching would raise the explicitly non-retryable ``AcpAuthRequired``."""

    def test_not_yet_valid_stderr_is_not_an_auth_failure(self):
        assert is_auth_failure_output(_STDERR_NOT_YET_VALID) is False

    def test_expired_stderr_is_still_an_auth_failure(self):
        assert (
            is_auth_failure_output(
                "ExpiredTokenException: The security token included in the request is expired"
            )
            is True
        )


class TestTheCarveOutIsScopedToTheWording:
    @pytest.mark.parametrize("status", ["HTTP 401", "HTTP 403", "status code 401", "status 403"])
    def test_a_bare_status_is_still_terminal_session_expiry(self, status):
        """Scoping to the 403 status instead of the wording would silently make
        every expired session retryable."""
        err = {"code": -32603, "message": "Internal error", "data": status}
        assert _is_transient_raw_error(err) is False
        assert "kiro-cli login" in _format_acp_error(err).lower()

    def test_an_invalid_bearer_token_is_still_terminal(self):
        """The account-switch rejection ("the bearer token included in the
        request is invalid") is a REJECTED credential, not a not-yet-propagated
        one, so it must keep its sign-in guidance."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The bearer token included in the request is invalid.",
        }
        assert _is_transient_raw_error(err) is False
        assert "kiro-cli login" in _format_acp_error(err).lower()
        assert is_auth_failure_output("Access denied: the bearer token is invalid") is True

    def test_the_pattern_does_not_span_sentences(self):
        """Fenced to one sentence and one line, so an unrelated "is invalid" in
        a combined haystack cannot pair with a distant "security token"."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "AccessDeniedException: the security token is required. "
                "The requested configuration is invalid."
            ),
        }
        assert _is_transient_raw_error(err) is False
