"""Tests for the ArtifactComment durable store (comments.json sidecar)."""

from __future__ import annotations

import json
import uuid

import pytest

from kiro_crew.artifacts import (
    ArtifactComment,
    ArtifactStore,
)


@pytest.fixture
def store(tmp_path):
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Test Artifact", content="<p>Hello</p>", slug="test-art")
    return s


class TestCommentStore:
    def test_list_empty(self, store):
        comments = store.list_comments("test-art")
        assert comments == []

    def test_add_and_list(self, store):
        c = ArtifactComment(
            id=str(uuid.uuid4()),
            origin="local",
            scope="private",
            author="nrb",
            body="This looks good",
            thread_id="",
            created_at="2026-06-10T00:00:00Z",
            updated_at="2026-06-10T00:00:00Z",
        )
        c.thread_id = c.id
        store.add_comment("test-art", c)
        comments = store.list_comments("test-art")
        assert len(comments) == 1
        assert comments[0].body == "This looks good"
        assert comments[0].author == "nrb"

    def test_update_comment(self, store):
        c = ArtifactComment(
            id="cmt-1",
            body="original",
            thread_id="cmt-1",
            created_at="2026-06-10T00:00:00Z",
            updated_at="2026-06-10T00:00:00Z",
        )
        store.add_comment("test-art", c)
        result = store.update_comment("test-art", "cmt-1", status="review", body="updated")
        assert result is not None
        assert result.status == "review"
        assert result.body == "updated"
        # Persisted
        reloaded = store.list_comments("test-art")
        assert reloaded[0].status == "review"

    def test_update_nonexistent(self, store):
        result = store.update_comment("test-art", "nonexistent", body="x")
        assert result is None

    def test_delete_comment(self, store):
        c = ArtifactComment(
            id="cmt-del", body="bye", thread_id="cmt-del", created_at="", updated_at=""
        )
        store.add_comment("test-art", c)
        assert store.delete_comment("test-art", "cmt-del") is True
        assert store.list_comments("test-art") == []

    def test_delete_nonexistent(self, store):
        assert store.delete_comment("test-art", "nope") is False

    def test_delete_root_cascades_to_replies(self, store):
        root = ArtifactComment(
            id="root",
            origin="local",
            scope="private",
            author="nrb",
            body="root",
            thread_id="root",
            created_at="",
            updated_at="",
        )
        r1 = ArtifactComment(
            id="r1",
            origin="local",
            scope="private",
            author="nrb",
            body="reply 1",
            thread_id="root",
            parent_id="root",
            created_at="",
            updated_at="",
        )
        r2 = ArtifactComment(
            id="r2",
            origin="local",
            scope="private",
            author="nrb",
            body="reply 2",
            thread_id="root",
            parent_id="root",
            created_at="",
            updated_at="",
        )
        for c in (root, r1, r2):
            store.add_comment("test-art", c)
        assert len(store.list_comments("test-art")) == 3
        # Deleting the parent removes the whole thread (children are not orphaned).
        assert store.delete_comment("test-art", "root") is True
        assert store.list_comments("test-art") == []

    def test_delete_reply_keeps_root_and_siblings(self, store):
        root = ArtifactComment(
            id="root",
            origin="local",
            scope="private",
            author="nrb",
            body="root",
            thread_id="root",
            created_at="",
            updated_at="",
        )
        r1 = ArtifactComment(
            id="r1",
            origin="local",
            scope="private",
            author="nrb",
            body="reply 1",
            thread_id="root",
            parent_id="root",
            created_at="",
            updated_at="",
        )
        r2 = ArtifactComment(
            id="r2",
            origin="local",
            scope="private",
            author="nrb",
            body="reply 2",
            thread_id="root",
            parent_id="root",
            created_at="",
            updated_at="",
        )
        for c in (root, r1, r2):
            store.add_comment("test-art", c)
        # Deleting a reply removes only that reply; root + sibling survive.
        assert store.delete_comment("test-art", "r1") is True
        ids = {c.id for c in store.list_comments("test-art")}
        assert ids == {"root", "r2"}

    def test_comments_json_persistence(self, store):
        c = ArtifactComment(
            id="persist-1",
            origin="local",
            scope="private",
            author="agent",
            is_agent=True,
            body="test body",
            anchor_quote="hello",
            anchor_start_offset=5,
            anchor_end_offset=10,
            anchor_version=2,
            thread_id="persist-1",
            status="open",
            sync_state="local_only",
            created_at="2026-06-10T00:00:00Z",
            updated_at="2026-06-10T00:00:00Z",
        )
        store.add_comment("test-art", c)

        # Read raw file
        path = store.root / "test-art" / "comments.json"
        assert path.exists()
        raw = json.loads(path.read_text())
        assert len(raw) == 1
        assert raw[0]["anchor_quote"] == "hello"
        assert raw[0]["anchor_start_offset"] == 5
        assert raw[0]["is_agent"] is True

    def test_tolerant_load_missing_file(self, store):
        """Missing comments.json returns empty list, no crash."""
        comments = store.list_comments("test-art")
        assert comments == []

    def test_tolerant_load_malformed(self, store):
        """Malformed comments.json returns empty list."""
        path = store.root / "test-art" / "comments.json"
        path.write_text("not json at all")
        comments = store.list_comments("test-art")
        assert comments == []


def _anchored(cid: str, quote: str, **kw) -> ArtifactComment:
    c = ArtifactComment(
        id=cid,
        body=f"comment on {quote!r}",
        thread_id=cid,
        anchor_quote=quote,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
        **kw,
    )
    return c


class TestAnchorRescan:
    """Content writes through update() rescan comment anchors and flip the
    dedicated ``anchor_orphaned`` field (Mesh-2752). The flag is symmetric
    (restored content clears it) and never touches ``sync_state``, which
    tracks provider push status."""

    def test_content_update_orphans_missing_anchor(self, store):
        store.add_comment("test-art", _anchored("c1", "Hello"))
        store.update("test-art", content="<p>Goodbye</p>")
        c = store.list_comments("test-art")[0]
        assert c.anchor_orphaned is True

    def test_anchor_still_present_not_orphaned(self, store):
        store.add_comment("test-art", _anchored("c1", "Hello"))
        store.update("test-art", content="<p>Hello again</p>")
        c = store.list_comments("test-art")[0]
        assert c.anchor_orphaned is False

    def test_restored_content_clears_orphan_flag(self, store):
        store.add_comment("test-art", _anchored("c1", "Hello"))
        store.update("test-art", content="<p>Goodbye</p>")
        assert store.list_comments("test-art")[0].anchor_orphaned is True
        # Content that brings the quote back (e.g. a revert) restores.
        store.update("test-art", content="<p>Hello</p>")
        assert store.list_comments("test-art")[0].anchor_orphaned is False

    def test_resolved_comments_skipped(self, store):
        store.add_comment("test-art", _anchored("c1", "Hello", status="resolved"))
        store.update("test-art", content="<p>Goodbye</p>")
        assert store.list_comments("test-art")[0].anchor_orphaned is False

    def test_unanchored_comments_untouched(self, store):
        c = ArtifactComment(id="c1", body="doc-level", thread_id="c1")
        store.add_comment("test-art", c)
        store.update("test-art", content="<p>Goodbye</p>")
        assert store.list_comments("test-art")[0].anchor_orphaned is False

    def test_orphan_flag_persists_and_sync_state_untouched(self, store):
        store.add_comment("test-art", _anchored("c1", "Hello", sync_state="pending_push"))
        store.update("test-art", content="<p>Goodbye</p>")
        raw = json.loads((store.root / "test-art" / "comments.json").read_text())
        assert raw[0]["anchor_orphaned"] is True
        assert raw[0]["sync_state"] == "pending_push"

    def test_metadata_only_update_does_not_rescan(self, store):
        """No content write → anchors untouched (rescan only piggybacks on
        content changes)."""
        store.add_comment("test-art", _anchored("c1", "Hello"))
        store.update("test-art", description="new description")
        assert store.list_comments("test-art")[0].anchor_orphaned is False


class TestCommentActivityEvents:
    """record_comment_event appends ``comment`` lifecycle entries with the
    action / snippet / reason metadata (Mesh-2752)."""

    def test_deleted_event_appended(self, store):
        store.record_comment_event(
            "test-art",
            action="deleted",
            by="agent",
            session_id="slot-1",
            comment_snippet="delete this paragraph",
            reason="applied in v2: paragraph removed",
        )
        art = store.get("test-art")
        ev = art.events[-1]
        assert ev["type"] == "comment"
        assert ev["by"] == "agent"
        assert ev["session_id"] == "slot-1"
        assert ev["metadata"]["action"] == "deleted"
        assert ev["metadata"]["comment_snippet"] == "delete this paragraph"
        assert ev["metadata"]["reason"] == "applied in v2: paragraph removed"

    def test_reviewed_event_without_reason(self, store):
        store.record_comment_event(
            "test-art", action="reviewed", by="agent", comment_snippet="reframe"
        )
        ev = store.get("test-art").events[-1]
        assert ev["metadata"]["action"] == "reviewed"
        assert "reason" not in ev["metadata"]

    def test_unknown_event_type_still_rejected(self, store):
        from kiro_crew.artifacts import ArtifactValidationError

        art = store.get("test-art")
        with pytest.raises(ArtifactValidationError):
            store._append_event(art, type="bogus")

    def test_comment_event_type_accepted_by_append(self, store):
        art = store.get("test-art")
        store._append_event(art, type="comment", by="user")
        assert art.events[-1]["type"] == "comment"


class TestCommentRetentionCap:
    """add_comment is FIFO-bounded at MAX_COMMENTS_PER_ARTIFACT (whole threads)."""

    def _root(self, n: int) -> ArtifactComment:
        cid = f"root-{n:04d}"
        return ArtifactComment(id=cid, body=f"c{n}", thread_id=cid)

    def _reply(self, root_id: str, n: int) -> ArtifactComment:
        cid = f"reply-{n:04d}"
        return ArtifactComment(id=cid, body=f"r{n}", thread_id=root_id, parent_id=root_id)

    def test_cap_drops_oldest_threads_keeps_newest(self, store, monkeypatch):
        from kiro_crew import artifacts as art_mod

        # Shrink the cap so the test is cheap.
        monkeypatch.setattr(art_mod, "MAX_COMMENTS_PER_ARTIFACT", 3)
        for n in range(5):
            store.add_comment("test-art", self._root(n))
        comments = store.list_comments("test-art")
        # Bounded at the cap, and the NEWEST survive (oldest roots pruned).
        assert len(comments) == 3
        ids = {c.id for c in comments}
        assert "root-0004" in ids  # just-added kept
        assert "root-0000" not in ids and "root-0001" not in ids  # oldest dropped

    def test_cap_drops_whole_thread_never_orphans_reply(self, store, monkeypatch):
        from kiro_crew import artifacts as art_mod

        monkeypatch.setattr(art_mod, "MAX_COMMENTS_PER_ARTIFACT", 3)
        # Oldest thread: root-0000 + two replies (3 comments in one thread).
        store.add_comment("test-art", self._root(0))
        store.add_comment("test-art", self._reply("root-0000", 1))
        store.add_comment("test-art", self._reply("root-0000", 2))
        # Adding a 4th (a new root) exceeds the cap -> the oldest WHOLE thread
        # (root-0000 + its replies) is dropped, not just one comment.
        store.add_comment("test-art", self._root(9))
        comments = store.list_comments("test-art")
        ids = {c.id for c in comments}
        assert ids == {"root-0009"}
        # No reply survived without its root.
        assert not any(c.parent_id and c.thread_id not in ids for c in comments)
