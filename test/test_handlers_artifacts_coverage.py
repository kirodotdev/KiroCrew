"""Coverage-focused tests for :mod:`kiro_crew.dashboard.handlers.artifacts`.

Targets the error/validation/not-found branches that the existing suites
(``test_artifacts_handlers.py``, ``test_remote_artifacts.py``) leave uncovered:

* the version / lifecycle-event endpoints (invalid version, version miss,
  ``type`` and ``metadata`` validation, restricted-session denial),
* ``POST /api/artifacts/{slug}/publish/refresh`` (403 / 404 / 400 / success),
* the remote-artifact comment quartet — capability rejection, provider error,
  provider timeout, body validation and the success payloads,
* the remote-comment TTL/LRU cache helpers and the redaction helpers'
  depth-cap + base64 branches.

Harness is the established one: MagicMock aiohttp requests (the
``test_artifacts_handlers.py`` pattern), a real :class:`ArtifactStore` rooted at
``tmp_path``, and an in-test :class:`PublishProvider` registered into the
(normally empty) public registry. No network, no subprocesses, no real provider.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew import publish_provider
from kiro_crew.artifacts import (
    ArtifactError,
    ArtifactFolderStore,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
)
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    _id_embeds_hard_credential,
    _redact_remote_response,
    _remote_cache_put,
    _remote_cache_sweep,
    _serialize_remote_comment,
    api_artifact_events,
    api_artifact_folder_delete,
    api_artifact_folder_update,
    api_artifact_record_event,
    api_artifact_refresh_sharing,
    api_artifact_set_folder,
    api_artifact_set_pinned,
    api_artifact_version_detail,
    api_artifact_versions,
    api_remote_artifact_comments,
    api_remote_artifact_delete_comment,
    api_remote_artifact_get,
    api_remote_artifact_mark_review,
    api_remote_artifact_post_comment,
    api_remote_artifact_reply_comment,
)
from kiro_crew.publish_provider import Capability, CommentAnchor, PublishProvider, RemoteComment

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """Real ArtifactStore under tmp_path, wired as the process default."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    session_key: str | None = "dashboard:test",
    restricted: bool = False,
    no_state: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """MagicMock aiohttp Request shaped for these handlers."""
    req = MagicMock()
    headers: dict[str, str] = {}
    if session_key is not None:
        headers["X-Session-Key"] = session_key
    if extra_headers:
        headers.update(extra_headers)
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    req.rel_url.query = query or {}
    if isinstance(body, dict):
        req.read = AsyncMock(return_value=json.dumps(body).encode())
    elif isinstance(body, bytes):
        req.read = AsyncMock(return_value=body)
    else:
        req.read = AsyncMock(return_value=b"")
    state = MagicMock()
    state.get_slot.return_value = None
    req.app = {
        "state": None if no_state else state,
        "_restricted_session": restricted,
    }
    return req


@pytest.fixture
def patch_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make _is_restricted_session read req.app['_restricted_session']."""

    def _stub(_state: Any, req: Any) -> bool:
        return bool(req.app.get("_restricted_session", False))

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


@pytest.fixture
def capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Collect SEL events instead of writing them."""
    events: list[dict] = []
    monkeypatch.setattr(art_handlers, "_audit", lambda **kw: events.append(kw))
    return events


def _json_body(resp: Any) -> dict:
    return json.loads(resp.body)


# ── Versions ────────────────────────────────────────────────────────────────


class TestVersionEndpoints:
    @pytest.mark.asyncio
    async def test_versions_lists_every_snapshot(self, isolated_store: ArtifactStore) -> None:
        art = isolated_store.create(name="Doc", content="v1", kind="markdown")
        # Only an explicit snapshot mints a new numbered version (the default
        # silent save keeps the version dropdown unchanged).
        isolated_store.update(art.slug, content="v2", snapshot=True)
        resp = await api_artifact_versions(_request(match={"slug": art.slug}))
        assert resp.status == 200
        assert _json_body(resp)["versions"] == [1, 2]

    @pytest.mark.asyncio
    async def test_versions_unknown_slug_404(self, isolated_store: ArtifactStore) -> None:
        resp = await api_artifact_versions(_request(match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_versions_invalid_slug_400(self, isolated_store: ArtifactStore) -> None:
        # A slug that fails store-side validation is a 400, not a 404.
        resp = await api_artifact_versions(_request(match={"slug": "NOT A SLUG"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_version_detail_non_numeric_version_400(
        self, isolated_store: ArtifactStore
    ) -> None:
        art = isolated_store.create(name="Doc", content="v1", kind="markdown")
        resp = await api_artifact_version_detail(
            _request(match={"slug": art.slug, "version": "latest"})
        )
        assert resp.status == 400
        assert "invalid version" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_version_detail_version_miss_404(self, isolated_store: ArtifactStore) -> None:
        art = isolated_store.create(name="Doc", content="v1", kind="markdown")
        resp = await api_artifact_version_detail(
            _request(match={"slug": art.slug, "version": "99"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_version_detail_returns_historical_content(
        self, isolated_store: ArtifactStore
    ) -> None:
        art = isolated_store.create(name="Doc", content="first", kind="markdown")
        isolated_store.update(art.slug, content="second", snapshot=True)
        resp = await api_artifact_version_detail(_request(match={"slug": art.slug, "version": "1"}))
        assert resp.status == 200
        assert _json_body(resp)["content"] == "first"

    @pytest.mark.asyncio
    async def test_version_detail_invalid_slug_400(self, isolated_store: ArtifactStore) -> None:
        resp = await api_artifact_version_detail(_request(match={"slug": "A B", "version": "1"}))
        assert resp.status == 400


# ── Lifecycle events ────────────────────────────────────────────────────────


class TestArtifactEvents:
    @pytest.mark.asyncio
    async def test_events_returns_log_for_real_artifact(
        self, isolated_store: ArtifactStore
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_events(_request(match={"slug": art.slug}))
        assert resp.status == 200
        data = _json_body(resp)
        assert data["slug"] == art.slug
        # Never empty for a real artifact (lazy backfill synthesizes `created`).
        assert data["events"]

    @pytest.mark.asyncio
    async def test_events_unknown_slug_404(self, isolated_store: ArtifactStore) -> None:
        resp = await api_artifact_events(_request(match={"slug": "missing"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_events_invalid_slug_400(self, isolated_store: ArtifactStore) -> None:
        resp = await api_artifact_events(_request(match={"slug": "A B"}))
        assert resp.status == 400


class TestRecordEvent:
    """``POST /api/artifacts/{slug}/events`` — a pure annotation endpoint, so
    everything except ``type='referenced'`` must be rejected at the boundary."""

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, isolated_store: ArtifactStore, capture_audit: list[dict]
    ) -> None:
        # Deny-by-default: no dashboard state at all → 403 with an audited denial.
        req = _request(body={"type": "referenced"}, match={"slug": "s"}, no_state=True)
        resp = await api_artifact_record_event(req)
        assert resp.status == 403
        assert capture_audit[-1]["outcome"] == "denied"
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, isolated_store: ArtifactStore, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        req = _request(body={"type": "referenced"}, match={"slug": "s"}, restricted=True)
        resp = await api_artifact_record_event(req)
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "restricted session"

    @pytest.mark.asyncio
    async def test_malformed_json_body_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        req = _request(body=b"{not json", match={"slug": "s"})
        resp = await api_artifact_record_event(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_wrong_event_type_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(body={"type": "edited"}, match={"slug": art.slug})
        resp = await api_artifact_record_event(req)
        assert resp.status == 400
        assert "type='referenced'" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_object_metadata_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced", "metadata": ["not", "a", "dict"]},
            match={"slug": art.slug},
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 400
        assert "metadata must be an object" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_string_message_ts_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced", "metadata": {"message_ts": 12345}},
            match={"slug": art.slug},
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 400
        assert "message_ts" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_integer_widget_index_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced", "metadata": {"widget_index": "2"}},
            match={"slug": art.slug},
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 400
        assert "widget_index" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_unknown_slug_404(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        req = _request(body={"type": "referenced"}, match={"slug": "ghost"})
        resp = await api_artifact_record_event(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_slug_400(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        req = _request(body={"type": "referenced"}, match={"slug": "A B"})
        resp = await api_artifact_record_event(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_store_error_maps_to_500(
        self, isolated_store: ArtifactStore, patch_restricted: None, monkeypatch
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise OSError("meta.json is read-only")

        monkeypatch.setattr(isolated_store, "record_impression", _boom)
        req = _request(body={"type": "referenced"}, match={"slug": art.slug})
        resp = await api_artifact_record_event(req)
        assert resp.status == 500
        assert "read-only" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_records_referenced_event_as_user(
        self, isolated_store: ArtifactStore, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced", "metadata": {"message_ts": "170.5", "widget_index": 0}},
            match={"slug": art.slug},
            session_key="dashboard:s1",
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 200
        event = _json_body(resp)["event"]
        assert event["type"] == "referenced"
        assert event["by"] == "user"
        assert capture_audit[-1]["extra"]["suppressed"] is False

    @pytest.mark.asyncio
    async def test_mcp_request_is_attributed_to_agent(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced"},
            match={"slug": art.slug},
            session_key="mcp:s2",
            extra_headers={"X-Internal-Secret": "s3cr3t"},
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 200
        assert _json_body(resp)["event"]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_dashboard_ui_session_key_is_dropped(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        # The literal ``dashboard:ui`` is not a real session, so it must not be
        # recorded as the event's session id.
        art = isolated_store.create(name="Doc", content="x", kind="markdown")
        req = _request(
            body={"type": "referenced"}, match={"slug": art.slug}, session_key="dashboard:ui"
        )
        resp = await api_artifact_record_event(req)
        assert resp.status == 200
        assert not _json_body(resp)["event"].get("session_id")

    @pytest.mark.asyncio
    async def test_second_impression_in_same_session_is_suppressed(
        self, isolated_store: ArtifactStore, patch_restricted: None
    ) -> None:
        art = isolated_store.create(name="Doc", content="x", kind="markdown")

        def _req() -> MagicMock:
            return _request(
                body={"type": "referenced"},
                match={"slug": art.slug},
                session_key="dashboard:dup",
            )

        first = await api_artifact_record_event(_req())
        assert first.status == 200
        second = await api_artifact_record_event(_req())
        assert second.status == 200
        payload = _json_body(second)
        assert payload["suppressed"] is True
        assert payload["event"] is None


# ── Refresh sharing ─────────────────────────────────────────────────────────


class TestRefreshSharing:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, isolated_store: ArtifactStore, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_refresh_sharing(_request(match={"slug": "s"}, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, isolated_store: ArtifactStore, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_refresh_sharing(_request(match={"slug": "s"}, restricted=True))
        assert resp.status == 403
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_unknown_slug_404(
        self,
        isolated_store: ArtifactStore,
        patch_restricted: None,
        capture_audit: list[dict],
        monkeypatch,
    ) -> None:
        async def _missing(_slug: str) -> Any:
            raise ArtifactNotFoundError("artifact not found: ghost")

        monkeypatch.setattr(art_handlers.publish_sync, "refresh_publication", _missing)
        resp = await api_artifact_refresh_sharing(_request(match={"slug": "ghost"}))
        assert resp.status == 404
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_validation_error_is_400_and_audited_as_denied(
        self,
        isolated_store: ArtifactStore,
        patch_restricted: None,
        capture_audit: list[dict],
        monkeypatch,
    ) -> None:
        async def _invalid(_slug: str) -> Any:
            raise ArtifactValidationError("artifact is not published")

        monkeypatch.setattr(art_handlers.publish_sync, "refresh_publication", _invalid)
        resp = await api_artifact_refresh_sharing(_request(match={"slug": "s"}))
        assert resp.status == 400
        assert "not published" in _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_success_returns_serialized_artifact_with_content(
        self,
        isolated_store: ArtifactStore,
        patch_restricted: None,
        capture_audit: list[dict],
        monkeypatch,
    ) -> None:
        art = isolated_store.create(name="Shared Doc", content="body text", kind="markdown")
        calls: list[str] = []

        async def _refresh(slug: str) -> Any:
            calls.append(slug)
            return isolated_store.get(slug)

        monkeypatch.setattr(art_handlers.publish_sync, "refresh_publication", _refresh)
        resp = await api_artifact_refresh_sharing(_request(match={"slug": art.slug}))
        assert resp.status == 200
        assert calls == [art.slug]
        assert _json_body(resp)["content"] == "body text"
        assert capture_audit[-1]["outcome"] == "success"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated artifact + folder stores wired into the module globals."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    fstore = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", fstore)
    return store, fstore


class TestFolderUpdate:
    """``PATCH /api/artifact-folders/{id}`` — the mutation-branch and error
    mapping the happy-path suite doesn't reach."""

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, stores, capture_audit: list[dict]) -> None:
        resp = await api_artifact_folder_update(_request(match={"id": "f1"}, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_folder_update(_request(match={"id": "f1"}, restricted=True))
        assert resp.status == 403
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_malformed_body_400(self, stores, patch_restricted: None) -> None:
        _store, fstore = stores
        folder = fstore.create("Reports")
        resp = await api_artifact_folder_update(_request(body=b"{bad", match={"id": folder["id"]}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sets_icon_color_and_order(self, stores, patch_restricted: None) -> None:
        _store, fstore = stores
        folder = fstore.create("Reports")
        resp = await api_artifact_folder_update(
            _request(
                body={"icon": "📊", "color": "#ff0000", "order": 3},
                match={"id": folder["id"]},
            )
        )
        assert resp.status == 200
        data = _json_body(resp)
        assert data["icon"] == "📊"
        assert data["color"] == "#ff0000"
        assert fstore.get(folder["id"])["order"] == 3

    @pytest.mark.asyncio
    async def test_reparent_to_root_clears_parent(self, stores, patch_restricted: None) -> None:
        _store, fstore = stores
        parent = fstore.create("Parent")
        child = fstore.create("Child", parent_id=parent["id"])
        resp = await api_artifact_folder_update(
            _request(body={"parent_id": ""}, match={"id": child["id"]})
        )
        assert resp.status == 200
        assert not fstore.get(child["id"])["parent_id"]

    @pytest.mark.asyncio
    async def test_non_integer_order_is_400_and_audited_as_denied(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        # ``int(body["order"])`` raises ValueError, which the handler maps to a
        # 400 rather than letting it escape as a 500.
        _store, fstore = stores
        folder = fstore.create("Reports")
        resp = await api_artifact_folder_update(
            _request(body={"order": "third"}, match={"id": folder["id"]})
        )
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_concurrent_delete_during_apply_is_404(
        self, stores, patch_restricted: None, monkeypatch
    ) -> None:
        # exists()/get() pass the pre-check, then the folder vanishes before the
        # off-loop apply runs — the handler must 404, not 500.
        _store, fstore = stores
        folder = fstore.create("Reports")
        calls = {"n": 0}
        real_get = fstore.get

        def _vanishing(fid: str):
            calls["n"] += 1
            return None if calls["n"] > 1 else real_get(fid)

        monkeypatch.setattr(fstore, "get", _vanishing)
        resp = await api_artifact_folder_update(
            _request(body={"name": "New"}, match={"id": folder["id"]})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_store_error_maps_to_500(
        self, stores, patch_restricted: None, monkeypatch, capture_audit: list[dict]
    ) -> None:
        _store, fstore = stores
        folder = fstore.create("Reports")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise ArtifactError("folder index is corrupt")

        monkeypatch.setattr(fstore, "rename", _boom)
        resp = await api_artifact_folder_update(
            _request(body={"name": "New"}, match={"id": folder["id"]})
        )
        assert resp.status == 500
        assert capture_audit[-1]["outcome"] == "error"


class TestFolderDelete:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, stores, capture_audit: list[dict]) -> None:
        resp = await api_artifact_folder_delete(_request(match={"id": "f1"}, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_folder_delete(_request(match={"id": "f1"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_store_error_maps_to_500(
        self, stores, patch_restricted: None, monkeypatch, capture_audit: list[dict]
    ) -> None:
        _store, fstore = stores
        folder = fstore.create("Reports")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise ArtifactError("cannot rewrite folder index")

        monkeypatch.setattr(fstore, "delete", _boom)
        resp = await api_artifact_folder_delete(_request(match={"id": folder["id"]}))
        assert resp.status == 500
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_delete_contents_flag_is_parsed_from_query(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        store, fstore = stores
        folder = fstore.create("Reports")
        art = store.create(name="Doc", content="x", kind="markdown", folder_id=folder["id"])
        resp = await api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "YES"})
        )
        assert resp.status == 200
        assert capture_audit[-1]["extra"]["delete_contents"] is True
        assert art.slug in _json_body(resp)["deleted_artifact_slugs"]


class TestSetFolderErrors:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, stores, capture_audit: list[dict]) -> None:
        resp = await api_artifact_set_folder(_request(match={"slug": "s"}, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_set_folder(_request(match={"slug": "s"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_malformed_body_400(self, stores, patch_restricted: None) -> None:
        resp = await api_artifact_set_folder(_request(body=b"}{", match={"slug": "s"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_folder_id_is_400(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        # ``folder_id`` resolves with create_missing=False, so an unknown id
        # comes back as a resolver error (400, audited denied) — never a silent
        # unfile.
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": "no-such-folder"}, match={"slug": art.slug})
        )
        assert resp.status == 400
        assert "no-such-folder" in _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_non_string_folder_ref_is_400(self, stores, patch_restricted: None) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": 17}, match={"slug": art.slug})
        )
        assert resp.status == 400
        assert "must be a string" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_resolved_id_deleted_before_the_move_is_400(
        self, stores, patch_restricted: None, monkeypatch, capture_audit: list[dict]
    ) -> None:
        # Defensive branch: the ref resolved cleanly but the folder is gone by
        # the time the handler re-checks (concurrent delete) → 400, not a move
        # into a dangling id.
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        monkeypatch.setattr(
            art_handlers, "_resolve_folder_ref", lambda _ref, create_missing: ("phantom", None)
        )
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": "phantom"}, match={"slug": art.slug})
        )
        assert resp.status == 400
        assert _json_body(resp)["error"] == "folder not found"
        assert capture_audit[-1]["extra"]["folder_id"] == "phantom"

    @pytest.mark.asyncio
    async def test_unknown_slug_is_404(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        _store, fstore = stores
        folder = fstore.create("Reports")
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": folder["id"]}, match={"slug": "ghost"})
        )
        assert resp.status == 404
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_slug_is_400(self, stores, patch_restricted: None) -> None:
        resp = await api_artifact_set_folder(_request(body={"folder": ""}, match={"slug": "A B"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_store_error_maps_to_500(
        self, stores, patch_restricted: None, monkeypatch, capture_audit: list[dict]
    ) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise ArtifactError("meta.json write failed")

        monkeypatch.setattr(store, "set_folder", _boom)
        resp = await api_artifact_set_folder(
            _request(body={"folder": ""}, match={"slug": art.slug})
        )
        assert resp.status == 500
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_folder_path_autocreates_and_moves(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        store, fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_set_folder(
            _request(body={"folder": "Reports/2026"}, match={"slug": art.slug})
        )
        assert resp.status == 200
        folder_id = capture_audit[-1]["extra"]["folder_id"]
        assert fstore.breadcrumb(folder_id) == "Reports/2026"


class TestSetPinned:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, stores, capture_audit: list[dict]) -> None:
        resp = await api_artifact_set_pinned(_request(match={"slug": "s"}, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_set_pinned(_request(match={"slug": "s"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_malformed_body_400_and_audited(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_set_pinned(_request(body=b"nope", match={"slug": "s"}))
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_non_boolean_pinned_400(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_set_pinned(
            _request(body={"pinned": "true"}, match={"slug": art.slug})
        )
        assert resp.status == 400
        assert "must be a boolean" in _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_missing_pinned_key_400(self, stores, patch_restricted: None) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        resp = await api_artifact_set_pinned(_request(body={}, match={"slug": art.slug}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_slug_404(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        resp = await api_artifact_set_pinned(
            _request(body={"pinned": True}, match={"slug": "ghost"})
        )
        assert resp.status == 404
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_pin_then_unpin_round_trip(
        self, stores, patch_restricted: None, capture_audit: list[dict]
    ) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")
        pinned = await api_artifact_set_pinned(
            _request(body={"pinned": True}, match={"slug": art.slug})
        )
        assert pinned.status == 200
        assert _json_body(pinned)["pinned"] is True
        unpinned = await api_artifact_set_pinned(
            _request(body={"pinned": False}, match={"slug": art.slug})
        )
        assert _json_body(unpinned)["pinned"] is False
        assert capture_audit[-1]["extra"]["pinned"] is False

    @pytest.mark.asyncio
    async def test_store_error_maps_to_500(
        self, stores, patch_restricted: None, monkeypatch, capture_audit: list[dict]
    ) -> None:
        store, _fstore = stores
        art = store.create(name="Doc", content="x", kind="markdown")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise ArtifactError("meta.json write failed")

        monkeypatch.setattr(store, "set_pinned", _boom)
        resp = await api_artifact_set_pinned(
            _request(body={"pinned": True}, match={"slug": art.slug})
        )
        assert resp.status == 500
        assert capture_audit[-1]["outcome"] == "error"


# ── Remote-artifact provider harness ────────────────────────────────────────


class _FakeRemoteProvider(PublishProvider):
    """Minimal provider covering the remote read + comment surface.

    Every method is a pure in-memory stub — no sockets, no subprocesses. Each
    behaviour a handler branch needs (missing capability, provider raise,
    provider timeout) is selected by flipping an attribute, so a test never has
    to wait on a real clock.
    """

    name = "fakeprov"
    display_name = "Fake Provider"
    install_hint = "install the fake provider"

    def __init__(self) -> None:
        self.caps: set[Capability] = {
            Capability.CONTENT_PULL,
            Capability.COMMENTS_READ,
            Capability.COMMENTS_WRITE,
        }
        self.calls: list[str] = []
        self.raises: Exception | None = None
        self.times_out = False
        self.content: dict | None = {
            "content": "<h1>remote</h1>",
            "content_type": "text/html",
            "title": "Remote One",
            "owner": "someone",
            "visibility": "SHARED",
        }
        self.comments: list[RemoteComment] = []

    # -- required abstract surface (never exercised by these tests) --------
    def available(self) -> bool:
        return True

    async def ensure_ready(self) -> bool:
        return True

    def view_url_for(self, external_id: str) -> str:
        return f"https://remote.example.com/a/{external_id}"

    async def publish(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def push_version(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def update_sharing(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def unpublish(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    # -- behaviour under test ---------------------------------------------
    def capabilities(self) -> set[Capability]:
        return self.caps

    def _maybe_fail(self, label: str) -> None:
        self.calls.append(label)
        if self.times_out:
            # Raise the exact exception ``asyncio.wait_for`` would raise on a
            # hung provider. Deterministic: no sleeping, no wall-clock reliance.
            raise asyncio.TimeoutError()
        if self.raises is not None:
            raise self.raises

    async def fetch_content(self, *, external_id: str) -> dict | None:
        self._maybe_fail("fetch_content")
        return self.content

    async def fetch_comments(self, *, external_id: str) -> list[RemoteComment]:
        self._maybe_fail("fetch_comments")
        return self.comments

    async def post_comment(
        self, *, external_id: str, body: str, anchor: CommentAnchor | None = None
    ) -> RemoteComment:
        self._maybe_fail("post_comment")
        return RemoteComment(
            remote_id="rc-new",
            thread_id="rc-new",
            author="me",
            body=body,
            anchor=anchor,
        )

    async def reply_comment(
        self, *, external_id: str, parent_remote_id: str, body: str
    ) -> RemoteComment:
        self._maybe_fail("reply_comment")
        return RemoteComment(
            remote_id="rc-reply",
            thread_id=parent_remote_id,
            author="me",
            body=body,
            parent_id=parent_remote_id,
        )

    async def mark_review(self, *, external_id: str, remote_id: str) -> None:
        self._maybe_fail("mark_review")

    async def delete_comment(self, *, external_id: str, remote_id: str) -> None:
        self._maybe_fail("delete_comment")


@pytest.fixture
def remote_provider():
    """Register the fake provider, restoring the (empty) public registry after."""
    prov = _FakeRemoteProvider()
    saved = dict(publish_provider._FACTORIES)
    publish_provider.reset_providers()
    publish_provider.register_provider(prov.name, lambda: prov)
    publish_provider._INSTANCES[prov.name] = prov
    yield prov
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


@pytest.fixture(autouse=True)
def clean_remote_comment_cache():
    """The remote-comment cache is module state — isolate every test from it."""
    art_handlers._remote_comment_cache.clear()
    yield
    art_handlers._remote_comment_cache.clear()


@pytest.fixture
def gate_open(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Publish-governance gate permits, recording the provider it was asked about."""
    calls: list[str] = []

    def _permit(_request: Any, provider_name: str) -> None:
        calls.append(provider_name)
        return None

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _permit)
    return calls


_MATCH = {"provider": "fakeprov", "external_id": "ext-1", "comment_id": "c9"}


# ── Remote artifact GET ─────────────────────────────────────────────────────


class TestRemoteArtifactGet:
    @pytest.mark.asyncio
    async def test_success_returns_redacted_payload(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 200
        assert _json_body(resp)["title"] == "Remote One"
        assert capture_audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_strips_local_path_from_provider_payload(
        self, patch_restricted: None, remote_provider
    ) -> None:
        assert remote_provider.content is not None
        remote_provider.content = dict(remote_provider.content, localPath="/home/me/secret.md")
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 200
        assert "localPath" not in _json_body(resp)

    @pytest.mark.asyncio
    async def test_missing_capability_400(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        remote_provider.caps = set()
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 400
        assert "does not support fetching content" in _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "denied"
        assert remote_provider.calls == []

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_502(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        remote_provider.raises = RuntimeError("upstream exploded")
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 502
        assert "upstream exploded" in _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_provider_timeout_maps_to_504_with_non_empty_error(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        remote_provider.times_out = True
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 504
        # str(asyncio.TimeoutError()) is "" — the handler must supply real text.
        assert _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_unreadable_artifact_404(self, patch_restricted: None, remote_provider) -> None:
        remote_provider.content = None
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"})
        resp = await api_remote_artifact_get(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, remote_provider, capture_audit: list[dict]
    ) -> None:
        req = _request(match={"provider": "fakeprov", "external_id": "ext-1"}, no_state=True)
        resp = await api_remote_artifact_get(req)
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"


# ── Remote comments: read ───────────────────────────────────────────────────


class TestRemoteComments:
    @pytest.mark.asyncio
    async def test_serializes_and_caches_provider_comments(
        self, patch_restricted: None, remote_provider
    ) -> None:
        remote_provider.comments = [
            RemoteComment(remote_id="c1", thread_id="c1", author="alice", body="hello"),
            RemoteComment(remote_id="c2", thread_id="c1", author="bob", body="gone", deleted=True),
        ]
        req = _request(match=_MATCH)
        first = await api_remote_artifact_comments(req)
        assert first.status == 200
        data = _json_body(first)
        # Deleted comments are filtered out.
        assert [c["id"] for c in data["comments"]] == ["c1"]
        assert data["comments"][0]["origin"] == "fakeprov:c1"
        assert data["remote_sync_error"] is None

        # Second call is served from the TTL cache — the provider is not re-hit.
        second = await api_remote_artifact_comments(_request(match=_MATCH))
        assert _json_body(second)["cached"] is True
        assert remote_provider.calls == ["fetch_comments"]

    @pytest.mark.asyncio
    async def test_missing_read_capability_degrades_with_sync_error(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        remote_provider.caps = {Capability.CONTENT_PULL}
        resp = await api_remote_artifact_comments(_request(match=_MATCH))
        # Degrades (200 + error string) rather than failing the detail view.
        assert resp.status == 200
        assert "does not support comments" in _json_body(resp)["remote_sync_error"]
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_provider_error_becomes_remote_sync_error(
        self, patch_restricted: None, remote_provider
    ) -> None:
        remote_provider.raises = RuntimeError("comments backend down")
        resp = await api_remote_artifact_comments(_request(match=_MATCH))
        assert resp.status == 200
        assert "comments backend down" in _json_body(resp)["remote_sync_error"]
        assert _json_body(resp)["comments"] == []

    @pytest.mark.asyncio
    async def test_provider_timeout_becomes_non_empty_sync_error(
        self, patch_restricted: None, remote_provider, capture_audit: list[dict]
    ) -> None:
        remote_provider.times_out = True
        resp = await api_remote_artifact_comments(_request(match=_MATCH))
        assert resp.status == 200
        assert _json_body(resp)["remote_sync_error"] == "remote provider timed out"
        assert capture_audit[-1]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_failed_fetch_is_not_cached(
        self, patch_restricted: None, remote_provider
    ) -> None:
        remote_provider.raises = RuntimeError("transient")
        await api_remote_artifact_comments(_request(match=_MATCH))
        remote_provider.raises = None
        remote_provider.comments = [
            RemoteComment(remote_id="c1", thread_id="c1", author="a", body="b")
        ]
        resp = await api_remote_artifact_comments(_request(match=_MATCH))
        assert [c["id"] for c in _json_body(resp)["comments"]] == ["c1"]

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, remote_provider, capture_audit: list[dict]
    ) -> None:
        resp = await api_remote_artifact_comments(_request(match=_MATCH, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"


# ── Remote comments: write quartet ──────────────────────────────────────────


class TestRemotePostComment:
    @pytest.mark.asyncio
    async def test_empty_text_400_before_gate(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_post_comment(_request(body={"text": "   "}, match=_MATCH))
        assert resp.status == 400
        assert "text is required" in _json_body(resp)["error"]
        assert gate_open == []

    @pytest.mark.asyncio
    async def test_oversized_text_400(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_post_comment(
            _request(body={"text": "x" * 10001}, match=_MATCH)
        )
        assert resp.status == 400
        assert "exceeds 10000" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_malformed_json_400(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_post_comment(_request(body=b"{oops", match=_MATCH))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_write_capability_400(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.caps = {Capability.COMMENTS_READ}
        resp = await api_remote_artifact_post_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"
        assert remote_provider.calls == []

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_502(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.raises = RuntimeError("write rejected")
        resp = await api_remote_artifact_post_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 502
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_success_returns_201_and_invalidates_cache(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        art_handlers._remote_comment_cache["fakeprov:ext-1"] = (0.0, [])
        resp = await api_remote_artifact_post_comment(
            _request(body={"text": "hello there"}, match=_MATCH)
        )
        assert resp.status == 201
        comment = _json_body(resp)["comment"]
        assert comment["id"] == "rc-new"
        assert comment["body"] == "hello there"
        assert "fakeprov:ext-1" not in art_handlers._remote_comment_cache
        assert capture_audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_anchor_is_forwarded_and_echoed(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_post_comment(
            _request(
                body={
                    "text": "about this line",
                    "anchor": {
                        "quote": "the quoted span",
                        "prefix": "before ",
                        "suffix": " after",
                        "start_offset": 4,
                        "end_offset": 19,
                        "version_number": 2,
                    },
                },
                match=_MATCH,
            )
        )
        assert resp.status == 201
        anchor = _json_body(resp)["comment"]["anchor"]
        assert anchor["quote"] == "the quoted span"
        assert anchor["start_offset"] == 4
        assert anchor["version_number"] == 2

    @pytest.mark.asyncio
    async def test_anchor_without_quote_is_ignored(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_post_comment(
            _request(body={"text": "hi", "anchor": {"prefix": "no quote"}}, match=_MATCH)
        )
        assert resp.status == 201
        assert "anchor" not in _json_body(resp)["comment"]

    @pytest.mark.asyncio
    async def test_provider_timeout_maps_to_504(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.times_out = True
        resp = await api_remote_artifact_post_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 504
        assert _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "timeout"


class TestRemoteReplyComment:
    @pytest.mark.asyncio
    async def test_empty_text_400(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_reply_comment(_request(body={"text": ""}, match=_MATCH))
        assert resp.status == 400
        assert gate_open == []

    @pytest.mark.asyncio
    async def test_oversized_text_400(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_reply_comment(
            _request(body={"text": "y" * 10001}, match=_MATCH)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_malformed_json_400(
        self, patch_restricted: None, remote_provider, gate_open: list[str]
    ) -> None:
        resp = await api_remote_artifact_reply_comment(_request(body=b"[[", match=_MATCH))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_write_capability_400(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.caps = {Capability.COMMENTS_READ}
        resp = await api_remote_artifact_reply_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_success_returns_201_with_parent_linkage(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        resp = await api_remote_artifact_reply_comment(
            _request(body={"text": "agreed"}, match=_MATCH)
        )
        assert resp.status == 201
        comment = _json_body(resp)["comment"]
        assert comment["parent_id"] == "c9"
        assert comment["thread_id"] == "c9"
        assert capture_audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_502(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.raises = RuntimeError("reply rejected")
        resp = await api_remote_artifact_reply_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 502
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_provider_timeout_maps_to_504(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.times_out = True
        resp = await api_remote_artifact_reply_comment(_request(body={"text": "hi"}, match=_MATCH))
        assert resp.status == 504
        assert _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "timeout"


class TestRemoteMarkReview:
    @pytest.mark.asyncio
    async def test_missing_write_capability_400(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.caps = {Capability.COMMENTS_READ}
        resp = await api_remote_artifact_mark_review(_request(match=_MATCH))
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"
        assert remote_provider.calls == []

    @pytest.mark.asyncio
    async def test_success_returns_review_status_and_clears_cache(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        art_handlers._remote_comment_cache["fakeprov:ext-1"] = (0.0, [])
        resp = await api_remote_artifact_mark_review(_request(match=_MATCH))
        assert resp.status == 200
        assert _json_body(resp) == {"status": "review"}
        assert remote_provider.calls == ["mark_review"]
        assert "fakeprov:ext-1" not in art_handlers._remote_comment_cache
        assert capture_audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_502(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.raises = RuntimeError("status change refused")
        resp = await api_remote_artifact_mark_review(_request(match=_MATCH))
        assert resp.status == 502
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_provider_timeout_maps_to_504(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.times_out = True
        resp = await api_remote_artifact_mark_review(_request(match=_MATCH))
        assert resp.status == 504
        assert _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, remote_provider, capture_audit: list[dict]
    ) -> None:
        resp = await api_remote_artifact_mark_review(_request(match=_MATCH, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"


class TestRemoteDeleteComment:
    @pytest.mark.asyncio
    async def test_missing_write_capability_400(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.caps = {Capability.COMMENTS_READ}
        resp = await api_remote_artifact_delete_comment(_request(match=_MATCH))
        assert resp.status == 400
        assert capture_audit[-1]["outcome"] == "denied"
        assert remote_provider.calls == []

    @pytest.mark.asyncio
    async def test_success_returns_deleted_and_clears_cache(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        art_handlers._remote_comment_cache["fakeprov:ext-1"] = (0.0, [])
        resp = await api_remote_artifact_delete_comment(_request(match=_MATCH))
        assert resp.status == 200
        assert _json_body(resp) == {"deleted": True}
        assert remote_provider.calls == ["delete_comment"]
        assert "fakeprov:ext-1" not in art_handlers._remote_comment_cache
        assert capture_audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_provider_error_maps_to_502(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.raises = RuntimeError("delete refused")
        resp = await api_remote_artifact_delete_comment(_request(match=_MATCH))
        assert resp.status == 502
        assert capture_audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_provider_timeout_maps_to_504(
        self,
        patch_restricted: None,
        remote_provider,
        gate_open: list[str],
        capture_audit: list[dict],
    ) -> None:
        remote_provider.times_out = True
        resp = await api_remote_artifact_delete_comment(_request(match=_MATCH))
        assert resp.status == 504
        assert _json_body(resp)["error"]
        assert capture_audit[-1]["outcome"] == "timeout"

    @pytest.mark.asyncio
    async def test_missing_state_denies_403(
        self, remote_provider, capture_audit: list[dict]
    ) -> None:
        resp = await api_remote_artifact_delete_comment(_request(match=_MATCH, no_state=True))
        assert resp.status == 403
        assert capture_audit[-1]["error"] == "missing dashboard state"


# ── Remote-comment cache helpers ────────────────────────────────────────────


class TestRemoteCommentCache:
    def test_sweep_evicts_only_entries_past_the_ttl(self) -> None:
        ttl = art_handlers._REMOTE_COMMENT_TTL_SECS
        now = 1000.0
        art_handlers._remote_comment_cache["p:fresh"] = (now - 1.0, [])
        art_handlers._remote_comment_cache["p:stale"] = (now - ttl - 1.0, [])
        _remote_cache_sweep(now)
        assert list(art_handlers._remote_comment_cache) == ["p:fresh"]

    def test_put_refreshes_position_lru_style(self) -> None:
        _remote_cache_put("p:a", (1.0, []))
        _remote_cache_put("p:b", (2.0, []))
        _remote_cache_put("p:a", (3.0, []))
        # Re-putting moves the key to the end (most-recently used).
        assert list(art_handlers._remote_comment_cache) == ["p:b", "p:a"]
        assert art_handlers._remote_comment_cache["p:a"][0] == 3.0

    def test_put_evicts_oldest_past_the_cap(self) -> None:
        cap = art_handlers._REMOTE_COMMENT_CACHE_MAX
        for i in range(cap + 3):
            _remote_cache_put(f"p:{i}", (float(i), []))
        assert len(art_handlers._remote_comment_cache) == cap
        # The three oldest keys were evicted, the newest survives.
        assert "p:0" not in art_handlers._remote_comment_cache
        assert "p:2" not in art_handlers._remote_comment_cache
        assert f"p:{cap + 2}" in art_handlers._remote_comment_cache


# ── Serialization + redaction helpers ───────────────────────────────────────


class TestSerializeRemoteComment:
    def test_provider_keys_the_origin_not_a_hardcoded_name(self) -> None:
        rc = RemoteComment(
            remote_id="r7",
            thread_id="t7",
            author="alice",
            body="body",
            status="review",
            is_agent=True,
            created_at="2026-07-01T00:00:00Z",
        )
        out = _serialize_remote_comment(rc, "otherprov")
        assert out["provider"] == "otherprov"
        assert out["origin"] == "otherprov:r7"
        assert out["scope"] == "shared"
        assert out["sync_state"] == "synced"
        assert out["status"] == "review"
        assert out["is_agent"] is True

    def test_anchor_with_none_prefix_and_suffix_survives(self) -> None:
        rc = RemoteComment(
            remote_id="r1",
            thread_id="r1",
            author="a",
            body="b",
            anchor=CommentAnchor(quote="span", prefix=None, suffix=None),
        )
        out = _serialize_remote_comment(rc, "fakeprov")
        assert out["anchor"]["quote"] == "span"
        assert out["anchor"]["prefix"] is None
        assert out["anchor"]["suffix"] is None

    def test_anchor_without_quote_is_omitted(self) -> None:
        rc = RemoteComment(
            remote_id="r1",
            thread_id="r1",
            author="a",
            body="b",
            anchor=CommentAnchor(quote=None, start_offset=3),
        )
        assert "anchor" not in _serialize_remote_comment(rc, "fakeprov")


class TestIdEmbedsHardCredential:
    # The base64 scanner only considers runs of >= 40 alphabet chars
    # (``_B64_CHUNK_RE``), so every encoded fixture below is padded past that
    # floor — a shorter encoding is not a chunk at all and never decoded.
    def test_literal_credential_is_detected(self) -> None:
        assert _id_embeds_hard_credential("id-AKIAIOSFODNN7EXAMPLE") is True

    def test_benign_id_is_not_flagged(self) -> None:
        assert _id_embeds_hard_credential("3f9a2b71-0c4d-4e8a-9b11-77d0c2e5a1f4") is False

    def test_undecodable_chunk_is_skipped_without_raising(self) -> None:
        # A 41-char alphabet run IS a chunk but is not a valid padded base64
        # length (41 % 4 != 0), so strict decoding raises and the scan must
        # continue rather than propagate.
        assert _id_embeds_hard_credential("A" * 41) is False

    def test_base64_encoded_credential_is_detected(self) -> None:
        encoded = base64.b64encode(b"AKIAIOSFODNN7EXAMPLE-with-trailing-filler").decode()
        assert len(encoded) >= 40
        assert _id_embeds_hard_credential(encoded) is True

    def test_base64_that_decodes_to_harmless_text_is_not_flagged(self) -> None:
        encoded = base64.b64encode(b"just an ordinary artifact title here").decode()
        assert len(encoded) >= 40
        assert _id_embeds_hard_credential(encoded) is False


class TestRedactRemoteResponseDepthCap:
    """``_redact_remote_response`` truncates rather than recursing past
    ``_MAX_REDACT_DEPTH``. The top-level key's value is walked at depth 1, so a
    leaf placed under exactly ``_MAX_REDACT_DEPTH`` dict wrappers lands at
    depth ``_MAX_REDACT_DEPTH + 1`` — the first level past the cap, where the
    per-type truncation branches live."""

    def _at_boundary(self, leaf: Any) -> dict:
        node: Any = leaf
        for _ in range(art_handlers._MAX_REDACT_DEPTH):
            node = {"child": node}
        return {"root": node}

    def _leaf_of(self, out: dict) -> Any:
        node: Any = out["root"]
        while isinstance(node, dict) and "child" in node:
            node = node["child"]
        return node

    def test_dict_past_the_cap_is_emptied(self) -> None:
        out = _redact_remote_response(self._at_boundary({"deep": "value"}))
        assert self._leaf_of(out) == {}

    def test_list_past_the_cap_is_emptied(self) -> None:
        out = _redact_remote_response(self._at_boundary(["a", "b"]))
        assert self._leaf_of(out) == []

    def test_string_past_the_cap_is_still_redacted(self) -> None:
        out = _redact_remote_response(self._at_boundary("AKIAIOSFODNN7EXAMPLE"))
        assert self._leaf_of(out) != "AKIAIOSFODNN7EXAMPLE"

    def test_non_string_scalar_past_the_cap_survives(self) -> None:
        out = _redact_remote_response(self._at_boundary(7))
        assert self._leaf_of(out) == 7

    def test_empty_string_past_the_cap_survives(self) -> None:
        out = _redact_remote_response(self._at_boundary(""))
        assert self._leaf_of(out) == ""

    def test_already_redacted_top_level_key_is_not_rescanned(self) -> None:
        payload = {"content": "AKIAIOSFODNN7EXAMPLE", "title": "AKIAIOSFODNN7EXAMPLE"}
        out = _redact_remote_response(payload, already_redacted=frozenset({"content"}))
        assert out["content"] == "AKIAIOSFODNN7EXAMPLE"
        assert out["title"] != "AKIAIOSFODNN7EXAMPLE"

    def test_external_id_keeps_benign_high_entropy_value(self) -> None:
        out = _redact_remote_response({"external_id": "9f8e7d6c5b4a39281706"})
        assert out["external_id"] == "9f8e7d6c5b4a39281706"

    def test_external_id_with_hard_credential_is_replaced(self) -> None:
        out = _redact_remote_response({"external_id": "AKIAIOSFODNN7EXAMPLE"})
        assert out["external_id"] == art_handlers._REMOTE_ID_CRED_TAG
