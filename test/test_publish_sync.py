"""Tests for the publish sync engine + publication persistence (Mesh-1880).

The public fork registers NO concrete publish provider (the registry is empty →
``get_provider`` raises ``PublishUnavailableError``). These tests exercise the
provider-agnostic orchestration in ``publish_sync`` against an in-test
``DummyPublishProvider`` (a minimal :class:`PublishProvider` subclass) registered
under the default provider name. The dummy accepts the same canned
response shapes the concrete provider maps, so the orchestration flow
(publish / push_version / sharing / unpublish / refresh / pull / clone /
overwrite) is covered end-to-end with no real provider present.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import (
    ArtifactPublication,
    ArtifactStore,
)
from kiro_crew.publish_provider import (
    Capability,
    PublishError,
    PublishProvider,
    PublishResult,
    PushResult,
)

# Optimistic-concurrency mismatch markers on a version push (mirrors the
# concrete provider's classification).
_CONFLICT_MARKERS = ("sha", "conflict", "expected")


class DummyPublishProvider(PublishProvider):
    """Minimal in-test PublishProvider.

    Records calls and returns canned responses in the same shape a concrete
    provider maps from its backend, so the provider-agnostic orchestration in
    ``publish_sync`` can be tested with no real provider registered.
    """

    name = "artifactory"  # the default provider name publish_sync resolves
    display_name = "Test Provider"
    install_hint = "the test provider is unavailable"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.ready = True
        self.upload_response = {
            "artifactId": "uuid-123",
            "artifactUrl": "https://artifacts.example.com/artifact/uuid-123",
            "versionNumber": 1,
            "sha256": "sha-v1",
            "ownerAlias": "nrb",
            "status": "READY",
        }
        self.upload_version_response = {
            "artifactId": "uuid-123",
            "artifactUrl": "https://artifacts.example.com/artifact/uuid-123",
            "versionNumber": 2,
            "sha256": "sha-v2",
        }
        self.update_sharing_response: dict = {"artifactId": "uuid-123"}
        self.delete_response: dict = {"deleted": True}
        self.get_response: dict = {}

    # ── availability ──────────────────────────────────────────────────────

    def available(self) -> bool:
        return self.ready

    async def ensure_ready(self) -> bool:
        return self.ready

    def view_url_for(self, external_id: str) -> str:
        return f"https://artifacts.example.com/artifact/{external_id}"

    def capabilities(self) -> set[Capability]:
        return {Capability.CONTENT_VERSIONS, Capability.CONTENT_PULL, Capability.SHARING}

    # ── interface ─────────────────────────────────────────────────────────

    async def publish(
        self, *, file_path, content_type, title, summary, tags, visibility, shared_with
    ) -> PublishResult:
        self.calls.append(
            (
                "upload",
                {
                    "file_path": file_path,
                    "content_type": content_type,
                    "title": title,
                    "summary": summary,
                    "tags": tags,
                    "visibility": visibility,
                    "shared_with": shared_with,
                },
            )
        )
        res = self.upload_response
        external_id = str(res.get("artifactId") or "")
        if not external_id:
            raise PublishError(f"upload returned no artifactId: {res!r}")
        return PublishResult(
            external_id=external_id,
            view_url=str(res.get("artifactUrl") or self.view_url_for(external_id)),
            version_number=int(res.get("versionNumber") or 1),
            concurrency_token=str(res.get("sha256") or ""),
            owner=str(res.get("ownerAlias") or ""),
        )

    async def push_version(self, *, external_id, file_path, expected_token) -> PushResult:
        self.calls.append(
            (
                "upload_version",
                {
                    "artifact_id": external_id,
                    "file_path": file_path,
                    "expected_current_sha256": expected_token,
                    "wait_for_ready": False,
                },
            )
        )
        res = self.upload_version_response
        err = res.get("error")
        if err:
            msg = str(err)
            conflict = any(m in msg.lower() for m in _CONFLICT_MARKERS)
            return PushResult(concurrency_token=expected_token, conflict=conflict, error=msg)
        return PushResult(
            version_number=int(res.get("versionNumber") or 0),
            concurrency_token=str(res.get("sha256") or expected_token),
        )

    async def update_sharing(self, *, external_id, visibility, shared_with) -> None:
        self.calls.append(
            (
                "update_sharing",
                {"artifact_id": external_id, "visibility": visibility, "shared_with": shared_with},
            )
        )
        if self.update_sharing_response.get("error"):
            raise PublishError(str(self.update_sharing_response["error"]))

    async def unpublish(self, *, external_id) -> None:
        self.calls.append(("delete", {"artifact_id": external_id}))
        if self.delete_response.get("error"):
            raise PublishError(str(self.delete_response["error"]))

    async def fetch_state(self, *, external_id) -> dict | None:
        self.calls.append(("get", {"artifact_id": external_id}))
        res = self.get_response
        if not isinstance(res, dict) or res.get("error"):
            return None
        art = res.get("artifact") if isinstance(res.get("artifact"), dict) else res
        if not isinstance(art, dict):
            return None
        out: dict = {}
        vis = art.get("visibility")
        if isinstance(vis, str):
            out["visibility"] = vis
        shared = art.get("sharedWith")
        if isinstance(shared, list):
            out["shared_with"] = [str(a) for a in shared if isinstance(a, str)]
        ver = art.get("currentVersionNumber")
        if isinstance(ver, int):
            out["current_version"] = ver
        sha = art.get("sha256")
        if isinstance(sha, str):
            out["sha256"] = sha
        return out or None

    async def fetch_content(self, *, external_id) -> dict | None:
        self.calls.append(("get", {"artifact_id": external_id}))
        res = self.get_response
        if not isinstance(res, dict) or res.get("error"):
            return None
        art_obj = res.get("artifact")
        meta: dict = art_obj if isinstance(art_obj, dict) else res
        local_path = str(res.get("localPath") or meta.get("localPath") or "")
        if not local_path or not os.path.exists(local_path):
            return None
        with open(local_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        raw_tags = meta.get("tags")
        tags = (
            [str(t) for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
        )
        raw_shared = meta.get("sharedWith")
        shared_with = (
            [str(s) for s in raw_shared if isinstance(s, str)]
            if isinstance(raw_shared, list)
            else []
        )
        cur_v = meta.get("currentVersionNumber")
        return {
            "content": content,
            "content_type": str(meta.get("contentType") or "text/plain"),
            "title": str(meta.get("title") or ""),
            "owner": str(meta.get("ownerAlias") or ""),
            "visibility": str(meta.get("visibility") or "PRIVATE"),
            "shared_with": shared_with,
            "tags": tags,
            "current_version": int(cur_v) if isinstance(cur_v, int) else None,
            "view_url": str(meta.get("artifactUrl") or self.view_url_for(external_id)),
            "sha256": str(meta.get("sha256") or ""),
        }

    # ── test helper ───────────────────────────────────────────────────────

    def called(self, tool: str) -> list[dict]:
        return [args for name, args in self.calls if name == tool]


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture
def fake_client():
    return DummyPublishProvider()


@pytest.fixture(autouse=True)
def wire(monkeypatch, store, fake_client):
    """Point the sync engine at the test store + register the dummy provider
    under the default provider name so ``get_provider`` resolves to it."""
    monkeypatch.setattr(publish_sync, "get_default_store", lambda: store)
    publish_provider.reset_providers()
    saved_factories = dict(publish_provider._FACTORIES)
    publish_provider.register_provider(fake_client.name, lambda: fake_client)
    # Ensure get_provider returns THIS instance (not a fresh factory build).
    publish_provider._INSTANCES[fake_client.name] = fake_client
    yield store
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved_factories)
    publish_provider.reset_providers()


# ── publish ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_sets_publication(store, fake_client):
    art = store.create(name="Doc", content="hello", kind="text")
    summary = await publish_sync.publish(art.slug, visibility="PRIVATE")

    assert summary["artifact_id"] == "uuid-123"
    assert summary["view_url"].endswith("/artifact/uuid-123")
    assert summary["visibility"] == "PRIVATE"

    reloaded = store.get(art.slug)
    pub = reloaded.publication
    assert pub is not None
    assert pub.artifact_id == "uuid-123"
    assert pub.last_pushed_sha256 == "sha-v1"
    assert pub.last_synced_kirocrew_version == 1
    assert pub.version_map == {"1": 1}
    assert pub.published_by == "nrb"
    # uploaded the rendered text content
    upload_args = fake_client.called("upload")[0]
    assert upload_args["title"] == "Doc"
    assert upload_args["visibility"] == "PRIVATE"


@pytest.mark.asyncio
async def test_publish_shared_with(store, fake_client):
    art = store.create(name="Doc", content="hi", kind="text")
    await publish_sync.publish(art.slug, visibility="SHARED", shared_with=["alice", "bob"])
    upload_args = fake_client.called("upload")[0]
    assert upload_args["shared_with"] == ["alice", "bob"]
    assert store.get(art.slug).publication.shared_with == ["alice", "bob"]


@pytest.mark.asyncio
async def test_publish_widget_wraps_html(store, fake_client):
    store.create(name="W", content="<h1>Hi</h1>", kind="widget", slug="w")
    await publish_sync.publish("w")
    upload_args = fake_client.called("upload")[0]
    assert upload_args["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_publish_unavailable_when_provider_absent(store, fake_client):
    # Provider not ready AND self-install fails → publish surfaces the 503 hint.
    fake_client.ready = False
    store.create(name="Doc", content="hi", kind="text", slug="d")
    with pytest.raises(publish_sync.PublishUnavailableError):
        await publish_sync.publish("d")


# ── push_version ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_version_updates_sha_and_map(store, fake_client):
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    # New KiroCrew version
    store.update("d", content="v2 content", snapshot=True)
    await publish_sync.push_version(store.get("d"))

    uv = fake_client.called("upload_version")
    assert len(uv) == 1
    assert uv[0]["expected_current_sha256"] == "sha-v1"  # the previous sha
    pub = store.get("d").publication
    assert pub.last_pushed_sha256 == "sha-v2"
    assert pub.last_synced_kirocrew_version == 2
    assert pub.version_map == {"1": 1, "2": 2}
    assert pub.last_error == ""


@pytest.mark.asyncio
async def test_push_version_skips_already_synced_version(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")  # last_synced_kirocrew_version = 1
    # No new KiroCrew version since publish → push is a no-op (we push exactly
    # once per KiroCrew version to preserve the 1:1 invariant + idempotency).
    await publish_sync.push_version(store.get("d"))
    assert fake_client.called("upload_version") == []
    # force=True (re-publish path) pushes anyway.
    await publish_sync.push_version(store.get("d"), force=True)
    assert len(fake_client.called("upload_version")) == 1


@pytest.mark.asyncio
async def test_push_version_conflict_sets_last_error(store, fake_client):
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    fake_client.upload_version_response = {"error": "expected sha mismatch"}
    store.update("d", content="v2", snapshot=True)
    await publish_sync.push_version(store.get("d"))
    pub = store.get("d").publication
    assert "conflict" in pub.last_error.lower()
    assert pub.last_pushed_sha256 == "sha-v1"  # unchanged on conflict


@pytest.mark.asyncio
async def test_push_version_noop_when_not_published(store, fake_client):
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.push_version(store.get("d"))
    assert fake_client.called("upload_version") == []


# ── sharing ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_sharing(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    summary = await publish_sync.update_sharing("d", visibility="PUBLIC", shared_with=[])
    assert summary["visibility"] == "PUBLIC"
    assert store.get("d").publication.visibility == "PUBLIC"
    assert fake_client.called("update_sharing")[0]["visibility"] == "PUBLIC"


@pytest.mark.asyncio
async def test_unshare_sets_private(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d", visibility="SHARED", shared_with=["alice"])
    await publish_sync.unshare("d")
    pub = store.get("d").publication
    assert pub.visibility == "PRIVATE"
    assert pub.shared_with == []


# ── unpublish / delete ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unpublish_clears_publication(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    await publish_sync.unpublish("d")
    assert fake_client.called("delete")[0]["artifact_id"] == "uuid-123"
    assert store.get("d").publication is None


@pytest.mark.asyncio
async def test_unpublish_unpublished_raises(store):
    store.create(name="Doc", content="x", kind="text", slug="d")
    with pytest.raises(publish_sync.NotPublishedError):
        await publish_sync.unpublish("d")


@pytest.mark.asyncio
async def test_update_sharing_unpublished_raises(store):
    store.create(name="Doc", content="x", kind="text", slug="d")
    with pytest.raises(publish_sync.NotPublishedError):
        await publish_sync.update_sharing("d", visibility="PUBLIC")


@pytest.mark.asyncio
async def test_republish_preserves_push_error(store, fake_client):
    # On the re-publish path, a content-push failure must not be masked by the
    # subsequent sharing update (which clears last_error). AutoSDE regression.
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    store.update("d", content="v2", snapshot=True)  # new KiroCrew version
    fake_client.upload_version_response = {"error": "expected sha mismatch"}
    result = await publish_sync.publish("d", visibility="PUBLIC")
    assert "conflict" in result["last_error"].lower()
    assert "conflict" in (store.get("d").publication.last_error or "").lower()


@pytest.mark.asyncio
async def test_delete_for_artifact_best_effort(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    art = store.get("d")
    await publish_sync.delete_for_artifact(art)
    assert fake_client.called("delete")[0]["artifact_id"] == "uuid-123"
    # store untouched (the caller deletes locally afterwards)
    assert store.get("d").publication is not None


# ── persistence ────────────────────────────────────────────────────────────────


def test_publication_roundtrip(tmp_path):
    store = ArtifactStore(root=tmp_path / "a")
    art = store.create(name="Doc", content="x", kind="text", slug="d")
    pub = ArtifactPublication(
        artifact_id="uuid-9",
        view_url="https://x/artifact/uuid-9",
        visibility="SHARED",
        shared_with=["carol"],
        last_pushed_sha256="abc",
        last_synced_kirocrew_version=art.version,
        version_map={"1": 1},
        published_by="nrb",
    )
    store.set_publication("d", pub)

    # Fresh store instance → forces a meta.json reload.
    store2 = ArtifactStore(root=tmp_path / "a")
    reloaded = store2.get("d").publication
    assert reloaded is not None
    assert reloaded.artifact_id == "uuid-9"
    assert reloaded.visibility == "SHARED"
    assert reloaded.shared_with == ["carol"]
    assert reloaded.version_map == {"1": 1}


def test_tolerant_load_meta_without_publication(tmp_path):
    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    # Simulate a legacy meta.json with no publication key.
    meta_path = tmp_path / "a" / "d" / "meta.json"
    raw = json.loads(meta_path.read_text())
    raw.pop("publication", None)
    meta_path.write_text(json.dumps(raw))
    assert store.get("d").publication is None


def test_tolerant_load_non_numeric_last_synced(tmp_path):
    # A corrupted/hand-edited meta.json with a non-numeric version must not
    # crash the load — it falls back to 0 (AutoSDE: tolerant-load contract).
    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    meta_path = tmp_path / "a" / "d" / "meta.json"
    raw = json.loads(meta_path.read_text())
    raw["publication"] = {
        "artifact_id": "uuid-x",
        "view_url": "https://x/artifact/uuid-x",
        "last_synced_kirocrew_version": "not-a-number",
        "version_map": {"1": "also-bad"},
    }
    meta_path.write_text(json.dumps(raw))
    pub = store.get("d").publication
    assert pub is not None
    assert pub.last_synced_kirocrew_version == 0
    assert pub.version_map == {}  # bad entries dropped


def test_clear_publication(tmp_path):
    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    store.set_publication("d", ArtifactPublication(artifact_id="u", view_url="https://x"))
    store.clear_publication("d")
    assert store.get("d").publication is None


def test_update_publication_unknown_field_rejected(tmp_path):
    from kiro_crew.artifacts import ArtifactValidationError

    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    store.set_publication("d", ArtifactPublication(artifact_id="u", view_url="https://x"))
    with pytest.raises(ArtifactValidationError):
        store.update_publication("d", bogus_field="nope")


def test_update_publication_requires_existing(tmp_path):
    from kiro_crew.artifacts import ArtifactValidationError

    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    with pytest.raises(ArtifactValidationError):
        store.update_publication("d", visibility="PUBLIC")


# ── widget wrap ─────────────────────────────────────────────────────────────


def test_wrap_widget_html_self_contained():
    html = publish_sync.wrap_widget_html("<h1>Hi</h1>")
    assert html.startswith("<!DOCTYPE html>")
    assert "<h1>Hi</h1>" in html
    assert '<script src="https://cdn.tailwindcss.com"></script>' in html
    # MCP auto-injects <base>; the wrapper must not add it.
    assert "<base" not in html


def test_redact_untrusted_scans_every_source():
    # Regression (PR #14 nrb): the `manual` source is no longer a redaction
    # bypass. `source` is set once at create and NOT re-derived when an agent
    # later updates the content, so a `manual`-labelled artifact can carry
    # LLM/agent bytes by publish time — it MUST still be scanned. An AKIA-shaped
    # credential is redacted regardless of source.
    secret = "leak AKIAIOSFODNN7EXAMPLE now"
    for source in ("manual", "chat", "cron", "subagent", "import"):
        out = publish_sync._redact_untrusted(secret, source)
        assert "AKIAIOSFODNN7EXAMPLE" not in out, source
    # Empty text short-circuits unchanged.
    assert publish_sync._redact_untrusted("", "manual") == ""


def test_parse_publication_missing_id_returns_none():
    # A malformed block lacking artifact_id is treated as unpublished.
    assert ArtifactStore._parse_publication({"view_url": "https://x"}) is None
    assert ArtifactStore._parse_publication(None) is None
    assert ArtifactStore._parse_publication("not-a-dict") is None


# ── refresh: out-of-band drift detection ─────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_flags_rollback(store, fake_client):
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    # The remote bytes changed out-of-band AT THE SAME version (an external
    # edit or rollback): the version still matches what KiroCrew published, but
    # the sha no longer does. This is genuine drift to surface (a cloud-ahead
    # version is now a pullable edit, not drift — covered separately).
    fake_client.get_response = {
        "artifact": {
            "visibility": "PRIVATE",
            "sharedWith": [],
            "currentVersionNumber": 1,
            "sha256": "sha-external",
        }
    }
    refreshed = await publish_sync.refresh_publication(art.slug)
    assert refreshed.publication is not None
    assert refreshed.publication.last_error.startswith("The remote copy changed outside KiroCrew")


@pytest.mark.asyncio
async def test_refresh_clears_drift_when_reconciled(store, fake_client):
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    store.update_publication(
        art.slug, last_error="The remote copy changed outside KiroCrew: it is showing v9."
    )
    # The remote now matches what KiroCrew published again → note clears.
    fake_client.get_response = {
        "artifact": {
            "visibility": "PRIVATE",
            "sharedWith": [],
            "currentVersionNumber": 1,
            "sha256": "sha-v1",
        }
    }
    refreshed = await publish_sync.refresh_publication(art.slug)
    assert refreshed.publication is not None
    assert refreshed.publication.last_error == ""


# ── bidirectional sync: pull_upstream / clone_from_remote / upstream_status ───

import getpass  # noqa: E402

from kiro_crew.artifacts import ForkMetadata  # noqa: E402


def _remote_get(
    tmp_path, content, *, version, sha, owner="nrb", ctype="text/plain", shared=None, shared_v2=None
):
    """A fake get_artifact response: artifact metadata + downloaded localPath."""
    f = tmp_path / f"remote-{version}-{sha}.txt"
    f.write_text(content)
    return {
        "artifact": {
            "title": "Doc",
            "ownerAlias": owner,
            "visibility": "PRIVATE",
            "sharedWith": shared or [],
            "sharedWithV2": shared_v2 or [],
            "currentVersionNumber": version,
            "sha256": sha,
            "contentType": ctype,
            "artifactUrl": "https://x/uuid-123",
        },
        "localPath": str(f),
    }


def _track_publication(store, slug, *, sha="sha-v1", cloud_v=1):
    pub = ArtifactPublication(
        artifact_id="uuid-123",
        view_url="https://x/uuid-123",
        last_pushed_sha256=sha,
        last_synced_kirocrew_version=1,
        version_map={"1": cloud_v},
        auto_sync=True,
    )
    return store.set_publication(slug, pub)


@pytest.mark.asyncio
async def test_pull_upstream_publication_pulls_when_ahead(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="old", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 2  # new local snapshot
    assert reloaded.publication.last_synced_kirocrew_version == 2
    assert reloaded.publication.version_map["2"] == 2


@pytest.mark.asyncio
async def test_pull_upstream_up_to_date(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="same", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "same", version=1, sha="sha-v1")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is False
    assert store.get(art.slug).version == 1


@pytest.mark.asyncio
async def test_pull_upstream_no_conflict_appends_when_both_diverged(store, fake_client, tmp_path):
    """A local unsynced SNAPSHOT is already in history, so pulling never refuses
    and never clobbers it — the cloud version lands as a NEW version on top, and
    pulling NEVER pushes (the local edit is not uploaded over the upstream)."""
    art = store.create(name="Doc", content="old", kind="text")  # v1
    _track_publication(store, art.slug)
    # Local advances via an explicit snapshot → v2 (already in history), last_synced stays 1.
    store.update(art.slug, content="my local edit", snapshot=True)
    fake_client.get_response = _remote_get(tmp_path, "their edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result.get("conflict") is not True
    reloaded = store.get(art.slug)
    assert reloaded.content == "their edit"  # cloud content is now current
    assert reloaded.version == 3  # appended on top of my v2
    assert store.get(art.slug, version=2).content == "my local edit"  # preserved in history
    assert reloaded.publication.last_synced_kirocrew_version == 3
    # Pull never pushes: the local edit was NOT uploaded over the upstream.
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_pull_upstream_checkpoints_live_dirty_before_pull(store, fake_client, tmp_path):
    """Unsaved WORKING edits (live_dirty, not yet snapshotted) are snapshotted as
    a local-only checkpoint BEFORE the pull, so they're preserved in history and
    never lost — and the checkpoint is push-suppressed (no upload)."""
    art = store.create(name="Doc", content="old", kind="text")  # v1
    _track_publication(store, art.slug)
    # Unsaved working edit: live content changes, NO snapshot → still v1, live_dirty.
    store.update(art.slug, content="unsaved working edit")
    assert store.get(art.slug).live_dirty is True
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["preserved_version"] == 2  # working edit checkpointed first
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 3  # v1 old, v2 checkpoint(working edit), v3 pulled
    assert store.get(art.slug, version=2).content == "unsaved working edit"  # preserved
    assert reloaded.publication.last_synced_kirocrew_version == 3
    # The checkpoint did NOT auto-publish the working edit (race defused).
    assert fake_client.called("upload_version") == []


def _file_backed(store, tmp_path, content="old"):
    """A file-backed artifact whose backing file exists on disk."""
    src = tmp_path / "doc.md"
    src.write_text(content)
    art = store.create(name="Doc", content=content, kind="markdown", source_path=str(src))
    return art, src


@pytest.mark.asyncio
async def test_upstream_status_reports_ahead_for_file_backed(store, fake_client, tmp_path):
    """A file-backed publication participates in drift detection — the pull
    banner is not suppressed just because the artifact has a source_path
    (Mesh-2735: the old blanket suppression made published working files
    silently push-only)."""
    art, _src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    status = await publish_sync.upstream_status(art.slug)
    assert status["tracked"] is True
    assert status["upstream_ahead"] is True


@pytest.mark.asyncio
async def test_pull_upstream_file_backed_writes_through_to_source_file(
    store, fake_client, tmp_path
):
    """Pulling into a file-backed artifact lands the content as a new local
    version AND writes it through to the backing file on disk."""
    art, src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert src.read_text() == "collab edit"  # write-through to the working file
    reloaded = store.get(art.slug)
    assert reloaded.content == "collab edit"
    assert reloaded.version == 2
    assert reloaded.publication.last_synced_kirocrew_version == 2


@pytest.mark.asyncio
async def test_pull_upstream_file_backed_checkpoints_external_file_edit(
    store, fake_client, tmp_path
):
    """An external edit to the backing file (live_dirty) is checkpointed as a
    local-only version BEFORE the pulled content overwrites the file — the
    pre-pull file bytes are never lost, and the checkpoint is push-suppressed."""
    art, src = _file_backed(store, tmp_path)  # v1 "old"
    _track_publication(store, art.slug)
    src.write_text("my unsaved file edit")  # external editor writes the file
    assert store.get(art.slug).live_dirty is True
    fake_client.get_response = _remote_get(tmp_path, "collab edit", version=2, sha="sha-v2")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["preserved_version"] == 2  # file edit checkpointed first
    assert store.get(art.slug, version=2).content == "my unsaved file edit"
    assert src.read_text() == "collab edit"  # pulled content reached the file
    assert store.get(art.slug).version == 3
    # The checkpoint did NOT auto-publish the file edit (race defused).
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_overwrite_upstream_file_backed_pushes_live_file_bytes(store, fake_client, tmp_path):
    """Overwrite ("keep mine") is push-only — it never writes locally — so a
    file-backed artifact pushes its live file bytes like any other."""
    art, src = _file_backed(store, tmp_path)
    _track_publication(store, art.slug)
    src.write_text("my file content")  # live file bytes to push
    fake_client.get_response = _remote_get(tmp_path, "their edit", version=2, sha="sha-v2")
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True
    assert fake_client.called("upload_version")  # push landed
    assert src.read_text() == "my file content"  # local file untouched


@pytest.mark.asyncio
async def test_pull_upstream_rejects_non_text_content_type(store, fake_client, tmp_path):
    """A binary upstream (image/PDF/…) is refused, not read as UTF-8 mojibake."""
    art = store.create(name="Doc", content="old", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(
        tmp_path, "\x89PNG...", version=2, sha="sha-v2", ctype="image/png"
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is False
    assert "unsupported content type" in result["reason"]
    assert store.get(art.slug).content == "old"  # untouched


@pytest.mark.asyncio
async def test_pull_upstream_widget_remaps_kind_to_html(store, fake_client, tmp_path):
    """A FOREIGN (sentinel-less) document pulled into a widget can't be unwrapped
    to an inner fragment, so it degrades to html (keeping kind=widget would
    double-wrap a full document on the next push). The pre-pull v1 snapshot still
    renders as a widget via per-version kind."""
    art = store.create(name="W", content="<b>inner</b>", kind="widget")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(
        tmp_path,
        "<!DOCTYPE html><html><body>foreign</body></html>",
        version=2,
        sha="sha-v2",
        ctype="text/html",
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.kind == "html"
    assert store.get(art.slug, version=1).kind == "widget"


@pytest.mark.asyncio
async def test_pull_upstream_fork_origin(store, fake_client, tmp_path):
    art = store.create(name="F", content="old", kind="html")
    store.set_fork_metadata(
        art.slug, ForkMetadata(upstream_artifact_id="uuid-123", upstream_version=1)
    )
    fake_client.get_response = _remote_get(tmp_path, "origin v2", version=2, sha="s")
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    assert result["source"] == "origin"
    reloaded = store.get(art.slug)
    assert reloaded.content == "origin v2"
    assert reloaded.fork_metadata.upstream_version == 2


def test_source_target_origin_resolves_fork_provider(store):
    """A fork's ``origin`` pull must resolve against the provider it was forked
    from, not always DEFAULT_PROVIDER — otherwise pulling a fork of provider B
    would query provider A (wrong content, or sync failure)."""
    art = store.create(name="Fp", content="x", kind="html")
    store.set_fork_metadata(
        art.slug,
        ForkMetadata(
            upstream_artifact_id="ext-9",
            upstream_version=1,
            upstream_provider="provider-b",
        ),
    )
    reloaded = store.get(art.slug)
    provider_name, ext_id, _v, is_pub = publish_sync._source_target(reloaded, "origin")
    assert provider_name == "provider-b"
    assert ext_id == "ext-9"
    assert is_pub is False


def test_source_target_origin_legacy_fork_falls_back_to_default(store):
    """A pre-multi-provider fork record (no upstream_provider) falls back to
    DEFAULT_PROVIDER so single-provider forks keep resolving."""
    art = store.create(name="Fl", content="x", kind="html")
    store.set_fork_metadata(art.slug, ForkMetadata(upstream_artifact_id="ext-legacy"))
    reloaded = store.get(art.slug)
    provider_name, ext_id, _v, _is_pub = publish_sync._source_target(reloaded, "origin")
    assert provider_name == publish_sync.DEFAULT_PROVIDER
    assert ext_id == "ext-legacy"


@pytest.mark.asyncio
async def test_clone_from_remote_owned(store, fake_client, tmp_path):
    me = getpass.getuser()
    fake_client.get_response = _remote_get(tmp_path, "my doc", version=2, sha="s", owner=me)
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.publication is not None
    assert art.publication.auto_sync is True  # clone always turns sync intent on
    assert art.content == "my doc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,shared,role",
    [
        ("me", [], None),  # I own it
        ("alice", ["me"], None),  # legacy shared-with membership
        ("alice", [], "EDITOR"),
        ("alice", [], "ADMIN"),
        ("alice", [], "VIEWER"),  # view-only on the remote
        ("alice", [], "COMMENTER"),  # comment-only on the remote
        ("alice", [], None),  # no individual grant (e.g. group / LDAP only)
        ("", [], None),  # unknown owner / untrusted remote data
    ],
)
async def test_clone_from_remote_auto_sync_is_intent_not_permission(
    store, fake_client, tmp_path, owner, shared, role
):
    """auto_sync is the user's INTENT and is ALWAYS on for a clone — it is never
    derived from, or limited by, the caller's role on the remote copy. Whether a
    push actually lands is the provider's call at push time (see push_version),
    so a permission read can never wrongly pre-disable a clone — including the
    case where edit rights come via a group/LDAP grant we can't resolve locally.
    A read can prove I *can* write, but never that I *can't*; only the provider's
    denial is authoritative.
    """
    me = getpass.getuser()
    owner = me if owner == "me" else owner
    shared = [me if s == "me" else s for s in shared]
    sv2 = [{"alias": me, "role": role}] if role else []
    fake_client.get_response = _remote_get(
        tmp_path, "doc", version=1, sha="s", owner=owner, shared=shared, shared_v2=sv2
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.publication is not None
    assert art.publication.auto_sync is True
    assert art.publication.published_by == owner  # mirrors the remote owner


@pytest.mark.asyncio
async def test_clone_rejects_non_text_content_type(store, fake_client, tmp_path):
    """Clone refuses a binary artifact rather than reading it as UTF-8 mojibake."""
    me = getpass.getuser()
    fake_client.get_response = _remote_get(
        tmp_path, "%PDF-1.7...", version=1, sha="s", owner=me, ctype="application/pdf"
    )
    with pytest.raises(publish_sync.PublishError) as ei:
        await publish_sync.clone_from_remote("uuid-123")
    assert "not a text artifact" in str(ei.value)


@pytest.mark.asyncio
async def test_clone_idempotent(store, fake_client, tmp_path):
    me = getpass.getuser()
    fake_client.get_response = _remote_get(tmp_path, "doc", version=1, sha="s", owner=me)
    a1 = await publish_sync.clone_from_remote("uuid-123")
    a2 = await publish_sync.clone_from_remote("uuid-123")
    assert a1.slug == a2.slug


@pytest.mark.asyncio
async def test_clone_from_remote_widget_unwraps_to_widget(store, fake_client, tmp_path):
    """A widget published remotely is a sentinel-wrapped standalone document.
    Cloning it locally recovers the inner fragment and keeps kind=widget, so a
    cloned remote-only widget is still inline-embeddable in chat (not degraded
    to html) — mirroring pull_upstream's unwrap. v1's per-version kind is widget
    so the clone is convertible/revertable as a widget."""
    me = getpass.getuser()
    wrapped = publish_sync.wrap_widget_html("<b>cloned widget</b>")
    fake_client.get_response = _remote_get(
        tmp_path, wrapped, version=1, sha="s", owner=me, ctype="text/html"
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.kind == "widget"  # recovered fragment → still a widget
    assert art.content == "<b>cloned widget</b>"  # exact inner, unwrapped
    assert art.version_kinds.get("1") == "widget"
    assert art.publication is not None


@pytest.mark.asyncio
async def test_clone_from_remote_foreign_html_stays_html(store, fake_client, tmp_path):
    """A foreign / sentinel-less HTML document clones as html (no false widget)."""
    me = getpass.getuser()
    fake_client.get_response = _remote_get(
        tmp_path,
        "<!DOCTYPE html><html><body>foreign</body></html>",
        version=1,
        sha="s",
        owner=me,
        ctype="text/html",
    )
    art = await publish_sync.clone_from_remote("uuid-123")
    assert art.kind == "html"


@pytest.mark.asyncio
async def test_upstream_status_reports_ahead(store, fake_client, tmp_path):
    art = store.create(name="Doc", content="x", kind="text")
    _track_publication(store, art.slug)
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=5, sha="s")
    status = await publish_sync.upstream_status(art.slug)
    assert status["tracked"] is True
    assert status["upstream_ahead"] is True
    assert status["cloud_version"] == 5
    assert status["source"] == "publication"
    assert status["live_dirty"] is False


@pytest.mark.asyncio
async def test_upstream_status_reports_live_dirty(store, fake_client, tmp_path):
    """Unsaved working edits surface as live_dirty so the banner can tell the
    user their edits will be preserved (checkpointed) before a pull."""
    art = store.create(name="Doc", content="x", kind="text")
    _track_publication(store, art.slug)
    store.update(art.slug, content="unsaved edit")  # no snapshot → live_dirty
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=5, sha="s")
    status = await publish_sync.upstream_status(art.slug)
    assert status["live_dirty"] is True
    assert status["upstream_ahead"] is True


@pytest.mark.asyncio
async def test_push_version_denial_keeps_auto_sync_intent(store, fake_client):
    """A push the provider rejects for lack of write access records a clear
    last_error but NEVER disables auto_sync. auto_sync is the user's sync INTENT,
    decoupled from permission: the provider re-evaluates write access on the NEXT
    snapshot's push, so a later grant resumes syncing automatically and a revoke
    just keeps surfacing the error. We never pre-disable intent from a read — a
    read can prove I *can* write but never that I *can't*; only the provider's
    denial is authoritative.
    """
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    fake_client.upload_version_response = {
        "error": "AccessDenied: principal is not authorized to perform upload_version"
    }
    store.update("d", content="v2", snapshot=True)
    await publish_sync.push_version(store.get("d"))
    pub = store.get("d").publication
    assert pub.auto_sync is True  # intent preserved across a permission denial
    assert "sync failed" in pub.last_error.lower()


def test_is_pullable_content_type_allowlist():
    for ct in (
        "text/plain",
        "text/html",
        "text/markdown",
        "application/json",
        "image/svg+xml",
        "application/xml",
        "text/csv",
        "application/ld+json",
        "",
    ):
        assert publish_sync._is_pullable_content_type(ct) is True, ct
    for ct in (
        "image/png",
        "application/pdf",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        assert publish_sync._is_pullable_content_type(ct) is False, ct


@pytest.mark.asyncio
async def test_refresh_does_not_flag_upstream_ahead(store, fake_client):
    """A cloud-strictly-ahead version is a pullable edit, not drift-to-clobber."""
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    fake_client.get_response = {
        "artifact": {
            "visibility": "PRIVATE",
            "sharedWith": [],
            "currentVersionNumber": 2,
            "sha256": "sha-ahead",
        }
    }
    refreshed = await publish_sync.refresh_publication(art.slug)
    assert refreshed.publication is not None
    assert refreshed.publication.last_error == ""


@pytest.mark.asyncio
async def test_pull_upstream_widget_unwraps_and_stays_widget(store, fake_client, tmp_path):
    """A KiroCrew-published widget pulled back is unwrapped from its standalone
    document to the original inner fragment and STAYS a widget — so it remains
    inline-embeddable in chat (no html regression)."""
    art = store.create(name="W", content="<b>inner</b>", kind="widget")
    _track_publication(store, art.slug)
    # The remote bytes are what KiroCrew uploads: the sentinel-wrapped doc.
    wrapped = publish_sync.wrap_widget_html("<b>collab edit</b>")
    fake_client.get_response = _remote_get(
        tmp_path, wrapped, version=2, sha="sha-v2", ctype="text/html"
    )
    result = await publish_sync.pull_upstream(art.slug)
    assert result["pulled"] is True
    reloaded = store.get(art.slug)
    assert reloaded.kind == "widget"  # recovered fragment → still a widget
    assert reloaded.content == "<b>collab edit</b>"  # exact inner, unwrapped


def test_wrap_unwrap_widget_roundtrip():
    inner = '<div class="x">hi <span>there</span></div>'
    wrapped = publish_sync.wrap_widget_html(inner)
    assert wrapped.lstrip().lower().startswith("<!doctype")
    assert publish_sync.unwrap_widget_html(wrapped) == inner
    # Body injection appended outside the sentinels is excluded on unwrap.
    injected = wrapped.replace("</body>", "<script>/*provider*/</script></body>")
    assert publish_sync.unwrap_widget_html(injected) == inner
    # Sentinel-less / foreign document → not unwrappable.
    assert publish_sync.unwrap_widget_html("<!DOCTYPE html><html><body>x</body></html>") is None
    assert publish_sync.unwrap_widget_html("") is None
    # Idempotent: wrapping an already-standalone document does not double-wrap.
    assert publish_sync.wrap_widget_html(wrapped) == wrapped


@pytest.mark.asyncio
async def test_overwrite_upstream_forces_local_over_remote(store, fake_client, tmp_path):
    """Overwrite makes the local content the remote's new current version even
    when the remote moved ahead — WITHOUT pulling the remote's bytes locally.
    It adopts the remote's current token then force-pushes; the local content is
    untouched and auto_sync intent is preserved."""
    art = store.create(name="Doc", content="my version", kind="text")
    _track_publication(store, art.slug)  # last_pushed_sha256="sha-v1", synced v1
    # Remote moved ahead to v5 with a different sha (a collaborator's edit).
    fake_client.get_response = _remote_get(
        tmp_path, "their edit", version=5, sha="sha-remote-ahead"
    )
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True
    # The push adopted the remote's CURRENT token (force-overwrite), not the
    # stale one — proving we overrode the ahead remote rather than conflicting.
    uv = fake_client.called("upload_version")
    assert len(uv) == 1
    assert uv[0]["expected_current_sha256"] == "sha-remote-ahead"
    reloaded = store.get(art.slug)
    assert reloaded.content == "my version"  # remote bytes NOT pulled locally
    assert reloaded.publication.auto_sync is True  # intent preserved
    assert reloaded.publication.last_error == ""


@pytest.mark.asyncio
async def test_overwrite_upstream_rejects_non_publication(store, fake_client, tmp_path):
    """A fork (origin lineage only, no publication push target) can't be
    overwritten — there's nothing to push back to."""
    art = store.create(name="F", content="x", kind="html")
    store.set_fork_metadata(art.slug, ForkMetadata(upstream_artifact_id="uuid-123"))
    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is False
    assert "not a publication" in result["reason"]
    assert fake_client.called("upload_version") == []


@pytest.mark.asyncio
async def test_push_snapshots_live_dirty_before_push_no_map_drift(store, fake_client, tmp_path):
    """Pushing while live_dirty (e.g. an agent edited the live version without
    snapshotting, then Overwrite is invoked) must snapshot the live content into
    a REAL local version first, so the pushed remote version is backed by a local
    version whose bytes match. Reproduces the version_map<->content drift: before
    the fix, the live edit was pushed but the map keyed on the prior snapshot's
    number, whose stored bytes differed from what was pushed.
    """
    art = store.create(name="Doc", content="original", kind="text")  # v1
    _track_publication(store, art.slug)  # synced v1, version_map {"1": 1}
    # Agent edits the LIVE version, no snapshot -> still v1, but live_dirty.
    store.update(art.slug, content="blue live edit")
    assert store.get(art.slug).live_dirty is True
    assert store.get(art.slug).version == 1
    # Remote moved ahead from elsewhere; user clicks Overwrite.
    fake_client.get_response = _remote_get(
        tmp_path, "their edit", version=5, sha="sha-remote-ahead"
    )

    result = await publish_sync.overwrite_upstream(art.slug)
    assert result["overwritten"] is True

    reloaded = store.get(art.slug)
    pub = reloaded.publication
    # The live edit was captured as a real new local version (v2), not left
    # dangling in current.html.
    assert reloaded.version == 2
    assert reloaded.live_dirty is False
    assert reloaded.content == "blue live edit"
    # The map keys on the version that was actually pushed, and that version's
    # content matches the remote it points at — invariant intact, no drift.
    assert pub.last_synced_kirocrew_version == 2
    assert pub.version_map["2"] == fake_client.upload_version_response["versionNumber"]
    # No stale entry maps a prior version number whose bytes differ from what
    # was pushed (the pre-fix drift signature was version_map["1"] -> remote).
    assert "1" in pub.version_map and pub.version_map["1"] == 1
    assert result["local_version"] == 2
