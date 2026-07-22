"""Tests for the LOCAL artifact comment HTTP handlers.

Covers post / reply / mark-review / resolve / reopen / delete and the GET list
for artifacts WITHOUT a publication (so the provider fetch-on-view path is
skipped and the tests stay hermetic — provider sync is covered separately in
test_remote_artifact_comments.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as h


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Doc", content="# Hello\n\nsome body text", slug="doc", kind="markdown")
    monkeypatch.setattr(art_mod, "_default_store", s)
    return s


@pytest.fixture(autouse=True)
def not_restricted(monkeypatch):
    monkeypatch.setattr(
        h,
        "_is_restricted_session",
        lambda state, request: bool(getattr(request, "_restricted", False)),
    )


def _req(
    *, body: dict | None = None, match: dict | None = None, restricted: bool = False
) -> MagicMock:
    r = MagicMock()
    r.headers = {"X-Session-Key": "dashboard:test"}
    r.match_info = match or {}
    r.query = {}
    r._restricted = restricted
    r.read = AsyncMock(return_value=json.dumps(body).encode() if isinstance(body, dict) else b"")
    r.app = {"state": MagicMock()}
    return r


def _j(resp) -> dict:
    return json.loads(resp.body)


async def _post(slug: str, **body) -> dict:
    resp = await h.api_artifact_post_comment(_req(body=body, match={"slug": slug}))
    assert resp.status == 201, _j(resp)
    return _j(resp)["comment"]


class TestPostComment:
    @pytest.mark.asyncio
    async def test_restricted_session_403(self, store):
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "hi"}, match={"slug": "doc"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_empty_text_400(self, store):
        resp = await h.api_artifact_post_comment(_req(body={"text": "   "}, match={"slug": "doc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_artifact_404(self, store):
        resp = await h.api_artifact_post_comment(_req(body={"text": "hi"}, match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_post_anchored_persists(self, store):
        cmt = await _post(
            "doc",
            text="needs work",
            scope="private",
            anchor={"quote": "some body text", "start_offset": 9, "end_offset": 23},
        )
        stored = store.list_comments("doc")
        assert len(stored) == 1
        assert stored[0].id == cmt["id"]
        assert stored[0].body == "needs work"
        assert stored[0].anchor_quote == "some body text"
        assert stored[0].author  # defaulted to the dashboard user alias


class TestReplyComment:
    @pytest.mark.asyncio
    async def test_reply_persists_thread(self, store):
        root = await _post("doc", text="root")
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "a reply"}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 201
        stored = store.list_comments("doc")
        reply = next(c for c in stored if c.parent_id == root["id"])
        assert reply.body == "a reply"
        assert reply.thread_id == root["id"]

    @pytest.mark.asyncio
    async def test_reply_parent_not_found_404(self, store):
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "missing"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reply_empty_text_400(self, store):
        root = await _post("doc", text="root")
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": ""}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 400


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_mark_review(self, store):
        root = await _post("doc", text="root")
        resp = await h.api_artifact_mark_review(
            _req(match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200
        assert _j(resp)["status"] == "review"
        assert store.list_comments("doc")[0].status == "review"

    @pytest.mark.asyncio
    async def test_resolve_then_reopen(self, store):
        root = await _post("doc", text="root")
        rresp = await h.api_artifact_resolve_comment(
            _req(body={}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert rresp.status == 200
        assert store.list_comments("doc")[0].status == "resolved"
        oresp = await h.api_artifact_reopen_comment(
            _req(match={"slug": "doc", "comment_id": root["id"]})
        )
        assert oresp.status == 200
        assert store.list_comments("doc")[0].status == "open"

    @pytest.mark.asyncio
    async def test_agent_cannot_resolve_403(self, store):
        root = await _post("doc", text="root")
        resp = await h.api_artifact_resolve_comment(
            _req(body={"is_agent": True}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 403
        # status unchanged
        assert store.list_comments("doc")[0].status == "open"

    @pytest.mark.asyncio
    async def test_resolve_missing_404(self, store):
        resp = await h.api_artifact_resolve_comment(
            _req(body={}, match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reopen_missing_404(self, store):
        resp = await h.api_artifact_reopen_comment(
            _req(match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404


class TestDeleteAndList:
    @pytest.mark.asyncio
    async def test_delete_removes(self, store):
        root = await _post("doc", text="bye")
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200
        assert _j(resp)["deleted"] is True
        assert store.list_comments("doc") == []

    @pytest.mark.asyncio
    async def test_delete_missing_404(self, store):
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_lists_comments_no_publication(self, store):
        await _post(
            "doc", text="first", anchor={"quote": "Hello", "start_offset": 2, "end_offset": 7}
        )
        await _post("doc", text="second")
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}))
        body = _j(resp)
        assert resp.status == 200
        assert body["remote_sync_error"] is None
        assert len(body["comments"]) == 2
        assert any(c.get("anchor") for c in body["comments"])

    @pytest.mark.asyncio
    async def test_get_anchor_includes_prefix_suffix(self, store):
        # Regression: the local GET serializer must echo prefix/suffix so the
        # markdown overlay can disambiguate a repeated quote — rangeForAnchor
        # matches on quote+prefix+suffix and ignores the offsets. Without these
        # a comment on a later occurrence highlights the first match. Must match
        # the remote serializer (_serialize_remote_comment).
        await _post(
            "doc",
            text="anchored",
            anchor={
                "quote": "body",
                "prefix": "some ",
                "suffix": " text",
                "start_offset": 14,
                "end_offset": 18,
            },
        )
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}))
        anchored = next(c for c in _j(resp)["comments"] if c.get("anchor"))
        assert anchored["anchor"]["quote"] == "body"
        assert anchored["anchor"]["prefix"] == "some "
        assert anchored["anchor"]["suffix"] == " text"

    @pytest.mark.asyncio
    async def test_get_unknown_artifact_404(self, store):
        resp = await h.api_artifact_comments(_req(match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_restricted_session_403(self, store):
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}, restricted=True))
        assert resp.status == 403


class TestEditComment:
    @pytest.mark.asyncio
    async def test_edit_updates_body_local(self, store):
        root = await _post("doc", text="original")
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "edited body"}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200, _j(resp)
        # No publication → the edit stays local, never touches a provider.
        assert _j(resp)["comment"]["remote_synced"] is False
        stored = store.list_comments("doc")[0]
        assert stored.body == "edited body"

    @pytest.mark.asyncio
    async def test_edit_stores_agent_body_verbatim_no_emoji(self, store):
        # Agent provenance is the structured is_agent flag, not a body prefix —
        # an edit stores the new body verbatim, never stamping an emoji into it
        # (the dashboard renders a lucide Bot icon from is_agent per CLAUDE.md).
        root = await _post("doc", text="original", is_agent=True)
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "corrected"}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200, _j(resp)
        stored = store.list_comments("doc")[0]
        assert stored.body == "corrected"
        assert stored.is_agent is True
        assert "\U0001f916" not in stored.body

    @pytest.mark.asyncio
    async def test_edit_empty_text_400(self, store):
        root = await _post("doc", text="original")
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "   "}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_edit_missing_404(self, store):
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_edit_restricted_session_403(self, store):
        root = await _post("doc", text="original")
        resp = await h.api_artifact_edit_comment(
            _req(
                body={"text": "x"}, match={"slug": "doc", "comment_id": root["id"]}, restricted=True
            )
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_edit_pushes_in_place_for_chorus_origin(self, store, monkeypatch):
        from kiro_crew.artifacts import ArtifactComment
        from kiro_crew.publish_provider import Capability

        # A comment that was shared to Chorus — origin carries the remote id.
        store.add_comment(
            "doc",
            ArtifactComment(
                id="cx",
                origin="chorus:5",
                provider="chorus",
                scope="shared",
                author="nrb",
                body="orig",
                thread_id="cx",
                target_provider="chorus",
                target_external_id="D1",
            ),
        )
        edit = AsyncMock()
        prov = MagicMock()
        prov.capabilities.return_value = {Capability.COMMENTS_EDIT}
        prov.edit_comment = edit
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.get_provider", lambda name: prov
        )

        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "fixed"}, match={"slug": "doc", "comment_id": "cx"})
        )
        assert resp.status == 200, _j(resp)
        assert _j(resp)["comment"]["remote_synced"] is True
        edit.assert_awaited_once()
        assert edit.await_args.kwargs["remote_id"] == "5"
        assert edit.await_args.kwargs["body"] == "fixed"
        assert next(c for c in store.list_comments("doc") if c.id == "cx").body == "fixed"

    @pytest.mark.asyncio
    async def test_edit_stays_local_when_provider_lacks_edit_capability(self, store, monkeypatch):
        from kiro_crew.artifacts import ArtifactComment

        # Artifactory-origin comment: provider has no COMMENTS_EDIT → local-only.
        store.add_comment(
            "doc",
            ArtifactComment(
                id="ca",
                origin="artifactory:9",
                provider="artifactory",
                scope="shared",
                author="nrb",
                body="orig",
                thread_id="ca",
                target_provider="artifactory",
                target_external_id="X9",
            ),
        )
        prov = MagicMock()
        prov.capabilities.return_value = set()  # no COMMENTS_EDIT
        prov.edit_comment = AsyncMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.get_provider", lambda name: prov
        )

        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "fixed"}, match={"slug": "doc", "comment_id": "ca"})
        )
        assert resp.status == 200, _j(resp)
        assert _j(resp)["comment"]["remote_synced"] is False
        prov.edit_comment.assert_not_awaited()
        assert next(c for c in store.list_comments("doc") if c.id == "ca").body == "fixed"

    @pytest.mark.asyncio
    async def test_edit_gated_by_publish_governance(self, store, monkeypatch):
        # Regression (PR #14 nrb): a shared-comment edit is outbound egress and
        # must pass the same capabilities.publish gate as artifact publish. A
        # policy that DENIES the destination keeps the edit LOCAL (remote_synced
        # False) even though the provider supports COMMENTS_EDIT.
        import dataclasses

        from kiro_crew.artifacts import ArtifactComment
        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy
        from kiro_crew.publish_provider import Capability

        store.add_comment(
            "doc",
            ArtifactComment(
                id="cg",
                origin="chorus:5",
                provider="chorus",
                scope="shared",
                author="nrb",
                body="orig",
                thread_id="cg",
                target_provider="chorus",
                target_external_id="D1",
            ),
        )
        prov = MagicMock()
        prov.capabilities.return_value = {Capability.COMMENTS_EDIT}
        prov.edit_comment = AsyncMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.get_provider", lambda name: prov
        )
        # Ceiling: publish enabled, but destinations allow only "artifactory".
        from kiro_crew.config.loader import KiroCrewConfig

        base = build_default_context(KiroCrewConfig.load())
        ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "publish": {
                        "enabled": True,
                        "scopes": {"destinations": {"mode": "allow", "allow": ["artifactory"]}},
                    }
                },
            }
        )
        ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))
        try:
            resp = await h.api_artifact_edit_comment(
                _req(body={"text": "fixed"}, match={"slug": "doc", "comment_id": "cg"})
            )
            assert resp.status == 200, _j(resp)
            # Denied destination → edit stays local, provider never called.
            assert _j(resp)["comment"]["remote_synced"] is False
            prov.edit_comment.assert_not_awaited()
            # Local store still updated (the edit isn't lost, just not pushed).
            assert next(c for c in store.list_comments("doc") if c.id == "cg").body == "fixed"
        finally:
            ctx_mod.reset_context()


def _agent_req(*, body: dict | None = None, match: dict | None = None) -> MagicMock:
    """A request whose actor infers as agent: MCP calls carry X-Internal-Secret
    (validated by upstream middleware) — the handlers key off the header, never
    a spoofable body flag."""
    r = _req(body=body, match=match)
    r.headers = {"X-Session-Key": "slot-abc", "X-Internal-Secret": "s3cret"}
    return r


class TestAgentDeleteComment:
    """Agent disposition contract on DELETE: header-inferred actor,
    required reason, provider-origin refusal, and the activity-feed trail."""

    @pytest.mark.asyncio
    async def test_agent_delete_local_with_reason_succeeds(self, store):
        root = await _post("doc", text="delete this line")
        resp = await h.api_artifact_delete_comment(
            _agent_req(
                body={"reason": "applied in v2: line removed"},
                match={"slug": "doc", "comment_id": root["id"]},
            )
        )
        assert resp.status == 200, _j(resp)
        assert store.list_comments("doc") == []
        # Activity feed carries the trail: action, snippet, reason, agent actor.
        ev = store.get("doc").events[-1]
        assert ev["type"] == "comment"
        assert ev["by"] == "agent"
        assert ev["session_id"] == "slot-abc"
        assert ev["metadata"]["action"] == "deleted"
        assert "delete this line" in ev["metadata"]["comment_snippet"]
        assert ev["metadata"]["reason"] == "applied in v2: line removed"

    @pytest.mark.asyncio
    async def test_agent_delete_without_reason_400(self, store):
        root = await _post("doc", text="keep me honest")
        resp = await h.api_artifact_delete_comment(
            _agent_req(body={}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 400
        assert len(store.list_comments("doc")) == 1  # untouched

    @pytest.mark.asyncio
    async def test_agent_delete_provider_origin_403(self, store):
        from kiro_crew.artifacts import ArtifactComment

        store.add_comment(
            "doc",
            ArtifactComment(
                id="pr1",
                origin="artifactory:7",
                provider="artifactory",
                scope="shared",
                author="alice",
                body="provider comment",
                thread_id="pr1",
                target_provider="artifactory",
                target_external_id="X7",
            ),
        )
        resp = await h.api_artifact_delete_comment(
            _agent_req(
                body={"reason": "applied"},
                match={"slug": "doc", "comment_id": "pr1"},
            )
        )
        assert resp.status == 403
        assert "mark_review" in _j(resp)["error"]
        assert len(store.list_comments("doc")) == 1  # untouched

    @pytest.mark.asyncio
    async def test_human_delete_unchanged_no_reason_required(self, store):
        root = await _post("doc", text="human cleanup")
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200
        assert store.list_comments("doc") == []
        ev = store.get("doc").events[-1]
        assert ev["metadata"]["action"] == "deleted"
        assert ev["by"] == "user"
        assert "reason" not in ev["metadata"]

    @pytest.mark.asyncio
    async def test_agent_resolve_via_header_403(self, store):
        """Resolve gating is header-based (the body flag alone was spoofable):
        any MCP-originated resolve is refused even without is_agent in the
        body."""
        root = await _post("doc", text="root")
        resp = await h.api_artifact_resolve_comment(
            _agent_req(body={}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 403
        assert store.list_comments("doc")[0].status == "open"


class TestCommentLifecycleEvents:
    @pytest.mark.asyncio
    async def test_mark_review_appends_reviewed_event(self, store):
        root = await _post("doc", text="judgment call here")
        resp = await h.api_artifact_mark_review(
            _agent_req(match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200
        ev = store.get("doc").events[-1]
        assert ev["type"] == "comment"
        assert ev["by"] == "agent"
        assert ev["metadata"]["action"] == "reviewed"
        assert "judgment call" in ev["metadata"]["comment_snippet"]

    @pytest.mark.asyncio
    async def test_resolve_appends_resolved_event(self, store):
        root = await _post("doc", text="all done")
        resp = await h.api_artifact_resolve_comment(
            _req(body={}, match={"slug": "doc", "comment_id": root["id"]})
        )
        assert resp.status == 200
        ev = store.get("doc").events[-1]
        assert ev["type"] == "comment"
        assert ev["by"] == "user"
        assert ev["metadata"]["action"] == "resolved"

    @pytest.mark.asyncio
    async def test_get_serializes_anchor_orphaned(self, store):
        await _post(
            "doc",
            text="anchored",
            anchor={"quote": "some body text", "start_offset": 9, "end_offset": 23},
        )
        # Rewrite content so the anchor no longer matches → rescan orphans it.
        store.update("doc", content="# Hello\n\ncompletely different")
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}))
        cmt = _j(resp)["comments"][0]
        assert cmt["anchor_orphaned"] is True
