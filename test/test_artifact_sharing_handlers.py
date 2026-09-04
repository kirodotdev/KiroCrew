"""Backend tests for the artifact sharing-widening / unpublish / refresh
endpoints and the ``_validate_sharing_body`` helper (CWE-1188 hardening).

Focus: prove the ``capabilities.publish`` governance gate on
``api_artifact_update_sharing`` actually blocks a PRIVATE→PUBLIC widening
(and never dispatches the provider ``update_sharing``) when policy denies,
and permits it when policy allows; that ``api_artifact_unpublish`` handles
its success + not-found paths; and that ``_validate_sharing_body`` enforces
the visibility enum + the SHARED-requires-alias rule.

Harness/fixtures mirror ``test/test_remote_artifacts.py`` (real ArtifactStore
at tmp_path, MagicMock request, monkeypatched ``_is_restricted_session`` and
``_publish_governance_denied``).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew import publish_sync
from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore, ArtifactValidationError
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    _validate_sharing_body,
    api_artifact_unpublish,
    api_artifact_update_sharing,
)
from kiro_crew.publish_provider import DEFAULT_PROVIDER

# ── Fixtures (mirrors test_remote_artifacts.py) ───────────────────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """Real ArtifactStore at tmp_path, wired as the process default for both
    the handler module and the publish_sync engine."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(publish_sync, "get_default_store", lambda: store)
    return store


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    session_key: str = "dashboard:test",
    restricted: bool = False,
) -> MagicMock:
    """MagicMock aiohttp Request shaped for these handlers."""
    req = MagicMock()
    req.headers = {"X-Session-Key": session_key}
    req.match_info = match or {}
    req.query = {}
    req.rel_url.query = {}
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
    """Publish governance gate permits (returns None) and records provider names."""
    calls: list[str] = []

    def _permit(_request, provider_name: str):
        calls.append(provider_name)
        return None

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _permit)
    return calls


@pytest.fixture
def gate_denied(monkeypatch):
    """Publish governance gate denies with a fixed reason and records provider names."""
    calls: list[str] = []

    def _deny(_request, provider_name: str):
        calls.append(provider_name)
        return "publishing not permitted by policy"

    monkeypatch.setattr(art_handlers, "_publish_governance_denied", _deny)
    return calls


def _json_body(resp) -> dict:
    return json.loads(resp.body)


# ── api_artifact_update_sharing: governance gate ──────────────────────────────


class TestUpdateSharingGovernance:
    @pytest.mark.asyncio
    async def test_widening_denied_does_not_dispatch_update_sharing(
        self, isolated_store, patch_restricted, gate_denied, monkeypatch
    ):
        """PRIVATE→PUBLIC widening is DENIED (403) when governance denies, and
        the provider ``update_sharing`` is NEVER awaited."""
        art = isolated_store.create(name="Doc", content="x", kind="html")
        dispatched = AsyncMock()
        monkeypatch.setattr(publish_sync, "update_sharing", dispatched)

        req = _request(match={"slug": art.slug}, body={"visibility": "PUBLIC"})
        resp = await api_artifact_update_sharing(req)

        assert resp.status == 403
        assert _json_body(resp)["error"] == "publishing not permitted by policy"
        # The gate ran (recorded the effective provider) but the outbound
        # mutation was never dispatched — bytes never left the box.
        assert gate_denied == [DEFAULT_PROVIDER]
        dispatched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_widening_permitted_dispatches_update_sharing(
        self, isolated_store, patch_restricted, gate_open, monkeypatch
    ):
        """When governance permits (returns None), update_sharing is dispatched
        and the handler returns 200 with the serialized artifact."""
        art = isolated_store.create(name="Doc", content="x", kind="html")
        dispatched = AsyncMock()
        monkeypatch.setattr(publish_sync, "update_sharing", dispatched)

        req = _request(
            match={"slug": art.slug},
            body={"visibility": "SHARED", "shared_with": ["alice"]},
        )
        resp = await api_artifact_update_sharing(req)

        assert resp.status == 200
        assert _json_body(resp)["slug"] == art.slug
        assert gate_open == [DEFAULT_PROVIDER]
        dispatched.assert_awaited_once()
        _, kwargs = dispatched.await_args
        assert kwargs["visibility"] == "SHARED"
        assert kwargs["shared_with"] == ["alice"]

    @pytest.mark.asyncio
    async def test_restricted_session_denied_before_gate(
        self, isolated_store, patch_restricted, gate_open, monkeypatch
    ):
        """A restricted session is refused (403) before any provider dispatch."""
        dispatched = AsyncMock()
        monkeypatch.setattr(publish_sync, "update_sharing", dispatched)
        req = _request(
            match={"slug": "whatever"}, body={"visibility": "PUBLIC"}, restricted=True
        )
        resp = await api_artifact_update_sharing(req)
        assert resp.status == 403
        dispatched.assert_not_awaited()
        assert gate_open == []

    @pytest.mark.asyncio
    async def test_invalid_body_rejected_before_gate(
        self, isolated_store, patch_restricted, gate_open, monkeypatch
    ):
        """An invalid visibility enum is a 400 and never reaches the gate."""
        dispatched = AsyncMock()
        monkeypatch.setattr(publish_sync, "update_sharing", dispatched)
        req = _request(match={"slug": "whatever"}, body={"visibility": "BOGUS"})
        resp = await api_artifact_update_sharing(req)
        assert resp.status == 400
        dispatched.assert_not_awaited()
        assert gate_open == []


# ── api_artifact_unpublish (no governance gate: success + not-found) ───────────


class TestUnpublish:
    @pytest.mark.asyncio
    async def test_unpublish_success(
        self, isolated_store, patch_restricted, monkeypatch
    ):
        art = isolated_store.create(name="Doc", content="x", kind="html")
        unpub = AsyncMock()
        monkeypatch.setattr(publish_sync, "unpublish", unpub)

        req = _request(match={"slug": art.slug})
        resp = await api_artifact_unpublish(req)

        assert resp.status == 200
        assert _json_body(resp)["slug"] == art.slug
        unpub.assert_awaited_once_with(art.slug)

    @pytest.mark.asyncio
    async def test_unpublish_not_found(
        self, isolated_store, patch_restricted, monkeypatch
    ):
        unpub = AsyncMock(side_effect=ArtifactNotFoundError("no such artifact: ghost"))
        monkeypatch.setattr(publish_sync, "unpublish", unpub)

        req = _request(match={"slug": "ghost"})
        resp = await api_artifact_unpublish(req)

        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unpublish_restricted_session(
        self, isolated_store, patch_restricted, monkeypatch
    ):
        unpub = AsyncMock()
        monkeypatch.setattr(publish_sync, "unpublish", unpub)
        req = _request(match={"slug": "whatever"}, restricted=True)
        resp = await api_artifact_unpublish(req)
        assert resp.status == 403
        unpub.assert_not_awaited()


# ── _validate_sharing_body ────────────────────────────────────────────────────


class TestValidateSharingBody:
    def test_private_defaults_empty_shared_with(self):
        assert _validate_sharing_body({"visibility": "PRIVATE"}) == ("PRIVATE", [])

    def test_missing_visibility_defaults_private(self):
        assert _validate_sharing_body({}) == ("PRIVATE", [])

    def test_shared_with_alias_ok(self):
        assert _validate_sharing_body(
            {"visibility": "SHARED", "shared_with": ["alice", "bob"]}
        ) == ("SHARED", ["alice", "bob"])

    def test_public_ok(self):
        assert _validate_sharing_body({"visibility": "PUBLIC"}) == ("PUBLIC", [])

    def test_invalid_visibility_enum_raises(self):
        with pytest.raises(ArtifactValidationError):
            _validate_sharing_body({"visibility": "WORLD"})

    def test_shared_without_alias_raises(self):
        with pytest.raises(ArtifactValidationError):
            _validate_sharing_body({"visibility": "SHARED", "shared_with": []})

    def test_shared_with_non_list_raises(self):
        with pytest.raises(ArtifactValidationError):
            _validate_sharing_body({"visibility": "SHARED", "shared_with": "alice"})

    def test_shared_with_non_string_elements_raises(self):
        with pytest.raises(ArtifactValidationError):
            _validate_sharing_body({"visibility": "PUBLIC", "shared_with": [1, 2]})
