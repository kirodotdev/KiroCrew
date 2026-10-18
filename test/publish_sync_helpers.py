"""Shared fixtures for ``test_publish_sync.py`` and ``test_publish_sync_upstream.py``.

``DummyPublishProvider`` is the in-test ``PublishProvider``: it answers the canned
response shapes the concrete provider maps and records every call. ``store`` is an
``ArtifactStore`` under the test's tmp dir and ``fake_client`` one such provider; the
autouse ``wire`` points the sync engine at that store and registers the provider under
the default provider name, so ``get_provider`` resolves to it.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import ArtifactStore
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


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture(name="fake_client")
def fake_client_fixture():
    return DummyPublishProvider()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the modules that import it calls monkeypatch.undo()
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
