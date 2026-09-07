"""Tests for the publish sync engine + publication persistence.

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

import asyncio
import json
import os
import re
from pathlib import Path

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

    name = publish_provider.DEFAULT_PROVIDER  # the default provider name publish_sync resolves
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
            "ownerAlias": "alice",
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
        #: What ``serving_notice`` reports on a re-probe. ``None`` means "cannot
        #: re-check" (the interface default); a ``(text, code)`` tuple is the
        #: current live notice (an empty pair meaning the condition has cleared).
        self.serving_notice_response: tuple[str, str] | None = None

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
            notice=str(res.get("notice") or ""),
            notice_code=str(res.get("notice_code") or ""),
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

    async def serving_notice(self, *, external_id) -> tuple[str, str] | None:
        self.calls.append(("serving_notice", {"artifact_id": external_id}))
        return self.serving_notice_response

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
    assert pub.published_by == "alice"
    # uploaded the rendered text content
    upload_args = fake_client.called("upload")[0]
    assert upload_args["title"] == "Doc"
    assert upload_args["visibility"] == "PRIVATE"


@pytest.mark.asyncio
async def test_publish_records_a_provider_notice_without_losing_the_publication(
    store, fake_client
):
    """A destination that stored the content but cannot serve it YET (a CDN mid-rollout)
    must not have to choose between telling the user and keeping the handle. Raising
    would skip `set_publication` entirely, stranding content that is already uploaded
    with nothing to withdraw it by, so the notice rides back on the result. It is a
    SUCCESS status, so it is recorded on `publication.notice` -- never `last_error`,
    which every consumer reads as failure (renders the publish red and withholds the
    URL)."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out; the same link will work shortly",
    }
    art = store.create(name="Doc", content="hello", kind="text")
    summary = await publish_sync.publish(art.slug, visibility="PUBLIC")

    pub = store.get(art.slug).publication
    assert pub is not None, "a notice must never cost the withdrawal handle"
    assert pub.artifact_id == "uuid-123"
    # The notice rides the dedicated non-error field, and last_error stays empty
    # so the publish is NOT rendered as a failure.
    assert "still rolling out" in pub.notice
    assert pub.last_error == ""
    assert summary["artifact_id"] == "uuid-123"


@pytest.mark.asyncio
async def test_publish_notice_appears_on_both_payload_keys(store, fake_client):
    """The API payload must carry the success notice on `notice` and leave
    `last_error` empty, so a frontend gating red on `last_error` shows the URL
    and can surface the rollout line separately."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "CloudFront still rolling out",
    }
    art = store.create(name="Doc", content="hello", kind="text")
    summary = await publish_sync.publish(art.slug, visibility="PUBLIC")

    assert summary["notice"] == "CloudFront still rolling out"
    assert summary["last_error"] == ""


@pytest.mark.asyncio
async def test_a_later_successful_push_does_not_clear_the_serving_notice(store, fake_client):
    """This previously asserted the opposite, on the premise that "a later successful push
    settles the rollout question". It does not.

    A push writes bytes to the OBJECT STORE. The notice describes the DELIVERY NETWORK --
    whether the distribution has finished rolling out, or is disabled. Those are
    independent: puts succeed normally against a distribution that is still InProgress or
    switched off, so a successful push is no evidence at all about the condition the
    notice reports. Clearing on that inference hid a true warning while the link still did
    not resolve. Only ``reprobe_notice``, which asks the destination, may clear it.
    """
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PUBLIC")
    assert store.get("d").publication.notice == "still rolling out"

    store.update("d", content="v2 content", snapshot=True)
    await publish_sync.push_version(store.get("d"))

    pub = store.get("d").publication
    assert pub.notice == "still rolling out"
    # `last_error` DOES clear: it described this operation, and this operation succeeded.
    assert pub.last_error == ""


@pytest.mark.asyncio
async def test_an_overwrite_does_not_clear_the_serving_notice(store, fake_client):
    """Third site of one bug, and the reasoning is identical to the push path.

    An overwrite force-pushes local bytes over the remote's current version. That is an
    OBJECT STORE operation; the notice describes the DELIVERY NETWORK -- still rolling
    out, or switched off -- and the overwrite probes neither. Blanking the notice here
    asserted "the link works now" on the strength of an operation that never asked, which
    is the same false premise the push site had. Only `reprobe_notice` may clear it.
    """
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PUBLIC")
    assert store.get("d").publication.notice == "still rolling out"

    store.update("d", content="v2 content", snapshot=True)
    await publish_sync.overwrite_upstream("d")

    pub = store.get("d").publication
    assert pub.notice == "still rolling out"
    # `last_error` still clears: that one DID describe this operation.
    assert pub.last_error == ""


@pytest.mark.asyncio
async def test_later_successful_visibility_change_clears_a_stale_notice(store, fake_client):
    """A visibility change reconciles the publication and settles the rollout
    question, so it too clears a stale notice."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PRIVATE")
    assert store.get("d").publication.notice == "still rolling out"

    await publish_sync.update_sharing("d", visibility="PUBLIC")

    pub = store.get("d").publication
    assert pub.notice == ""
    assert pub.last_error == ""


# ── notice_code discriminator (B1) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_carries_notice_code_on_payload_and_record(store, fake_client):
    """The publish result's ``notice_code`` must land on the stored publication
    AND the summary payload, alongside the human ``notice`` text."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "The drive's delivery network is DISABLED.",
        "notice_code": "distribution_disabled",
    }
    art = store.create(name="Doc", content="hello", kind="text")
    summary = await publish_sync.publish(art.slug, visibility="PUBLIC")

    assert summary["notice_code"] == "distribution_disabled"
    assert summary["notice"] == "The drive's delivery network is DISABLED."
    pub = store.get(art.slug).publication
    assert pub is not None
    assert pub.notice_code == "distribution_disabled"


@pytest.mark.asyncio
async def test_notice_code_round_trips_through_meta_json(store, fake_client):
    """``notice_code`` must persist to meta.json and deserialize back (additive,
    defaulted — a reload never loses it)."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
        "notice_code": "rolling_out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PUBLIC")

    # Fresh store instance -> forces a real meta.json read, not a cache hit.
    reloaded = ArtifactStore(root=store.root).get("d")
    assert reloaded.publication is not None
    assert reloaded.publication.notice_code == "rolling_out"
    assert reloaded.publication.notice == "still rolling out"


@pytest.mark.asyncio
async def test_notice_code_moves_with_notice_and_a_push_preserves_both(store, fake_client):
    """``notice_code`` moves with ``notice`` -- they never diverge -- and a push preserves
    BOTH rather than clearing them.

    A push re-uploads bytes to the object store; it does not touch the delivery network's
    rollout or enabled state, so it has learned nothing about the condition the notice
    describes. Clearing here hid a still-true warning while the URL was still unavailable.
    Only ``reprobe_notice``, which asks the destination, may clear it.
    """
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
        "notice_code": "rolling_out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PUBLIC")
    assert store.get("d").publication.notice_code == "rolling_out"

    store.update("d", content="v2 content", snapshot=True)
    await publish_sync.push_version(store.get("d"))

    pub = store.get("d").publication
    assert pub.notice == "still rolling out"
    assert pub.notice_code == "rolling_out"


@pytest.mark.asyncio
async def test_notice_code_cleared_with_notice_on_visibility_change(store, fake_client):
    """A visibility change clears both halves of the notice together."""
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
        "notice_code": "rolling_out",
    }
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PRIVATE")
    assert store.get("d").publication.notice_code == "rolling_out"

    await publish_sync.update_sharing("d", visibility="PUBLIC")

    pub = store.get("d").publication
    assert pub.notice == ""
    assert pub.notice_code == ""


# ── reprobe_notice (B2) ─────────────────────────────────────────────────────


async def _publish_with_rolling_out_notice(store, fake_client, slug="d"):
    fake_client.upload_response = {
        **fake_client.upload_response,
        "notice": "still rolling out",
        "notice_code": "rolling_out",
    }
    store.create(name="Doc", content="v1", kind="text", slug=slug)
    await publish_sync.publish(slug, visibility="PUBLIC")
    assert store.get(slug).publication.notice_code == "rolling_out"


@pytest.mark.asyncio
async def test_reprobe_clears_notice_when_destination_now_healthy(store, fake_client):
    """A re-probe against a destination that has finished rolling out (provider
    reports an EMPTY pair) clears both ``notice`` and ``notice_code``."""
    await _publish_with_rolling_out_notice(store, fake_client)

    fake_client.serving_notice_response = ("", "")
    await publish_sync.reprobe_notice("d")

    pub = store.get("d").publication
    assert pub.notice == ""
    assert pub.notice_code == ""
    # It actually probed the provider (not cleared on read without checking).
    assert fake_client.called("serving_notice")


@pytest.mark.asyncio
async def test_reprobe_leaves_notice_when_still_unhealthy(store, fake_client):
    """A re-probe against a destination that is still unhealthy leaves the
    notice in place (refreshed to the provider's current answer)."""
    await _publish_with_rolling_out_notice(store, fake_client)

    fake_client.serving_notice_response = (
        "The drive's delivery network is DISABLED.",
        "distribution_disabled",
    )
    await publish_sync.reprobe_notice("d")

    pub = store.get("d").publication
    assert pub.notice_code == "distribution_disabled"
    assert pub.notice == "The drive's delivery network is DISABLED."


@pytest.mark.asyncio
async def test_reprobe_leaves_notice_when_provider_cannot_recheck(store, fake_client):
    """When the provider cannot re-check (``serving_notice`` returns ``None``),
    the stored notice must be left exactly as it was — never cleared unverified."""
    await _publish_with_rolling_out_notice(store, fake_client)

    fake_client.serving_notice_response = None
    await publish_sync.reprobe_notice("d")

    pub = store.get("d").publication
    assert pub.notice == "still rolling out"
    assert pub.notice_code == "rolling_out"


@pytest.mark.asyncio
async def test_reprobe_is_noop_when_no_notice(store, fake_client):
    """A publication with no notice never probes the provider or writes."""
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", visibility="PUBLIC")
    assert store.get("d").publication.notice == ""

    await publish_sync.reprobe_notice("d")

    assert not fake_client.called("serving_notice")


@pytest.mark.asyncio
async def test_reprobe_leaves_notice_when_provider_unavailable(store, fake_client):
    """An unavailable provider is treated as "cannot re-check": the notice is
    left in place rather than cleared."""
    await _publish_with_rolling_out_notice(store, fake_client)

    fake_client.ready = False
    fake_client.serving_notice_response = ("", "")  # would clear IF it were consulted
    await publish_sync.reprobe_notice("d")

    pub = store.get("d").publication
    assert pub.notice == "still rolling out"
    assert pub.notice_code == "rolling_out"
    assert not fake_client.called("serving_notice")


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
async def test_push_version_re_pushes_widget_on_wrapper_revision_bump(store, fake_client, monkeypatch):
    """A widget with stale wrapper_revision is re-pushed even if content version matches (#3373)."""
    store.create(name="Widget", content="<p>hi</p>", kind="widget", slug="w")
    await publish_sync.publish("w")
    art = store.get("w")
    assert art.publication.last_synced_kirocrew_version == 1
    assert art.publication.wrapper_revision == publish_sync.WRAPPER_REVISION

    # Simulate a wrapper bump (CSP tightened) — revision goes up by 1.
    monkeypatch.setattr(publish_sync, "WRAPPER_REVISION", publish_sync.WRAPPER_REVISION + 1)
    fake_client.calls.clear()

    # Same artifact version, but wrapper is stale → should re-push.
    await publish_sync.push_version(store.get("w"))
    assert len(fake_client.called("upload_version")) == 1
    # After push, wrapper_revision is updated.
    art = store.get("w")
    assert art.publication.wrapper_revision == publish_sync.WRAPPER_REVISION


@pytest.mark.asyncio
async def test_push_version_skips_non_widget_on_wrapper_revision_bump(store, fake_client, monkeypatch):
    """Non-widget artifacts ignore wrapper_revision — only widgets wrap with CSP (#3373)."""
    store.create(name="Doc", content="hello", kind="text", slug="t")
    await publish_sync.publish("t")
    monkeypatch.setattr(publish_sync, "WRAPPER_REVISION", publish_sync.WRAPPER_REVISION + 1)
    fake_client.calls.clear()

    await publish_sync.push_version(store.get("t"))
    # Text artifact → no wrapper staleness → skip.
    assert fake_client.called("upload_version") == []


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
async def test_unpublish_keeps_the_publication_when_the_destination_fails(store, fake_client):
    """The publication block is the only handle to content that may still be served, so
    a failed removal must keep it rather than strand that content with no retry path."""
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    fake_client.delete_response = {"error": "Throttling: rate exceeded"}
    with pytest.raises(publish_sync.PublishError):
        await publish_sync.unpublish("d")
    assert store.get("d").publication is not None


@pytest.mark.asyncio
async def test_unpublish_keeps_the_publication_when_the_destination_is_unreachable(store, fake_client, monkeypatch):
    """Same invariant through the other mouth: an unavailable destination skips the
    delete entirely, so clearing the record there loses the handle just as silently."""
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    monkeypatch.setattr(type(fake_client), "available", lambda self: False)
    with pytest.raises(publish_sync.PublishUnavailableError):
        await publish_sync.unpublish("d")
    assert store.get("d").publication is not None


@pytest.mark.asyncio
async def test_going_public_fills_a_link_the_private_publish_could_not_derive(store, fake_client, monkeypatch):
    """A destination whose link is derived can only produce one once the object is
    served, so a private publish stores none. Nothing else revisits the field, so
    without this the artifact ends up public with no link and no way to get one."""
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    store.update_publication("d", view_url="", visibility="PRIVATE")
    monkeypatch.setattr(
        type(fake_client), "view_url_for", lambda self, external_id: f"https://drive/{external_id}"
    )
    await publish_sync.update_sharing("d", visibility="PUBLIC")
    assert store.get("d").publication.view_url == "https://drive/uuid-123"


@pytest.mark.asyncio
async def test_going_public_never_replaces_a_link_the_destination_returned(store, fake_client, monkeypatch):
    """Additive only: a destination that returned its own url at publish time keeps it,
    so this can add a link but never corrupt one it did not create."""
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    store.update_publication("d", view_url="https://remote/original", visibility="PRIVATE")
    monkeypatch.setattr(
        type(fake_client), "view_url_for", lambda self, external_id: "https://drive/derived"
    )
    await publish_sync.update_sharing("d", visibility="PUBLIC")
    assert store.get("d").publication.view_url == "https://remote/original"


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
    # subsequent sharing update (which clears last_error). review-bot regression.
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d")
    store.update("d", content="v2", snapshot=True)  # new KiroCrew version
    fake_client.upload_version_response = {"error": "expected sha mismatch"}
    result = await publish_sync.publish("d", visibility="PUBLIC")
    assert "conflict" in result["last_error"].lower()
    assert "conflict" in (store.get("d").publication.last_error or "").lower()


@pytest.mark.asyncio
async def test_delete_for_artifact_withdraws_and_reports_withdrawn(store, fake_client):
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    art = store.get("d")
    result = await publish_sync.delete_for_artifact(art)
    assert result is publish_sync.DeleteWithdrawal.WITHDRAWN
    assert fake_client.called("delete")[0]["artifact_id"] == "uuid-123"
    # store untouched (the caller deletes locally afterwards)
    assert store.get("d").publication is not None


@pytest.mark.asyncio
async def test_concurrent_first_publishes_mint_exactly_one_destination_id(
    store, fake_client, monkeypatch
):
    """The race: two concurrent FIRST publishes of one artifact each minted their own
    destination id, each uploaded a distinct public object, and whichever publication
    record was written second replaced the first -- leaving a world-readable copy whose
    handle was recorded nowhere, so nothing could ever withdraw it.

    Serializing per artifact in the ENGINE is what closes it, because the engine holds the
    identity the provider lacks: `PublishProvider.publish()` gets a rendered tempfile and
    no artifact id, so the provider's own per-id lock cannot help -- the id does not exist
    until it mints one, and two racers mint two.

    The assertion is on the count of provider publish CALLS, not just the final record: a
    record can look fine while a second object was already uploaded and orphaned.
    """
    calls: list[str] = []
    real_publish = type(fake_client).publish

    async def _counting_publish(self, **kw):
        # Yield inside the critical section so an unserialized second caller would
        # interleave here -- without the lock this is what lets both mint an id.
        await asyncio.sleep(0)
        res = await real_publish(self, **kw)
        calls.append(res.external_id)
        return res

    monkeypatch.setattr(type(fake_client), "publish", _counting_publish)
    store.create(name="Doc", content="x", kind="text", slug="d")

    await asyncio.gather(
        publish_sync.publish("d", visibility="PUBLIC"),
        publish_sync.publish("d", visibility="PUBLIC"),
    )

    # Exactly ONE object was created at the destination, so there is no second copy to
    # strand, and the surviving record names the id that was actually uploaded.
    assert len(calls) == 1, f"{len(calls)} destination objects created: {calls!r}"
    assert store.get("d").publication.artifact_id == calls[0]


@pytest.mark.asyncio
async def test_a_typed_drive_not_found_is_the_only_confirmed_gone_answer(
    store, fake_client, monkeypatch
):
    """`DriveNotFound` -- the TYPE -- is what says "there is nothing left to withdraw".

    Confirmed absence is the one failure that may let a delete through, so it must be
    unmistakable. Raised as a type, it survives rewording, translation and provider-
    specific phrasing.
    """
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    art = store.get("d")

    async def _gone(self, *, external_id):
        raise publish_sync.DriveNotFound("this account's drive could not be found")

    # Patched on the CLASS, so the stub is a bound method and must accept `self` --
    # a signature mismatch here would raise TypeError, land in the generic failure
    # branch, and make this test pass without the stub ever running.
    monkeypatch.setattr(type(fake_client), "unpublish", _gone, raising=False)
    result = await publish_sync.delete_for_artifact(art)
    assert result is publish_sync.DeleteWithdrawal.NOTHING_PUBLISHED


@pytest.mark.asyncio
async def test_a_confirmed_gone_destination_lets_the_unpublish_complete(
    store, fake_client, monkeypatch
):
    """`reachable_for` resolves the PROFILE, not the drive, so a deleted drive under a
    still-registered profile passes that guard and then raises inside the provider.

    Reported as retryable, that artifact would be stuck for good: unpublish would loop
    forever and the delete path refuses on the same destination, so neither exit works.
    Confirmed absence is the one outcome that may release the record -- the same rule the
    delete path follows, and it comes from the TYPE.
    """
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")

    async def _gone(self, *, external_id):
        raise publish_sync.DriveNotFound("this account's drive could not be found")

    # Bound-method signature: see the note on the sibling test above.
    monkeypatch.setattr(type(fake_client), "unpublish", _gone, raising=False)
    await publish_sync.unpublish("d")
    # Released: nothing is left to withdraw, so the handle protects nothing.
    assert store.get("d").publication is None


@pytest.mark.asyncio
async def test_an_unreachable_unpublish_does_not_offer_delete_as_the_way_out(
    store, fake_client, monkeypatch
):
    """The refusal message must not name an action that also refuses.

    It used to say "if it is gone, delete the artifact to drop the record along with it"
    -- true before the delete path began refusing on an unwithdrawn copy, and false
    after. A message naming a guaranteed-failing remedy is worse than naming none.
    """
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    monkeypatch.setattr(
        type(fake_client), "reachable_for", lambda self, *, external_id: False, raising=False
    )
    with pytest.raises(publish_sync.PublishUnavailableError) as caught:
        await publish_sync.unpublish("d")
    msg = str(caught.value)
    assert "delete the artifact to drop the record" not in msg
    assert "Restore access" in msg
    # The record is kept: it is the only handle to a copy that may still be served.
    assert store.get("d").publication is not None


@pytest.mark.asyncio
async def test_a_not_found_LOOKING_message_is_still_a_withdrawal_failure(
    store, fake_client, monkeypatch
):
    """The anti-sniffing pin, and the reason the type exists.

    This failure's message contains every marker a substring check would look for --
    "NoSuchBucket", "404", "Not Found" -- but it is NOT the typed confirmation, so it must
    NOT be read as "gone". A throttled or unauthorized reply can carry that text while the
    object is still served to the whole internet; treating it as absence would delete the
    only handle able to withdraw a live public copy. It is a plain FAILED: retryable, and
    the caller keeps the record.
    """
    store.create(name="Doc", content="x", kind="text", slug="d")
    await publish_sync.publish("d")
    art = store.get("d")

    async def _looks_gone(self, *, external_id):
        raise RuntimeError("An error occurred (NoSuchBucket) 404 Not Found: rate exceeded")

    monkeypatch.setattr(type(fake_client), "unpublish", _looks_gone, raising=False)
    result = await publish_sync.delete_for_artifact(art)
    assert result is publish_sync.DeleteWithdrawal.FAILED


@pytest.mark.asyncio
async def test_delete_for_artifact_reports_nothing_published_when_unpublished(store):
    """An artifact with no publication has nothing to withdraw -- the caller
    deletes it unchanged."""
    store.create(name="Doc", content="x", kind="text", slug="d")
    art = store.get("d")
    assert art.publication is None
    result = await publish_sync.delete_for_artifact(art)
    assert result is publish_sync.DeleteWithdrawal.NOTHING_PUBLISHED


@pytest.mark.asyncio
async def test_delete_for_artifact_reports_unreachable_on_an_unknown_provider(store):
    """A publication naming a destination this edition does not register cannot be
    reached, so no retry from here is meaningful: the outcome is UNREACHABLE (the
    escape-hatch case), NOT FAILED, and nothing raises -- provider resolution is the
    case the guard used to miss (it raised before the try block was entered)."""
    store.create(name="Doc", content="x", kind="text", slug="gone")
    art = store.get("gone")
    art.publication = ArtifactPublication(
        provider="a-destination-this-edition-does-not-have",
        artifact_id="uuid-999",
        view_url="",
        visibility="PUBLIC",
    )
    result = await publish_sync.delete_for_artifact(art)  # must not raise
    assert result is publish_sync.DeleteWithdrawal.UNREACHABLE


@pytest.mark.asyncio
async def test_delete_for_artifact_reports_unreachable_when_destination_is_down(
    store, fake_client, monkeypatch
):
    """A registered destination whose ``available()`` is False is gone-for-good as far
    as this process can tell (offline / credentials revoked / account closed): no call
    is attempted and no retry from here can reach it, so the outcome is UNREACHABLE and
    the caller proceeds with the local delete."""
    store.create(name="Doc", content="x", kind="text", slug="d3")
    await publish_sync.publish("d3")
    art = store.get("d3")
    monkeypatch.setattr(type(fake_client), "available", lambda self: False)
    result = await publish_sync.delete_for_artifact(art)
    assert result is publish_sync.DeleteWithdrawal.UNREACHABLE
    # No withdrawal was attempted against the unreachable destination.
    assert not fake_client.called("delete")


@pytest.mark.asyncio
async def test_delete_for_artifact_reports_failed_when_reachable_destination_rejects(
    store, fake_client
):
    """A reachable destination that REJECTS the withdrawal yields FAILED -- a retry can
    plausibly succeed, so the caller must keep the publication (the only retry handle).
    It still never raises: the outcome is a value, not an exception."""
    store.create(name="Doc", content="x", kind="text", slug="d2")
    await publish_sync.publish("d2")
    art = store.get("d2")
    fake_client.delete_response = {"error": "the destination refused the delete"}
    result = await publish_sync.delete_for_artifact(art)  # must not raise
    assert result is publish_sync.DeleteWithdrawal.FAILED


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
        published_by="alice",
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
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    raw.pop("publication", None)
    meta_path.write_text(json.dumps(raw))
    assert store.get("d").publication is None


def test_tolerant_load_non_numeric_last_synced(tmp_path):
    # A corrupted/hand-edited meta.json with a non-numeric version must not
    # crash the load — it falls back to 0 (review-bot: tolerant-load contract).
    store = ArtifactStore(root=tmp_path / "a")
    store.create(name="Doc", content="x", kind="text", slug="d")
    meta_path = tmp_path / "a" / "d" / "meta.json"
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
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
    # No external script is fetched: the Tailwind runtime is inlined from the
    # staged bundle, so the document renders with no network egress.
    assert "<script src=" not in html
    assert "cdn.tailwindcss.com" not in html
    # MCP auto-injects <base>; the wrapper must not add it.
    assert "<base" not in html


def _csp_directives(csp: str) -> dict[str, set[str]]:
    """Parse a CSP string into ``{directive: {source tokens}}``.

    Asserting against parsed tokens rather than searching the raw string is
    exact: a substring hit says nothing about WHICH directive carries a token,
    so `'unsafe-inline'` in style-src would satisfy a naive check aimed at
    script-src.
    """
    directives: dict[str, set[str]] = {}
    for part in csp.split(";"):
        fields = part.split()
        if fields:
            directives[fields[0]] = set(fields[1:])
    return directives


def test_published_csp_grants_no_eval():
    csp = _csp_directives(publish_sync._CSP)
    # The published CSP must not hand widget JS a dynamic-exec primitive. The
    # vendored Tailwind v4 runtime needs no eval, so the token has no purpose
    # here and its presence would undo the containment of LLM-authored inline
    # scripts.
    assert "'unsafe-eval'" not in csp["script-src"]
    # Pinned exactly, not by membership: the Play CDN that required eval is gone
    # from both directives that named it, and any future source added to either
    # one fails here rather than widening the policy silently. What survives is
    # inline widget bodies plus the two CDNs widget-authored Chart.js/D3 load
    # from; Tailwind v4 emits CSS as inline <style>, so style-src needs no CDN.
    assert csp["script-src"] == {
        "'unsafe-inline'",
        "https://cdn.jsdelivr.net",
        "https://cdnjs.cloudflare.com",
    }
    assert csp["style-src"] == {"'unsafe-inline'"}
    # And the containment the accepted inline risk rests on.
    assert csp["default-src"] == {"'none'"}
    assert csp["connect-src"] == {"'none'"}
    assert csp["form-action"] == {"'none'"}
    assert csp["base-uri"] == {"'none'"}


def test_wrap_widget_html_inlines_the_staged_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "tailwindcss-browser.js"
    runtime.write_text("/*tw-runtime*/globalThis.__tw=1;", encoding="utf-8")
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", runtime)

    html = publish_sync.wrap_widget_html("<p>x</p>")
    assert "<script>/*tw-runtime*/globalThis.__tw=1;</script>" in html
    # Inlined, not linked — an external viewer has no dashboard origin to resolve.
    assert "<script src=" not in html


def test_wrap_widget_html_without_staged_runtime_does_not_fall_back_to_cdn(tmp_path, monkeypatch):
    # An unbuilt source checkout has no staged bundle. The document must degrade
    # to unstyled utility classes rather than reintroduce the Play CDN, which
    # would require the 'unsafe-eval' the CSP no longer grants.
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", tmp_path / "absent.js")

    html = publish_sync.wrap_widget_html("<p>x</p>")
    assert "cdn.tailwindcss.com" not in html
    assert "<script" not in html
    # Still a well-formed document carrying the widget and the eval-free CSP.
    assert html.startswith("<!DOCTYPE html>")
    assert "<p>x</p>" in html
    assert "'unsafe-eval'" not in html


@pytest.mark.parametrize("close_tag", ["</script>", "</SCRIPT>", "</Script >", "</sCrIpT/"])
def test_wrap_widget_html_neutralises_script_close_in_runtime(close_tag, tmp_path, monkeypatch):
    # Guard against a dependency bump shipping a terminator in a string literal.
    # HTML tokenization matches `</script` in ANY ascii casing, so a
    # lowercase-only escape would let `</SCRIPT>` close the tag early and dump
    # the remainder of the bundle into the document body as text.
    runtime = tmp_path / "tailwindcss-browser.js"
    runtime.write_text(f'var s="{close_tag}";', encoding="utf-8")
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", runtime)

    html = publish_sync.wrap_widget_html("<p>x</p>")
    # Neutralised, and the tag name keeps its original casing so the string the
    # browser reconstructs is byte-identical to the bundle's.
    assert f'"<\\/{close_tag[2:]}";' in html
    # Exactly one script element: the runtime's own, not a truncated one plus
    # leaked bundle text.
    assert html.count("</script>") == 1


@pytest.mark.parametrize("lookalike", ["</ſcript>", "</scrıpt>", "</scrİpt>"])
def test_wrap_widget_html_leaves_non_ascii_lookalikes_untouched(lookalike, tmp_path, monkeypatch):
    # The terminator escape folds case only over ASCII. Python's Unicode
    # IGNORECASE maps U+017F onto `s` and U+0131/U+0130 onto `i`, but the HTML
    # tokenizer does not — so escaping these would insert a backslash the source
    # never had, corrupting a raw string literal for no security gain.
    runtime = tmp_path / "tailwindcss-browser.js"
    runtime.write_text(f'var s=String.raw`{lookalike}`;', encoding="utf-8")
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", runtime)

    html = publish_sync.wrap_widget_html("<p>x</p>")
    assert lookalike in html
    assert "<\\/" not in html


def test_tailwind_runtime_filename_matches_the_frontend_emit_target():
    # The runtime path has two owners in two languages: vendorPaths.ts declares
    # the URL path vite emits to, and _TAILWIND_RUNTIME_FILE reads the emitted
    # file off disk. They fail asymmetrically — a rename breaks the dashboard
    # loudly (404 + frontend test) but degrades publishing to a log warning and
    # an unstyled document. This turns that silent drift into a red test.
    vendor_paths = Path(__file__).resolve().parents[1] / "website" / "src" / "lib" / "vendorPaths.ts"
    declared = re.search(
        r"TAILWIND_RUNTIME_PATH\s*=\s*'([^']+)'", vendor_paths.read_text(encoding="utf-8")
    )
    assert declared, "vendorPaths.ts no longer declares TAILWIND_RUNTIME_PATH"
    assert publish_sync._TAILWIND_RUNTIME_FILE.name == declared.group(1).rsplit("/", 1)[-1]


def test_wrap_widget_html_round_trips_with_runtime_inlined(tmp_path, monkeypatch):
    # The inlined bundle sits between the CSP meta and the body sentinels; the
    # sentinel scan must still recover the exact inner fragment.
    runtime = tmp_path / "tailwindcss-browser.js"
    runtime.write_text("/*tw*/", encoding="utf-8")
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", runtime)

    inner = "<div class='grid gap-2'>round trip</div>"
    assert publish_sync.unwrap_widget_html(publish_sync.wrap_widget_html(inner)) == inner


def test_tailwind_runtime_js_returns_empty_on_unreadable_asset(tmp_path, monkeypatch):
    # A directory at the asset path raises OSError on read — the helper reports
    # it and returns empty rather than propagating into the publish call.
    monkeypatch.setattr(publish_sync, "_TAILWIND_RUNTIME_FILE", tmp_path)
    assert publish_sync._tailwind_runtime_js() == ""


def test_redact_untrusted_scans_every_source():
    # Regression (PR #14 alice): the `manual` source is no longer a redaction
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
    assert refreshed.publication.last_error.startswith("The remote copy changed outside Kiro Crew")


@pytest.mark.asyncio
async def test_refresh_clears_drift_when_reconciled(store, fake_client):
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    store.update_publication(
        art.slug, last_error="The remote copy changed outside Kiro Crew: it is showing v9."
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


@pytest.mark.asyncio
async def test_refresh_clears_drift_written_before_the_brand_rename(store, fake_client):
    """A drift note persisted under the OLD product spelling still clears.

    The prefix is a persisted sentinel, not just display text: it is written into
    ``last_error`` and matched back on the next reconcile. Publications created
    before the display name gained its space carry the unspaced brand, so matching
    only the current spelling would strand them -- the remote reconciles and the
    banner never clears. Without the legacy prefix in ``_DRIFT_PREFIXES`` this
    asserts ``last_error == ""`` against the untouched legacy string and fails.
    """
    art = store.create(name="Doc", content="hello", kind="text")
    await publish_sync.publish(art.slug, visibility="PRIVATE")
    store.update_publication(
        art.slug, last_error="The remote copy changed outside KiroCrew: it is showing v9."
    )
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
    tmp_path, content, *, version, sha, owner="alice", ctype="text/plain", shared=None, shared_v2=None
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
    (the old blanket suppression made published working files
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
    assert src.read_text(encoding="utf-8") == "collab edit"  # write-through to the working file
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
    assert src.read_text(encoding="utf-8") == "collab edit"  # pulled content reached the file
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
    assert src.read_text(encoding="utf-8") == "my file content"  # local file untouched


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
async def test_upstream_status_reports_local_ahead_on_wrapper_revision_bump(store, fake_client, tmp_path, monkeypatch):
    """Widget with stale wrapper_revision shows local_ahead even if content hasn't changed (#3373)."""
    art = store.create(name="W", content="<p>x</p>", kind="widget")
    _track_publication(store, art.slug)
    # Simulate wrapper bump.
    monkeypatch.setattr(publish_sync, "WRAPPER_REVISION", publish_sync.WRAPPER_REVISION + 1)
    fake_client.get_response = _remote_get(tmp_path, "ignored", version=1, sha="sha-v1")
    status = await publish_sync.upstream_status(art.slug)
    assert status["local_ahead"] is True


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


# ── the withdrawal paths ask about THIS publication's account ───────────────────
#
# `available()` is a destination-WIDE question ("is this kind of destination
# configured at all"), which is what makes it right for deciding whether to offer a
# new publish. A publication is bound to ONE account, so with two registered and the
# bound one removed the wide answer stays True while every call for this artifact
# raises -- and the raise was then read as a retryable rejection. The result was an
# artifact that could be neither withdrawn nor deleted, only told to retry forever.
# These pin the call sites to the narrow question by making the two answers DISAGREE:
# `available()` True, `reachable_for()` False.


@pytest.mark.asyncio
async def test_delete_asks_whether_this_publications_account_is_reachable(
    store, fake_client, monkeypatch
):
    store.create(name="Doc", content="x", kind="text", slug="d-bound")
    await publish_sync.publish("d-bound")
    art = store.get("d-bound")
    # The destination is configured (another account remains) but THIS artifact's is gone.
    monkeypatch.setattr(type(fake_client), "reachable_for", lambda self, *, external_id: False)
    assert fake_client.available() is True
    result = await publish_sync.delete_for_artifact(art)
    # UNREACHABLE, not FAILED: a removed account is permanent, so "retry" is a lie.
    assert result is publish_sync.DeleteWithdrawal.UNREACHABLE
    assert not fake_client.called("delete")


@pytest.mark.asyncio
async def test_unpublish_asks_whether_this_publications_account_is_reachable(
    store, fake_client, monkeypatch
):
    store.create(name="Doc", content="x", kind="text", slug="u-bound")
    await publish_sync.publish("u-bound")
    monkeypatch.setattr(type(fake_client), "reachable_for", lambda self, *, external_id: False)
    assert fake_client.available() is True
    with pytest.raises(publish_sync.PublishUnavailableError) as exc:
        await publish_sync.unpublish("u-bound")
    # The copy is kept and the message must not promise a retry will fix it. It used to
    # name deleting the artifact as the exit for the gone-account case; that exit no
    # longer exists, because the delete path refuses on this same destination, so the
    # message must not send the user at it. See the dedicated test above.
    assert "Restore access" in str(exc.value)
    assert "delete the artifact to drop the record" not in str(exc.value)
    assert not fake_client.called("delete")
    assert store.get("u-bound").publication is not None


@pytest.mark.asyncio
async def test_reachable_for_defaults_to_the_destination_wide_answer(fake_client):
    """Providers that bind nothing per publication must not have to implement it."""
    fake_client.ready = False
    assert fake_client.reachable_for(external_id="anything") is False
    fake_client.ready = True
    assert fake_client.reachable_for(external_id="anything") is True


# ── kinds this text pipeline cannot carry are refused, not published empty ──────


@pytest.mark.asyncio
async def test_publishing_an_image_artifact_is_refused_not_published_empty(store, fake_client):
    """`kind="image"` keeps its raster at `source_path` and carries no text body, so the
    text pipeline rendered it as "" and shipped a ZERO-BYTE object -- then recorded the
    publish as a success and handed back a link. A refusal is the honest outcome; an
    empty object reporting success is indistinguishable downstream from a real publish.
    """
    store.create(name="Shot", content="", kind="image", slug="img1")
    with pytest.raises(publish_sync.ArtifactValidationError) as exc:
        await publish_sync.publish("img1")
    assert "empty file" in str(exc.value)
    # Nothing was uploaded and nothing was recorded as published.
    assert not fake_client.called("upload")
    assert store.get("img1").publication is None


@pytest.mark.asyncio
async def test_text_kinds_still_publish(store, fake_client):
    """The refusal is scoped to the non-text kinds -- the ordinary path is untouched."""
    store.create(name="Doc", content="hello", kind="markdown", slug="ok1")
    await publish_sync.publish("ok1")
    assert store.get("ok1").publication is not None


@pytest.mark.asyncio
async def test_reprobe_leaves_notice_when_no_provider_is_registered(store, fake_client, monkeypatch):
    """`reprobe_notice` documents that an unavailable provider leaves the notice as it
    was. Resolution failing is the strongest form of unavailable, so it must take that
    path rather than raising -- the handler turns a raise into a 503 on a read-only
    reconcile the caller can do nothing about.
    """
    store.create(name="Doc", content="x", kind="text", slug="np1")
    await publish_sync.publish("np1")
    store.update_publication("np1", notice="still rolling out", notice_code="rolling_out")

    def _no_provider(name: str):
        raise publish_sync.PublishUnavailableError("no provider registered")

    monkeypatch.setattr(publish_sync, "_resolve_provider", _no_provider)
    art = await publish_sync.reprobe_notice("np1")  # must not raise
    assert art.publication is not None
    assert art.publication.notice == "still rolling out"
    assert art.publication.notice_code == "rolling_out"


@pytest.mark.asyncio
async def test_publish_redacts_the_providers_notice_before_persisting_it(store, fake_client):
    """`notice` is provider-controlled text that is persisted and then served to the
    dashboard, so it is scrubbed at the sink like every other provider-derived string
    that reaches the UI. The re-probe path already did this; the publish path -- where a
    notice is FIRST recorded -- did not, which is the sink that matters most.

    The payload is a CREDENTIAL pattern rather than a bare URL on purpose: measured
    against the real primitives, `redact_exfiltration_urls` leaves a plain
    `https://host/path` untouched while `redact_credentials` scrubs key-shaped strings,
    so a URL-based assertion here would pass whether or not the sink redacts anything.
    """
    fake_client.upload_response = dict(
        fake_client.upload_response,
        # AWS's own documentation example key -- not a live credential.
        notice="rolling out; debug key AKIAIOSFODNN7EXAMPLE",
        notice_code="rolling_out",
    )
    store.create(name="Doc", content="x", kind="text", slug="red1")
    await publish_sync.publish("red1")
    stored = store.get("red1").publication
    assert stored is not None
    # The key must not survive into the record the dashboard reads.
    assert "AKIAIOSFODNN7EXAMPLE" not in stored.notice
    assert "REDACTED" in stored.notice
    # The discriminator is a constrained enum, so it passes through untouched.
    assert stored.notice_code == "rolling_out"
