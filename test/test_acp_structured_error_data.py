"""Error frames whose provider text is in ``message`` and an object is in ``data``.

Kiro CLI 3.0.0 sends ``{"message": "<provider sentence>", "data": {"errorType": ...}}``.
The ``data``-scoped readers saw only the object's ``repr``, so the usage-limit row
showed the raw dict instead of the provider sentence, and the "temporarily
unavailable" capacity rejection was never retried. The other
shapes -- a string ``data``, claude-agent-acp's ``errorKind`` object, and an
``errorType`` object next to the ``Internal error`` boilerplate -- read as before.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import (
    PROVIDER_ERROR_MODEL_UNAVAILABLE,
    PROVIDER_ERROR_USAGE_LIMIT,
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
    classify_provider_error,
)
from kiro_crew.acp.structured_error_data import provider_data_text, structured_data_prose
from kiro_crew.taskq.adapters import acp_provider
from kiro_crew.taskq.dependency import KIND_DEPENDENCY_UNAVAILABLE, KIND_QUOTA_EXHAUSTED

_LIMIT_SENTENCE = "The monthly usage limit has been reached for this account"
_UNAVAILABLE_SENTENCE = (
    "The model you've selected is temporarily unavailable. Please use '/model' to "
    "select a different model and try again."
)

USAGE_LIMIT_3_0 = {
    "code": -32000,
    "message": _LIMIT_SENTENCE,
    "data": {"errorType": "MonthlyLimitError", "retryErrorType": "MONTHLY_LIMIT"},
}
UNAVAILABLE_3_0 = {
    "code": -32000,
    "message": _UNAVAILABLE_SENTENCE,
    "data": {"errorType": "ModelUnavailableError", "retryErrorType": "MODEL_UNAVAILABLE"},
}


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


class TestTheStructuredShapeKeepsTheProviderText:
    def test_usage_limit_row_quotes_the_provider_sentence(self):
        out = _format_acp_error(USAGE_LIMIT_3_0)
        assert out.startswith(f"{_LIMIT_SENTENCE}. Retrying will not help")
        assert "errorType" not in out
        assert "{" not in out

    def test_usage_limit_stays_terminal(self):
        assert _is_transient_raw_error(USAGE_LIMIT_3_0) is False
        assert _raised(USAGE_LIMIT_3_0).transient is False

    def test_unavailable_is_retried(self):
        assert _is_transient_raw_error(UNAVAILABLE_3_0) is True
        assert _raised(UNAVAILABLE_3_0).transient is True

    def test_unavailable_gets_the_capacity_wording(self):
        out = _format_acp_error(UNAVAILABLE_3_0)
        assert out.startswith("The selected model is unavailable on the backend right now")
        assert "errorType" not in out

    def test_an_unrecognised_structured_error_shows_the_sentence_alone(self):
        frame = {
            "code": -32000,
            "message": "Something new went wrong upstream",
            "data": {"errorType": "BrandNewError"},
        }
        assert _format_acp_error(frame) == "Something new went wrong upstream"

    def test_the_dependency_adapter_reads_the_same_text(self):
        unavailable = acp_provider.classify_raw_error(UNAVAILABLE_3_0)
        assert unavailable is not None and unavailable.kind == KIND_DEPENDENCY_UNAVAILABLE
        limit = acp_provider.classify_raw_error(USAGE_LIMIT_3_0)
        assert limit is not None and limit.kind == KIND_QUOTA_EXHAUSTED

    def test_a_structural_rejection_is_tagged_like_its_verdict(self):
        frame = {
            "code": -32000,
            "message": "The context window overflowed for this request",
            "data": {"errorType": "ContextWindowOverflowError"},
        }
        err = _raised(frame)
        assert err.transient is False
        assert err.structural_terminal is True
        assert err.context_overflow is True

    def test_a_pre_stream_generation_failure_is_retried_by_both_readers(self):
        frame = {
            "code": -32000,
            "message": "Kiro failed to generate a response",
            "data": {"errorType": "GenerationError"},
        }
        assert _is_transient_raw_error(frame) is True
        signal = acp_provider.classify_raw_error(frame)
        assert signal is not None and signal.kind == KIND_DEPENDENCY_UNAVAILABLE

    def test_the_classifier_agrees_given_the_provider_field(self):
        for frame, kind in (
            (UNAVAILABLE_3_0, PROVIDER_ERROR_MODEL_UNAVAILABLE),
            (USAGE_LIMIT_3_0, PROVIDER_ERROR_USAGE_LIMIT),
        ):
            data = provider_data_text(frame)
            verdict = classify_provider_error(f"{data} {frame['message']}", data=data)
            assert verdict.kind == kind


# Shapes that must read exactly as they did before the structured shape was read.
OLD_SHAPES = [
    pytest.param(
        {
            "code": -32603,
            "message": "Internal error",
            "data": f"{_LIMIT_SENTENCE} (request_id: ab-12)",
        },
        id="string_data",
    ),
    pytest.param(
        {"code": -32603, "message": "Internal error", "data": {"errorKind": "unknown"}},
        id="error_kind_object_with_boilerplate",
    ),
    pytest.param(
        {
            "code": -32603,
            "message": "Internal error: API Error: 400 Input is too long.",
            "data": {"errorKind": "unknown"},
        },
        id="error_kind_object_with_prose",
    ),
    pytest.param(
        {
            "code": -32603,
            "message": "Internal error",
            "data": {"errorType": "ModelUnavailableError"},
        },
        id="error_type_object_with_boilerplate",
    ),
    pytest.param({"code": -32603, "message": "Internal error"}, id="no_data"),
    pytest.param({"code": -32603, "message": _UNAVAILABLE_SENTENCE, "data": ""}, id="empty_data"),
]


class TestTheOtherShapesAreUnchanged:
    @pytest.mark.parametrize("frame", OLD_SHAPES)
    def test_the_provider_field_is_the_stringified_data(self, frame):
        assert structured_data_prose(frame) == ""
        assert provider_data_text(frame) == str(frame.get("data", "") or "")

    def test_a_string_usage_limit_still_quotes_data(self):
        frame = {"code": -32603, "message": "Internal error", "data": _LIMIT_SENTENCE}
        assert _format_acp_error(frame).startswith(f"{_LIMIT_SENTENCE}. Retrying")

    def test_an_unavailable_echo_in_message_alone_is_still_not_retried(self):
        """``message`` is read only for the structured shape, never with a string ``data``."""
        frame = {"code": -32603, "message": _UNAVAILABLE_SENTENCE, "data": "opaque failure"}
        assert _is_transient_raw_error(frame) is False

    def test_not_a_dict_reads_as_empty(self):
        assert provider_data_text("boom") == ""
        assert structured_data_prose(None) == ""
