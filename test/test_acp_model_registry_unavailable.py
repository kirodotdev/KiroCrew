"""The engine's model-list refusal is not worded as an HTTP 5xx.

KAS answers ``session/prompt`` with ``ModelRegistryUnavailableError`` when it
could not fetch the model list. The cause can persist: a sign-in that names no
profile ARN is refused with HTTP 400 ValidationException on every turn. The
frame's "Please try again" also matches the 5xx retry hint, so it has its own
wording; its retry verdict and classifier kind stay the 5xx hint's, which match
the engine's own ``SERVER_ERROR``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp.client import (
    PROVIDER_ERROR_HTTP_5XX,
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
    classify_provider_error,
)
from kiro_crew.acp.model_registry_errors import MODEL_REGISTRY_UNAVAILABLE_TEXT

# The frame KAS sends (packages/kiro-agent/src/types/errors.ts,
# ModelRegistryUnavailableError): the dict it sends, and the same object as JSON text.
_DATA_DICT = {"errorType": "ModelRegistryUnavailableError", "retryErrorType": "SERVER_ERROR"}
_MESSAGE = "Kiro could not load the available models. Please try again."
FRAMES = [
    {"code": -32000, "message": _MESSAGE, "data": _DATA_DICT},
    {"code": -32000, "message": _MESSAGE, "data": json.dumps(_DATA_DICT)},
]


@pytest.mark.parametrize("frame", FRAMES)
def test_not_worded_as_a_5xx(frame):
    out = _format_acp_error(frame)
    assert "5xx" not in out
    assert "transient error" not in out.lower()
    assert out.startswith(MODEL_REGISTRY_UNAVAILABLE_TEXT)
    assert "list of models" in out


@pytest.mark.parametrize("frame", FRAMES)
def test_retry_verdict_is_the_engines(frame):
    assert _is_transient_raw_error(frame) is True
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame)
    assert raised.value.transient is True
    assert "5xx" not in str(raised.value)


@pytest.mark.parametrize("frame", FRAMES)
def test_classifier_kind_and_verdict_are_unchanged(frame):
    data = str(frame["data"])
    verdict = classify_provider_error(f"{data} {frame['message']}", data=data)
    assert verdict.kind == PROVIDER_ERROR_HTTP_5XX
    assert verdict.retryable


_ECHO = "ModelRegistryUnavailableError. Please try again."


@pytest.mark.parametrize(
    "frame",
    [
        {"code": -32603, "message": _ECHO, "data": "HTTP 503"},
        {"code": -32000, "message": _ECHO, "data": {"errorType": "InternalServerError"}},
        {"code": -32603, "message": "Internal error", "data": _ECHO},
    ],
    ids=["message", "message-beside-structured-data", "prose-data"],
)
def test_a_name_echoed_in_prose_does_not_match(frame):
    """Only ``data.errorType`` names the refusal; the name in any prose changes nothing."""
    assert "5xx" in _format_acp_error(frame)


def test_a_real_5xx_keeps_its_wording():
    frame = {
        "code": -32603,
        "message": "Internal error",
        "data": "InternalServerException HTTP 500",
    }
    assert "5xx" in _format_acp_error(frame)
