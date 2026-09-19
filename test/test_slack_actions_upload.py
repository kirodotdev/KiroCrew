"""Tests for the three-stage Slack external upload protocol.

Every stage's failure is exercised: stage-1 input rejection and Slack error
codes, stage-2 non-200 POST, stage-3 finalization refusal and double-complete.
Also asserts files.upload is recorded as deprecated. No happy-path-only coverage.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.slack.upload import (
    DEPRECATED_UPLOAD_METHOD,
    MAX_ALT_TXT_CHARS,
    UploadSession,
    UploadStage,
    UploadState,
    post_bytes_ok,
    stage_of_error,
    validate_get_url_inputs,
)


def test_files_upload_single_call_is_recorded_deprecated():
    assert DEPRECATED_UPLOAD_METHOD == "files.upload"


# --- stage 1 inputs ----------------------------------------------------------


def test_stage1_zero_length_is_rejected_as_missing_argument():
    errs = validate_get_url_inputs(length=0, filename="a.png")
    assert any("missing_argument" in e for e in errs)


def test_stage1_empty_filename_rejected():
    errs = validate_get_url_inputs(length=10, filename="")
    assert any("filename" in e for e in errs)


def test_stage1_alt_txt_over_cap_rejected():
    errs = validate_get_url_inputs(
        length=10, filename="a.png", alt_txt="x" * (MAX_ALT_TXT_CHARS + 1)
    )
    assert any("alt_txt_too_large" in e for e in errs)


def test_stage1_valid_inputs_have_no_errors():
    assert validate_get_url_inputs(length=10, filename="a.png") == []


# --- error -> stage attribution ---------------------------------------------


@pytest.mark.parametrize(
    "code,stage",
    [
        ("file_uploads_disabled", UploadStage.GET_URL),
        ("file_type_not_allowed", UploadStage.GET_URL),
        ("storage_limit_reached", UploadStage.GET_URL),
        ("file_not_found", UploadStage.COMPLETE),
        ("not_in_channel", UploadStage.COMPLETE),
    ],
)
def test_stage_of_error_attributes_known_codes(code, stage):
    assert stage_of_error(code) is stage


def test_stage_of_error_unknown_code_is_none():
    assert stage_of_error("totally_unknown") is None


# --- stage 2 (bare POST) -----------------------------------------------------


def test_post_bytes_ok_only_on_200():
    assert post_bytes_ok(200) is True
    for bad in (201, 400, 403, 500, 502):
        assert post_bytes_ok(bad) is False


# --- session state machine ---------------------------------------------------


def _new_session() -> UploadSession:
    return UploadSession(filename="a.png", length=123)


def test_happy_path_state_transitions():
    s = _new_session()
    s.acquired_url(upload_url="https://files.slack.com/upload/v1/X", file_id="F1")
    assert s.state is UploadState.URL_ACQUIRED
    assert s.posted_bytes(200) is True
    assert s.state is UploadState.BYTES_POSTED
    s.completed()
    assert s.is_complete is True


def test_cannot_post_bytes_before_acquiring_url():
    s = _new_session()
    with pytest.raises(ValueError):
        s.posted_bytes(200)


def test_cannot_complete_before_posting_bytes():
    s = _new_session()
    s.acquired_url(upload_url="https://x", file_id="F1")
    with pytest.raises(ValueError):
        s.completed()


def test_stage2_non_200_marks_failed_at_post_stage():
    s = _new_session()
    s.acquired_url(upload_url="https://x", file_id="F1")
    assert s.posted_bytes(500) is False
    assert s.is_failed is True
    assert s.failed_stage is UploadStage.POST_BYTES
    assert s.error_code == "http_500"


def test_double_complete_is_refused_no_idempotency_key():
    s = _new_session()
    s.acquired_url(upload_url="https://x", file_id="F1")
    s.posted_bytes(200)
    s.completed()
    with pytest.raises(ValueError):
        s.completed()  # the missing-idempotency-key guard


def test_stage1_must_return_both_url_and_file_id():
    s = _new_session()
    with pytest.raises(ValueError):
        s.acquired_url(upload_url="", file_id="F1")
    s2 = _new_session()
    with pytest.raises(ValueError):
        s2.acquired_url(upload_url="https://x", file_id="")


def test_stage1_failure_records_stage_and_code():
    s = _new_session()
    s.failed(UploadStage.GET_URL, "file_uploads_disabled")
    assert s.is_failed is True
    assert s.failed_stage is UploadStage.GET_URL
    assert s.error_code == "file_uploads_disabled"


def test_cannot_acquire_url_twice():
    s = _new_session()
    s.acquired_url(upload_url="https://x", file_id="F1")
    with pytest.raises(ValueError):
        s.acquired_url(upload_url="https://y", file_id="F2")
