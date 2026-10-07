"""Owner gate on the state-changing artifact routes.

A signed-in dashboard subject who is not the owner (``app == ""``, ``user !=
owner_id``) gets the shared 403 ``owner_only`` from every artifact write route,
before the body is read or the store is touched. The owner, the loopback
internal-secret transport the agent ``artifact_*`` tools use (``internal_auth``
set, ``app`` absent) and app tokens keep working as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as handlers

OWNER = "U_OWNER_0001"
NON_OWNER = "U_ALLOWLISTED_CHANNEL_USER"

#: Every artifact route that changes state. Each one must refuse a non-owner.
WRITE_ROUTES = [
    "api_artifacts_create",
    "api_artifact_update",
    "api_artifact_delete",
    "api_artifact_settle_blank",
    "api_artifact_record_event",
    "api_artifact_publish",
    "api_artifact_unpublish",
    "api_artifact_refresh_sharing",
    "api_artifact_reprobe_notice",
    "api_artifact_update_sharing",
    "api_artifact_relocate",
    "api_artifact_pull_latest",
    "api_artifact_overwrite_remote",
    "api_artifact_materialize",
    "api_artifact_folder_create",
    "api_artifact_folder_update",
    "api_artifact_folder_delete",
    "api_artifact_set_folder",
    "api_artifact_set_pinned",
    "api_artifact_post_comment",
    "api_artifact_edit_comment",
    "api_artifact_reply_comment",
    "api_artifact_mark_review",
    "api_artifact_resolve_comment",
    "api_artifact_reopen_comment",
    "api_artifact_delete_comment",
    "api_remote_artifacts_clone",
    "api_remote_artifacts_fork",
    "api_remote_artifact_post_comment",
    "api_remote_artifact_reply_comment",
    "api_remote_artifact_mark_review",
    "api_remote_artifact_delete_comment",
]


class _Req(dict):
    """aiohttp-request stand-in: dict items carry the auth middleware's claims."""


def _request(claims: dict, *, body: dict | None = None, match: dict | None = None) -> _Req:
    req = _Req(claims)
    req.headers = {}  # no X-Session-Key: not an incognito/guest session
    req.match_info = match or {}
    req.query = {}
    req.read = AsyncMock(return_value=json.dumps(body or {}).encode())
    req.json = AsyncMock(return_value=body or {})
    req.remote = "127.0.0.1"
    state = MagicMock()
    state.owner_id = OWNER
    state.get_slot.return_value = None
    req.app = {"state": state}
    return req


def _owner(**kw) -> _Req:
    return _request({"user": OWNER, "app": ""}, **kw)


def _non_owner(**kw) -> _Req:
    return _request({"user": NON_OWNER, "app": ""}, **kw)


def _agent_tool(**kw) -> _Req:
    """The internal-secret transport: ``internal_auth`` set, ``app`` left absent."""
    return _request({"user": "kiro-cli", "internal_auth": True}, **kw)


def _app_token(**kw) -> _Req:
    return _request({"user": "app:helper", "app": "helper"}, **kw)


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", s)
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("route", WRITE_ROUTES)
async def test_non_owner_is_refused_before_any_effect(route: str, store, tmp_path) -> None:
    art = store.create(name="owner doc", content="owner content", kind="markdown")
    before = _tree(tmp_path)
    req = _non_owner(
        body={"name": "x", "content": "y", "folder": "f", "pinned": True},
        match={
            "slug": art.slug,
            "id": "f1",
            "comment_id": "c1",
            "provider": "p",
            "external_id": "e1",
        },
    )
    resp = await getattr(handlers, route)(req)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "owner_only"
    req.read.assert_not_awaited()
    req.json.assert_not_awaited()
    assert _tree(tmp_path) == before


@pytest.mark.asyncio
async def test_owner_create_and_delete_still_work(store) -> None:
    resp = await handlers.api_artifacts_create(
        _owner(body={"name": "mine", "content": "hello", "kind": "markdown"})
    )
    assert resp.status == 201
    slug = json.loads(resp.body)["slug"]
    resp = await handlers.api_artifact_update(
        _owner(body={"name": "renamed"}, match={"slug": slug})
    )
    assert resp.status == 200
    resp = await handlers.api_artifact_delete(_owner(match={"slug": slug}))
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [_agent_tool, _app_token], ids=["agent_tool", "app_token"])
async def test_agent_tools_and_app_tokens_are_unchanged(caller, store) -> None:
    resp = await handlers.api_artifacts_create(
        caller(body={"name": "made", "content": "hello", "kind": "markdown"})
    )
    assert resp.status == 201
    slug = json.loads(resp.body)["slug"]
    resp = await handlers.api_artifact_delete(caller(match={"slug": slug}))
    assert resp.status == 200
    with pytest.raises(art_mod.ArtifactNotFoundError):
        store.get(slug)
