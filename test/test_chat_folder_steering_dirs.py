"""Folder ``steering_dirs``: validation, accumulative inheritance, PATCH clears.

A chat folder may carry extra steering directories loaded for every chat in its
subtree. They validate per directory like ``project_dir`` (absolute, existing,
not sensitive) plus a list cap and no duplicates, resolve ACCUMULATIVELY up the
``parent_id`` chain (unlike the nearest-wins project resolver), and a PATCH with
an empty list clears them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import (
    MAX_FOLDER_STEERING_DIRS,
    _resolve_folder_steering_dirs,
    _validate_steering_dirs,
    api_chat_folder_create,
    api_chat_folder_update,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── validation ──


def test_validate_rejects_relative_path():
    resolved, err = _validate_steering_dirs(["not/absolute"])
    assert resolved == []
    assert err and "absolute" in err.lower()


def test_validate_rejects_missing_directory(tmp_path):
    missing = str(tmp_path / "nope")
    resolved, err = _validate_steering_dirs([missing])
    assert resolved == []
    assert err and "existing directory" in err.lower()


def test_validate_rejects_sensitive_path(tmp_path, monkeypatch):
    real = tmp_path / "standards"
    real.mkdir()
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.is_sensitive_path", lambda p: True)
    resolved, err = _validate_steering_dirs([str(real)])
    assert resolved == []
    assert err and "sensitive" in err.lower()


def test_validate_rejects_over_cap(tmp_path):
    dirs = []
    for i in range(MAX_FOLDER_STEERING_DIRS + 1):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(str(d))
    resolved, err = _validate_steering_dirs(dirs)
    assert resolved == []
    assert err and str(MAX_FOLDER_STEERING_DIRS) in err


def test_validate_rejects_duplicates(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    # Two spellings of one directory still count as a duplicate (compared by the
    # resolved realpath).
    resolved, err = _validate_steering_dirs([str(d), str(d) + "/."])
    assert resolved == []
    assert err and "repeat" in err.lower()


def test_validate_accepts_empty_and_none():
    assert _validate_steering_dirs([]) == ([], None)
    assert _validate_steering_dirs(None) == ([], None)


def test_validate_resolves_and_dedups_ok(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    resolved, err = _validate_steering_dirs([str(a), str(b)])
    assert err is None
    assert resolved == [str(a.resolve()), str(b.resolve())]


# ── accumulative inheritance + cycle guard ──


def test_resolver_accumulates_root_first(tmp_path):
    org = tmp_path / "org"
    repo = tmp_path / "repo"
    org.mkdir()
    repo.mkdir()
    folders: list[dict[str, Any]] = [
        {"id": "root", "parent_id": None, "steering_dirs": [str(org)]},
        {"id": "child", "parent_id": "root", "steering_dirs": [str(repo)]},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "child")
    assert err is None
    # Root ancestor's dirs lead, child's follow.
    assert resolved == [str(org.resolve()), str(repo.resolve())]


def test_resolver_dedups_across_levels(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folders: list[dict[str, Any]] = [
        {"id": "root", "parent_id": None, "steering_dirs": [str(shared)]},
        {"id": "child", "parent_id": "root", "steering_dirs": [str(shared)]},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "child")
    assert err is None
    assert resolved == [str(shared.resolve())]


def test_resolver_cycle_guarded(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    # A parent_id cycle must terminate, not spin.
    folders: list[dict[str, Any]] = [
        {"id": "x", "parent_id": "y", "steering_dirs": [str(a)]},
        {"id": "y", "parent_id": "x"},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "x")
    assert err is None
    assert resolved == [str(a.resolve())]


def test_resolver_empty_when_no_dirs():
    folders = [{"id": "root", "parent_id": None}]
    assert _resolve_folder_steering_dirs(folders, "root") == ([], None)


def test_resolver_refile_reresolves_to_new_folder(tmp_path):
    # Re-filing a slot from one folder to another re-resolves its effective
    # steering dirs from the NEW folder_id — the seam resolves per folder_id, so
    # a slot moved between these folders sees a different result each time.
    orig = tmp_path / "orig"
    dest = tmp_path / "dest"
    orig.mkdir()
    dest.mkdir()
    folders = [
        {"id": "a", "parent_id": None, "steering_dirs": [str(orig)]},
        {"id": "b", "parent_id": None, "steering_dirs": [str(dest)]},
    ]
    from_a, _ = _resolve_folder_steering_dirs(folders, "a")
    from_b, _ = _resolve_folder_steering_dirs(folders, "b")
    assert from_a == [str(orig.resolve())]
    assert from_b == [str(dest.resolve())]


def test_resolver_rejects_stored_relative_dir():
    # folders.json is not trusted: a stored relative path fails re-validation
    # rather than being handed to the collector.
    folders = [{"id": "a", "parent_id": None, "steering_dirs": ["relative/dir"]}]
    resolved, err = _resolve_folder_steering_dirs(folders, "a")
    assert resolved == []
    assert err is not None


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
async def test_create_stores_steering_dirs(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": [str(d)]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
    assert body["steering_dirs"] == [str(d.resolve())]


@pytest.mark.asyncio
async def test_create_rejects_non_array_steering_dirs():
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": "not-a-list"},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"


@pytest.mark.asyncio
async def test_patch_empty_list_clears(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    folders = [
        {
            "id": "fldr0001",
            "name": "Org",
            "parent_id": None,
            "order": 0,
            "steering_dirs": [str(d.resolve())],
        }
    ]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": []},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 200, await resp.text()
    # Empty list drops the key entirely, so "absent means none" stays canonical.
    assert "steering_dirs" not in folders[0]


@pytest.mark.asyncio
async def test_patch_rejects_bad_dir(tmp_path):
    folders = [{"id": "fldr0001", "name": "Org", "parent_id": None, "order": 0}]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": [str(tmp_path / "missing")]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"
