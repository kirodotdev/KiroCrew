"""Tests for the artifact-folder HTTP handlers (Mesh-2720).

Mirrors ``test_artifacts_handlers.py``: MagicMock requests + a real
:class:`ArtifactStore` and :class:`ArtifactFolderStore` rooted at tmp dirs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactFolderStore, ArtifactNotFoundError, ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_folder_create,
    api_artifact_folder_delete,
    api_artifact_folder_update,
    api_artifact_folders,
    api_artifact_set_folder,
    api_artifact_update,
    api_artifacts_create,
    api_artifacts_list,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated artifact + folder stores wired into the module globals."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    fstore = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", fstore)
    return store, fstore


@pytest.fixture
def patch_restricted(monkeypatch):
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _request(
    *,
    body: dict | None = None,
    match: dict | None = None,
    query: dict | None = None,
    restricted: bool = False,
) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = query or {}
    encoded = json.dumps(body).encode() if isinstance(body, dict) else b""
    req.read = AsyncMock(return_value=encoded)
    req.app = {"state": MagicMock(), "_restricted_session": restricted}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


# ── List ─────────────────────────────────────────────────────────────────────


class TestFoldersList:
    @pytest.mark.asyncio
    async def test_empty(self, stores) -> None:
        resp = await api_artifact_folders(_request())
        assert resp.status == 200
        assert _body(resp)["folders"] == []

    @pytest.mark.asyncio
    async def test_lists_with_counts_and_path(self, stores) -> None:
        store, fstore = stores
        f = fstore.create("Reports")
        store.create(name="a", content="x", folder_id=f["id"])
        resp = await api_artifact_folders(_request())
        folders = _body(resp)["folders"]
        assert len(folders) == 1
        assert folders[0]["item_count"] == 1
        assert folders[0]["path"] == "Reports"


# ── Create ───────────────────────────────────────────────────────────────────


class TestFolderCreate:
    @pytest.mark.asyncio
    async def test_create_root(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_create(_request(body={"name": "Reports"}))
        assert resp.status == 201
        assert _body(resp)["name"] == "Reports"

    @pytest.mark.asyncio
    async def test_create_with_parent_path_mkdir_p(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        resp = await api_artifact_folder_create(
            _request(body={"name": "Q3", "parent": "A/B"})
        )
        assert resp.status == 201
        # A, B (auto-created) and Q3 now exist.
        assert len(fstore.list()) == 3
        assert _body(resp)["path"] == "A/B/Q3"

    @pytest.mark.asyncio
    async def test_missing_name_400(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_create(_request(body={"name": "  "}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_parent_id_is_id_only_no_path_autocreate(
        self, stores, patch_restricted
    ) -> None:
        """``parent_id`` must never be interpreted as a path (id-only contract):
        a path-looking value errors instead of mkdir-p'ing folders."""
        _store, fstore = stores
        resp = await api_artifact_folder_create(
            _request(body={"name": "Q3", "parent_id": "A/B"})
        )
        assert resp.status == 400
        # Nothing was auto-created — not A, not B, not Q3.
        assert fstore.list() == []

    @pytest.mark.asyncio
    async def test_parent_id_accepts_real_id(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        parent = fstore.create("Reports")
        resp = await api_artifact_folder_create(
            _request(body={"name": "Q3", "parent_id": parent["id"]})
        )
        assert resp.status == 201
        assert _body(resp)["path"] == "Reports/Q3"

    @pytest.mark.asyncio
    async def test_restricted_denied(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_create(
            _request(body={"name": "X"}, restricted=True)
        )
        assert resp.status == 403


# ── Update ───────────────────────────────────────────────────────────────────


class TestFolderUpdate:
    @pytest.mark.asyncio
    async def test_rename(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        f = fstore.create("Old")
        resp = await api_artifact_folder_update(
            _request(body={"name": "New"}, match={"id": f["id"]})
        )
        assert resp.status == 200
        assert fstore.get(f["id"])["name"] == "New"

    @pytest.mark.asyncio
    async def test_reparent_cycle_400(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        a = fstore.create("A")
        b = fstore.create("B", parent_id=a["id"])
        resp = await api_artifact_folder_update(
            _request(body={"parent_id": b["id"]}, match={"id": a["id"]})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_404(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_update(
            _request(body={"name": "x"}, match={"id": "nope"})
        )
        assert resp.status == 404


# ── Delete ───────────────────────────────────────────────────────────────────


class TestFolderDelete:
    @pytest.mark.asyncio
    async def test_keep_default(self, stores, patch_restricted) -> None:
        store, fstore = stores
        f = fstore.create("F")
        art = store.create(name="a", content="x", folder_id=f["id"])
        resp = await api_artifact_folder_delete(_request(match={"id": f["id"]}))
        assert resp.status == 200
        assert _body(resp)["ok"] is True
        # Artifact kept, re-parented to root.
        assert store.get(art.slug).folder_id == ""

    @pytest.mark.asyncio
    async def test_cascade_query(self, stores, patch_restricted) -> None:
        store, fstore = stores
        f = fstore.create("F")
        art = store.create(name="a", content="x", folder_id=f["id"])
        resp = await api_artifact_folder_delete(
            _request(match={"id": f["id"]}, query={"delete_contents": "true"})
        )
        assert resp.status == 200
        with pytest.raises(ArtifactNotFoundError):
            store.get(art.slug)

    @pytest.mark.asyncio
    async def test_unknown_404(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_delete(_request(match={"id": "nope"}))
        assert resp.status == 404


# ── Set folder (move artifact) ───────────────────────────────────────────────


class TestSetFolder:
    @pytest.mark.asyncio
    async def test_move_by_id(self, stores, patch_restricted) -> None:
        store, fstore = stores
        f = fstore.create("F")
        art = store.create(name="a", content="x")
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": f["id"]}, match={"slug": art.slug})
        )
        assert resp.status == 200
        assert store.get(art.slug).folder_id == f["id"]

    @pytest.mark.asyncio
    async def test_move_by_path_mkdir_p(self, stores, patch_restricted) -> None:
        store, fstore = stores
        art = store.create(name="a", content="x")
        resp = await api_artifact_set_folder(
            _request(body={"folder": "A/B"}, match={"slug": art.slug})
        )
        assert resp.status == 200
        leaf = fstore.resolve_path("A/B")
        assert store.get(art.slug).folder_id == leaf

    @pytest.mark.asyncio
    async def test_unfile(self, stores, patch_restricted) -> None:
        store, fstore = stores
        f = fstore.create("F")
        art = store.create(name="a", content="x", folder_id=f["id"])
        resp = await api_artifact_set_folder(
            _request(body={"folder": ""}, match={"slug": art.slug})
        )
        assert resp.status == 200
        assert store.get(art.slug).folder_id == ""

    @pytest.mark.asyncio
    async def test_dangling_id_400(self, stores, patch_restricted) -> None:
        store, _fstore = stores
        art = store.create(name="a", content="x")
        resp = await api_artifact_set_folder(
            _request(body={"folder_id": "ghost"}, match={"slug": art.slug})
        )
        assert resp.status == 400


# ── Create/list artifact with folder ─────────────────────────────────────────


class TestArtifactCreateWithFolder:
    @pytest.mark.asyncio
    async def test_create_files_into_folder_path(self, stores, patch_restricted) -> None:
        store, fstore = stores
        resp = await api_artifacts_create(
            _request(body={"name": "doc", "content": "x", "folder": "Reports/Q3"})
        )
        assert resp.status == 201
        slug = _body(resp)["slug"]
        leaf = fstore.resolve_path("Reports/Q3")
        assert store.get(slug).folder_id == leaf

    @pytest.mark.asyncio
    async def test_list_scopes_by_folder_query(self, stores) -> None:
        store, fstore = stores
        f = fstore.create("F")
        store.create(name="a", content="x", folder_id=f["id"])
        store.create(name="b", content="y")  # unfiled

        # ?folder=<id> → only that folder.
        resp = await api_artifacts_list(_request(query={"folder": f["id"]}))
        names = [a["name"] for a in _body(resp)["artifacts"]]
        assert names == ["a"]

        # ?folder= (empty) → unfiled only.
        resp = await api_artifacts_list(_request(query={"folder": ""}))
        names = [a["name"] for a in _body(resp)["artifacts"]]
        assert names == ["b"]

        # absent → all.
        resp = await api_artifacts_list(_request())
        assert len(_body(resp)["artifacts"]) == 2


# ── Audit context (Mesh-2720 / AutoSDE security-controls) ────────────────────


class TestUpdateFolderAudit:
    @pytest.mark.asyncio
    async def test_update_with_folder_audits_folder_id(
        self, stores, patch_restricted, monkeypatch
    ) -> None:
        """When the generic update also places the artifact in a folder, the
        success SEL audit must carry ``folder_id`` (mutation audited with its
        full effect — matches ``api_artifact_set_folder``)."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        store, fstore = stores
        art = store.create(name="doc", content="x")
        f = fstore.create("Reports")
        audits: list[dict] = []

        def _spy(**kwargs) -> None:
            audits.append(kwargs)

        monkeypatch.setattr(art_handlers, "_audit", _spy)
        resp = await api_artifact_update(
            _request(body={"folder": f["id"]}, match={"slug": art.slug})
        )
        assert resp.status == 200
        success = [a for a in audits if a.get("outcome") == "success"]
        assert success, f"no success audit emitted: {audits}"
        assert success[-1]["extra"]["folder_id"] == f["id"]

    @pytest.mark.asyncio
    async def test_update_without_folder_audit_has_no_folder_key(
        self, stores, patch_restricted, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        store, _fstore = stores
        art = store.create(name="doc", content="x")
        audits: list[dict] = []
        monkeypatch.setattr(art_handlers, "_audit", lambda **kw: audits.append(kw))
        resp = await api_artifact_update(
            _request(body={"description": "d"}, match={"slug": art.slug})
        )
        assert resp.status == 200
        success = [a for a in audits if a.get("outcome") == "success"]
        assert success and "folder_id" not in success[-1]["extra"]


# ── Folder color + auto-emoji icon (UX feedback round 2) ────────────────────


class TestFolderColorHandlers:
    @pytest.mark.asyncio
    async def test_create_with_color(self, stores, patch_restricted) -> None:
        resp = await api_artifact_folder_create(
            _request(body={"name": "Reports", "color": "#3B82F6"})
        )
        assert resp.status == 201
        assert _body(resp)["color"] == "#3b82f6"

    @pytest.mark.asyncio
    async def test_create_with_invalid_color_400(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        resp = await api_artifact_folder_create(
            _request(body={"name": "Reports", "color": "not-a-color"})
        )
        assert resp.status == 400
        assert fstore.list() == []

    @pytest.mark.asyncio
    async def test_update_color_and_clear(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        f = fstore.create("Reports")
        resp = await api_artifact_folder_update(
            _request(body={"color": "#ef4444"}, match={"id": f["id"]})
        )
        assert resp.status == 200
        assert _body(resp)["color"] == "#ef4444"
        resp = await api_artifact_folder_update(
            _request(body={"color": ""}, match={"id": f["id"]})
        )
        assert resp.status == 200
        assert "color" not in _body(resp)


class TestFolderAutoIcon:
    @pytest.mark.asyncio
    async def test_create_derives_emoji_icon(self, stores, patch_restricted, monkeypatch) -> None:
        """Create fires the background emoji task; a valid emoji is stored."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        async def _fake_gen(_state, _name: str) -> str:
            return "🚀"

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.generate_emoji_for_name", _fake_gen
        )
        _store, fstore = stores
        resp = await api_artifact_folder_create(_request(body={"name": "Rockets"}))
        assert resp.status == 201
        fid = _body(resp)["id"]
        # Drain the fire-and-forget icon task.
        for t in list(art_handlers._ARTIFACT_FOLDER_ICON_TASKS):
            await t
        assert fstore.get(fid)["icon"] == "🚀"

    @pytest.mark.asyncio
    async def test_rename_rederives_icon_but_explicit_icon_wins(
        self, stores, patch_restricted, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        calls: list[str] = []

        async def _fake_gen(_state, name: str) -> str:
            calls.append(name)
            return "🐛"

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.generate_emoji_for_name", _fake_gen
        )
        _store, fstore = stores
        f = fstore.create("Old")
        # Rename → regenerates from the new name.
        resp = await api_artifact_folder_update(
            _request(body={"name": "Bugs"}, match={"id": f["id"]})
        )
        assert resp.status == 200
        for t in list(art_handlers._ARTIFACT_FOLDER_ICON_TASKS):
            await t
        assert fstore.get(f["id"])["icon"] == "🐛"
        assert calls == ["Bugs"]
        # Rename WITH an explicit icon → no regeneration; explicit icon wins.
        resp = await api_artifact_folder_update(
            _request(body={"name": "Fixes", "icon": "🔧"}, match={"id": f["id"]})
        )
        assert resp.status == 200
        for t in list(art_handlers._ARTIFACT_FOLDER_ICON_TASKS):
            await t
        assert fstore.get(f["id"])["icon"] == "🔧"
        assert calls == ["Bugs"]

    @pytest.mark.asyncio
    async def test_invalid_llm_reply_leaves_icon_unset(
        self, stores, patch_restricted, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        async def _fake_gen(_state, _name: str) -> str:
            return ""  # generate_emoji_for_name returns "" on invalid replies

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.artifacts.generate_emoji_for_name", _fake_gen
        )
        _store, fstore = stores
        resp = await api_artifact_folder_create(_request(body={"name": "Plain"}))
        assert resp.status == 201
        for t in list(art_handlers._ARTIFACT_FOLDER_ICON_TASKS):
            await t
        assert "icon" not in fstore.get(_body(resp)["id"])


class TestFolderIconRedaction:
    def test_serializer_redacts_icon_like_name(self) -> None:
        """_serialize_folder must run the icon through the same redaction as
        the folder name — icon is LLM-derived or user-supplied via the
        set_icon API, so it can't be served back to the dashboard verbatim.
        (An exfil URL with a credential-bearing query is the canonical
        redactable payload; the 16-char store cap is a second, independent
        guard on the API path.)"""
        from kiro_crew.dashboard.handlers.artifacts import _serialize_folder

        payload = "https://evil.example.com/x?d=" + "A" * 64
        out = _serialize_folder({"id": "f1", "name": "ok", "icon": payload})
        assert payload not in json.dumps(out)
        assert "REDACTED" in out["icon"]

    @pytest.mark.asyncio
    async def test_benign_icon_roundtrips(self, stores, patch_restricted) -> None:
        _store, fstore = stores
        f = fstore.create("Reports")
        resp = await api_artifact_folder_update(
            _request(body={"icon": "🚀"}, match={"id": f["id"]})
        )
        assert resp.status == 200
        assert _body(resp)["icon"] == "🚀"


class TestMixedInternalPathRegistration:
    def test_artifact_folders_is_a_mixed_internal_path(self) -> None:
        """Regression (fork adaptation of Mesh-2720): the 5 artifact_folder_*
        MCP tools authenticate via X-Internal-Secret. token_auth prefix
        matching is ``path == p or path.startswith(p + "/")``, so
        "/api/artifact-folders" is NOT covered by the "/api/artifacts" entry
        (hyphen, not slash). If server.py drops the dedicated entry, every
        folder MCP call falls through to cookie auth and fails with
        "Token required"."""
        import inspect

        from kiro_crew.dashboard import server as server_mod

        src = inspect.getsource(server_mod)
        start = src.index("mixed_internal_paths=frozenset(")
        end = src.index("internal_secret=", start)
        block = src[start:end]
        assert '"/api/artifact-folders"' in block
        # Confidence check: the prefix-match rule this guards against.
        assert not "/api/artifact-folders".startswith("/api/artifacts" + "/")
