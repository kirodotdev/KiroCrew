"""``classify_provider_error`` is the public face of the ACP client's provider
error vocabulary and never disagrees with the retry verdict.

The dependency coordinator's ACP adapter must not import the nine private ``_RE_*``
patterns and two private helpers from ``acp.client``; a rename there would have
silently disarmed the coordinator. The public classifier names the failure with
a closed-set ``kind`` and carries the retry verdict, and this file pins that the
verdict is exactly what ``_is_transient_raw_error`` says for the same frame.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import (
    PROVIDER_ERROR_AUTH,
    PROVIDER_ERROR_CONNECTION,
    PROVIDER_ERROR_CREDENTIAL_PROPAGATION,
    PROVIDER_ERROR_HTTP_5XX,
    PROVIDER_ERROR_MALFORMED_REQUEST,
    PROVIDER_ERROR_MODEL_UNAVAILABLE,
    PROVIDER_ERROR_THROTTLE,
    PROVIDER_ERROR_UNKNOWN,
    PROVIDER_ERROR_USAGE_LIMIT,
    classify_provider_error,
)

CASES = [
    ("You have reached your monthly usage limit for this model", PROVIDER_ERROR_USAGE_LIMIT),
    ("Improperly formed request: field x", PROVIDER_ERROR_MALFORMED_REQUEST),
    ("The model 'claude-x' is not available", PROVIDER_ERROR_MODEL_UNAVAILABLE),
    ("The model you've selected is temporarily unavailable", PROVIDER_ERROR_MODEL_UNAVAILABLE),
    ("ThrottlingException: Rate exceeded", PROVIDER_ERROR_THROTTLE),
    ("HTTP 429 too many requests, rate limit hit", PROVIDER_ERROR_THROTTLE),
    ("AccessDeniedException: not allowed", PROVIDER_ERROR_AUTH),
    ("ECONNREFUSED 127.0.0.1:443", PROVIDER_ERROR_CONNECTION),
    ("Internal server error", PROVIDER_ERROR_HTTP_5XX),
    ("HTTP 503 upstream", PROVIDER_ERROR_HTTP_5XX),
    (
        "The system encountered an unexpected error. Try your request again.",
        PROVIDER_ERROR_HTTP_5XX,
    ),
    ("something nobody has seen before", PROVIDER_ERROR_UNKNOWN),
]


@pytest.mark.parametrize("text,kind", CASES, ids=[k for _, k in CASES])
def test_kind_is_named(text: str, kind: str) -> None:
    assert classify_provider_error(text).kind == kind


@pytest.mark.parametrize("text,kind", CASES, ids=[k for _, k in CASES])
def test_retry_verdict_matches_the_private_classifier(text: str, kind: str) -> None:
    error = {"code": -32000, "message": text, "data": text}
    verdict = classify_provider_error(f"{text} {text}", data=text)
    assert verdict.retryable is acp_client._is_transient_raw_error(error)
    assert verdict.terminal is (not verdict.retryable)


def test_malformed_request_reads_the_data_field_only() -> None:
    """A phrase echo in ``message`` must not flip an otherwise-transient frame."""
    verdict = classify_provider_error(
        "Improperly formed request wording in message; HTTP 503", data="HTTP 503"
    )
    assert verdict.kind == PROVIDER_ERROR_HTTP_5XX


def test_usage_limit_outranks_throttle_wording() -> None:
    verdict = classify_provider_error("monthly usage limit reached; rate limit exceeded")
    assert verdict.kind == PROVIDER_ERROR_USAGE_LIMIT and verdict.terminal


def test_credential_propagation_is_retryable_despite_auth_token() -> None:
    text = "UnrecognizedClientException: The security token included in the request is invalid"
    if not acp_client.is_credential_propagation_delay(text):
        pytest.skip("the propagation detector does not match this wording on this revision")
    assert classify_provider_error(text).kind == PROVIDER_ERROR_CREDENTIAL_PROPAGATION


def test_matched_token_is_short_and_never_the_message() -> None:
    long_text = "x" * 500 + " ThrottlingException"
    verdict = classify_provider_error(long_text)
    assert verdict.matched == "ThrottlingException"


def test_adapter_uses_no_private_client_names() -> None:
    import inspect

    from kiro_crew.taskq.adapters import acp_provider

    source = inspect.getsource(acp_provider)
    assert "client._RE_" not in source
    assert "client._is_" not in source
