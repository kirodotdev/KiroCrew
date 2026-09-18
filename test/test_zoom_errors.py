"""Tests for Zoom error classification and credential redaction (W11-A).

The generic error taxonomy/envelope/redaction now live in the W01 control plane;
this suite covers the Zoom-SPECIFIC folds: the Zoom-code -> RUN-01 mapping
(including the ambiguous-3001 rule, negative fault test 1's error side), the
control-plane-built typed error, and the Zoom-shape credential detector plus the
proof that a leaked Zoom credential never survives (negative fault test 6).
"""

from kiro_crew.connections.control_plane import ERROR_CLASSES
from kiro_crew.connections.vendors.zoom.errors import (
    AMBIGUOUS_ABSENCE_CODE,
    classify_error,
    contains_zoom_credential,
    zoom_operation_error,
)


class TestErrorClassification:
    def test_3001_is_ambiguous_not_not_found(self):
        # negative fault test 1 (error side): Zoom 3001 is ambiguous between a
        # real absence and an un-double-encoded UUID, so it maps to the RUN-01
        # ``ambiguous`` class, never ``not_found``. Uses the exported constant
        # rather than a bare literal, giving AMBIGUOUS_ABSENCE_CODE a consumer.
        assert AMBIGUOUS_ABSENCE_CODE == 3001
        assert classify_error(AMBIGUOUS_ABSENCE_CODE) == "ambiguous"
        assert classify_error(AMBIGUOUS_ABSENCE_CODE) != "not_found"

    def test_3001_stays_ambiguous_even_with_404_status(self):
        assert classify_error(AMBIGUOUS_ABSENCE_CODE, http_status=404) == "ambiguous"

    def test_classes_are_all_run01_members(self):
        for code in (3001, 124, 1001, 200, 2314, 300, 429):
            assert classify_error(code) in ERROR_CLASSES

    def test_auth_code(self):
        assert classify_error(124) == "auth"

    def test_forbidden_codes(self):
        assert classify_error(200) == "forbidden"
        assert classify_error(2314) == "forbidden"

    def test_not_found_code(self):
        assert classify_error(1001) == "not_found"

    def test_input_code(self):
        assert classify_error(300) == "input"

    def test_throttle_code(self):
        assert classify_error(429) == "throttle"

    def test_http_status_fallback(self):
        assert classify_error(None, http_status=401) == "auth"
        assert classify_error(None, http_status=403) == "forbidden"
        assert classify_error(None, http_status=404) == "not_found"
        assert classify_error(None, http_status=429) == "throttle"
        assert classify_error(None, http_status=503) == "temporary"

    def test_unknown_defaults_to_temporary(self):
        assert classify_error(None) == "temporary"
        assert classify_error(999999) == "temporary"


class TestZoomOperationError:
    def test_builds_typed_error_with_class(self):
        err = zoom_operation_error(3001, "meeting not found")
        assert err["error_class"] == "ambiguous"
        assert "error_class" in err and "detail" in err

    def test_detail_is_redacted_by_control_plane(self):
        # a token reflected into the detail must be scrubbed by the control
        # plane's redact-then-truncate discipline (via operation_error).
        leaky = "failed: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aBcDeF123456"
        err = zoom_operation_error(124, leaky)
        assert "eyJhbGciOiJIUzI1NiJ9" not in err["detail"]
        assert contains_zoom_credential(err["detail"]) is False

    def test_detail_capped(self):
        err = zoom_operation_error(300, "x" * 5000)
        assert len(err["detail"]) <= 200


class TestContainsZoomCredential:
    def test_bearer_token_detected(self):
        assert contains_zoom_credential("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aBc")

    def test_signed_download_url_detected(self):
        url = "https://zoom.us/rec/download/Qg75t7xZBtEbAkjdlgbfdngBBBB"
        assert contains_zoom_credential(url)

    def test_signed_play_url_with_pwd_detected(self):
        url = "https://zoom.us/rec/share/xyz?pwd=yNYIS408EJygs7rE5vVsJwXIz4"
        assert contains_zoom_credential(url)

    def test_access_token_field_detected(self):
        assert contains_zoom_credential('{"access_token": "abcDEF1234567890xyz"}')

    def test_refresh_token_field_detected(self):
        assert contains_zoom_credential('"refresh_token": "abcDEF1234567890xyz"')

    def test_download_access_token_detected(self):
        assert contains_zoom_credential('"download_access_token": "abJhbGciOiJIUzUxMiJ9"')

    def test_clean_text_has_no_credential(self):
        assert contains_zoom_credential("meeting 97763643886 does not exist") is False

    def test_empty_string_has_no_credential(self):
        assert contains_zoom_credential("") is False


class TestRedactionViaControlPlane:
    """Negative fault test 6: a Zoom token or signed URL never survives into a
    typed error's detail (scrubbed by the control plane's redacted_detail)."""

    def test_bearer_token_scrubbed_in_error(self):
        err = zoom_operation_error(124, "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aBcDeF123456")
        assert contains_zoom_credential(err["detail"]) is False

    def test_signed_media_url_scrubbed_in_error(self):
        err = zoom_operation_error(
            1001, "download at https://zoom.us/rec/download/Qg75t7xZBtEbAkjdlgbfdng"
        )
        assert contains_zoom_credential(err["detail"]) is False

    def test_token_fields_scrubbed_in_error(self):
        err = zoom_operation_error(300, '{"access_token": "abcDEF1234567890xyz"}')
        assert contains_zoom_credential(err["detail"]) is False

    def test_mixed_leak_fully_scrubbed_in_error(self):
        leak = (
            "err Bearer eyJ0eXAiOiJKV1Qi.payload url "
            "https://zoom.us/rec/play/AbC123?access_token=SECRETTOKENVALUE99"
        )
        err = zoom_operation_error(429, leak)
        assert "SECRETTOKENVALUE99" not in err["detail"]
        assert contains_zoom_credential(err["detail"]) is False
