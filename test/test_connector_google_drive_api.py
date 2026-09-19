"""Google Drive v3 request-builder, error-classification, and pagination tests.

Pure functions -- no network, no account. These prove the wire shapes the brief
requires: BOTH drive flags on every all-drives read, native types -> export /
binary -> alt=media, shortcut recognition, the stale-page-token recogniser that
does NOT invent a status code, and the 403-overload split (throttle vs forbidden).
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.google_drive import drive_api, errors, pagination

# --- files.list / changes: BOTH drive flags, always ------------------------


def test_list_files_sets_both_shared_drive_flags() -> None:
    req = drive_api.build_list_files()
    # The exact bug the brief warns about: one flag without the other silently
    # searches only My Drive. Both must be present together.
    assert req.params["supportsAllDrives"] is True
    assert req.params["includeItemsFromAllDrives"] is True
    assert req.params["corpora"] == "allDrives"
    assert req.url == f"{drive_api.API_ROOT}/files"


def test_list_files_scoped_to_one_shared_drive() -> None:
    req = drive_api.build_list_files(drive_id="0AByShared")
    assert req.params["corpora"] == "drive"
    assert req.params["driveId"] == "0AByShared"
    assert req.params["supportsAllDrives"] is True
    assert req.params["includeItemsFromAllDrives"] is True


def test_list_changes_requires_a_page_token() -> None:
    with pytest.raises(ValueError):
        drive_api.build_list_changes("")


def test_list_changes_sets_both_flags_and_includes_removed() -> None:
    req = drive_api.build_list_changes("tok123")
    assert req.params["pageToken"] == "tok123"
    assert req.params["supportsAllDrives"] is True
    assert req.params["includeItemsFromAllDrives"] is True
    assert req.params["includeRemoved"] is True


def test_start_page_token_request_shape() -> None:
    req = drive_api.build_get_start_page_token()
    assert req.url == f"{drive_api.API_ROOT}/changes/startPageToken"
    assert req.params["supportsAllDrives"] is True


# --- native -> export, binary -> alt=media, shortcut -----------------------


def test_native_google_types_are_export_not_download() -> None:
    assert drive_api.is_google_native("application/vnd.google-apps.document")
    assert drive_api.export_mime_for("application/vnd.google-apps.document") == "text/plain"
    assert drive_api.export_mime_for("application/vnd.google-apps.spreadsheet") == "text/csv"
    # A folder and a shortcut share the prefix but are NOT exportable documents.
    assert not drive_api.is_google_native(drive_api.FOLDER_MIME)
    assert not drive_api.is_google_native(drive_api.SHORTCUT_MIME)


def test_native_type_with_no_text_export_maps_to_none() -> None:
    # A Drawing/Form has no text export target we request -> None (skip content),
    # never a guessed binary target.
    assert drive_api.export_mime_for("application/vnd.google-apps.drawing") is None


def test_build_export_is_alt_media_bytes() -> None:
    req = drive_api.build_export("FID", "text/plain")
    assert req.alt_media is True
    assert req.url.endswith("/FID/export")
    assert req.params["mimeType"] == "text/plain"


def test_build_get_media_is_alt_media() -> None:
    req = drive_api.build_get_media("FID")
    assert req.alt_media is True
    assert req.params["alt"] == "media"


def test_shortcut_and_folder_recognition() -> None:
    assert drive_api.is_shortcut(drive_api.SHORTCUT_MIME)
    assert drive_api.is_folder(drive_api.FOLDER_MIME)
    assert not drive_api.is_shortcut("application/pdf")


def test_file_id_is_url_quoted_in_path() -> None:
    # A hostile id must not escape the path segment.
    req = drive_api.build_get_metadata("a/b?c")
    assert "a%2Fb%3Fc" in req.url


# --- error classification: the 403 overload split -------------------------


def test_403_rate_limit_is_throttle() -> None:
    f = errors.DriveFailure(status=403, reason="userRateLimitExceeded")
    assert errors.classify(f) == "throttle"


def test_403_permission_is_forbidden() -> None:
    f = errors.DriveFailure(status=403, reason="insufficientFilePermissions")
    assert errors.classify(f) == "forbidden"


def test_403_unknown_reason_defaults_to_forbidden_not_throttle() -> None:
    # Conservative: an unknown 403 is a permission problem, not retried.
    f = errors.DriveFailure(status=403, reason="somethingNew")
    assert errors.classify(f) == "forbidden"


def test_status_class_table() -> None:
    assert errors.classify(errors.DriveFailure(status=401)) == "auth"
    assert errors.classify(errors.DriveFailure(status=404)) == "not_found"
    assert errors.classify(errors.DriveFailure(status=429)) == "throttle"
    assert errors.classify(errors.DriveFailure(status=400)) == "input"
    assert errors.classify(errors.DriveFailure(status=503)) == "temporary"


def test_retry_after_header_parsed_and_malformed_ignored() -> None:
    assert (
        errors.retry_after_seconds(errors.DriveFailure(status=429, headers={"Retry-After": "12"}))
        == 12.0
    )
    assert (
        errors.retry_after_seconds(errors.DriveFailure(status=429, headers={"Retry-After": "soon"}))
        is None
    )
    assert errors.retry_after_seconds(errors.DriveFailure(status=429)) is None


# --- stale page token: recognised, no invented status code -----------------


def test_stale_page_token_recognised_on_400_with_invalid_token_reason() -> None:
    assert (
        errors.is_stale_page_token(errors.DriveFailure(status=400, reason="invalidPageToken"))
        is True
    )
    assert (
        errors.is_stale_page_token(errors.DriveFailure(status=400, reason="pageTokenInvalid"))
        is True
    )


def test_stale_page_token_false_for_other_400_and_other_status() -> None:
    # A generic 400 is a real input error, NOT a resync -- must not be swallowed.
    assert errors.is_stale_page_token(errors.DriveFailure(status=400, reason="badRequest")) is False
    # And a 404/410 is not read as a stale token (no invented code).
    assert errors.is_stale_page_token(errors.DriveFailure(status=404)) is False
    assert errors.is_stale_page_token(errors.DriveFailure(status=410)) is False


# --- pagination parsing: two distinct cursor contracts ---------------------


def test_change_page_reads_both_tokens_distinctly() -> None:
    body = {
        "changes": [{"fileId": "F1", "removed": False}],
        "nextPageToken": "next-within-batch",
    }
    page = pagination.parse_change_page(body)
    assert page.next_page_token == "next-within-batch"
    assert page.new_start_page_token is None
    assert not page.is_last_page


def test_change_page_final_page_carries_new_start_token() -> None:
    body = {"changes": [], "newStartPageToken": "resume-here"}
    page = pagination.parse_change_page(body)
    assert page.next_page_token is None
    assert page.new_start_page_token == "resume-here"
    assert page.is_last_page


def test_removed_change_detected_both_shapes() -> None:
    assert pagination.is_removed_change({"fileId": "F", "removed": True})
    assert pagination.is_removed_change({"fileId": "F", "file": {"trashed": True}})
    assert not pagination.is_removed_change({"fileId": "F", "file": {"trashed": False}})


def test_file_list_tolerates_missing_and_malformed_files() -> None:
    assert pagination.parse_file_list({}).files == ()
    assert pagination.parse_file_list({"files": "notalist"}).files == ()
    good = pagination.parse_file_list({"files": [{"id": "F1"}, "junk", {"id": "F2"}]})
    assert [f["id"] for f in good.files] == ["F1", "F2"]


def test_read_start_page_token_malformed_is_none() -> None:
    assert pagination.read_start_page_token({}) is None
    assert pagination.read_start_page_token({"startPageToken": 123}) is None
    assert pagination.read_start_page_token({"startPageToken": "tok"}) == "tok"
