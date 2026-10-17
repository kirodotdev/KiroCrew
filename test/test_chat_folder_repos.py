"""Multi-repo project folders: a folder may hold extra repository directories
beside its primary ``project_dir``.

Each ``repos`` entry validates per directory like ``project_dir`` (absolute,
existing, not sensitive), plus a list cap, no duplicates, and no entry equal to
the folder's own ``project_dir``. Resolution is NEAREST-folder-wins (repos
travel with the project directory), unlike the accumulative steering resolver.
A PATCH with an empty list clears them, and a folder with no ``repos`` key is
byte-for-byte what it is today.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.context_assembly import turn as _turn
from kiro_crew.dashboard.chat_folders import (
    MAX_FOLDER_REPOS,
    _resolve_folder_repos,
    _validate_repos,
    api_chat_folder_create,
    api_chat_folder_update,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── validator ──


def test_validate_rejects_relative_path():
    resolved, err = _validate_repos(["not/absolute"])
    assert resolved == []
    assert err and "absolute" in err.lower()


def test_validate_rejects_missing_directory(tmp_path):
    resolved, err = _validate_repos([str(tmp_path / "nope")])
    assert resolved == []
    assert err and "existing directory" in err.lower()


def test_validate_rejects_sensitive_path(tmp_path, monkeypatch):
    d = tmp_path / "repo"
    d.mkdir()
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.is_sensitive_path", lambda p: True)
    resolved, err = _validate_repos([str(d)])
    assert resolved == []
    assert err and "sensitive" in err.lower()


def test_validate_rejects_over_cap(tmp_path):
    dirs = []
    for i in range(MAX_FOLDER_REPOS + 1):
        p = tmp_path / f"r{i}"
        p.mkdir()
        dirs.append(str(p))
    resolved, err = _validate_repos(dirs)
    assert resolved == []
    assert err and str(MAX_FOLDER_REPOS) in err


def test_validate_rejects_duplicates(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    resolved, err = _validate_repos([str(d), str(d)])
    assert resolved == []
    assert err and "repeat" in err.lower()


def test_validate_rejects_an_entry_equal_to_the_primary(tmp_path):
    primary = tmp_path / "foo"
    primary.mkdir()
    resolved, err = _validate_repos([str(primary)], primary=str(primary.resolve()))
    assert resolved == []
    assert err and "differ" in err.lower()


def test_validate_accepts_empty_list_and_refuses_explicit_none():
    assert _validate_repos([]) == ([], None)
    resolved, err = _validate_repos(None)
    assert resolved == []
    assert err and "list" in err.lower()


def test_validate_resolves_and_dedups_ok(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    resolved, err = _validate_repos([str(a), str(b)])
    assert err is None
    assert resolved == [str(a.resolve()), str(b.resolve())]


# ── resolver (nearest-wins) ──


def test_resolver_returns_repos_of_the_nearest_project_folder(tmp_path):
    foo = tmp_path / "foo"
    bar = tmp_path / "bar"
    foo.mkdir()
    bar.mkdir()
    folders = [
        {
            "id": "a",
            "parent_id": "",
            "project_dir": str(foo.resolve()),
            "repos": [str(bar.resolve())],
        },
        {"id": "b", "parent_id": "a"},
        {"id": "c", "parent_id": "b"},
    ]
    resolved, err = _resolve_folder_repos(folders, "c")
    assert err is None
    assert resolved == [str(bar.resolve())]


def test_resolver_does_not_accumulate_up_the_chain(tmp_path):
    root = tmp_path / "root"
    root_extra = tmp_path / "root_extra"
    sub = tmp_path / "sub"
    sub_extra = tmp_path / "sub_extra"
    for p in (root, root_extra, sub, sub_extra):
        p.mkdir()
    folders = [
        {
            "id": "a",
            "parent_id": "",
            "project_dir": str(root.resolve()),
            "repos": [str(root_extra.resolve())],
        },
        {
            "id": "b",
            "parent_id": "a",
            "project_dir": str(sub.resolve()),
            "repos": [str(sub_extra.resolve())],
        },
    ]
    # `b` is the nearest project folder, so ONLY its repos apply — the root's
    # extra repo is not added on top.
    resolved, err = _resolve_folder_repos(folders, "b")
    assert err is None
    assert resolved == [str(sub_extra.resolve())]


def test_resolver_empty_when_no_project_folder(tmp_path):
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    folders = [
        {"id": "a", "parent_id": "", "repos": [str(orphan.resolve())]},
        {"id": "b", "parent_id": "a"},
    ]
    # `a` lists repos but declares no project_dir, so there is no project folder
    # and no repos resolve — repos belong to a project folder only.
    assert _resolve_folder_repos(folders, "b") == ([], None)


def test_resolver_empty_when_folder_declares_no_repos(tmp_path):
    foo = tmp_path / "foo"
    foo.mkdir()
    folders = [{"id": "a", "parent_id": "", "project_dir": str(foo.resolve())}]
    assert _resolve_folder_repos(folders, "a") == ([], None)


def test_resolver_cycle_guarded():
    folders = [{"id": "a", "parent_id": "b"}, {"id": "b", "parent_id": "a"}]
    assert _resolve_folder_repos(folders, "a") == ([], None)


def test_resolver_skips_an_entry_that_now_equals_the_primary(tmp_path):
    foo = tmp_path / "foo"
    bar = tmp_path / "bar"
    foo.mkdir()
    bar.mkdir()
    # A stored repo equal to the (resolved) primary is dropped at read time.
    folders = [
        {
            "id": "a",
            "parent_id": "",
            "project_dir": str(foo),
            "repos": [str(foo.resolve()), str(bar.resolve())],
        }
    ]
    resolved, err = _resolve_folder_repos(folders, "a")
    assert err is None
    assert resolved == [str(bar.resolve())]


# ── [PROJECT] rail rendering ──


def _rail(project, folder_repos):
    return "".join(
        _turn.rail_parts(
            project=project,
            folder_repos=folder_repos,
            context_groups=None,
            board_tags=None,
            minimal_context=False,
            folder_path=None,
            session_key="dashboard:chat-1-100",
            agent=None,
            request_prefix_context=None,
        )
    )


def test_project_rail_is_unchanged_with_no_extra_repos():
    text = _rail("/p/foo", ())
    assert "[PROJECT] Active project directory: /p/foo" in text
    assert "additional repositor" not in text.lower()


def test_project_rail_lists_extra_repos():
    text = _rail("/p/foo", ("/p/bar", "/p/baz"))
    assert "[PROJECT] Active project directory: /p/foo" in text
    assert "- /p/bar" in text
    assert "- /p/baz" in text
    assert "additional repositor" in text.lower()


def test_project_rail_absent_without_a_project():
    assert _rail(None, ("/p/bar",)) == ""


# ── handler wiring: create + PATCH clears ──


def _state(folders: list[dict[str, Any]]) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read(fn: Any) -> Any:
        return fn(state._folders)

    state.mutate_folders = _mutate
    state.read_folders = _read
    return state


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


@pytest.mark.asyncio
async def test_create_stores_repos(tmp_path):
    foo = tmp_path / "foo"
    bar = tmp_path / "bar"
    foo.mkdir()
    bar.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Platform", "project_dir": str(foo), "repos": [str(bar)]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
    assert body["project_dir"] == str(foo.resolve())
    assert body["repos"] == [str(bar.resolve())]


@pytest.mark.asyncio
async def test_create_rejects_non_array_repos():
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Platform", "repos": "not-a-list"},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "repos_invalid"


@pytest.mark.asyncio
async def test_create_rejects_a_repo_equal_to_project_dir(tmp_path):
    foo = tmp_path / "foo"
    foo.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Platform", "project_dir": str(foo), "repos": [str(foo)]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "repos_invalid"


@pytest.mark.asyncio
async def test_folder_with_no_repos_key_is_unchanged(tmp_path):
    """A create without repos stores no ``repos`` key at all (absent = none)."""
    foo = tmp_path / "foo"
    foo.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Platform", "project_dir": str(foo)},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
    assert "repos" not in body


@pytest.mark.asyncio
async def test_patch_empty_list_clears(tmp_path):
    foo = tmp_path / "foo"
    bar = tmp_path / "bar"
    foo.mkdir()
    bar.mkdir()
    folders = [
        {
            "id": "fldr0001",
            "name": "Platform",
            "parent_id": None,
            "order": 0,
            "project_dir": str(foo.resolve()),
            "repos": [str(bar.resolve())],
        }
    ]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"repos": []},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 200, await resp.text()
    assert "repos" not in folders[0]


@pytest.mark.asyncio
async def test_patch_explicit_null_is_refused_and_keeps_the_stored_list(tmp_path):
    foo = tmp_path / "foo"
    bar = tmp_path / "bar"
    foo.mkdir()
    bar.mkdir()
    stored = [str(bar.resolve())]
    folders = [
        {
            "id": "fldr0001",
            "name": "Platform",
            "parent_id": None,
            "order": 0,
            "project_dir": str(foo.resolve()),
            "repos": stored,
        }
    ]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"repos": None},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "repos_invalid"
    assert folders[0]["repos"] == stored


@pytest.mark.asyncio
async def test_patch_rejects_bad_dir(tmp_path):
    folders = [{"id": "fldr0001", "name": "Platform", "parent_id": None, "order": 0}]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"repos": [str(tmp_path / "missing")]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "repos_invalid"
