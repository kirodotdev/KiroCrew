"""Tests for ArtifactStore.merge_remote_comments — inbound provider comment sync.

The provider is authoritative for its own comments; the local store mirrors them
on fetch-on-view. These cover the reconciliation contract: add new, update
mutable fields, drop tombstones, cascade-drop a deleted thread root, never touch
local comments, and never wipe provider comments merely absent from one fetch.
"""

from __future__ import annotations

import pytest

from kiro_crew.artifacts import ArtifactComment, ArtifactStore

_PROVIDER = "prov-a"


@pytest.fixture
def store(tmp_path):
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Test Artifact", content="<p>Hello</p>", slug="test-art")
    return s


def _remote(remote_id, *, body="b", status="open", parent=None, deleted=False, author="alice"):
    """Build a provider-origin ArtifactComment as the fetch-on-view mapper would."""
    origin = f"{_PROVIDER}:{remote_id}"
    thread_id = parent if parent else remote_id
    return ArtifactComment(
        id=remote_id,
        origin=origin,
        provider=_PROVIDER,
        scope="shared",
        author=author,
        body=body,
        thread_id=thread_id,
        parent_id=parent,
        status=status,
        sync_state="synced",
        deleted=deleted,
    )


def _local(cid, *, body="local body"):
    c = ArtifactComment(id=cid, origin="local", scope="private", author="me", body=body)
    c.thread_id = cid
    return c


class TestMergeRemoteComments:
    def test_adds_new_provider_comments(self, store):
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1", body="hi")])
        got = store.list_comments("test-art")
        assert len(got) == 1
        assert got[0].origin == f"{_PROVIDER}:r1"
        assert got[0].body == "hi"

    def test_updates_mutable_fields(self, store):
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1", status="open")])
        store.merge_remote_comments(
            "test-art", _PROVIDER, [_remote("r1", status="resolved", body="edited")]
        )
        got = store.list_comments("test-art")
        assert len(got) == 1
        assert got[0].status == "resolved"
        assert got[0].body == "edited"

    def test_drops_tombstoned(self, store):
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1")])
        assert len(store.list_comments("test-art")) == 1
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1", deleted=True)])
        assert store.list_comments("test-art") == []

    def test_cascade_drops_deleted_root_thread(self, store):
        # A root + a reply, then the root comes back tombstoned — both go.
        root = _remote("root")
        reply = _remote("reply", parent="root")
        store.merge_remote_comments("test-art", _PROVIDER, [root, reply])
        assert len(store.list_comments("test-art")) == 2
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("root", deleted=True), reply])
        assert store.list_comments("test-art") == []

    def test_leaves_local_untouched(self, store):
        store.add_comment("test-art", _local("loc1"))
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1")])
        got = {c.id: c for c in store.list_comments("test-art")}
        assert "loc1" in got and got["loc1"].origin == "local"
        assert "r1" in got

    def test_absent_provider_comment_not_wiped(self, store):
        # A provider comment seen once, then absent from a later (empty) fetch —
        # keep it (transient/paginated empty is NOT a delete; deletes tombstone).
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1")])
        store.merge_remote_comments("test-art", _PROVIDER, [])
        got = store.list_comments("test-art")
        assert len(got) == 1 and got[0].id == "r1"

    def test_returns_existing_when_no_change(self, store):
        store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1")])
        # Same fetch again → no change → returns the existing list unchanged.
        result = store.merge_remote_comments("test-art", _PROVIDER, [_remote("r1")])
        assert len(result) == 1 and result[0].id == "r1"


_AKIA = "AKIAIOSFODNN7EXAMPLE"  # canonical AWS access-key-id shaped test token


class TestRemoteCommentAnchorRedaction:
    """Provider-controlled anchor text is echoed to the dashboard, so the
    serializer must run it through the same credential/exfil redactor as the
    body — merged remote comments are stored raw, so redaction can't only live
    on the local POST path (backend-security-controls)."""

    def test_serialize_remote_comment_redacts_anchor_fields(self):
        from kiro_crew.dashboard.handlers.artifacts import _serialize_remote_comment
        from kiro_crew.publish_provider import CommentAnchor, RemoteComment

        rc = RemoteComment(
            remote_id="c1",
            thread_id="c1",
            author="alice",
            body=f"body {_AKIA}",
            anchor=CommentAnchor(
                quote=f"quote {_AKIA}",
                prefix=f"prefix {_AKIA}",
                suffix=f"suffix {_AKIA}",
            ),
        )
        entry = _serialize_remote_comment(rc, _PROVIDER)
        assert _AKIA not in entry["body"]
        assert _AKIA not in entry["anchor"]["quote"]
        assert _AKIA not in entry["anchor"]["prefix"]
        assert _AKIA not in entry["anchor"]["suffix"]

    def test_serialize_remote_comment_preserves_none_anchor_parts(self):
        from kiro_crew.dashboard.handlers.artifacts import _serialize_remote_comment
        from kiro_crew.publish_provider import CommentAnchor, RemoteComment

        rc = RemoteComment(
            remote_id="c2",
            thread_id="c2",
            author="bob",
            body="clean body",
            anchor=CommentAnchor(quote="just a quote", prefix=None, suffix=None),
        )
        entry = _serialize_remote_comment(rc, _PROVIDER)
        assert entry["anchor"]["quote"] == "just a quote"
        assert entry["anchor"]["prefix"] is None
        assert entry["anchor"]["suffix"] is None
