"""Tests for surfacing the provider's TRUE error text.

kiro-cli wraps every mid-stream provider failure in one envelope::

    Encountered an error in the response stream: <the real cause>

``_RE_5XX_HINT`` used to match the literal ``response stream``, i.e. the
ENVELOPE rather than anything about the failure inside it. Because the 5xx
branch sits near the end of the if/elif chain, that made it a catch-all: every
provider failure without an earlier curated branch was rewritten to "The model
backend hit a transient error (HTTP 5xx) ... retry in a moment", the real cause
was discarded, and ``_is_transient_raw_error`` agreed it was retryable so the
turn burned the whole retry ladder first.

Reported case: a monthly-usage-limit rejection. The terminal CLI printed "The
monthly usage limit has been reached" while the dashboard, for the same class of
error, claimed a momentary 5xx and told the user to retry — which can never
succeed until the allowance resets.

Two changes are pinned here:

1. The envelope is no longer a transient token, and unrecognised failures show
   the provider's own text (CLI parity) instead of a ``repr`` of the JSON-RPC
   dict.
2. Usage-limit exhaustion is a first-class TERMINAL branch, so it is never
   retried and says so.
"""

import pytest

from kiro_crew.acp.client import (
    _format_acp_error,
    _is_transient_raw_error,
    _provider_detail,
)

_ENVELOPE = "Encountered an error in the response stream: "
_REQ = "8182dc3a-700a-4832-bd91-563bf5875626"


def _err(data: str, message: str = "Internal error") -> dict:
    return {"code": -32603, "message": message, "data": data}


# The exact reported shape, request id included.
_MONTHLY_LIMIT = _err(
    f"{_ENVELOPE}The monthly usage limit has been reached (request_id: {_REQ})"
)


class TestUsageLimitIsTerminalAndTruthful:
    def test_shows_the_providers_own_sentence(self):
        """The dashboard must say what the CLI says, not invent a 5xx."""
        out = _format_acp_error(_MONTHLY_LIMIT)
        assert "The monthly usage limit has been reached" in out
        # The wrong old message must be gone.
        assert "transient error" not in out.lower()
        assert "5xx" not in out
        # The envelope is transport noise, not part of the message.
        assert "response stream" not in out
        # request_id survives for support correlation, and exactly once.
        assert out.count(_REQ) == 1

    def test_says_retrying_will_not_help(self):
        out = _format_acp_error(_MONTHLY_LIMIT)
        assert "Retrying will not help" in out

    def test_is_not_retried(self):
        """The retry ladder must not spend attempts on a spent allowance."""
        assert _is_transient_raw_error(_MONTHLY_LIMIT) is False

    def test_provider_sentence_is_punctuated_before_guidance(self):
        """Provider text has no trailing period; guidance must not run into it."""
        out = _format_acp_error(_MONTHLY_LIMIT)
        assert "reached. Retrying" in out

    @pytest.mark.parametrize(
        "data",
        [
            "The monthly usage limit has been reached",
            "You have reached your daily limit for this model",
            "weekly limit exceeded for your plan",
            "MonthlyLimitError: allowance consumed",
            "FreeTierLimitExceeded",
        ],
    )
    def test_limit_wording_variants_are_terminal(self, data):
        assert _is_transient_raw_error(_err(data)) is False

    def test_limit_outranks_throttle_wording(self):
        """A limit message that also reads as rate-limiting stays terminal.

        Ordering matters: the throttle branch returns transient, so if it were
        checked first a quota rejection would be retried on a backoff curve that
        can never clear it.
        """
        err = _err("The monthly usage limit has been reached: rate limit for your plan")
        assert _is_transient_raw_error(err) is False
        assert "Retrying will not help" in _format_acp_error(err)


class TestEnvelopeIsNotATransientSignal:
    def test_envelope_alone_is_not_transient(self):
        """The regression: the wrapper by itself must decide nothing."""
        assert _is_transient_raw_error(_err(f"{_ENVELOPE}Input is too long")) is False

    def test_unrecognised_failure_shows_real_text(self):
        out = _format_acp_error(
            _err(f"{_ENVELOPE}Input is too long for the requested model (request_id: {_REQ})")
        )
        assert out == f"Input is too long for the requested model (request_id: {_REQ})"

    def test_genuine_5xx_inside_envelope_still_transient(self):
        """Removing the envelope token must not cost us real 5xx detection.

        A real backend blip carries its own token (a named exception, an
        HTTP status, or an explicit retry hint) inside the envelope, and that
        is what the classifier keys on now.
        """
        err = _err(f"{_ENVELOPE}InternalServerError ... please try again.")
        assert _is_transient_raw_error(err) is True
        assert "transient error" in _format_acp_error(err).lower()

    def test_bare_retry_hint_still_transient(self):
        assert _is_transient_raw_error(_err("Service hiccup, please try again.")) is True


class TestProviderDetail:
    def test_strips_envelope_and_trailing_request_id(self):
        assert (
            _provider_detail(f"{_ENVELOPE}Something broke (request_id: {_REQ})")
            == "Something broke"
        )

    def test_passes_through_unwrapped_text(self):
        assert _provider_detail("ValidationException: bad field") == (
            "ValidationException: bad field"
        )

    def test_empty_for_no_detail(self):
        assert _provider_detail("") == ""
        assert _provider_detail(_ENVELOPE) == ""

    def test_opaque_shape_keeps_the_raw_dict(self):
        """With no usable detail the dict is still better than nothing."""
        out = _format_acp_error({"code": -32603, "message": "Internal error", "data": ""})
        assert "Prompt error: {" in out
