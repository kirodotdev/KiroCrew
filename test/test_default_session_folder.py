"""The sidebar folder new dashboard chats are filed into (``dashboard.default_session_folder``).

Three layers, each tested where it decides:

* the config loader keeps a trimmed string and reads a non-string as unset;
* ``POST /api/chat/slots`` files a fresh unnamed owner chat that asked for the
  default (``default_folder: true``) and named no folder. Every explicit choice
  wins, other callers keep today's placement, and a default that does not
  resolve leaves the chat unfiled instead of refusing it;
* ``PUT /api/dashboard/config`` accepts only one of the person's folders, and
  never re-judges an unchanged round-tripped value.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.default_session_folder import (
    nearest_default_agent,
    resolve_default_session_folder,
)


def _folder(fid: str, **extra: Any) -> dict[str, Any]:
    return {"id": fid, "name": fid.upper(), "order": 0, "parent_id": "", **extra}


# ── Resolution ──


def test_resolves_an_existing_person_folder():
    folders = [_folder("inbox")]
    assert resolve_default_session_folder(folders, "inbox") == ("inbox", "")


@pytest.mark.parametrize("configured", ["", "gone"])
def test_an_empty_or_missing_default_is_unfiled(configured):
    assert resolve_default_session_folder([_folder("inbox")], configured) == ("", "")


def test_a_folder_another_principal_owns_is_never_the_default():
    folders = [_folder("appfolder", owner_app="some-app")]
    assert resolve_default_session_folder(folders, "appfolder") == ("", "")


def test_the_agent_pin_is_inherited_from_the_nearest_ancestor():
    folders = [
        _folder("root", default_agent="reviewer"),
        _folder("mid", parent_id="root"),
        _folder("leaf", parent_id="mid", default_agent=""),
    ]
    assert resolve_default_session_folder(folders, "leaf") == ("leaf", "reviewer")


def test_a_cyclic_parent_chain_ends_the_walk():
    folders = [_folder("a", parent_id="b"), _folder("b", parent_id="a")]
    assert nearest_default_agent(folders, "a") == ""


# ── Config ──


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  a1b2c3d4e5f6  ", "a1b2c3d4e5f6"), (7, ""), (["x"], ""), (None, "")],
)
def test_config_reads_a_non_string_as_unset(cfg_file, raw, expected):
    cfg_file.write_text(json.dumps({"dashboard": {"default_session_folder": raw}}), "utf-8")
    assert KiroCrewConfig.load().dashboard.default_session_folder == expected


def test_config_default_is_unset():
    assert KiroCrewConfig().dashboard.default_session_folder == ""


# ── POST /api/chat/slots ──


@pytest.fixture
def owner_cfg(tmp_path, monkeypatch):
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    default_dir = tmp_path / "default-project"
    default_dir.mkdir()
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _ws: str(default_dir))
    cfg._test_default_dir = str(default_dir)  # type: ignore[attr-defined]
    return cfg


async def _create(
    state: Any, payload: dict[str, Any], *, owner: bool = True
) -> tuple[int, dict[str, Any]]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app) if owner else app)) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        return resp.status, await resp.json()


@pytest.fixture
def filed_state(tmp_path, owner_cfg):
    owner_cfg.dashboard.default_session_folder = "inbox"
    state = _make_state(tmp_path)
    state._folders = [_folder("inbox"), _folder("work")]
    return state


@pytest.mark.asyncio
async def test_a_new_chat_lands_in_the_default_folder(filed_state):
    status, data = await _create(filed_state, {"default_folder": True})
    assert status == 200, data
    assert data["folder_id"] == "inbox"
    assert filed_state._slots[data["key"]].folder_id == "inbox"


@pytest.mark.asyncio
async def test_an_explicit_folder_wins(filed_state):
    status, data = await _create(filed_state, {"default_folder": True, "folder_id": "work"})
    assert status == 200, data
    assert data["folder_id"] == "work"


@pytest.mark.asyncio
async def test_no_default_set_stays_unfiled(tmp_path, owner_cfg):
    state = _make_state(tmp_path)
    state._folders = [_folder("inbox")]
    status, data = await _create(state, {"default_folder": True})
    assert status == 200, data
    assert not data.get("folder_id")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="not-asked"),
        pytest.param({"default_folder": "yes"}, id="not-true"),
        pytest.param({"default_folder": True, "name": "reopened"}, id="named"),
        pytest.param({"default_folder": True, "ephemeral": True}, id="ephemeral"),
        pytest.param({"default_folder": True, "artifact": "release-notes"}, id="artifact"),
    ],
)
@pytest.mark.asyncio
async def test_other_creates_keep_their_placement(filed_state, payload):
    status, data = await _create(filed_state, payload)
    assert status == 200, data
    assert not data.get("folder_id")


@pytest.mark.asyncio
async def test_a_non_owner_caller_is_not_filed(filed_state):
    status, data = await _create(filed_state, {"default_folder": True}, owner=False)
    assert status == 200, data
    assert not data.get("folder_id")


@pytest.mark.asyncio
async def test_a_deleted_default_leaves_the_chat_unfiled(filed_state):
    filed_state._folders = [_folder("work")]
    status, data = await _create(filed_state, {"default_folder": True})
    assert status == 200, data
    assert not data.get("folder_id")


@pytest.mark.asyncio
async def test_the_folder_project_and_agent_pin_apply(tmp_path, filed_state):
    linked = tmp_path / "linked"
    linked.mkdir()
    filed_state._folders = [_folder("inbox", project_dir=str(linked), default_agent="kirocrew")]
    status, data = await _create(filed_state, {"default_folder": True})
    assert status == 200, data
    assert data["folder_id"] == "inbox"
    assert data["project"] == os.path.realpath(linked)
    assert filed_state._slots[data["key"]].agent == "kirocrew"


@pytest.mark.asyncio
async def test_a_default_with_a_missing_project_leaves_the_chat_unfiled(
    tmp_path, owner_cfg, filed_state
):
    filed_state._folders = [_folder("inbox", project_dir=str(tmp_path / "gone"))]
    status, data = await _create(filed_state, {"default_folder": True})
    assert status == 200, data
    assert not data.get("folder_id")
    assert data["project"] == owner_cfg._test_default_dir


# ── PUT /api/dashboard/config ──


@pytest.fixture()
def config_client_app(cfg_file, tmp_path):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    state = _make_state(tmp_path)
    state._folders = [_folder("inbox"), _folder("appfolder", owner_app="some-app")]
    with patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()):
        app = web.Application()
        app["state"] = state
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        yield as_owner(app)


@pytest.mark.asyncio
async def test_put_sets_and_clears_the_default(config_client_app):
    async with TestClient(TestServer(config_client_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"default_session_folder": "inbox"})
        assert resp.status == 200, await resp.text()
        body = await (await client.get("/api/dashboard/config")).json()
        assert body["default_session_folder"] == "inbox"
        resp = await client.put("/api/dashboard/config", json={"default_session_folder": ""})
        assert resp.status == 200, await resp.text()
    assert KiroCrewConfig.load().dashboard.default_session_folder == ""


@pytest.mark.parametrize("value", ["missing", "appfolder", 7])
@pytest.mark.asyncio
async def test_put_refuses_anything_but_a_person_folder(config_client_app, value):
    async with TestClient(TestServer(config_client_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"default_session_folder": value})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_default_session_folder"
    assert KiroCrewConfig.load().dashboard.default_session_folder == ""


@pytest.mark.asyncio
async def test_an_unchanged_deleted_default_does_not_block_other_saves(config_client_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"default_session_folder": "deletedsince"}}), "utf-8"
    )
    async with TestClient(TestServer(config_client_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={"default_session_folder": "deletedsince", "auto_open_git_panel": True},
        )
        assert resp.status == 200, await resp.text()
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.auto_open_git_panel is True
    assert cfg.dashboard.default_session_folder == "deletedsince"
