"""Tests for the comment-forwarding filter (resolved-comment replay fix).

Covers:
- The shared predicate ``filter_comments_for_forward`` (unit).
- The handler query-param integration (``?exclude_resolved=true``).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactComment, ArtifactStore, filter_comments_for_forward
from kiro_crew.dashboard.handlers import artifacts as h


# ── Unit tests for filter_comments_for_forward ───────────────────────────────


def _c(
    id: str,
    status: str = "open",
    anchor_version: int | None = None,
    parent_id: str | None = None,
) -> ArtifactComment:
    return ArtifactComment(
        id=id,
        status=status,
        anchor_version=anchor_version,
        parent_id=parent_id,
        thread_id=parent_id or id,
    )


class TestFilterCommentsForForward:
    def test_excludes_resolved(self):
        comments = [_c("r", status="resolved"), _c("k", status="open")]
        assert [c.id for c in filter_comments_for_forward(comments)] == ["k"]

    def test_keeps_review_status(self):
        """`review` means addressed-but-unconfirmed, so it still forwards."""
        assert [c.id for c in filter_comments_for_forward([_c("rev", status="review")])] == ["rev"]

    def test_reply_inherits_root_status(self):
        root = _c("root", status="resolved")
        reply = _c("reply", parent_id="root")
        assert filter_comments_for_forward([root, reply]) == []

    def test_keeps_reply_when_root_open(self):
        root = _c("root")
        reply = _c("reply", parent_id="root")
        assert len(filter_comments_for_forward([root, reply])) == 2

    def test_old_anchor_version_still_forwards(self):
        """An older anchor is NOT staleness.

        The span a v1 comment points at usually still exists at v5, so the
        comment is live feedback nobody has addressed. Dropping it here would
        stop forwarding a thread the sidebar still shows as open — silent loss
        of intent. ``anchor_orphaned`` covers the genuinely-stale case.
        """
        comments = [_c("old", anchor_version=1), _c("cur", anchor_version=5)]
        assert [c.id for c in filter_comments_for_forward(comments)] == ["old", "cur"]

    def test_old_anchor_version_still_dropped_when_resolved(self):
        comments = [_c("old", status="resolved", anchor_version=1), _c("cur", anchor_version=5)]
        assert [c.id for c in filter_comments_for_forward(comments)] == ["cur"]

    def test_disabled_returns_all(self):
        comments = [_c("r", status="resolved"), _c("o")]
        assert len(filter_comments_for_forward(comments, exclude_resolved=False)) == 2

    def test_parent_cycle_terminates(self):
        a = _c("a", parent_id="b")
        b = _c("b", parent_id="a")
        assert len(filter_comments_for_forward([a, b])) == 2

    def test_missing_parent_treated_as_root(self):
        orphan = _c("orphan", parent_id="gone")
        assert [c.id for c in filter_comments_for_forward([orphan])] == ["orphan"]

    def test_empty_input(self):
        assert filter_comments_for_forward([]) == []


# ── Handler integration: query-param filtering ───────────────────────────────


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Test", content="body", slug="test", kind="markdown")
    s.update("test", content="body v2", snapshot=True)
    monkeypatch.setattr(art_mod, "_default_store", s)
    return s


@pytest.fixture(autouse=True)
def not_restricted(monkeypatch):
    monkeypatch.setattr(
        h, "_is_restricted_session", lambda state, request: False
    )


def _req(slug: str, query: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.headers = {"X-Session-Key": "dashboard:test"}
    r.match_info = {"slug": slug}
    r.query = query or {}
    r.read = AsyncMock(return_value=b"")
    r.app = {"state": MagicMock()}
    return r


def _j(resp) -> dict:
    return json.loads(resp.body)


class TestHandlerFiltering:
    @pytest.mark.asyncio
    async def test_default_no_params_returns_all(self, store):
        """Without the param the full unfiltered list is returned."""
        store.add_comment("test", ArtifactComment(
            id="c1", status="resolved", thread_id="c1",
        ))
        store.add_comment("test", ArtifactComment(
            id="c2", status="open", anchor_version=1, thread_id="c2",
        ))
        resp = await h.api_artifact_comments(_req("test"))
        body = _j(resp)
        assert resp.status == 200
        assert len(body["comments"]) == 2

    @pytest.mark.asyncio
    async def test_exclude_resolved_filters(self, store):
        store.add_comment("test", ArtifactComment(
            id="c1", status="resolved", thread_id="c1",
        ))
        store.add_comment("test", ArtifactComment(
            id="c2", status="open", thread_id="c2",
        ))
        resp = await h.api_artifact_comments(_req("test", {"exclude_resolved": "true"}))
        body = _j(resp)
        assert [c["id"] for c in body["comments"]] == ["c2"]

    @pytest.mark.asyncio
    async def test_exclude_resolved_keeps_old_anchor_versions(self, store):
        """The artifact is at v2; an open v1-anchored comment still comes back."""
        store.add_comment("test", ArtifactComment(
            id="old", status="open", anchor_version=1, thread_id="old",
        ))
        store.add_comment("test", ArtifactComment(
            id="cur", status="open", anchor_version=2, thread_id="cur",
        ))
        resp = await h.api_artifact_comments(_req("test", {"exclude_resolved": "true"}))
        ids = [c["id"] for c in _j(resp)["comments"]]
        assert ids == ["old", "cur"]
