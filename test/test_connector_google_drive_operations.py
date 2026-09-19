"""DriveOperations sequences over the REAL W01 execute/PageWalk (scripted replies).

Proves the Drive v3 sequences end to end through the true control-plane path with
no account: a multi-page files.list walk, export vs alt=media selection, shortcut
resolution, incremental changes with a durable checkpoint, and the stale-token ->
resync boundary. Also asserts BOTH shared-drive flags on every list request (the
silent-My-Drive-only bug the brief warns about).
"""

from __future__ import annotations

import pytest
from _google_drive_harness import (
    ScriptedReplyTransport,
    W01Runner,
    bytes_reply,
    json_reply,
)

from kiro_crew.connections.vendors.google_drive.operations import (
    DriveOperationError,
    DriveOperations,
)


def _ops(transport, page_size=100):
    return DriveOperations(W01Runner(transport), page_size=page_size)


# --- files.list multi-page, both drive flags -------------------------------


def test_list_files_walks_all_pages_through_w01():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u and "pageToken" not in u,
        json_reply(200, {"files": [{"id": "F1"}], "nextPageToken": "p2"}),
    )
    t.route(
        lambda u, a: "/files" in u and "pageToken=p2" in u,
        json_reply(200, {"files": [{"id": "F2"}]}),
    )
    files = _ops(t).list_files()
    assert [f["id"] for f in files] == ["F1", "F2"]
    # Every built request carried BOTH shared-drive flags.
    for req in t.requests:
        assert "supportsAllDrives=true" in req.url
        assert "includeItemsFromAllDrives=true" in req.url


def test_list_files_scoped_to_shared_drive_sets_corpora_drive():
    t = ScriptedReplyTransport()
    t.route(lambda u, a: "/files" in u, json_reply(200, {"files": [{"id": "F1"}]}))
    _ops(t).list_files(drive_id="0AByShared")
    assert "corpora=drive" in t.requests[0].url
    assert "driveId=0AByShared" in t.requests[0].url


# --- content: export | media | shortcut ------------------------------------


def test_native_doc_uses_export():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/export" in u,
        bytes_reply(200, b"exported text", {"Content-Type": "text/plain"}),
    )
    body, mime = _ops(t).fetch_content(
        {"id": "F1", "mimeType": "application/vnd.google-apps.document"}
    )
    assert body == b"exported text"
    assert mime == "text/plain"
    assert any("/export" in r.url for r in t.requests)


def test_binary_uses_alt_media():
    t = ScriptedReplyTransport()
    t.route(lambda u, a: "alt=media" in u, bytes_reply(200, b"%PDF-1.7"))
    body, mime = _ops(t).fetch_content({"id": "F2", "mimeType": "application/pdf"})
    assert body == b"%PDF-1.7"
    assert mime == "application/pdf"


def test_shortcut_resolved_to_target():
    t = ScriptedReplyTransport()
    shortcut = {
        "id": "SC",
        "mimeType": "application/vnd.google-apps.shortcut",
        "shortcutDetails": {"targetId": "TGT", "targetMimeType": "application/pdf"},
    }
    t.route(
        lambda u, a: "/files/TGT" in u and "alt=media" not in u,
        json_reply(200, {"id": "TGT", "mimeType": "application/pdf"}),
    )
    t.route(lambda u, a: "/files/TGT" in u and "alt=media" in u, bytes_reply(200, b"target-bytes"))
    body, mime = _ops(t).fetch_content(shortcut)
    assert body == b"target-bytes"
    assert mime == "application/pdf"


def test_shortcut_to_shortcut_refused():
    t = ScriptedReplyTransport()
    sc1 = {
        "id": "SC1",
        "mimeType": "application/vnd.google-apps.shortcut",
        "shortcutDetails": {"targetId": "SC2"},
    }
    t.route(
        lambda u, a: "/files/SC2" in u,
        json_reply(
            200,
            {
                "id": "SC2",
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": "SC3"},
            },
        ),
    )
    body, mime = _ops(t).fetch_content(sc1)
    assert (body, mime) == (b"", "")


def test_folder_has_no_content_and_no_request():
    t = ScriptedReplyTransport()
    body, mime = _ops(t).fetch_content(
        {"id": "D", "mimeType": "application/vnd.google-apps.folder"}
    )
    assert (body, mime) == (b"", "")
    assert t.requests == []


# --- change feed: incremental + resync -------------------------------------


def test_list_changes_returns_changes_and_checkpoint():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [{"fileId": "F1", "removed": False, "file": {"id": "F1"}}],
                "newStartPageToken": "cp2",
            },
        ),
    )
    changes, checkpoint = _ops(t).list_changes("start")
    assert [c["fileId"] for c in changes] == ["F1"]
    assert checkpoint == "cp2"


def test_stale_token_triggers_resync():
    # A no-longer-usable page token surfaces as an input-class failure on the
    # change feed (Google documents the resync FLOW but no exact status/reason,
    # and W01 redacts the provider reason anyway -- see _is_stale_page_token).
    # The documented recovery is to re-fetch a fresh startPageToken as the resync
    # boundary: empty changes, fresh checkpoint.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(400, {"error": {"message": "page token no longer valid"}}),
    )
    t.route(
        lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "fresh"})
    )
    changes, checkpoint = _ops(t).list_changes("stale")
    assert changes == []
    assert checkpoint == "fresh"


def test_non_input_failure_on_changes_is_not_swallowed_as_resync():
    # SAFETY: only an input-class failure is the resync boundary. A
    # credential/permission/backoff failure (here 403 -> forbidden) must RAISE,
    # never be silently mistaken for "just re-fetch the token". A getStartPageToken
    # route is present to prove it is NOT taken.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(403, {"error": {"message": "insufficient permissions"}}),
    )
    t.route(
        lambda u, a: "/changes/startPageToken" in u,
        json_reply(200, {"startPageToken": "must-not-be-used"}),
    )
    with pytest.raises(DriveOperationError) as ei:
        _ops(t).list_changes("tok")
    assert ei.value.error_class == "forbidden"


def test_401_maps_to_auth_error():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: True,
        json_reply(
            401, {"error": {"message": "Invalid Credentials", "errors": [{"reason": "authError"}]}}
        ),
    )
    with pytest.raises(DriveOperationError) as ei:
        _ops(t).get_metadata("F1")
    assert ei.value.error_class == "auth"


def test_get_start_page_token_missing_is_error():
    t = ScriptedReplyTransport()
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {}))
    with pytest.raises(DriveOperationError):
        _ops(t).get_start_page_token()
