"""``POST /api/knowledge/dedup`` — the Plane B cross-source dedup route (R2).

Residual 3: the ``knowledge_dedup`` MCP tool had no sidecar endpoint, so a
supervised sidecar could not run a dedup on behalf of the CLI. The route reuses
the SAME ``dedup_sweep`` + ``KnowledgeStore`` path the MCP tool uses
(``mcp_tools/knowledge.py``), returns ``{removed, preview, duplicates:[{loser,
winner, reason, itemsDeleted}]}``, and is admitted POST-only to a supervised
internal-secret caller.

Two layers, matching the repo's split (see ``test_crew_mcp_servers_route.py``):
the handler shape against a small seeded store, and the admission matrix through
the middleware.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers_system as hs
from kiro_crew.dashboard.handlers_system import api_knowledge_dedup
from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    supervised_mixed_internal_method_paths,
    supervised_mixed_internal_paths,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.store import KnowledgeStore
from test.test_token_auth import _make_request, _ok_handler

pytestmark = pytest.mark.asyncio

SECRET = "phase6-dedup-secret"


def _seed_duplicate_store(config_home) -> None:
    """One exact-hash cross-source duplicate (an upload and a folder file sharing
    a content hash), the minimal case ``dedup_sweep`` collapses — mirrors
    ``test_knowledge_dedup.py::test_exact_hash_collapses_upload_into_folder``."""
    db_path = config_home / "workspace" / "knowledge" / "knowledge.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = KnowledgeStore(str(db_path))
    # upload source
    sid_u = store.add_source(name="Doc.docx", source_type="local_file", uri="upload://Doc.docx")
    iid_u = store.add_item(
        title="Doc.docx", content="body", item_type="document", source_id=sid_u,
        content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
    store.db.execute(
        "UPDATE items SET embedding_sig = ?, created_at = ? WHERE id = ?",
        ("sig1", "2026-01-01T00:00:00", iid_u))
    store.db.execute(
        "UPDATE sources SET updated_at = ? WHERE id = ?", ("2026-01-01T00:00:00", sid_u))
    # folder source with same content hash
    sid_f = store.add_source(name="Projects", source_type="local_folder", uri="/tmp/Projects")
    iid_f = store.add_item(
        title="Doc.docx", content="body", item_type="document", source_id=sid_f,
        content_hash="H1", embedding=floats_to_bytes([1.0, 0.0, 0.0, 0.0]))
    store.db.execute(
        "UPDATE items SET embedding_sig = ?, created_at = ? WHERE id = ?",
        ("sig1", "2026-02-01T00:00:00", iid_f))
    store.db.execute(
        "INSERT INTO folder_file_state "
        "(source_id, file_path, content_hash, mtime, item_ids, last_seen, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid_f, "/p/Doc.docx", "bytehash", 1000.0, json.dumps([iid_f]),
         "2026-02-01T00:00:00", "done"))
    store.db.commit()
    store.db.close()


def _request(*, internal_auth: bool = True, apply: object = None) -> MagicMock:
    request = MagicMock()
    store = {"internal_auth": internal_auth}
    request.get.side_effect = store.get
    request.app = {"supervised": True}

    async def _json():
        return {} if apply is None else {"apply": apply}

    request.json.side_effect = _json
    return request


async def _body(request: MagicMock) -> dict:
    resp = await api_knowledge_dedup(request)
    return json.loads(resp.body)


# -- handler shape -------------------------------------------------------------


async def test_preview_reports_the_duplicate_without_deleting(tmp_path, monkeypatch) -> None:
    _seed_duplicate_store(tmp_path)
    monkeypatch.setattr(hs, "config_dir", lambda: tmp_path)

    data = await _body(_request(apply=False))
    assert data["preview"] is True
    assert data["removed"] == 1
    assert len(data["duplicates"]) == 1
    dup = data["duplicates"][0]
    assert set(dup) == {"loser", "winner", "reason", "itemsDeleted"}
    assert dup["reason"] == "exact"
    assert dup["loser"] == "Doc.docx"
    assert isinstance(dup["itemsDeleted"], int) and dup["itemsDeleted"] >= 1

    # Preview mutated nothing: a second preview still finds the same duplicate.
    again = await _body(_request(apply=False))
    assert again["removed"] == 1


async def test_default_body_is_a_preview(tmp_path, monkeypatch) -> None:
    _seed_duplicate_store(tmp_path)
    monkeypatch.setattr(hs, "config_dir", lambda: tmp_path)
    data = await _body(_request(apply=None))  # {} body -> apply defaults False
    assert data["preview"] is True
    assert data["removed"] == 1


async def test_apply_deletes_and_is_idempotent(tmp_path, monkeypatch) -> None:
    _seed_duplicate_store(tmp_path)
    monkeypatch.setattr(hs, "config_dir", lambda: tmp_path)

    applied = await _body(_request(apply=True))
    assert applied["preview"] is False
    assert applied["removed"] == 1
    assert applied["duplicates"][0]["itemsDeleted"] >= 1

    # Idempotent: the collapse is done, so a re-run finds nothing.
    after = await _body(_request(apply=True))
    assert after["removed"] == 0
    assert after["duplicates"] == []


async def test_no_store_returns_zero_not_error(tmp_path, monkeypatch) -> None:
    # tmp_path has no knowledge.db — an unconfigured library is a valid state.
    monkeypatch.setattr(hs, "config_dir", lambda: tmp_path)
    data = await _body(_request(apply=False))
    assert data == {"removed": 0, "preview": True, "duplicates": []}

    data = await _body(_request(apply=True))
    assert data == {"removed": 0, "preview": False, "duplicates": []}


async def test_missing_internal_auth_is_refused() -> None:
    resp = await api_knowledge_dedup(_request(internal_auth=False))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "internal_required"


# -- admission matrix ----------------------------------------------------------


def _mw(supervised: bool):
    return token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(supervised),
        mixed_internal_methods=supervised_mixed_internal_method_paths(supervised),
        internal_secret=SECRET,
    )


def test_route_is_method_scoped_to_post_when_supervised() -> None:
    methods = supervised_mixed_internal_method_paths(True)
    assert methods.get("/api/knowledge/dedup") == frozenset({"POST"})
    # Not a bare-prefix supervised path, and never in the base set.
    assert "/api/knowledge/dedup" not in supervised_mixed_internal_paths(True)
    assert "/api/knowledge/dedup" not in _MIXED_INTERNAL_API_PATHS


def test_unsupervised_has_no_dedup_method_admission() -> None:
    assert supervised_mixed_internal_method_paths(False) == {}


async def test_supervised_post_with_secret_reaches_route() -> None:
    req = _make_request(
        path="/api/knowledge/dedup", method="POST", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


async def test_supervised_get_is_denied() -> None:
    # Only POST is admitted; a GET on the same path falls through to cookie auth.
    req = _make_request(
        path="/api/knowledge/dedup", method="GET", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403


async def test_unsupervised_secret_holder_denied() -> None:
    req = _make_request(
        path="/api/knowledge/dedup", method="POST", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(False)(req, _ok_handler)
    assert resp.status == 403


async def test_wrong_secret_denied() -> None:
    req = _make_request(
        path="/api/knowledge/dedup", method="POST", headers={"X-Internal-Secret": "nope"}
    )
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403
