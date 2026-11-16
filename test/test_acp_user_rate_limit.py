"""A per-user request-rate limit from Kiro is not worded as a model-provider throttle.

Kiro answers HTTP 429 with the reason ``USER_REQUEST_RATE_EXCEEDED`` when one
user sends requests faster than their rate limit allows. That limit is per user,
not per model, so every model spends the same allowance. The throttle row told
the user to "switch to a different model in the picker", which cannot clear it,
and users who followed it sent more requests into the same limit.

Pinned here: the user-rate frame names the limit in plain words, says a model
switch will not help, and advises waiting. Its retry verdict and throttle tag
are the same as before. A throttle that does not carry the user-rate reason or
wording keeps the provider wording and its switch advice.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import AcpError, _format_acp_error, _is_transient_raw_error
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.llm_helpers import is_transient_backend_error

_USER_RATE_WORDING = "You're sending requests faster than your rate limit allows."

# The reason and the wording together, in the structured shape the engine uses.
_REASON_AND_WORDING = {
    "code": -32603,
    "message": _USER_RATE_WORDING,
    "data": {
        "errorType": "ClientThrottleError",
        "throttleReason": "USER_REQUEST_RATE_EXCEEDED",
        "retryErrorType": "THROTTLING",
    },
}
# The reason alone, behind a generic JSON-RPC message.
_REASON_ONLY = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "throttleReason": "USER_REQUEST_RATE_EXCEEDED",
        "retryErrorType": "THROTTLING",
    },
}
# The wording alone, with no reason field.
_WORDING_ONLY = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": _USER_RATE_WORDING,
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}
# The reason inside a plain-text ``data`` string.
_REASON_IN_TEXT = {
    "code": -32603,
    "message": "Internal error",
    "data": "ThrottlingException: throttleReason=USER_REQUEST_RATE_EXCEEDED (request_id: 0a1b-2c3d)",
}
_USER_RATE_FRAMES = [_REASON_AND_WORDING, _REASON_ONLY, _WORDING_ONLY, _REASON_IN_TEXT]
_USER_RATE_IDS = ["reason-and-wording", "reason-only", "wording-only", "reason-in-text"]

# Throttles without the user-rate reason: today's provider wording must stay.
_PROVIDER_THROTTLE = {
    "code": -32603,
    "message": "Internal error",
    "data": "ThrottlingException: Rate exceeded",
}
_KAS_PLAIN_THROTTLE = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}


def _raised(frame: dict) -> AcpError:
    try:
        _raise_acp_error(frame, backend="kas")
    except AcpError as exc:
        return exc
    raise AssertionError("the frame raised nothing")


@pytest.mark.parametrize("backend", ["kas", ""], ids=["kas", "kiro-cli"])
@pytest.mark.parametrize("frame", _USER_RATE_FRAMES, ids=_USER_RATE_IDS)
def test_user_rate_limit_names_the_limit_and_not_a_model_switch(frame, backend):
    out = _format_acp_error(frame, backend=backend)
    assert out.startswith("Kiro is limiting how fast you can send requests"), out
    assert "Switching to a different model will not help" in out
    assert "wait" in out.lower()
    assert "switch to a different model in the picker" not in out
    assert "Bedrock" not in out
    assert "model provider" not in out
    # Users have no terminal: the text names no command or config file.
    assert "~/.kiro" not in out and "kirocrew " not in out


def test_user_rate_limit_keeps_the_request_id():
    assert _format_acp_error(_REASON_IN_TEXT, backend="kas").endswith("(request_id: 0a1b-2c3d)")


@pytest.mark.parametrize("frame", _USER_RATE_FRAMES, ids=_USER_RATE_IDS)
def test_user_rate_limit_stays_a_retryable_throttle(frame):
    assert _is_transient_raw_error(frame) is True
    err = _raised(frame)
    assert err.transient is True
    assert err.throttle is True
    # The string fallback that restored transcripts use must agree.
    assert is_transient_backend_error(str(err))


@pytest.mark.parametrize(
    ("frame", "backend", "lead"),
    [
        (_PROVIDER_THROTTLE, "kas", "The model provider is throttling requests."),
        (_PROVIDER_THROTTLE, "", "Bedrock is throttling requests."),
        (_KAS_PLAIN_THROTTLE, "kas", "The model provider is throttling requests."),
    ],
    ids=["provider-on-kas", "provider-on-kiro-cli", "kas-plain-throttle"],
)
def test_other_throttles_keep_the_provider_wording(frame, backend, lead):
    out = _format_acp_error(frame, backend=backend)
    assert out.startswith(lead)
    assert "switch to a different model in the picker" in out
