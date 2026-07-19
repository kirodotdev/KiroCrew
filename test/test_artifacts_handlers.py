"""Tests for :mod:`kiro_crew.dashboard.handlers.artifacts`.

Uses MagicMock requests (matching the test_dashboard_cron_channel.py pattern)
plus a real :class:`ArtifactStore` rooted at a tmp dir for end-to-end coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    _MAX_BODY_BYTES,
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_materialize,
    api_artifact_relocate,
    api_artifact_session_docs,
    api_artifact_set_pinned,
    api_artifact_update,
    api_artifact_version_detail,
    api_artifact_versions,
    api_artifacts_create,
    api_artifacts_list,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """Replace the module-level default store with one rooted at tmp_path."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    session_key: str = "dashboard:test",
    restricted: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock aiohttp Request with the right shape for our handlers."""
    req = MagicMock()
    headers = {"X-Session-Key": session_key}
    if extra_headers:
        headers.update(extra_headers)
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    if isinstance(body, dict):
        encoded = json.dumps(body).encode()
        req.read = AsyncMock(return_value=encoded)
    elif isinstance(body, bytes):
        req.read = AsyncMock(return_value=body)
    else:
        req.read = AsyncMock(return_value=b"")
    # Attach the restricted-session flag via a stub on the request app.
    # Provide a non-None state so handlers don't short-circuit on the
    # state-is-None deny-by-default guard.
    req.app = {"state": MagicMock(), "_restricted_session": restricted}
    return req


@pytest.fixture
def patch_restricted(monkeypatch):
    """Make _is_restricted_session read req.app['_restricted_session']."""
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _json_body(resp) -> dict:
    return json.loads(resp.body)


# ── List ────────────────────────────────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_empty(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifacts_list(_request())
        assert resp.status == 200
        assert _json_body(resp) == {"artifacts": []}

    @pytest.mark.asyncio
    async def test_returns_items(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="aa")
        isolated_store.create(name="b", content="bb", tags=["x"])
        resp = await api_artifacts_list(_request())
        body = _json_body(resp)
        assert len(body["artifacts"]) == 2
        # Newest first.
        assert body["artifacts"][0]["slug"] == "b"
        # Content is not included on list responses.
        assert "content" not in body["artifacts"][0]

    @pytest.mark.asyncio
    async def test_no_snippet_by_default(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="<p>hello world</p>")
        resp = await api_artifacts_list(_request())
        assert "snippet" not in _json_body(resp)["artifacts"][0]

    @pytest.mark.asyncio
    async def test_snippet_when_requested(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="<p>hello   <b>world</b></p>")
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        art = _json_body(resp)["artifacts"][0]
        # Tags stripped, whitespace collapsed.
        assert art["snippet"] == "hello world"

    @pytest.mark.asyncio
    async def test_snippet_truncated_and_bounded(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="word " * 200)
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        snippet = _json_body(resp)["artifacts"][0]["snippet"]
        assert 0 < len(snippet) <= 160

    @pytest.mark.asyncio
    async def test_snippet_strips_markdown(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(
            name="Doc",
            kind="markdown",
            content="# Title\n\n**Bold** and _italic_ and `code` and [link](http://x)\n- item",
        )
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        snip = _json_body(resp)["artifacts"][0]["snippet"]
        assert not any(ch in snip for ch in "#*_`")
        for word in ("Title", "Bold", "italic", "code", "link", "item"):
            assert word in snip

    @pytest.mark.asyncio
    async def test_content_match_finds_by_content(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Alpha", content="the quick brown fox")
        isolated_store.create(name="Beta", content="nothing here")
        resp = await api_artifacts_list(_request(query={"q": "brown", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_content_snippet_is_match_centered(
        self, isolated_store, patch_restricted
    ) -> None:
        content = "line one\nline two\nHERE is the MATCH keyword\nline four\nline five\nline six"
        isolated_store.create(name="Doc", content=content, kind="markdown")
        resp = await api_artifacts_list(
            _request(query={"q": "match", "content": "1", "snippet": "1"})
        )
        snip = _json_body(resp)["artifacts"][0]["snippet"]
        lines = snip.split("\n")
        assert len(lines) <= 5
        assert "match" in snip.lower()  # matched term present (frontend highlights it)
        assert "line two" in snip and "line four" in snip  # a line before and after

    @pytest.mark.asyncio
    async def test_content_match_finds_by_tag(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Alpha", content="x", tags=["ops"])
        isolated_store.create(name="Beta", content="x")
        resp = await api_artifacts_list(_request(query={"q": "ops", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_content_match_finds_by_description(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="Alpha", content="x", description="review dashboard")
        isolated_store.create(name="Beta", content="x")
        resp = await api_artifacts_list(_request(query={"q": "dashboard", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_q_without_content_flag_is_name_only(
        self, isolated_store, patch_restricted
    ) -> None:
        # A content hit must NOT match unless ?content=1 is set.
        isolated_store.create(name="Alpha", content="the quick brown fox")
        resp = await api_artifacts_list(_request(query={"q": "brown"}))
        assert _json_body(resp)["artifacts"] == []

    @pytest.mark.asyncio
    async def test_filter_by_tag(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="x")
        isolated_store.create(name="b", content="x", tags=["op"])
        resp = await api_artifacts_list(_request(query={"tag": "op"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"b"}

    @pytest.mark.asyncio
    async def test_filter_by_kind(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="x", kind="widget")
        isolated_store.create(name="b", content="x", kind="markdown")
        resp = await api_artifacts_list(_request(query={"kind": "markdown"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"b"}

    @pytest.mark.asyncio
    async def test_filter_by_q(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="CR queue", content="x")
        isolated_store.create(name="Tickets", content="x")
        resp = await api_artifacts_list(_request(query={"q": "queue"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"cr-queue"}


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_artifact(self, isolated_store, patch_restricted) -> None:
        body = {"name": "Hello", "content": "<p>hello</p>", "tags": ["greeting"]}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        result = _json_body(resp)
        assert result["slug"] == "hello"
        assert result["version"] == 1
        assert result["content"] == "<p>hello</p>"
        # Persisted on disk.
        assert (isolated_store.root / "hello" / "current.html").exists()

    @pytest.mark.asyncio
    async def test_validation_error_returns_400(self, isolated_store, patch_restricted) -> None:
        body = {"name": "", "content": "x"}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 400
        assert "name" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_duplicate_slug_returns_409(self, isolated_store, patch_restricted) -> None:
        body = {"name": "x", "content": "a", "slug": "taken"}
        await api_artifacts_create(_request(body=body))
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        body = {"name": "x", "content": "a"}
        resp = await api_artifacts_create(_request(body=body, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_json(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifacts_create(_request(body=b"{not json"))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body(self, isolated_store, patch_restricted) -> None:
        big = b"x" * (_MAX_BODY_BYTES + 1)
        resp = await api_artifacts_create(_request(body=big))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: store.create() raising the base ArtifactError (e.g. a
        # sensitive-path refusal from _write_text() that fires after the
        # duplicate-slug check passes) used to be caught by the same except
        # branch as ArtifactAlreadyExistsError, returning a misleading 409. Now
        # the two are distinguished — duplicates are 409, all other store
        # errors are 500.
        from kiro_crew.artifacts import ArtifactError

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to write sensitive path: ~/.aws/credentials")

        monkeypatch.setattr(isolated_store, "create", _boom)
        body = {"name": "x", "content": "a"}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]

    # ── source_path auto-dedup (Mesh-1654 Phase 6) ──────────────────────
    @pytest.mark.asyncio
    async def test_first_save_with_source_path_creates_201(
        self, isolated_store, patch_restricted
    ) -> None:
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": "/p/brd.md",
        }
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        result = _json_body(resp)
        assert result["version"] == 1

    @pytest.mark.asyncio
    async def test_resave_with_same_source_path_silently_bumps_to_200(
        self, isolated_store, patch_restricted
    ) -> None:
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": "/p/brd.md",
        }
        first = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        slug = _json_body(first)["slug"]
        # Same path, different content. Backend should detect the dedup
        # and bump the existing artifact's version rather than creating a
        # parallel duplicate.
        body2 = {**body, "content": "# v2 updated"}
        second = await api_artifacts_create(_request(body=body2))
        assert second.status == 200  # 200 = bumped, 201 = created new
        result = _json_body(second)
        assert result["slug"] == slug
        assert result["version"] == 2
        assert result["content"] == "# v2 updated"

    @pytest.mark.asyncio
    async def test_resave_with_different_source_path_creates_new(
        self, isolated_store, patch_restricted
    ) -> None:
        body1 = {
            "name": "a",
            "content": "x",
            "kind": "markdown",
            "source": "manual",
            "source_path": "/p/a.md",
        }
        await api_artifacts_create(_request(body=body1))
        body2 = {
            "name": "b",
            "content": "y",
            "kind": "markdown",
            "source": "manual",
            "source_path": "/p/b.md",
        }
        resp = await api_artifacts_create(_request(body=body2))
        assert resp.status == 201  # different path → new artifact
        assert _json_body(resp)["version"] == 1

    @pytest.mark.asyncio
    async def test_save_without_source_path_never_dedups(
        self, isolated_store, patch_restricted
    ) -> None:
        # Chat-backed widgets are saved without source_path. Two saves of
        # the same widget content must produce two separate artifacts —
        # NOT silently merge into one — because a chat-backed artifact's
        # identity is its slug, not its source. Regression guard for the
        # bug nrb hit where a markdown file's "Add to artifacts" was
        # matching a previously-saved widget because the lookup degraded
        # to "first artifact in list".
        body = {"name": "widget", "content": "<p>hi</p>", "kind": "widget", "source": "chat"}
        first = await api_artifacts_create(_request(body=body))
        second = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        assert second.status == 201  # both genuine creates
        assert _json_body(first)["slug"] != _json_body(second)["slug"]

    @pytest.mark.asyncio
    async def test_mcp_dedup_resave_tags_event_as_agent(
        self, isolated_store, patch_restricted
    ) -> None:
        # AutoSDE round 12: the dedup path used to hardcode actor='user' so
        # MCP-driven re-saves silently appeared on the activity timeline as
        # 'edited by user' instead of 'iterated by agent'. Now the handler
        # infers actor from X-Internal-Secret like api_artifact_update.
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": "/p/brd.md",
        }
        first = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        body2 = {**body, "content": "# v2 from agent"}
        second = await api_artifacts_create(
            _request(
                body=body2,
                extra_headers={"X-Internal-Secret": "fake"},
            )
        )
        assert second.status == 200
        slug = _json_body(second)["slug"]
        # Latest event should be 'iterated' (agent), not 'edited' (user).
        art = isolated_store.get(slug)
        latest_event = art.events[-1]
        assert latest_event["type"] == "iterated"
        assert latest_event["by"] == "agent"


# ── Detail / Update / Delete ────────────────────────────────────────────────


class TestDetail:
    @pytest.mark.asyncio
    async def test_returns_content(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(_request(match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["content"] == "<x/>"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_detail(_request(match={"slug": "nope"}))
        assert resp.status == 404


class TestUpdate:
    @pytest.mark.asyncio
    async def test_content_change_with_snapshot_bumps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2", "snapshot": True}, match={"slug": "x"})
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2
        assert body["content"] == "v2"

    @pytest.mark.asyncio
    async def test_dashboard_save_without_snapshot_keeps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        # New behavior (Mesh-1654 round 5, explicit-snapshot model): a
        # dashboard PATCH with no snapshot flag updates the live state but
        # does NOT bump version. Versioning becomes deliberate.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 1  # version unchanged
        assert body["content"] == "v2"  # live state updated

    @pytest.mark.asyncio
    async def test_mcp_update_auto_snapshots(self, isolated_store, patch_restricted) -> None:
        # MCP-originated requests (X-Internal-Secret header) auto-snapshot
        # because each agent iteration is a meaningful state change worth
        # versioning.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(
                body={"content": "v2"},
                match={"slug": "x"},
                extra_headers={"X-Internal-Secret": "fake-secret"},
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2  # MCP call → auto-snapshot

    @pytest.mark.asyncio
    async def test_metadata_change_no_version_bump(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"description": "updated"}, match={"slug": "x"})
        )
        body = _json_body(resp)
        assert body["version"] == 1
        assert body["description"] == "updated"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_update(_request(body={"content": "x"}, match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2"}, match={"slug": "x"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_field_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        # over-long description
        resp = await api_artifact_update(
            _request(body={"description": "z" * 5_000}, match={"slug": "x"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: store.update() raising the base ArtifactError (e.g. a
        # sensitive-path refusal from _write_text) used to escape the handler
        # and surface as an unhandled 500 with no audit trail. Now caught
        # explicitly and audited as an error.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="v1", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to write sensitive path: ~/.aws/credentials")

        monkeypatch.setattr(isolated_store, "update", _boom)
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"ok": True}
        assert not (isolated_store.root / "x").exists()

    @pytest.mark.asyncio
    async def test_missing_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_delete(_request(match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: a base ArtifactError raised by store.delete() (e.g. a
        # future store-level sensitive-path or filesystem refusal) used to
        # escape the handler and 500 silently. Now caught and audited.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="a", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to remove sensitive path: ~/...")

        monkeypatch.setattr(isolated_store, "delete", _boom)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


# ── Versions ────────────────────────────────────────────────────────────────


class TestVersions:
    @pytest.mark.asyncio
    async def test_lists_versions(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2", snapshot=True)
        resp = await api_artifact_versions(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"slug": "x", "versions": [1, 2]}

    @pytest.mark.asyncio
    async def test_specific_version(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "1"}))
        body = _json_body(resp)
        assert body["content"] == "v1"

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "abc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_out_of_range_404(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "99"}))
        assert resp.status == 404


# ── Record events (referenced) ─────────────────────────────────────────────


class TestRecordEvent:
    """Tests for ``POST /api/artifacts/<slug>/events``.

    This endpoint exists so ``WidgetFrame`` can log a ``referenced`` event
    each time a chat impression of a saved artifact mounts (Mesh-1715
    follow-up). It deliberately rejects content-mutating event types —
    those have to come through create/update so version-bump bookkeeping
    stays coupled to actual content changes.
    """

    @pytest.mark.asyncio
    async def test_referenced_event_recorded_with_metadata(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={
                    "type": "referenced",
                    "metadata": {
                        "message_ts": "1779995123.456789",
                        "widget_index": 0,
                    },
                },
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["slug"] == "x"
        assert body["event"]["type"] == "referenced"
        assert body["event"]["by"] == "user"  # X-Internal-Secret absent
        assert body["event"]["metadata"]["message_ts"] == "1779995123.456789"
        assert body["event"]["metadata"]["widget_index"] == 0
        # Verify the event is persisted in the artifact's event log.
        art = isolated_store.get("x")
        ref_events = [e for e in art.events if e.get("type") == "referenced"]
        assert len(ref_events) == 1

    @pytest.mark.asyncio
    async def test_mcp_actor_inferred_from_internal_secret(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                extra_headers={"X-Internal-Secret": "anything-non-empty"},
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_dashboard_ui_session_dropped(self, isolated_store, patch_restricted) -> None:
        # Browser dashboard sends X-Session-Key="dashboard:ui" as a default
        # for non-chat-scoped requests. That literal isn't a real slot key
        # and would mislead the activity timeline if recorded — the handler
        # explicitly drops it. Same rule as the create/update endpoints.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="dashboard:ui",
            )
        )
        assert resp.status == 200
        # No session_id key in the event.
        assert "session_id" not in _json_body(resp)["event"]

    @pytest.mark.asyncio
    async def test_real_session_key_recorded(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-3-1779995123",
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["session_id"] == "chat-3-1779995123"

    @pytest.mark.asyncio
    async def test_rejects_content_mutating_event_types(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        for bad_type in ("created", "edited", "iterated", "reverted"):
            resp = await api_artifact_record_event(
                _request(
                    match={"slug": "x"},
                    body={"type": bad_type},
                )
            )
            assert resp.status == 400, f"expected 400 for type={bad_type!r}, got {resp.status}"

    @pytest.mark.asyncio
    async def test_unknown_slug_returns_404(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        resp = await api_artifact_record_event(
            _request(
                match={"slug": "does-not-exist"},
                body={"type": "referenced"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_forbidden(self, isolated_store, patch_restricted) -> None:
        # Appending events mutates meta.json, so a restricted session must
        # be rejected with 403 like the other mutation endpoints — it must
        # not be able to flood an artifact's event log.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                restricted=True,
            )
        )
        assert resp.status == 403
        # No event recorded.
        art = isolated_store.get("x")
        assert [e for e in art.events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_suppressed_response_when_session_has_cud(
        self, isolated_store, patch_restricted
    ) -> None:
        # When the session already has a CUD event, the impression is
        # suppressed: the handler must return suppressed:true with a null
        # event, NOT a stale prior event echoed as if it were recorded.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        isolated_store.update(
            "x",
            content="<div>v2</div>",
            session_id="chat-9-1779995123",
            actor="agent",
            snapshot=True,
        )
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-9-1779995123",
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["suppressed"] is True
        assert body["event"] is None
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_same_impression_in_session_recorded_once(
        self, isolated_store, patch_restricted
    ) -> None:
        # The reported Mesh-1715 bug: reloading / revisiting a tab clears
        # the frontend sessionStorage debounce, so the same chat session
        # re-POSTs a `referenced` for the same impression. The store must
        # record it once — the second POST returns suppressed:true.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        body = {"type": "referenced", "metadata": {"message_ts": "1780036091.1", "widget_index": 0}}
        first = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        second = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        assert _json_body(first)["event"]["type"] == "referenced"
        assert _json_body(second)["suppressed"] is True
        ref = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(ref) == 1

    @pytest.mark.asyncio
    async def test_does_not_bump_version_or_change_content(
        self, isolated_store, patch_restricted
    ) -> None:
        # A referenced event is pure observability — it must not touch
        # the artifact's content or version. Regression guard so a
        # future refactor that accidentally routes referenced events
        # through update() (which DOES bump on content change) doesn't
        # silently turn impression-logging into a version-churn engine.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        art = isolated_store.create(name="X", content="<div>orig</div>", slug="x", kind="widget")
        original_version = art.version
        original_content = isolated_store.get("x").content

        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-a-1")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-b-2")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-c-3")
        )

        post = isolated_store.get("x")
        assert post.version == original_version
        assert post.content == original_content
        ref_events = [e for e in post.events if e.get("type") == "referenced"]
        assert len(ref_events) == 3


# ── Denial audit (SEL) for new pin / materialize / session-doc routes ─────────


class TestDenialAudit:
    """Every denial/error exit on the new routes must emit a SEL audit event."""

    def _capture_sel(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        return sel_stub

    @pytest.mark.asyncio
    async def test_materialize_non_string_path_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": 123}))
        assert resp.status == 400
        sel_stub.log_tool_invocation.assert_called_once()
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_materialize"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_materialize_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": "/x.md"}, restricted=True))
        assert resp.status == 403
        assert sel_stub.log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_set_pinned_non_bool_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_set_pinned(_request(match={"slug": "x"}, body={"pinned": "yes"}))
        assert resp.status == 400
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_set_pinned"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_session_docs_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_session_docs(_request(restricted=True))
        assert resp.status == 403
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_session_docs"
        assert kwargs["outcome"] == "denied"


# ── Relocate (fixed-root containment) ─────────────────────────────────────────


class TestRelocate:
    """api_artifact_relocate confines source_path to $HOME (+ configured roots),
    so an agent cannot aim an artifact at /etc/passwd or another user's files
    and exfiltrate them via a later GET (PR #14 nrb + CodeQL py/path-injection)."""

    @pytest.mark.asyncio
    async def test_home_file_allowed(self, isolated_store, patch_restricted, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        target = home / "notes.md"
        target.write_text("# hi")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)
        assert isolated_store.get("doc").source_path == str(target.resolve())

    @pytest.mark.asyncio
    async def test_outside_home_denied(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        # A file OUTSIDE home (sibling tmp dir) must be refused with 403.
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("secret")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(outside)})
        )
        assert resp.status == 403
        assert "home" in _json_body(resp)["error"].lower()

    @pytest.mark.asyncio
    async def test_configured_extra_root_allowed(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        extra = tmp_path / "shared"
        extra.mkdir()
        target = extra / "doc.md"
        target.write_text("# shared")
        # Configure the extra root via publish.relocate_roots.
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

        cfg = KiroCrewConfig()
        cfg.publish = PublishConfig(relocate_roots=[str(extra)])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)

    @pytest.mark.asyncio
    async def test_traversal_denied(self, isolated_store, patch_restricted, monkeypatch):
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": "../../etc/passwd"})
        )
        assert resp.status == 403
        assert "traversal" in _json_body(resp)["error"].lower()
