"""Tests for the provider-neutral remote-artifact surface (G4).

Covers the provider-routed browse/clone/fork handlers, the upstream sync
trio (pull-latest / upstream-status / overwrite-remote), the governance
gates on the egress-arming routes (overwrite + clone), the inert public
edition behavior (empty registry → 404/503), the ``--slack-only`` scoped-API
allowlist entry, and route registration.

Uses MagicMock requests (test_artifacts_handlers.py pattern) plus a real
:class:`ArtifactStore` at a tmp dir, and a minimal in-test PublishProvider
registered into the (normally empty) public registry.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import ArtifactPublication, ArtifactStore, ForkMetadata
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_overwrite_remote,
    api_artifact_pull_latest,
    api_artifact_update,
    api_artifact_upstream_status,
    api_remote_artifact_comments,
    api_remote_artifact_delete_comment,
    api_remote_artifact_get,
    api_remote_artifact_mark_review,
    api_remote_artifact_post_comment,
    api_remote_artifact_reply_comment,
    api_remote_artifacts_browse,
    api_remote_artifacts_clone,
    api_remote_artifacts_fork,
)
from kiro_crew.publish_provider import Capability, PublishProvider, RemoteComment

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """Real ArtifactStore at tmp_path, wired as the process default for both
    the handler module and the publish_sync engine."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(publish_sync, "get_default_store", lambda: store)
    return store


class BrowseFakeProvider(PublishProvider):
    """Minimal provider exposing the discovery + content-pull surface."""

    name = "fakeprov"
    display_name = "Fake Provider"
    install_hint = "install the fake provider"

    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.listing: dict | None = {
            "artifacts": [
                {
                    "external_id": "ext-1",
                    "title": "Remote One",
                    "owner": "someone",
                    "view_url": "https://remote.example.com/a/ext-1",
                    "updated_at": "2026-07-01T00:00:00Z",
                    "snippet": "",
                }
            ],
            "next_page_token": None,
        }
        self.content: dict | None = {
            "content": "<h1>remote</h1>",
            "content_type": "text/html",
            "title": "Remote One",
            "owner": "someone",
            "visibility": "SHARED",
            "shared_with": [],
            "tags": [],
            "current_version": 3,
            "view_url": "https://remote.example.com/a/ext-1",
            "sha256": "sha-remote",
        }

    def available(self) -> bool:
        return True

    async def ensure_ready(self) -> bool:
        return True

    def view_url_for(self, external_id: str) -> str:
        return f"https://remote.example.com/a/{external_id}"

    async def publish(self, **kwargs):  # pragma: no cover — not exercised
        raise NotImplementedError

    async def push_version(self, **kwargs):  # pragma: no cover — not exercised
        raise NotImplementedError

    async def update_sharing(self, **kwargs):  # pragma: no cover — not exercised
        raise NotImplementedError

    async def unpublish(self, **kwargs):  # pragma: no cover — not exercised
        raise NotImplementedError

    async def list_remote(self, *, scope: str = "mine", page_token: str | None = None):
        self.list_calls.append({"scope": scope, "page_token": page_token})
        return self.listing

    async def search_remote(self, *, query: str, page_token: str | None = None):
        self.search_calls.append({"query": query, "page_token": page_token})
        return self.listing

    async def fetch_content(self, *, external_id: str):
        return self.content

    async def fetch_state(self, *, external_id: str):
        return {"current_version": 3, "sha256": "sha-remote"}


@pytest.fixture
def fake_provider():
    return BrowseFakeProvider()


@pytest.fixture
def registered_provider(fake_provider):
    """Register the fake provider, restoring the (empty) public registry after."""
    saved = dict(publish_provider._FACTORIES)
    publish_provider.reset_providers()
    publish_provider.register_provider(fake_provider.name, lambda: fake_provider)
    publish_provider._INSTANCES[fake_provider.name] = fake_provider
    yield fake_provider
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


@pytest.fixture(autouse=True)
def empty_registry_guard():
    """Snapshot/restore the registry so a failed test can't leak providers."""
    saved = dict(publish_provider._FACTORIES)
    yield
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    session_key: str = "dashboard:test",
    restricted: bool = False,
) -> MagicMock:
    """MagicMock aiohttp Request shaped for these handlers (incl. rel_url)."""
    req = MagicMock()
    req.headers = {"X-Session-Key": session_key}
    req.match_info = match or {}
    req.query = query or {}
    req.rel_url.query = query or {}
    if isinstance(body, dict):
        req.read = AsyncMock(return_value=json.dumps(body).encode())
    elif isinstance(body, bytes):
        req.read = AsyncMock(return_value=body)
    else:
        req.read = AsyncMock(return_value=b"")
    req.app = {"state": MagicMock(), "_restricted_session": restricted}
    return req


@pytest.fixture
def patch_restricted(monkeypatch):
    """Make _is_restricted_session read req.app['_restricted_session']."""

    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


@pytest.fixture
def gate_open(monkeypatch):
    """Publish governance gate permits (returns None) and records calls."""
    calls: list[str] = []

    def _permit(_request, provider_name: str):
        calls.append(provider_name)
        return None

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _permit)
    return calls


@pytest.fixture
def gate_denied(monkeypatch):
    """Publish governance gate denies with a fixed reason and records calls."""
    calls: list[str] = []

    def _deny(_request, provider_name: str):
        calls.append(provider_name)
        return "publishing not permitted by policy"

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _deny)
    return calls


def _json_body(resp) -> dict:
    return json.loads(resp.body)


# ── Browse ──────────────────────────────────────────────────────────────────


class TestRemoteBrowse:
    @pytest.mark.asyncio
    async def test_browse_dispatches_list_remote(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(match={"provider": "fakeprov"}, query={"scope": "shared"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 200
        assert registered_provider.list_calls == [{"scope": "shared", "page_token": None}]
        data = _json_body(resp)
        assert data["artifacts"][0]["external_id"] == "ext-1"
        # No local copy yet → annotated local_slug is None.
        assert data["artifacts"][0]["local_slug"] is None

    @pytest.mark.asyncio
    async def test_browse_defaults_to_mine_scope(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(match={"provider": "fakeprov"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 200
        assert registered_provider.list_calls == [{"scope": "mine", "page_token": None}]

    @pytest.mark.asyncio
    async def test_browse_query_dispatches_search_remote(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(match={"provider": "fakeprov"}, query={"q": "dashboards"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 200
        assert registered_provider.search_calls == [{"query": "dashboards", "page_token": None}]
        assert registered_provider.list_calls == []

    @pytest.mark.asyncio
    async def test_browse_annotates_local_slug_for_cloned_artifact(
        self, isolated_store, patch_restricted, registered_provider
    ):
        art = isolated_store.create(name="Local copy", content="x", kind="html")
        isolated_store.set_fork_metadata(
            art.slug,
            ForkMetadata(
                upstream_artifact_id="ext-1",
                upstream_url="https://remote.example.com/a/ext-1",
                upstream_owner="someone",
                upstream_version=1,
                forked_at="2026-07-01T00:00:00Z",
                # Provider-attributed like a real fork_from_remote — the scoped
                # (provider\x00id) key is the primary annotation path.
                upstream_provider="fakeprov",
            ),
        )
        req = _request(match={"provider": "fakeprov"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 200
        assert _json_body(resp)["artifacts"][0]["local_slug"] == art.slug

    @pytest.mark.asyncio
    async def test_browse_unregistered_provider_503_when_registry_empty(
        self, isolated_store, patch_restricted
    ):
        # Public edition: NO provider registered → PublishUnavailableError → 503,
        # matching the clone/fork handlers (the surface exists, the provider
        # tooling doesn't) rather than a 404 "not found".
        req = _request(match={"provider": "fakeprov"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 503
        assert "fakeprov" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_browse_unsupported_scope_is_400(
        self, isolated_store, patch_restricted, registered_provider
    ):
        registered_provider.listing = None
        req = _request(match={"provider": "fakeprov"}, query={"scope": "public"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 400
        assert "does not support" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_browse_restricted_session_403(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(match={"provider": "fakeprov"}, restricted=True)
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 403
        assert registered_provider.list_calls == []


# ── Clone / Fork ────────────────────────────────────────────────────────────


class TestRemoteClone:
    @pytest.mark.asyncio
    async def test_clone_creates_publication_bound_copy(
        self, isolated_store, patch_restricted, registered_provider, gate_open
    ):
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 201
        data = _json_body(resp)
        art = isolated_store.get(data["slug"])
        assert art.publication is not None
        assert art.publication.artifact_id == "ext-1"
        assert art.publication.provider == "fakeprov"
        assert art.publication.auto_sync is True
        # The egress-arming gate ran on the routed provider name.
        assert gate_open == ["fakeprov"]

    @pytest.mark.asyncio
    async def test_clone_external_id_with_slash_from_body(
        self, isolated_store, patch_restricted, registered_provider, gate_open
    ):
        """A provider-native id containing '/' must round-trip via the body
        (a path segment would 404 on the decoded slash)."""
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "repo/path/ext-1"})
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 201
        art = isolated_store.get(_json_body(resp)["slug"])
        assert art.publication is not None
        assert art.publication.artifact_id == "repo/path/ext-1"

    @pytest.mark.asyncio
    async def test_snapshot_on_autosync_publication_pushes_gated(
        self, isolated_store, patch_restricted, gate_open, monkeypatch
    ):
        """A content snapshot on an auto_sync publication triggers a governed
        push — the leg that makes a clone actually bidirectional."""
        art = isolated_store.create(name="P", content="v1", kind="html")
        isolated_store.set_publication(
            art.slug,
            ArtifactPublication(artifact_id="ext-1", view_url="u", provider="fakeprov"),
        )
        pushed: list[str] = []
        monkeypatch.setattr(
            publish_sync,
            "push_version_by_slug",
            AsyncMock(side_effect=lambda slug: pushed.append(slug)),
        )
        resp = await api_artifact_update(
            _request(body={"content": "v2", "snapshot": True}, match={"slug": art.slug})
        )
        assert resp.status == 200
        assert pushed == [art.slug]
        assert gate_open == ["fakeprov"]

    @pytest.mark.asyncio
    async def test_snapshot_push_skipped_when_governance_denies(
        self, isolated_store, patch_restricted, gate_denied, monkeypatch
    ):
        """Governance-denied surface edits locally but never egresses."""
        art = isolated_store.create(name="P", content="v1", kind="html")
        isolated_store.set_publication(
            art.slug,
            ArtifactPublication(artifact_id="ext-1", view_url="u", provider="fakeprov"),
        )
        push = AsyncMock()
        monkeypatch.setattr(publish_sync, "push_version_by_slug", push)
        resp = await api_artifact_update(
            _request(body={"content": "v2", "snapshot": True}, match={"slug": art.slug})
        )
        assert resp.status == 200
        push.assert_not_awaited()
        assert gate_denied == ["fakeprov"]

    @pytest.mark.asyncio
    async def test_metadata_only_update_does_not_push(
        self, isolated_store, patch_restricted, gate_open, monkeypatch
    ):
        """A non-content (rename) update never pushes even on an auto_sync pub."""
        art = isolated_store.create(name="P", content="v1", kind="html")
        isolated_store.set_publication(
            art.slug,
            ArtifactPublication(artifact_id="ext-1", view_url="u", provider="fakeprov"),
        )
        push = AsyncMock()
        monkeypatch.setattr(publish_sync, "push_version_by_slug", push)
        resp = await api_artifact_update(
            _request(body={"name": "Renamed"}, match={"slug": art.slug})
        )
        assert resp.status == 200
        push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clone_governance_denied_403_before_dispatch(
        self, isolated_store, patch_restricted, registered_provider, gate_denied, monkeypatch
    ):
        engine = AsyncMock()
        monkeypatch.setattr(publish_sync, "clone_from_remote", engine)
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 403
        assert "not permitted" in _json_body(resp)["error"]
        assert gate_denied == ["fakeprov"]
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clone_503_when_registry_empty(self, isolated_store, patch_restricted, gate_open):
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_clone_missing_external_id_400(
        self, isolated_store, patch_restricted, registered_provider, gate_open
    ):
        req = _request(match={"provider": "fakeprov"})
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_clone_restricted_session_403(
        self, isolated_store, patch_restricted, registered_provider, gate_open
    ):
        req = _request(
            match={"provider": "fakeprov"}, body={"external_id": "ext-1"}, restricted=True
        )
        resp = await api_remote_artifacts_clone(req)
        assert resp.status == 403
        assert gate_open == []


class TestRemoteFork:
    @pytest.mark.asyncio
    async def test_fork_creates_pull_only_lineage(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_fork(req)
        assert resp.status == 201
        data = _json_body(resp)
        art = isolated_store.get(data["slug"])
        assert art.publication is None
        assert art.fork_metadata is not None
        assert art.fork_metadata.upstream_artifact_id == "ext-1"
        assert "forked" in art.tags

    @pytest.mark.asyncio
    async def test_fork_is_ungated_ingress(
        self, isolated_store, patch_restricted, registered_provider, gate_denied
    ):
        # Fork never arms a push, so the publish gate must NOT block it.
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_fork(req)
        assert resp.status == 201
        assert gate_denied == []

    @pytest.mark.asyncio
    async def test_fork_503_when_registry_empty(self, isolated_store, patch_restricted):
        req = _request(match={"provider": "fakeprov"}, body={"external_id": "ext-1"})
        resp = await api_remote_artifacts_fork(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_fork_restricted_session_403(
        self, isolated_store, patch_restricted, registered_provider
    ):
        req = _request(
            match={"provider": "fakeprov"}, body={"external_id": "ext-1"}, restricted=True
        )
        resp = await api_remote_artifacts_fork(req)
        assert resp.status == 403


# ── Upstream sync trio ──────────────────────────────────────────────────────


def _forked_artifact(store: ArtifactStore):
    art = store.create(name="Forked", content="v1", kind="text")
    return store.set_fork_metadata(
        art.slug,
        ForkMetadata(
            upstream_artifact_id="ext-1",
            upstream_url="https://remote.example.com/a/ext-1",
            upstream_owner="someone",
            upstream_version=1,
            forked_at="2026-07-01T00:00:00Z",
        ),
    )


class TestUpstreamStatus:
    @pytest.mark.asyncio
    async def test_status_untracked_artifact(self, isolated_store, patch_restricted):
        art = isolated_store.create(name="Plain", content="x", kind="text")
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_upstream_status(req)
        assert resp.status == 200
        data = _json_body(resp)
        assert data["tracked"] is False
        assert data["upstream_ahead"] is False

    @pytest.mark.asyncio
    async def test_status_fork_reports_upstream_ahead(
        self, isolated_store, patch_restricted, registered_provider, monkeypatch
    ):
        # upstream_status resolves the fork origin via DEFAULT_PROVIDER.
        monkeypatch.setattr(publish_sync, "DEFAULT_PROVIDER", "fakeprov")
        art = _forked_artifact(isolated_store)
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_upstream_status(req)
        assert resp.status == 200
        data = _json_body(resp)
        assert data["tracked"] is True
        # Remote current_version=3 > recorded upstream_version=1.
        assert data["upstream_ahead"] is True

    @pytest.mark.asyncio
    async def test_status_unknown_slug_404(self, isolated_store, patch_restricted):
        req = _request(match={"slug": "nope"})
        resp = await api_artifact_upstream_status(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_status_restricted_session_403(self, isolated_store, patch_restricted):
        req = _request(match={"slug": "any"}, restricted=True)
        resp = await api_artifact_upstream_status(req)
        assert resp.status == 403


class TestPullLatest:
    @pytest.mark.asyncio
    async def test_pull_untracked_artifact_400(self, isolated_store, patch_restricted):
        art = isolated_store.create(name="Plain", content="x", kind="text")
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 400
        assert "does not track" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_pull_fork_lands_new_snapshot(
        self, isolated_store, patch_restricted, registered_provider, monkeypatch
    ):
        monkeypatch.setattr(publish_sync, "DEFAULT_PROVIDER", "fakeprov")
        art = _forked_artifact(isolated_store)
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 200
        data = _json_body(resp)
        assert data["pull_result"]["pulled"] is True
        refreshed = isolated_store.get(art.slug)
        assert refreshed.version > art.version
        assert refreshed.content == "<h1>remote</h1>"

    @pytest.mark.asyncio
    async def test_pull_unknown_slug_404(self, isolated_store, patch_restricted):
        req = _request(match={"slug": "nope"})
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_pull_restricted_session_403(self, isolated_store, patch_restricted):
        req = _request(match={"slug": "any"}, restricted=True)
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 403


class TestOverwriteRemote:
    @pytest.mark.asyncio
    async def test_overwrite_governance_denied_403_before_dispatch(
        self, isolated_store, patch_restricted, gate_denied, monkeypatch
    ):
        engine = AsyncMock()
        monkeypatch.setattr(publish_sync, "overwrite_upstream", engine)
        art = isolated_store.create(name="Pub", content="x", kind="text")
        isolated_store.set_publication(
            art.slug,
            art_mod.ArtifactPublication(
                artifact_id="ext-9",
                view_url="https://remote.example.com/a/ext-9",
                provider="fakeprov",
            ),
        )
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_overwrite_remote(req)
        assert resp.status == 403
        # The gate evaluated the RESOLVED publication provider (fail-closed
        # capabilities.publish ∩ destinations:<provider>), not a default.
        assert gate_denied == ["fakeprov"]
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overwrite_permitted_reports_unpublished_artifact(
        self, isolated_store, patch_restricted, gate_open
    ):
        # Not published: the engine reports overwritten=False cleanly (200),
        # but only AFTER the gate ran (on the default provider name).
        art = isolated_store.create(name="Plain", content="x", kind="text")
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_overwrite_remote(req)
        assert resp.status == 200
        data = _json_body(resp)
        assert data["overwrite_result"]["overwritten"] is False
        assert gate_open == [publish_provider.DEFAULT_PROVIDER]

    @pytest.mark.asyncio
    async def test_overwrite_unknown_slug_404(self, isolated_store, patch_restricted, gate_open):
        req = _request(match={"slug": "nope"})
        resp = await api_artifact_overwrite_remote(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_overwrite_restricted_session_403(
        self, isolated_store, patch_restricted, gate_denied
    ):
        req = _request(match={"slug": "any"}, restricted=True)
        resp = await api_artifact_overwrite_remote(req)
        assert resp.status == 403
        assert gate_denied == []


# ── Redaction walker + local-slug annotation (correctness) ──────────────────


class TestRedactRemoteResponse:
    def test_redacts_credential_nested_in_list_within_list(self):
        """The prior walker only redacted strings/dicts inside a TOP-LEVEL list;
        a list nested inside a list silently passed through unredacted."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        data = {"rows": [["safe", [secret, "also-safe"]]]}
        out = art_handlers._redact_remote_response(data)
        assert secret not in json.dumps(out)
        assert out["rows"][0][1][0] == "[REDACTED: credential]"

    def test_redacts_credential_in_dict_within_nested_list(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        data = {"a": [[{"token": secret}]]}
        out = art_handlers._redact_remote_response(data)
        assert secret not in json.dumps(out)

    def test_deeply_nested_response_does_not_raise(self):
        """A pathologically nested provider response is truncated at the depth
        cap rather than driving a RecursionError (which would 500 the handler)."""
        payload: dict = {}
        node = payload
        for _ in range(5000):
            child: dict = {}
            node["next"] = child
            node = child
        node["leaf"] = "AKIAIOSFODNN7EXAMPLE"
        # Must not raise; depth beyond the cap is truncated to {}.
        out = art_handlers._redact_remote_response(payload)
        assert isinstance(out, dict)

    def test_already_redacted_keys_are_not_rescanned(self):
        # A key named in ``already_redacted`` is passed through verbatim (the
        # caller already redacted it) — proves the double-pass is skipped.
        raw = "AKIAIOSFODNN7EXAMPLE"
        out = art_handlers._redact_remote_response(
            {"content": raw, "other": raw}, already_redacted=frozenset({"content"})
        )
        assert out["content"] == raw  # skipped
        assert out["other"] == "[REDACTED: credential]"  # still redacted

    def test_strips_local_path(self):
        out = art_handlers._redact_remote_response({"localPath": "/home/u/x", "keep": "y"})
        assert "localPath" not in out
        assert out["keep"] == "y"


class TestIndexByArtifactId:
    def test_index_covers_publication_and_fork(self, isolated_store):
        pub_art = isolated_store.create(name="Pub", content="x", kind="text")
        isolated_store.set_publication(
            pub_art.slug,
            art_mod.ArtifactPublication(
                artifact_id="pub-1", view_url="https://r/pub-1", provider="fakeprov"
            ),
        )
        fork_art = isolated_store.create(name="Fork", content="y", kind="text")
        isolated_store.set_fork_metadata(
            fork_art.slug,
            ForkMetadata(
                upstream_artifact_id="fork-1",
                upstream_url="https://r/fork-1",
                upstream_owner="o",
                upstream_version=1,
                forked_at="2026-07-01T00:00:00Z",
            ),
        )
        index = isolated_store.index_by_artifact_id()
        # A publication with a provider is keyed ONLY under the provider-scoped
        # key (no bare-id key — that would let another provider's browse collide).
        assert index[isolated_store.artifact_index_key("fakeprov", "pub-1")] == pub_art.slug
        assert "pub-1" not in index
        # The fork fixture has no upstream_provider (legacy record), so it emits
        # both the (empty-provider) scoped key and the bare-id fallback.
        assert index["fork-1"] == fork_art.slug

    def test_shared_id_across_providers_does_not_cross_annotate(self, isolated_store):
        """A local copy from provider A with id X must NOT make a browse row for
        provider B's own id X look already-local (which would hide B's clone)."""
        a = isolated_store.create(name="A", content="x", kind="text")
        isolated_store.set_publication(
            a.slug,
            art_mod.ArtifactPublication(artifact_id="shared", view_url="u", provider="prov-a"),
        )
        index = isolated_store.index_by_artifact_id()
        # A's provider-scoped key resolves; the bare id does NOT (no cross-provider
        # fallback for a provider-tagged record).
        assert index[isolated_store.artifact_index_key("prov-a", "shared")] == a.slug
        assert "shared" not in index
        # So a prov-b lookup for the same id misses → row stays clone-able.
        assert index.get(isolated_store.artifact_index_key("prov-b", "shared")) is None

    def test_index_lookup_is_provider_scoped(self, isolated_store):
        """Two providers sharing an id must map to their OWN local copies —
        a browse against provider B never annotates provider A's artifact."""
        a = isolated_store.create(name="A", content="x", kind="text")
        isolated_store.set_publication(
            a.slug,
            art_mod.ArtifactPublication(artifact_id="123", view_url="u", provider="prov-a"),
        )
        b = isolated_store.create(name="B", content="y", kind="text")
        isolated_store.set_publication(
            b.slug,
            art_mod.ArtifactPublication(artifact_id="123", view_url="u", provider="prov-b"),
        )
        index = isolated_store.index_by_artifact_id()
        assert index[isolated_store.artifact_index_key("prov-a", "123")] == a.slug
        assert index[isolated_store.artifact_index_key("prov-b", "123")] == b.slug

    def test_find_by_artifact_id_provider_scoped_with_legacy_fallback(self, isolated_store):
        a = isolated_store.create(name="A", content="x", kind="text")
        isolated_store.set_publication(
            a.slug,
            art_mod.ArtifactPublication(artifact_id="123", view_url="u", provider="prov-a"),
        )
        # Wrong provider → no match even though the bare id matches.
        assert isolated_store.find_by_artifact_id("123", provider="prov-b") is None
        # Right provider → match.
        assert isolated_store.find_by_artifact_id("123", provider="prov-a").slug == a.slug
        # Legacy fork record (no upstream_provider) originated from the DEFAULT
        # provider, so the empty-provider fallback matches ONLY a request for the
        # default provider — never an arbitrary provider B (which would let B's
        # clone bind an unrelated legacy artifact that happens to share the id).
        # ForkMetadata keeps an empty provider on load (unlike ArtifactPublication
        # which defaults to the registry default provider), so it is the faithful legacy case.
        from kiro_crew.publish_provider import DEFAULT_PROVIDER

        legacy = isolated_store.create(name="L", content="z", kind="text")
        isolated_store.set_fork_metadata(legacy.slug, ForkMetadata(upstream_artifact_id="999"))
        # Arbitrary non-default provider → NO match (the tightened rule).
        assert isolated_store.find_by_artifact_id("999", provider="prov-b") is None
        # Default provider → the legacy fallback matches.
        assert (
            isolated_store.find_by_artifact_id("999", provider=DEFAULT_PROVIDER).slug == legacy.slug
        )
        # No provider filter → id-only match still works.
        assert isolated_store.find_by_artifact_id("999").slug == legacy.slug

    @pytest.mark.asyncio
    async def test_browse_annotates_high_entropy_external_id(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """A high-entropy external_id would be rewritten to [REDACTED] by the
        credential heuristic; annotation must run on the UN-redacted rows so the
        already-cloned artifact is still matched (UI shows Open, not Clone)."""
        high_entropy = "aB3xY9Qm2LpN8kR5tV7wZ1cD4eF6gH0jS2uI9oP7"
        registered_provider.listing = {
            "artifacts": [{"external_id": high_entropy, "title": "Remote", "snippet": ""}],
            "next_page_token": None,
        }
        art = isolated_store.create(name="Local", content="x", kind="html")
        isolated_store.set_fork_metadata(
            art.slug,
            ForkMetadata(
                upstream_artifact_id=high_entropy,
                upstream_url="https://r/x",
                upstream_owner="o",
                upstream_version=1,
                forked_at="2026-07-01T00:00:00Z",
                upstream_provider="fakeprov",
            ),
        )
        req = _request(match={"provider": "fakeprov"})
        resp = await api_remote_artifacts_browse(req)
        assert resp.status == 200
        row = _json_body(resp)["artifacts"][0]
        # The local match was found (annotation runs before redaction), AND the
        # opaque external_id is preserved verbatim (NOT rewritten to [REDACTED])
        # so a clone/fork of a credential-shaped id sends the real id back to the
        # provider instead of the redaction marker.
        assert row["local_slug"] == art.slug
        assert row["external_id"] == high_entropy
        assert "REDACTED" not in row["external_id"]

    @pytest.mark.asyncio
    async def test_browse_redacts_title_but_preserves_id(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """A credential in a human-readable field (title) is still redacted; the
        opaque external_id is preserved as the action handle."""
        registered_provider.listing = {
            "artifacts": [
                {
                    "external_id": "aB3xY9Qm2LpN8kR5tV7wZ1cD4eF6gH0jS2uI9oP7",
                    "title": "leak AKIAIOSFODNN7EXAMPLE here",
                    "snippet": "",
                }
            ],
            "next_page_token": None,
        }
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        row = _json_body(resp)["artifacts"][0]
        assert row["external_id"] == "aB3xY9Qm2LpN8kR5tV7wZ1cD4eF6gH0jS2uI9oP7"
        assert "AKIAIOSFODNN7EXAMPLE" not in row["title"]

    @pytest.mark.asyncio
    async def test_browse_redacts_credential_embedded_in_external_id(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """The id-key exemption skips ONLY the entropy heuristic — a LITERAL
        credential (AKIA…) embedded in external_id is STILL hard-credential
        redacted, so a malicious provider can't leak a token to the dashboard
        via the id field."""
        registered_provider.listing = {
            "artifacts": [
                {"external_id": "doc-AKIAIOSFODNN7EXAMPLE-tail", "title": "Remote", "snippet": ""}
            ],
            "next_page_token": None,
        }
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        row = _json_body(resp)["artifacts"][0]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["external_id"]

    @pytest.mark.asyncio
    async def test_browse_redacts_base64_encoded_credential_in_external_id(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """A base64-ENCODED hard credential smuggled into external_id is decoded,
        detected, and redacted — the id exemption skips only the entropy
        heuristic, not the hard-credential floor (literal OR base64)."""
        # base64("AKIAIOSFODNN7EXAMPLE …padding to exceed 40 b64 chars"); decodes
        # to text containing a literal AWS access-key id.
        b64_cred = (
            "QUtJQUlPU0ZPRE5ON0VYQU1QTEUgYW5kIG1vcmUgcGFkZGluZyB0ZXh0IGhlcmUg"
            "dG8gZXhjZWVkIGZvcnR5IGJhc2U2NCBjaGFycw=="
        )
        registered_provider.listing = {
            "artifacts": [{"external_id": b64_cred, "title": "Remote", "snippet": ""}],
            "next_page_token": None,
        }
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        row = _json_body(resp)["artifacts"][0]
        assert row["external_id"] == "[REDACTED: credential]"

    @pytest.mark.asyncio
    async def test_browse_preserves_benign_high_entropy_id_that_looks_base64(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """A benign 40-char id that happens to look base64 decodes to
        non-credential bytes, so the base64 scan must NOT rewrite it — clone/fork
        of that id must still send the real handle back to the provider."""
        benign = "aB3xY9Qm2LpN8kR5tV7wZ1cD4eF6gH0jS2uI9oP7"
        registered_provider.listing = {
            "artifacts": [{"external_id": benign, "title": "Remote", "snippet": ""}],
            "next_page_token": None,
        }
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        row = _json_body(resp)["artifacts"][0]
        assert row["external_id"] == benign

    @pytest.mark.asyncio
    async def test_browse_legacy_bare_id_does_not_annotate_other_provider(
        self, isolated_store, patch_restricted, registered_provider
    ):
        """A legacy no-provider local record emits only a bare-id key; a browse
        against a DIFFERENT provider sharing that id must NOT inherit the legacy
        slug (else it would hide the clone/fork action on a genuinely-remote
        artifact). The bare-id fallback is DEFAULT_PROVIDER-only."""
        # Legacy local record: fork_metadata with NO upstream_provider recorded.
        legacy = isolated_store.create(name="Legacy", content="x", kind="html")
        isolated_store.set_fork_metadata(
            legacy.slug,
            ForkMetadata(
                upstream_artifact_id="shared-id",
                upstream_url="https://r/x",
                upstream_owner="o",
                upstream_version=1,
                forked_at="2026-07-01T00:00:00Z",
                # upstream_provider left "" → legacy bare-id record
            ),
        )
        # fakeprov is NOT the default provider, so its row must stay un-annotated.
        registered_provider.listing = {
            "artifacts": [{"external_id": "shared-id", "title": "Remote", "snippet": ""}],
            "next_page_token": None,
        }
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        row = _json_body(resp)["artifacts"][0]
        assert row["local_slug"] is None


class TestRemoteAuditRedaction:
    """SEL audit events for the remote routes must redact caller-supplied text.

    The SEL writer signs bytes as-written and does NOT redact, so ``_audit``
    redacts the ``error`` string and every ``extra`` metadata leaf before the
    event is logged. A provider exception can carry a credential/signed URL,
    and ``external_id`` in ``extra`` is provider-controlled.
    """

    def _capture_sel(self, monkeypatch):
        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        return sel_stub

    @pytest.mark.asyncio
    async def test_browse_error_redacts_credential_in_sel_error(
        self, isolated_store, patch_restricted, registered_provider, monkeypatch
    ):
        """A provider raising an exception whose text embeds a credential must
        not persist that credential in the SEL ``error`` field."""
        sel_stub = self._capture_sel(monkeypatch)

        async def _boom(*, scope="mine", page_token=None):
            raise RuntimeError("upstream 500 for token AKIAIOSFODNN7EXAMPLE")

        monkeypatch.setattr(registered_provider, "list_remote", _boom)
        resp = await api_remote_artifacts_browse(_request(match={"provider": "fakeprov"}))
        assert resp.status == 502
        # Both the response AND the persisted audit error are redacted.
        assert "AKIAIOSFODNN7EXAMPLE" not in _json_body(resp)["error"]
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in kwargs["error"]

    @pytest.mark.asyncio
    async def test_clone_error_redacts_credential_in_external_id_metadata(
        self, isolated_store, patch_restricted, registered_provider, gate_open, monkeypatch
    ):
        """A credential-shaped ``external_id`` echoed into ``extra`` metadata on
        the clone error path must be redacted before it reaches the SEL log."""
        sel_stub = self._capture_sel(monkeypatch)

        async def _boom(external_id, *, provider_name):
            raise RuntimeError("clone failed")

        monkeypatch.setattr(publish_sync, "clone_from_remote", _boom)
        bad_id = "doc-AKIAIOSFODNN7EXAMPLE-tail"
        resp = await api_remote_artifacts_clone(
            _request(match={"provider": "fakeprov"}, body={"external_id": bad_id})
        )
        assert resp.status == 502
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(kwargs["metadata"])


class TestPullLatestErrorMapping:
    @pytest.mark.asyncio
    async def test_pull_concurrent_delete_maps_to_404(
        self, isolated_store, patch_restricted, registered_provider, monkeypatch
    ):
        """A concurrent delete during the pull window raises ArtifactNotFound
        from the post-pull re-fetch → 404, not a 502 'pull failed'."""
        monkeypatch.setattr(publish_sync, "DEFAULT_PROVIDER", "fakeprov")
        art = _forked_artifact(isolated_store)

        async def _fake_pull(slug, *, source="auto", actor="user"):
            # Simulate another tab deleting the artifact mid-pull.
            isolated_store.delete(art.slug)
            return {"pulled": False, "reason": "up to date"}

        monkeypatch.setattr(publish_sync, "pull_upstream", _fake_pull)
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_pull_provider_unavailable_maps_to_503(
        self, isolated_store, patch_restricted, registered_provider, monkeypatch
    ):
        art = _forked_artifact(isolated_store)

        async def _raise_unavailable(slug, *, source="auto", actor="user"):
            raise publish_provider.PublishUnavailableError("unknown publish provider: 'x'")

        monkeypatch.setattr(publish_sync, "pull_upstream", _raise_unavailable)
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_pull_latest(req)
        assert resp.status == 503


class TestUpstreamStatusBestEffort:
    @pytest.mark.asyncio
    async def test_tracked_artifact_with_unregistered_provider_reports_200(
        self, isolated_store, patch_restricted
    ):
        """A publication whose provider is not registered (empty public registry
        / snapshot restored from a companion edition) must degrade to the
        best-effort tracked payload, NOT 500 out of the handler."""
        art = isolated_store.create(name="Pub", content="x", kind="text")
        isolated_store.set_publication(
            art.slug,
            art_mod.ArtifactPublication(
                artifact_id="ext-9", view_url="https://r/ext-9", provider=publish_provider.DEFAULT_PROVIDER
            ),
        )
        req = _request(match={"slug": art.slug})
        resp = await api_artifact_upstream_status(req)
        assert resp.status == 200
        data = _json_body(resp)
        assert data["tracked"] is True
        assert data["upstream_ahead"] is False
        assert data["conflict"] is False


# ── Routing + --slack-only auth parity ──────────────────────────────────────


class TestRoutesAndAllowlist:
    def test_remote_artifact_routes_registered(self):
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_mcp_routes

        app = web.Application()
        app["state"] = MagicMock()
        app["port"] = 5476
        _register_mcp_routes(app)
        routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
        expected = {
            ("GET", "/api/remote-artifacts/{provider}/browse"),
            ("POST", "/api/remote-artifacts/{provider}/clone"),
            ("POST", "/api/remote-artifacts/{provider}/fork"),
            ("POST", "/api/artifacts/{slug}/pull-latest"),
            ("GET", "/api/artifacts/{slug}/upstream-status"),
            ("POST", "/api/artifacts/{slug}/overwrite-remote"),
        }
        assert expected.issubset(routes), f"Missing routes: {expected - routes}"

    def test_remote_artifacts_prefix_in_slack_only_allowlist(self):
        """--slack-only auth parity: /api/remote-artifacts is a mixed internal
        prefix (browser cookie auth + X-Internal-Secret callers), shared by
        both entrypoints so the headless server can never drift."""
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        assert "/api/remote-artifacts" in _MIXED_INTERNAL_API_PATHS
        assert "/api/artifacts" in _MIXED_INTERNAL_API_PATHS


# ── Remote comment writes: publish governance gate ──────────────────────────


class _CommentProvider(BrowseFakeProvider):
    """Fake provider that supports comment writes, recording each provider
    call so a test can assert the governance gate ran BEFORE any egress."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def capabilities(self):
        return {Capability.COMMENTS_READ, Capability.COMMENTS_WRITE}

    async def post_comment(self, *, external_id, body, anchor=None):
        self.writes.append("post")
        return RemoteComment(remote_id="rc1", thread_id="rc1", author="me", body=body)

    async def reply_comment(self, *, external_id, parent_remote_id, body):
        self.writes.append("reply")
        return RemoteComment(
            remote_id="rc2",
            thread_id=parent_remote_id,
            author="me",
            body=body,
            parent_id=parent_remote_id,
        )

    async def mark_review(self, *, external_id, remote_id):
        self.writes.append("review")

    async def delete_comment(self, *, external_id, remote_id):
        self.writes.append("delete")


@pytest.fixture
def comment_provider(monkeypatch):
    prov = _CommentProvider()
    saved = dict(publish_provider._FACTORIES)
    publish_provider.reset_providers()
    publish_provider.register_provider(prov.name, lambda: prov)
    publish_provider._INSTANCES[prov.name] = prov
    yield prov
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


_MATCH = {"provider": "fakeprov", "external_id": "ext-1", "comment_id": "c9"}


class TestRemoteCommentGovernanceGate:
    """Every remote comment WRITE is outbound egress, so it must pass the same
    fail-closed capabilities.publish gate as artifact publish. A denied gate
    returns 403 and the provider write is never reached (bytes never leave)."""

    @pytest.mark.asyncio
    async def test_post_comment_denied_403_before_egress(
        self, patch_restricted, comment_provider, gate_denied
    ):
        req = _request(body={"text": "hi"}, match=_MATCH)
        resp = await api_remote_artifact_post_comment(req)
        assert resp.status == 403
        assert comment_provider.writes == []
        assert gate_denied == ["fakeprov"]

    @pytest.mark.asyncio
    async def test_reply_comment_denied_403_before_egress(
        self, patch_restricted, comment_provider, gate_denied
    ):
        req = _request(body={"text": "re"}, match=_MATCH)
        resp = await api_remote_artifact_reply_comment(req)
        assert resp.status == 403
        assert comment_provider.writes == []

    @pytest.mark.asyncio
    async def test_mark_review_denied_403_before_egress(
        self, patch_restricted, comment_provider, gate_denied
    ):
        req = _request(match=_MATCH)
        resp = await api_remote_artifact_mark_review(req)
        assert resp.status == 403
        assert comment_provider.writes == []

    @pytest.mark.asyncio
    async def test_delete_comment_denied_403_before_egress(
        self, patch_restricted, comment_provider, gate_denied
    ):
        req = _request(match=_MATCH)
        resp = await api_remote_artifact_delete_comment(req)
        assert resp.status == 403
        assert comment_provider.writes == []

    @pytest.mark.asyncio
    async def test_gate_open_permits_post_and_reaches_provider(
        self, patch_restricted, comment_provider, gate_open
    ):
        req = _request(body={"text": "hi"}, match=_MATCH)
        resp = await api_remote_artifact_post_comment(req)
        assert resp.status == 201
        assert comment_provider.writes == ["post"]
        assert gate_open == ["fakeprov"]


class TestRemoteDenialAudit:
    """Every permission decision on the remote-artifact endpoints emits an SEL
    event (backend-security-controls): both the restricted-session guard and the
    provider capability-gate rejection must audit a ``denied`` outcome."""

    @pytest.fixture
    def capture_audit(self, monkeypatch):
        events: list[dict] = []
        monkeypatch.setattr(
            art_handlers,
            "_audit",
            lambda **kw: events.append(kw),
        )
        return events

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler,match,body",
        [
            (api_remote_artifact_get, {"provider": "p", "external_id": "e"}, None),
            (api_remote_artifact_comments, {"provider": "p", "external_id": "e"}, None),
            (api_remote_artifact_post_comment, _MATCH, {"text": "x"}),
            (api_remote_artifact_reply_comment, _MATCH, {"text": "x"}),
            (api_remote_artifact_mark_review, _MATCH, None),
            (api_remote_artifact_delete_comment, _MATCH, None),
        ],
    )
    async def test_restricted_session_emits_denied_audit(
        self, patch_restricted, capture_audit, handler, match, body
    ):
        req = _request(match=match, body=body, restricted=True)
        resp = await handler(req)
        assert resp.status == 403
        # A denied SEL event was recorded for the permission decision.
        assert any(e.get("outcome") == "denied" for e in capture_audit)

    @pytest.mark.asyncio
    async def test_capability_rejection_emits_denied_audit(
        self, patch_restricted, capture_audit, gate_open, monkeypatch
    ):
        # A provider that lacks COMMENTS_WRITE → post rejects with 400 + audit.
        class _NoWrite(_CommentProvider):
            def capabilities(self):
                return {Capability.COMMENTS_READ}

        prov = _NoWrite()
        saved = dict(publish_provider._FACTORIES)
        publish_provider.reset_providers()
        publish_provider.register_provider(prov.name, lambda: prov)
        publish_provider._INSTANCES[prov.name] = prov
        try:
            req = _request(body={"text": "x"}, match=_MATCH)
            resp = await api_remote_artifact_post_comment(req)
            assert resp.status == 400
            assert any(e.get("outcome") == "denied" for e in capture_audit)
        finally:
            publish_provider._FACTORIES.clear()
            publish_provider._FACTORIES.update(saved)
            publish_provider.reset_providers()

    @pytest.mark.asyncio
    async def test_comments_read_rejection_emits_denied_audit(
        self, patch_restricted, capture_audit, monkeypatch
    ):
        # The comments GET soft-degrades (200 + remote_sync_error) when the
        # provider lacks COMMENTS_READ, but that capability-gate decision must
        # still emit a denied SEL event like every other permission decision.
        class _NoRead(_CommentProvider):
            def capabilities(self):
                return set()

        prov = _NoRead()
        saved = dict(publish_provider._FACTORIES)
        publish_provider.reset_providers()
        publish_provider.register_provider(prov.name, lambda: prov)
        publish_provider._INSTANCES[prov.name] = prov
        art_handlers._remote_comment_cache.clear()
        try:
            req = _request(match=_MATCH)
            resp = await api_remote_artifact_comments(req)
            assert resp.status == 200
            assert any(e.get("outcome") == "denied" for e in capture_audit)
        finally:
            publish_provider._FACTORIES.clear()
            publish_provider._FACTORIES.update(saved)
            publish_provider.reset_providers()


# ── Bounded provider awaits: timeout → 504 (CWE-400) ────────────────────────


class TestRemoteProviderTimeout:
    """A hung publish provider must not block the request (and its event-loop
    slot) indefinitely. Every remote provider await is wrapped in
    ``asyncio.wait_for(..., timeout=_REMOTE_PROVIDER_TIMEOUT_S)``; on timeout the
    primary read + write endpoints map to a 504 with a NON-EMPTY error body
    (``str(TimeoutError())`` is empty) and a distinct ``timeout`` SEL outcome.

    The provider methods NEVER complete (``await asyncio.sleep(3600)``) and the
    module timeout constant is monkeypatched to a tiny value, so ``wait_for``
    itself must fire the timeout. This proves the wrapper is doing the work:
    remove the ``asyncio.wait_for`` and these tests hang/fail instead of passing.
    """

    class _TimeoutProvider(BrowseFakeProvider):
        """CONTENT_PULL + comment-write capable so the handlers reach the awaited
        provider call rather than short-circuiting on a missing capability. The
        awaited calls never return, so only ``wait_for`` can end them."""

        def capabilities(self):
            return {
                Capability.CONTENT_PULL,
                Capability.COMMENTS_READ,
                Capability.COMMENTS_WRITE,
            }

        async def fetch_content(self, *, external_id):
            await asyncio.sleep(3600)

        async def post_comment(self, *, external_id, body, anchor=None):
            await asyncio.sleep(3600)

    @pytest.fixture
    def timeout_provider(self):
        prov = self._TimeoutProvider()
        saved = dict(publish_provider._FACTORIES)
        publish_provider.reset_providers()
        publish_provider.register_provider(prov.name, lambda: prov)
        publish_provider._INSTANCES[prov.name] = prov
        yield prov
        publish_provider._FACTORIES.clear()
        publish_provider._FACTORIES.update(saved)
        publish_provider.reset_providers()

    @pytest.fixture
    def fast_timeout(self, monkeypatch):
        # Patch the module constant so ``asyncio.wait_for`` fires almost
        # immediately — the never-completing provider await is bounded by this,
        # keeping the test fast. Patching the constant (not raising in the
        # provider) is what forces the wrapper to be exercised end-to-end.
        monkeypatch.setattr(art_handlers, "_REMOTE_PROVIDER_TIMEOUT_S", 0.01)

    @pytest.fixture
    def capture_audit(self, monkeypatch):
        events: list[dict] = []
        monkeypatch.setattr(art_handlers, "_audit", lambda **kw: events.append(kw))
        return events

    @pytest.mark.asyncio
    async def test_get_maps_provider_timeout_to_504(
        self, patch_restricted, timeout_provider, fast_timeout, capture_audit
    ):
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 504
        # Non-empty error body — str(asyncio.TimeoutError()) would be "".
        assert _json_body(resp)["error"]
        assert any(e.get("outcome") == "timeout" for e in capture_audit)

    @pytest.mark.asyncio
    async def test_post_comment_maps_provider_timeout_to_504(
        self, patch_restricted, timeout_provider, fast_timeout, gate_open, capture_audit
    ):
        req = _request(body={"text": "hi"}, match=_MATCH)
        resp = await api_remote_artifact_post_comment(req)
        assert resp.status == 504
        assert _json_body(resp)["error"]
        assert any(e.get("outcome") == "timeout" for e in capture_audit)
