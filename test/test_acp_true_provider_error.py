"""Tests for surfacing the provider's TRUE error text.

kiro-cli wraps every mid-stream provider failure in one envelope::

    Encountered an error in the response stream: <the real cause>

``_RE_5XX_HINT`` must NOT match the literal ``response stream`` -- that is the
ENVELOPE, not anything about the failure inside it. Because the 5xx branch sits
near the end of the if/elif chain, matching the envelope makes it a catch-all:
every provider failure without an earlier curated branch is rewritten to "The
model backend hit a transient error (HTTP 5xx) ... retry in a moment", the real
cause is discarded, and ``_is_transient_raw_error`` agrees it is retryable so the
turn burns the whole retry ladder first.

Reported case: a monthly-usage-limit rejection. The terminal CLI printed "The
monthly usage limit has been reached" while the dashboard, for the same class of
error, claimed a momentary 5xx and told the user to retry — which can never
succeed until the allowance resets.

Two changes are pinned here:

1. The envelope is not a transient token, and unrecognised failures show
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

    @pytest.mark.parametrize(
        "expected",
        [
            pytest.param("Retrying will not help", id="says_retrying_will_not_help"),
            # Provider text has no trailing period; guidance must not run into it.
            pytest.param("reached. Retrying", id="provider_sentence_is_punctuated_before_guidance"),
        ],
    )
    def test_guidance_wording(self, expected):
        assert expected in _format_acp_error(_MONTHLY_LIMIT)

    def test_is_not_retried(self):
        """The retry ladder must not spend attempts on a spent allowance."""
        assert _is_transient_raw_error(_MONTHLY_LIMIT) is False

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


class TestClaudeSeatUsageLimitWording:
    """A Claude seat (claude-agent-acp) does not go through Bedrock and words
    its exhausted allowance in Anthropic's own terms: a rolling SESSION /
    5-hour limit and a fixed WEEKLY limit, each quoting a reset time. This
    wording classifies as a terminal usage limit (not a transient throttle) and
    the formatter quotes the provider's own sentence, reset time included.
    """

    # Anthropic's own strings (support.claude.com "Troubleshoot Claude error
    # messages" / "How do usage and length limits work"), request id appended.
    _SESSION = _err(f"You've hit your session limit \u00b7 resets 5:50pm (request_id: {_REQ})")
    _FIVE_HOUR = _err(f"5-hour limit reached - resets 3pm (request_id: {_REQ})")
    _WEEKLY = _err(
        f"You've reached your weekly limit. It resets Monday at 9am. (request_id: {_REQ})"
    )

    @pytest.mark.parametrize("frame", [_SESSION, _FIVE_HOUR, _WEEKLY])
    def test_is_terminal_not_retried(self, frame):
        assert _is_transient_raw_error(frame) is False

    @pytest.mark.parametrize("frame", [_SESSION, _FIVE_HOUR, _WEEKLY])
    def test_not_worded_as_a_throttle_or_5xx(self, frame):
        out = _format_acp_error(frame, backend="claude")
        assert "throttling" not in out.lower()
        assert "Bedrock" not in out
        assert "5xx" not in out
        assert "Retrying will not help" in out

    def test_quotes_the_providers_sentence_and_reset_time(self):
        """The reset time is the user's only actionable fact, so the branch
        quotes the provider sentence verbatim rather than paraphrasing it."""
        out = _format_acp_error(self._SESSION, backend="claude")
        assert "You've hit your session limit \u00b7 resets 5:50pm" in out
        assert out.count(_REQ) == 1
        five = _format_acp_error(self._FIVE_HOUR, backend="claude")
        assert "5-hour limit reached - resets 3pm" in five
        week = _format_acp_error(self._WEEKLY, backend="claude")
        assert "It resets Monday at 9am" in week


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


class TestMalformedRequestIsTerminalAndActionable:
    """A structural "Improperly formed request" rejection must become
    actionable repair guidance and lock a terminal (non-retryable) verdict, so a
    deterministically-rejected payload is never re-sent in a loop."""

    # The provider passes this string through verbatim with a request id.
    _MALFORMED = _err(f"Improperly formed request. (request_id: {_REQ})")

    def test_rewrites_into_actionable_prose(self):
        """Not the raw dict, and not the bare provider string — real guidance."""
        out = _format_acp_error(self._MALFORMED)
        # The raw dict repr is gone.
        assert "'code': -32603" not in out
        assert str(self._MALFORMED) not in out
        # It names the failure class (malformed / structural).
        assert "malformed" in out.lower()
        assert "structural" in out.lower()
        # It says retrying as-is won't help.
        assert "will not help" in out.lower()
        # It offers a concrete repair affordance. Only `/compact` is accepted;
        # the non-existent `/chat new` is not, per
        # docs/system-specs/common/error-handling.md.
        assert "/compact" in out

    def test_request_id_is_preserved(self):
        out = _format_acp_error(self._MALFORMED)
        assert out.count(_REQ) == 1

    def test_is_terminal_not_retried(self):
        """The retry ladder must not re-send a structurally-rejected payload."""
        assert _is_transient_raw_error(self._MALFORMED) is False

    def test_matches_case_insensitively(self):
        """The phrase can arrive lower-cased inside the stream envelope."""
        err = _err(f"{_ENVELOPE}improperly formed request")
        assert _is_transient_raw_error(err) is False
        assert "malformed" in _format_acp_error(err).lower()

    def test_phrase_in_message_field_alone_does_not_trigger(self):
        """Scoping guard (mirrors the message-field-echo tests): the phrase
        appearing only in the JSON-RPC ``message`` — never the provider
        ``data`` — must NOT trip the branch, since ``data`` is the provider's
        own text and ``message`` is often boilerplate."""
        # Phrase only in `message`; `data` carries an unrelated, opaque payload.
        err = {
            "code": -32603,
            "message": "Improperly formed request",
            "data": "",
        }
        assert _is_transient_raw_error(err) is False  # opaque data -> still terminal
        out = _format_acp_error(err)
        # Because `data` is empty the malformed branch does not fire; the
        # unknown-shape fallback keeps the raw dict rather than the repair prose.
        assert "malformed" not in out.lower()
        assert "Prompt error: {" in out


class TestThrottleWordingByBackend:
    """A real throttle is transient on every backend, but only kiro-cli reaches
    the model through Bedrock, so only it may name Bedrock. Identity is
    POSITIVE per AGENTS.md "Harness parity": the kiro-cli id is the empty string
    (``ACP_BACKEND_KIRO``), and every other harness gets neutral wording.
    """

    _THROTTLE = _err("ThrottlingException: Rate exceeded")
    _GENERIC_RATE = _err("rate limit exceeded")

    def test_kiro_backend_names_bedrock(self):
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        out = _format_acp_error(self._THROTTLE, backend=ACP_BACKEND_KIRO)
        assert out.startswith("Bedrock is throttling requests.")

    def test_empty_backend_defaults_to_bedrock_wording(self):
        # The shared runtime reaches this with no backend in hand; the kiro id
        # IS the empty string, so the default resolves to Bedrock, not neutral.
        assert _format_acp_error(self._THROTTLE, backend="").startswith("Bedrock is throttling")

    @pytest.mark.parametrize("backend", ["claude", "codex", "goose", "opencode", "deepseek", "pi"])
    def test_non_bedrock_backends_get_neutral_wording(self, backend):
        out = _format_acp_error(self._THROTTLE, backend=backend)
        assert "Bedrock" not in out
        assert out.startswith("The model provider is throttling requests.")

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_throttle_stays_transient_on_every_backend(self, backend):
        # Wording changes, retry verdict does not: a throttle is retryable, and
        # the string fallback (llm_helpers "throttl" marker) must still agree.
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert _is_transient_raw_error(self._THROTTLE) is True
        assert is_transient_backend_error(_format_acp_error(self._THROTTLE, backend=backend))

    def test_real_bedrock_throttle_classification_is_unchanged(self):
        # The fix must not make the throttle classification less accurate: a
        # genuine Bedrock throttle on kiro-cli still reads as a retryable
        # throttle, named Bedrock, generic rate-limit wording included.
        assert _is_transient_raw_error(self._GENERIC_RATE) is True
        assert _format_acp_error(self._GENERIC_RATE, backend="").startswith(
            "Bedrock is throttling"
        )
