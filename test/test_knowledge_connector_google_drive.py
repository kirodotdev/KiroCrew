"""GoogleDriveConnector: one SourceRow per Drive file, over the true W01 path.

Proves segment 2 (per-document SourceRow -- never one blob per source) and the
connector half of segment 3 (SyncScheduler: snapshot vs incremental, and the
stale-token -> resync -> full-snapshot fallback), plus the never-invent-a-grant
ACL subject derivation and the required negative cases (malformed file object,
missing permissions, multi-page listing, removed change, content-fetch failure).

Every outbound Drive call runs through the SAME real W01 execute/PageWalk over a
scripted transport the operations tests use (via ``W01Runner`` +
``ScriptedReplyTransport``): the connector's operations_factory hands back a real
``DriveOperations`` bound to that runner, so the connector is exercised on the
true control-plane path with no Google account -- the ``code_complete`` ceiling.
"""

from __future__ import annotations

import asyncio

import pytest
from _google_drive_harness import ScriptedReplyTransport, W01Runner, bytes_reply, json_reply

from kiro_crew.connections.vendors.google_drive.operations import DriveOperations
from kiro_crew.knowledge.acl import PUBLIC_SUBJECT, ProviderResourceRef
from kiro_crew.knowledge.connectors.google_drive import GoogleDriveConnector
from kiro_crew.knowledge.rows import SourceRow


def _connector(transport) -> GoogleDriveConnector:
    # The factory the host would wire: a real DriveOperations over the real W01
    # runner. The connector never builds a token/session/HTTP itself.
    return GoogleDriveConnector(
        operations_factory=lambda source: DriveOperations(W01Runner(transport))
    )


def _run(coro):
    return asyncio.run(coro)


_MY_DRIVE = {"account": "my-drive", "tenant": "ws-acme"}


def _source_with_checkpoint(token: str) -> dict:
    """A source in the SHAPE the shared scheduler passes: a raw sources row whose
    ``properties`` is a JSON STRING holding the checkpoint (there is no top-level
    ``checkpoint`` column). This is what proves the incremental branch is entered
    from the real storage shape, not a synthetic top-level key.
    """
    import json as _json

    return {
        "account": "my-drive",
        "tenant": "ws-acme",
        "source_type": "google_drive",
        "properties": _json.dumps(
            {"checkpoint": token, "account": "my-drive", "tenant": "ws-acme"}
        ),
    }


# --- validate_config --------------------------------------------------------


def test_validate_requires_account_tenant_and_factory():
    c = _connector(ScriptedReplyTransport())
    ok, msg = c.validate_config({"tenant": "ws-acme"})
    assert not ok and "account" in msg
    ok, msg = c.validate_config({"account": "my-drive"})
    assert not ok and "tenant" in msg
    ok, msg = c.validate_config(_MY_DRIVE)
    assert ok and msg == ""
    # No factory wired -> cannot fetch, said at validate time.
    ok, msg = GoogleDriveConnector().validate_config(_MY_DRIVE)
    assert not ok and "factory" in msg


def test_supports_rows_and_source_type():
    c = _connector(ScriptedReplyTransport())
    assert c.supports_rows() is True
    assert c.source_type() == "google_drive"


# --- snapshot: one SourceRow PER FILE, not one blob -------------------------


def _snapshot_transport() -> ScriptedReplyTransport:
    t = ScriptedReplyTransport()
    # files.list -> two files (a native doc + a binary), single page.
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    {
                        "id": "DOC1",
                        "name": "Design",
                        "mimeType": "application/vnd.google-apps.document",
                        "version": "7",
                        "modifiedTime": "2026-09-01T00:00:00Z",
                        "permissions": [{"type": "user", "emailAddress": "alice@acme.com"}],
                    },
                    {
                        "id": "BIN1",
                        "name": "diagram.png",
                        "mimeType": "image/png",
                        "version": "2",
                        "modifiedTime": "2026-09-02T00:00:00Z",
                        "permissions": [{"type": "anyone"}],
                    },
                ]
            },
        ),
    )
    # native doc -> export
    t.route(lambda u, a: "/files/DOC1/export" in u, bytes_reply(200, b"design body text"))
    # binary -> alt=media
    t.route(lambda u, a: "/files/BIN1" in u and "alt=media" in u, bytes_reply(200, b"\x89PNG..."))
    # establish checkpoint after listing
    t.route(
        lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp-1"})
    )
    return t


def test_snapshot_emits_one_row_per_file_with_refs_and_checkpoint():
    rows, snapshot, checkpoint = _run(_connector(_snapshot_transport()).fetch_rows(dict(_MY_DRIVE)))
    assert snapshot is True
    assert checkpoint == "cp-1"
    # ONE row per file -- not aggregated into a single source blob.
    assert len(rows) == 2
    by_key = {r.key: r for r in rows}
    assert set(by_key) == {"DOC1", "BIN1"}
    for r in rows:
        assert isinstance(r, SourceRow)
        assert isinstance(r.resource_ref, ProviderResourceRef)
        assert r.resource_ref.provider == "google_drive"
        assert r.resource_ref.resource_id == r.key
        assert r.resource_ref.locator["fileId"] == r.key
        assert r.tenant == "ws-acme"
    # Per-file fingerprint binds version + modifiedTime (metadata-only change re-ingests).
    assert "drive-version 7" in by_key["DOC1"].text
    assert "design body text" in by_key["DOC1"].text
    assert "drive-version 2" in by_key["BIN1"].text


# --- ACL subject derivation: real principals, never invent public ----------


def test_subjects_from_real_principals_and_anyone_is_public():
    rows = {
        r.key: r for r in _run(_connector(_snapshot_transport()).fetch_rows(dict(_MY_DRIVE)))[0]
    }
    # DOC1 shared to a named user -> that email is the subject.
    assert rows["DOC1"].subjects == ("alice@acme.com",)
    # BIN1 has an explicit type==anyone permission -> genuinely public.
    assert rows["BIN1"].subjects == (PUBLIC_SUBJECT,)


def test_missing_permissions_field_is_empty_deny_all_not_public():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    # No permissions field at all -> must NOT default to public.
                    {
                        "id": "SECRET",
                        "name": "secret.txt",
                        "mimeType": "text/plain",
                        "version": "1",
                    },
                ]
            },
        ),
    )
    t.route(lambda u, a: "/files/SECRET" in u and "alt=media" in u, bytes_reply(200, b"top secret"))
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp"}))
    rows = _run(_connector(t).fetch_rows(dict(_MY_DRIVE)))[0]
    assert len(rows) == 1
    assert rows[0].subjects == ()  # explicit deny-all, query-time probe is authoritative
    assert PUBLIC_SUBJECT not in rows[0].subjects


# --- snapshot skips folders, multi-page listing -----------------------------


def test_snapshot_skips_folders_and_walks_multiple_pages():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u
        and "pageToken" not in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    {
                        "id": "FLD",
                        "name": "folder",
                        "mimeType": "application/vnd.google-apps.folder",
                    },
                    {"id": "T1", "name": "a.txt", "mimeType": "text/plain", "version": "1"},
                ],
                "nextPageToken": "pg2",
            },
        ),
    )
    t.route(
        lambda u, a: "/files" in u and "pageToken=pg2" in u,
        json_reply(
            200,
            {
                "files": [
                    {"id": "T2", "name": "b.txt", "mimeType": "text/plain", "version": "1"},
                ]
            },
        ),
    )
    t.route(lambda u, a: "/files/T1" in u and "alt=media" in u, bytes_reply(200, b"aaa"))
    t.route(lambda u, a: "/files/T2" in u and "alt=media" in u, bytes_reply(200, b"bbb"))
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp"}))
    rows = _run(_connector(t).fetch_rows(dict(_MY_DRIVE)))[0]
    # Folder produces NO row; both text files across both pages do.
    assert sorted(r.key for r in rows) == ["T1", "T2"]


def test_malformed_file_object_without_id_is_skipped():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    {"name": "no id here", "mimeType": "text/plain"},  # malformed: no id
                    {"id": "OK", "name": "ok.txt", "mimeType": "text/plain", "version": "1"},
                ]
            },
        ),
    )
    t.route(lambda u, a: "/files/OK" in u and "alt=media" in u, bytes_reply(200, b"ok"))
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp"}))
    rows = _run(_connector(t).fetch_rows(dict(_MY_DRIVE)))[0]
    assert [r.key for r in rows] == ["OK"]


# --- incremental sync: enters from the REAL properties shape ----------------


def test_incremental_entered_from_properties_shape_and_token_roundtrips():
    # The checkpoint lives in the properties JSON string (no top-level column) --
    # this proves the incremental branch is entered from the real storage shape,
    # and that the batch's newStartPageToken is returned as the advanced token.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [
                    {
                        "fileId": "C1",
                        "file": {
                            "id": "C1",
                            "name": "c.txt",
                            "mimeType": "text/plain",
                            "version": "9",
                            "permissions": [{"type": "user", "emailAddress": "bob@acme.com"}],
                        },
                    },
                ],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(lambda u, a: "/files/C1" in u and "alt=media" in u, bytes_reply(200, b"c body"))
    # No files.list / getStartPageToken routes: if the connector wrongly took the
    # snapshot path (checkpoint read as None), the harness would raise "no
    # scripted reply". Reaching the assertions proves it entered incremental.
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is False  # incremental round, no removal
    assert checkpoint == "cp-2"  # advanced to the batch's newStartPageToken
    assert [r.key for r in rows] == ["C1"]
    assert rows[0].subjects == ("bob@acme.com",)
    # The change feed WAS queried with the token from properties, not a snapshot.
    assert any("/changes" in req.url and "pageToken=cp-1" in req.url for req in t.requests)


def test_top_level_checkpoint_is_honoured_as_fallback():
    # A plain dict with a top-level checkpoint (no properties blob) still enters
    # incremental -- the fallback path, so a caller that already merged props works.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [
                    {
                        "fileId": "C1",
                        "file": {
                            "id": "C1",
                            "name": "c.txt",
                            "mimeType": "text/plain",
                            "version": "1",
                        },
                    },
                ],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(lambda u, a: "/files/C1" in u and "alt=media" in u, bytes_reply(200, b"body"))
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows({**_MY_DRIVE, "checkpoint": "cp-1"}))
    assert snapshot is False
    assert checkpoint == "cp-2"
    assert [r.key for r in rows] == ["C1"]


def test_malformed_properties_blob_degrades_to_first_sync_snapshot():
    # A corrupt properties string reads as no checkpoint -> a first-sync snapshot,
    # not a crash.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {"files": [{"id": "S1", "name": "s.txt", "mimeType": "text/plain", "version": "1"}]},
        ),
    )
    t.route(lambda u, a: "/files/S1" in u and "alt=media" in u, bytes_reply(200, b"s"))
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp"}))
    src = {
        "account": "my-drive",
        "tenant": "ws-acme",
        "source_type": "google_drive",
        "properties": "{not valid json",
    }
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(src))
    assert snapshot is True
    assert [r.key for r in rows] == ["S1"]
    assert checkpoint == "cp"


# --- removal in a batch reconciles via a full snapshot (shared delete path) --


def test_removed_change_reconciles_via_full_snapshot():
    # A removed file cannot be deleted through the incremental row list (the
    # pipeline deletes only on snapshot=True). The connector must reconcile via a
    # full snapshot NOW, through the SHARED protocol -- not defer to a resync that
    # may never come, and not build a second delete channel.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [
                    {
                        "fileId": "C1",
                        "file": {
                            "id": "C1",
                            "name": "c.txt",
                            "mimeType": "text/plain",
                            "version": "9",
                        },
                    },
                    {"fileId": "GONE", "removed": True},
                ],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    # The snapshot re-list the reconcile triggers:
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {"files": [{"id": "C1", "name": "c.txt", "mimeType": "text/plain", "version": "9"}]},
        ),
    )
    t.route(lambda u, a: "/files/C1" in u and "alt=media" in u, bytes_reply(200, b"c body"))
    t.route(
        lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp-2"})
    )
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is True  # delete lands through the shared snapshot protocol
    assert checkpoint == "cp-2"  # batch checkpoint still advanced (removal not re-processed)
    assert [r.key for r in rows] == ["C1"]  # the surviving file's row, from the re-list


def test_trashed_change_also_reconciles_via_full_snapshot():
    # A trashed file (file.trashed == True) is the same class as removed
    # (absent != deleted) and must also drive a snapshot reconcile.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [
                    {
                        "fileId": "T",
                        "file": {
                            "id": "T",
                            "name": "t.txt",
                            "mimeType": "text/plain",
                            "trashed": True,
                        },
                    },
                ],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(200, {"files": []}),
    )
    t.route(
        lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp-2"})
    )
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is True
    assert rows == []  # the trashed file is gone; snapshot has no rows -> pipeline deletes it


# --- metadata failure must NOT advance the checkpoint -----------------------


def test_metadata_failure_holds_checkpoint():
    # A changed file whose metadata (file object) is not inline and whose
    # get_metadata fails could not be turned into a row this round. Advancing the
    # checkpoint past it would skip it forever (the shared scheduler advances on a
    # "fully persisted" round, and a skipped change never becomes a row). So the
    # round must return the ORIGINAL checkpoint.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                # No inline "file" -> connector must get_metadata(C1), which fails.
                "changes": [{"fileId": "C1"}],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(
        lambda u, a: "/files/C1" in u and "alt=media" not in u,
        json_reply(500, {"error": {"message": "transient"}}),
    )
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is False
    assert rows == []
    # Checkpoint HELD at the original, NOT advanced to cp-2, so the next sync
    # re-reads the same batch and re-attempts the change.
    assert checkpoint == "cp-1"


def test_all_metadata_ok_advances_checkpoint():
    # Control: when every changed file resolves, the checkpoint DOES advance.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [{"fileId": "C1"}],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(
        lambda u, a: "/files/C1" in u and "alt=media" not in u,
        json_reply(200, {"id": "C1", "name": "c.txt", "mimeType": "text/plain", "version": "1"}),
    )
    t.route(lambda u, a: "/files/C1" in u and "alt=media" in u, bytes_reply(200, b"c"))
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is False
    assert [r.key for r in rows] == ["C1"]
    assert checkpoint == "cp-2"  # advanced: the round fully resolved


# --- stale token -> resync -> full snapshot fallback ------------------------


def test_stale_checkpoint_resyncs_into_full_snapshot():
    t = ScriptedReplyTransport()
    # incremental changes.list with the stale token -> input-class failure
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(400, {"error": {"message": "page token no longer valid"}}),
    )
    # resync fetches a fresh start token...
    t.route(
        lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp-fresh"})
    )
    # ...and the connector falls back to a full snapshot list.
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    {"id": "R1", "name": "r.txt", "mimeType": "text/plain", "version": "1"},
                ]
            },
        ),
    )
    t.route(lambda u, a: "/files/R1" in u and "alt=media" in u, bytes_reply(200, b"resynced"))
    rows, snapshot, checkpoint = _run(
        _connector(t).fetch_rows(_source_with_checkpoint("stale-token"))
    )
    assert snapshot is True  # resync reconciles via full snapshot
    assert checkpoint == "cp-fresh"
    assert [r.key for r in rows] == ["R1"]


# --- content-fetch failure on one file does not sink the batch --------------


def test_content_fetch_failure_skips_that_row_only():
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/files" in u
        and "/export" not in u
        and "startPageToken" not in u
        and "alt=media" not in u,
        json_reply(
            200,
            {
                "files": [
                    {"id": "BAD", "name": "bad.txt", "mimeType": "text/plain", "version": "1"},
                    {"id": "GOOD", "name": "good.txt", "mimeType": "text/plain", "version": "1"},
                ]
            },
        ),
    )
    # BAD's content fetch fails (403), GOOD's succeeds.
    t.route(
        lambda u, a: "/files/BAD" in u and "alt=media" in u,
        json_reply(403, {"error": {"message": "no access"}}),
    )
    t.route(lambda u, a: "/files/GOOD" in u and "alt=media" in u, bytes_reply(200, b"good"))
    t.route(lambda u, a: "/changes/startPageToken" in u, json_reply(200, {"startPageToken": "cp"}))
    rows = _run(_connector(t).fetch_rows(dict(_MY_DRIVE)))[0]
    # BAD is skipped (its content could not be read); GOOD still lands.
    assert [r.key for r in rows] == ["GOOD"]


# --- duplicate change records for one file collapse to one row --------------


def test_duplicate_change_records_dedup_to_last_state():
    # Drive may report a file several times in one batch (edited then re-shared).
    # Only the LAST state matters, and the file must not be ingested twice.
    t = ScriptedReplyTransport()
    t.route(
        lambda u, a: "/changes" in u and "startPageToken" not in u,
        json_reply(
            200,
            {
                "changes": [
                    {
                        "fileId": "C1",
                        "file": {
                            "id": "C1",
                            "name": "old.txt",
                            "mimeType": "text/plain",
                            "version": "1",
                        },
                    },
                    {
                        "fileId": "C1",
                        "file": {
                            "id": "C1",
                            "name": "new.txt",
                            "mimeType": "text/plain",
                            "version": "2",
                        },
                    },
                ],
                "newStartPageToken": "cp-2",
            },
        ),
    )
    t.route(lambda u, a: "/files/C1" in u and "alt=media" in u, bytes_reply(200, b"body"))
    rows, snapshot, checkpoint = _run(_connector(t).fetch_rows(_source_with_checkpoint("cp-1")))
    assert snapshot is False
    assert checkpoint == "cp-2"
    # Exactly ONE row for C1 (not two), carrying the LAST reported state (v2).
    assert [r.key for r in rows] == ["C1"]
    assert "drive-version 2" in rows[0].text


# --- fetch() is refused: this is a structured connector ---------------------


def test_plain_fetch_is_not_implemented():
    with pytest.raises(NotImplementedError):
        _run(_connector(ScriptedReplyTransport()).fetch(dict(_MY_DRIVE)))
