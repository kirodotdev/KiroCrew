"""Tests for ACP client: retryability classification of raw JSON-RPC errors."""

from unittest.mock import patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client


class TestIsTransientRawError:
    """_is_transient_raw_error classifies retryability from the RAW JSON-RPC
    error so the verdict is independent of the formatted message."""

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(
                "Encountered an error in the response stream: ... "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                "{ source: InternalServerError(...) please try again ...)))",
                id="internal_server_error_is_transient",
            ),
            # kiro-cli's pre-stream generation failure wrapper: no request_id, no
            # error class, none of the 5xx/throttle tokens. Retryable — this shape
            # was surfaced to the user with no retry during a model-capacity blip.
            pytest.param(
                "Kiro failed to generate a response",
                id="kiro_generic_generation_failure_is_transient",
            ),
        ],
    )
    def test_internal_error_is_transient(self, data):
        from kiro_crew.acp.client import _is_transient_raw_error

        err = {"code": -32603, "message": "Internal error", "data": data}
        assert _is_transient_raw_error(err) is True

    def test_status_50x_and_hint_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "HTTP 503 Service Unavailable"}) is True
        assert _is_transient_raw_error({"data": "HTTP 504 Gateway Timeout"}) is True
        assert _is_transient_raw_error({"data": "status code 529 overloaded"}) is True
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "DispatchFailure ConnectionReset"}) is True
        assert _is_transient_raw_error({"data": "please try again"}) is True

    def test_5xx_token_in_message_field_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # 5xx signal carried in `message` (not `data`) must still be caught:
        # the classifier scans the combined haystack, so this does not fail
        # fast the way a data-only scan would (SHOULD-FIX). Auth is
        # still checked first, so an auth error with a stray 50x stays terminal.
        assert _is_transient_raw_error({"message": "InternalServerError", "data": ""}) is True
        assert _is_transient_raw_error({"message": "HTTP 503", "data": "Internal error"}) is True
        assert (
            _is_transient_raw_error(
                {"message": "please try again", "data": "AccessDeniedException"}
            )
            is False
        )

    def test_throttle_and_model_unavailable_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "ThrottlingException: slow down"}) is True
        assert _is_transient_raw_error({"message": "Rate limit exceeded", "data": ""}) is True
        assert _is_transient_raw_error({"data": "The model 'opus' is not available."}) is True

    def test_auth_and_unknown_are_not_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Auth is terminal — a retry can't fix an expired/denied credential.
        assert _is_transient_raw_error({"data": "AccessDeniedException: nope"}) is False
        assert _is_transient_raw_error({"data": "ExpiredTokenException"}) is False
        # Unknown shapes and non-dicts are terminal (fail-fast, not retry).
        assert _is_transient_raw_error({"data": "max_tokens 500 exceeded"}) is False
        # 501 Not Implemented is terminal — only 500/502/503/504 + 529 retry.
        assert _is_transient_raw_error({"data": "HTTP 501 Not Implemented"}) is False
        assert (
            _is_transient_raw_error({"code": -32603, "message": "Internal error", "data": ""})
            is False
        )
        assert _is_transient_raw_error(None) is False
        assert _is_transient_raw_error("boom") is False

    def test_invalid_bearer_token_is_not_transient(self):
        """A rejected credential must be terminal, not fed to the retry ladder.

        The rejection carries no status code and no expiry wording, so without
        its own pattern it reaches the 5xx family — and a co-occurring transport
        error is enough to make it look retryable, spending the whole ladder on
        a credential no retry can revive.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        assert (
            _is_transient_raw_error(
                {"data": "The bearer token included in the request is invalid."}
            )
            is False
        )
        assert _is_transient_raw_error({"data": "invalid bearer token"}) is False
        # A co-occurring transport error must not flip it to retryable.
        assert (
            _is_transient_raw_error(
                {
                    "message": "Encountered an error in the response stream",
                    "data": "ConnectionResetError: the bearer token is invalid",
                }
            )
            is False
        )
        # An unrelated 'invalid' must stay out of the credential class.
        assert (
            _is_transient_raw_error(
                {"data": "ServiceUnavailableException: HTTP 503, invalid window"}
            )
            is True
        )

    def test_session_expired_is_not_transient(self):
        """Regression test: kiro-cli session expiry must be terminal.

        These error shapes can fall through to the 5xx branch (when they
        also carried DispatchFailure/ConnectionResetError), telling the user to
        retry when re-authentication was required.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        # Direct session-expired wording from kiro-cli.
        assert _is_transient_raw_error({"data": "session expired"}) is False
        assert _is_transient_raw_error({"data": "session has expired"}) is False
        assert _is_transient_raw_error({"data": "not logged in"}) is False
        assert _is_transient_raw_error({"data": "not authenticated"}) is False
        assert _is_transient_raw_error({"data": "login required"}) is False
        assert _is_transient_raw_error({"data": "authentication required"}) is False
        assert _is_transient_raw_error({"data": "re-authenticate"}) is False
        assert _is_transient_raw_error({"message": "session timed out", "data": ""}) is False
        # Session expiry with a co-occurring 5xx token: the session-expiry
        # branch must win (checked first).
        assert (
            _is_transient_raw_error({"data": "DispatchFailure: session expired", "message": ""})
            is False
        )
        assert (
            _is_transient_raw_error({"data": "ConnectionResetError: not logged in", "message": ""})
            is False
        )
        # A bare 401/403 — the shape an expired session actually arrives in.
        assert _is_transient_raw_error({"data": "HTTP 401"}) is False
        assert _is_transient_raw_error({"data": "HTTP 403"}) is False
        assert _is_transient_raw_error({"data": "status code 401"}) is False
        # 401 alongside the transport error left by the aborted request: the
        # 5xx family must not reclaim it and re-arm the retry ladder.
        assert (
            _is_transient_raw_error(
                {"data": "DispatchFailure ConnectionResetError: HTTP 401", "message": ""}
            )
            is False
        )
        # Real 5xx stays retryable — the auth-status branch must not overreach.
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "HTTP 503"}) is True

    @pytest.mark.parametrize(
        "phrase",
        [
            pytest.param(
                "Kiro failed to generate a response",
                id="generation_failure_scoped_to_data_and_loses_to_auth",
            ),
            pytest.param(
                "The service failed to process the request",
                id="process_failure_scoped_to_data_and_loses_to_auth",
            ),
        ],
    )
    def test_wrapper_phrase_scoped_to_data_and_loses_to_auth(self, phrase):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Phrase only in `message` (data carries a deterministic failure):
        # scoped match must not fire — stays terminal.
        assert (
            _is_transient_raw_error(
                {"message": phrase, "data": "ValidationException: unsupported field"}
            )
            is False
        )
        # Auth is checked first and stays terminal even when the phrase
        # co-occurs in data.
        assert _is_transient_raw_error({"data": f"AccessDeniedException: {phrase}"}) is False

    def test_kiro_process_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error, classify_provider_error

        # kiro-cli's post-stream sibling of the generation-failure wrapper: a
        # request_id is present, no error class, none of the 5xx tokens. This
        # exact shape otherwise ends the turn with a terminal card and no retry.
        data = (
            "The service failed to process the request "
            "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
        )
        err = {"code": -32603, "message": "Internal error", "data": data}
        assert _is_transient_raw_error(err) is True
        verdict = classify_provider_error(data, data=data)
        assert verdict.retryable is True
        assert verdict.matched == "failed to process the request"

    def test_prose_spelled_dispatch_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The AWS Rust SDK spells ONE connector fault two ways: the Debug repr
        # is CamelCase (DispatchFailure) and the Display impl is prose
        # ("dispatch failure"). kiro-cli surfaces whichever the failing layer
        # produced. While the pattern was CamelCase-only and case-SENSITIVE the
        # prose form read as an unknown shape and was classified TERMINAL, so
        # every affected turn surfaced a bare error on the FIRST attempt and no
        # retry ladder ever ran.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "An unknown error occurred: dispatch failure"
            ),
        }
        assert _is_transient_raw_error(err) is True
        # The CamelCase spelling keeps classifying identically.
        assert _is_transient_raw_error({"data": "DispatchFailure ConnectionReset"}) is True
        # Same two-spelling split for the sibling connector fault.
        assert _is_transient_raw_error({"data": "connection reset by peer"}) is True

    def test_bare_internal_error_stays_terminal(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # -32603's canonical message is literally "Internal error". The
        # separator tolerance loosens only the gaps BETWEEN words, never the
        # tokens, so this still matches nothing (no "server", no "failure") and
        # keeps falling through to the unknown-shape branch instead of being
        # mis-told to retry a condition that will never succeed.
        assert (
            _is_transient_raw_error(
                {"code": -32603, "message": "Internal error", "data": "Input is too long"}
            )
            is False
        )

    def test_terminal_branches_outrank_a_co_occurring_dispatch_failure(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Every terminal branch is checked BEFORE the 5xx family, so widening
        # the 5xx pattern must not let a connector token rescue an error that
        # can only be reproduced by retrying it.
        for data in (
            "You've reached your monthly usage limit. dispatch failure",
            "dispatch failure: session expired",
            "AccessDeniedException dispatch failure",
            "Improperly formed request dispatch failure",
        ):
            assert _is_transient_raw_error({"data": data, "message": ""}) is False

    def test_prose_dispatch_failure_reaches_the_retry_decider(self):
        import pytest

        from kiro_crew.acp.client import AcpError, _raise_acp_error
        from kiro_crew.llm_helpers import acp_error_is_transient

        # The classifier and the retry decider are two different layers, and
        # the gap between them is what broke: acp_error_is_transient prefers the
        # structured AcpError.transient flag and consults its string markers
        # ONLY when no flag is present. _TRANSIENT_MARKERS already listed the
        # prose spellings, so a transient=False flag made that list unreachable
        # dead code on this path. Assert the end-to-end verdict, not just the
        # classifier, or the next regex edit silently reintroduces this.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "An unknown error occurred: dispatch failure"
            ),
        }
        with pytest.raises(AcpError) as ei:
            _raise_acp_error(err)
        exc = ei.value
        assert exc.transient is True
        assert acp_error_is_transient(exc) is True
        # The raw provider text is replaced by the actionable 5xx wording.
        assert "transient error (HTTP 5xx)" in str(exc)

    def test_raise_acp_error_carries_transient_flag_and_formatted_message(self):
        import pytest

        from kiro_crew.acp.client import AcpError, _raise_acp_error

        transient_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: please try again (request_id: abc-123)",
        }
        with pytest.raises(AcpError) as ei:
            _raise_acp_error(transient_err)
        exc = ei.value
        assert exc.transient is True
        # Message is the formatted, user-facing string (not the raw dict).
        assert "transient error (HTTP 5xx)" in str(exc)
        assert "abc-123" in str(exc)

        auth_err = {"code": -32603, "message": "Internal error", "data": "AccessDeniedException"}
        with pytest.raises(AcpError) as auth_ei:
            _raise_acp_error(auth_err)
        auth_exc = auth_ei.value
        assert auth_exc.transient is False
        assert "authentication failed" in str(auth_exc).lower()

    def test_context_window_overflow_is_structural_and_non_transient(self):
        import pytest

        import kiro_crew.acp as acp_package
        from kiro_crew.acp import transport_errors
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "The context window overflowed "
                "(request_id: 3844b25f-d540-4972-9b0b-03ddb5d177c6)"
            ),
        }
        with patch.object(
            transport_errors,
            "_is_transient_raw_error",
            wraps=transport_errors._is_transient_raw_error,
        ) as classify:
            with pytest.raises(AcpError) as raised:
                _raise_acp_error(error)

        exc = raised.value
        classify.assert_called_once_with(error, None)
        assert type(exc) is AcpError
        assert exc.transient is False
        assert exc.structural_terminal is True
        assert exc.context_overflow is True
        assert "Retrying on the same model session will not help" in str(exc)
        assert "3844b25f-d540-4972-9b0b-03ddb5d177c6" in str(exc)
        assert not hasattr(acp_client, "AcpContextOverflow")
        assert not hasattr(acp_package, "AcpContextOverflow")

    def test_context_window_overflow_tag_is_data_field_only(self):
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "The context window overflowed",
            "data": "opaque error detail",
        }
        with pytest.raises(AcpError) as raised:
            _raise_acp_error(error)

        exc = raised.value
        assert type(exc) is AcpError
        assert exc.transient is False
        assert exc.structural_terminal is False
        assert exc.context_overflow is False

    def test_context_window_overflow_wording_is_surface_neutral(self):
        # The formatter cannot tell a first turn from a later one, and
        # ``subagent_manager/run_lifecycle.py`` appends this text after its own
        # post-activity reason, so a startup-only claim would contradict the
        # caller. Both causes and both remedies must be named.
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "Internal error",
            "data": "The context window overflowed (request_id: 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0)",
        }
        with pytest.raises(AcpError) as raised:
            _raise_acp_error(error)

        text = str(raised.value)
        assert "before the turn could run" not in text
        assert "established conversation" in text
        assert "accumulated history" in text
        assert "fresh session" in text
        assert "always-loaded" in text
        assert "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0" in text
        assert raised.value.transient is False
        assert raised.value.structural_terminal is True
        assert raised.value.context_overflow is True

    def test_raise_acp_error_types_thinking_conversation_binding_mismatch(self):
        import pytest

        from kiro_crew.acp.client import (
            AcpConversationBindingMismatch,
            AcpError,
            _raise_acp_error,
        )

        mismatch = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "messages.3.content.0: Invalid `signature` in `thinking` block. "
                "The block is bound to a different conversation. Content before "
                "this block differs from when it was created."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch) as mismatch_error:
            _raise_acp_error(mismatch)
        assert mismatch_error.value.transient is False

        production_missing_predecessor = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "messages.3.content.0: Invalid `signature` in `thinking` block. "
                "The block is bound to a different conversation. "
                "The `thinking` block that preceded this one when it was created "
                "is missing from this request."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(production_missing_predecessor)

        reverse_order = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "The block is bound to a different conversation. "
                "messages.3.content.0: Invalid `signature` in `thinking` block."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(reverse_order)

        # Two unrelated fields cannot be combined into a recovery verdict.
        split_fields = {
            "code": -32603,
            "message": "The block is bound to a different conversation.",
            "data": "messages.3.content.0: Invalid `signature` in `thinking` block.",
        }
        with pytest.raises(AcpError) as split_error:
            _raise_acp_error(split_fields)
        assert not isinstance(split_error.value, AcpConversationBindingMismatch)

        # A signature that is merely invalid may be tampered or undecryptable.
        # The provider does not document conversation replacement as a repair for
        # that case, so it must stay outside the recoverable subtype.
        tampered = {
            "code": -32603,
            "message": "Prompt failed",
            "data": "messages.3.content.0: Invalid `signature` in `thinking` block.",
        }
        with pytest.raises(AcpError) as tampered_error:
            _raise_acp_error(tampered)
        assert not isinstance(tampered_error.value, AcpConversationBindingMismatch)

    def test_binding_mismatch_keeps_the_shared_raw_frame_tags(self):
        """The recoverable subtype is raised through the ordinary tagging path.

        Raising it before the shared tags are computed would drop facts other
        callers read from the same frame (here the usage-limit tag), so the
        subtype must change only the exception TYPE and its retry verdict.
        """
        import pytest

        from kiro_crew.acp.client import AcpConversationBindingMismatch, _raise_acp_error

        frame = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "Invalid `signature` in `thinking` block. The block is bound to a "
                "different conversation. Your monthly usage limit has been reached."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch) as raised:
            _raise_acp_error(frame)
        assert raised.value.transient is False
        assert getattr(raised.value, "usage_limit", False) is True

    def test_acp_error_default_transient_is_none(self):
        from kiro_crew.acp.client import AcpError

        assert AcpError("plain").transient is None
        assert AcpError("flagged", transient=True).transient is True
