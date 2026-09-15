"""Tests for Asana error classification.

Covers the status->category mapping, the ``errors`` array message extraction,
and the load-bearing cross-workspace denial rule: it is ALWAYS explicit (never
a silent fallback) and it NEVER asserts a 403-vs-404 status, because that code
is unobserved.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.asana.errors import (
    AsanaErrorCategory,
    AsanaErrorShapeError,
    CrossWorkspaceDenied,
    classify_cross_workspace,
    classify_error,
    is_retryable,
)

# ── status -> category ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, expected",
    [
        (400, AsanaErrorCategory.INVALID_REQUEST),
        (401, AsanaErrorCategory.NOT_AUTHENTICATED),
        (402, AsanaErrorCategory.PAYMENT_REQUIRED),
        (403, AsanaErrorCategory.FORBIDDEN),
        (404, AsanaErrorCategory.NOT_FOUND),
        (429, AsanaErrorCategory.RATE_LIMITED),
        (500, AsanaErrorCategory.SERVER_ERROR),
        (503, AsanaErrorCategory.SERVER_ERROR),
        (418, AsanaErrorCategory.OTHER),
    ],
)
def test_classify_error_status_map(status, expected):
    assert classify_error(status).category == expected


def test_classify_error_extracts_messages():
    err = classify_error(
        400, {"errors": [{"message": "name: Missing input"}, {"message": "second"}]}
    )
    assert err.messages == ("name: Missing input", "second")


def test_classify_error_no_body_yields_empty_messages():
    assert classify_error(500).messages == ()


def test_classify_error_malformed_errors_array_is_tolerated():
    # A 5xx may carry no JSON errors array; that is not itself a shape fault.
    assert classify_error(500, {"errors": "oops"}).messages == ()


def test_classify_error_rejects_non_int_status():
    with pytest.raises(AsanaErrorShapeError):
        classify_error("400")  # type: ignore[arg-type]


def test_classify_error_rejects_bool_status():
    with pytest.raises(AsanaErrorShapeError):
        classify_error(True)  # type: ignore[arg-type]


# ── cross-workspace denial is explicit and status-agnostic ──────────────────


def test_cross_workspace_denial_is_explicit_error():
    err = classify_cross_workspace(requested_gid="123", token_workspace="900")
    assert isinstance(err, CrossWorkspaceDenied)
    assert err.requested_gid == "123"
    assert err.token_workspace == "900"


def test_cross_workspace_denial_records_observed_status_verbatim_without_asserting_it():
    # The caller may report the status it saw; the model records it but pins
    # neither 403 nor 404 as canonical for this case.
    e403 = classify_cross_workspace(requested_gid="1", token_workspace="9", observed_status=403)
    e404 = classify_cross_workspace(requested_gid="1", token_workspace="9", observed_status=404)
    assert e403.status == 403
    assert e404.status == 404


def test_cross_workspace_denial_status_optional():
    # When the caller reports no status, the denial still stands (status None).
    err = classify_cross_workspace(requested_gid="1", token_workspace="9")
    assert err.status is None


def test_cross_workspace_denial_message_states_no_fallback():
    err = classify_cross_workspace(requested_gid="123", token_workspace="900", observed_status=404)
    text = str(err).lower()
    assert "another workspace" in text or "fall back" in text


def test_cross_workspace_category_is_not_guessed_from_a_status():
    # classify_error must NOT special-case a plain 403/404 as cross-workspace:
    # that condition depends on a caller fact and an unobserved code, so a bare
    # 403 stays FORBIDDEN and a bare 404 stays NOT_FOUND.
    assert classify_error(403).category is AsanaErrorCategory.FORBIDDEN
    assert classify_error(404).category is AsanaErrorCategory.NOT_FOUND


def test_cross_workspace_denial_rejects_empty_gid():
    with pytest.raises(AsanaErrorShapeError):
        classify_cross_workspace(requested_gid="  ", token_workspace="900")


def test_cross_workspace_denial_rejects_non_int_status():
    with pytest.raises(AsanaErrorShapeError):
        classify_cross_workspace(requested_gid="1", token_workspace="9", observed_status="404")


# ── retryability ────────────────────────────────────────────────────────────


def test_rate_limited_and_server_error_are_retryable():
    assert is_retryable(classify_error(429)) is True
    assert is_retryable(classify_error(503)) is True


def test_durable_conditions_are_not_retryable():
    for status in (400, 401, 402, 403, 404):
        assert is_retryable(classify_error(status)) is False
