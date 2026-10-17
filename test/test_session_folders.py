"""A fresh project folder for each new dashboard chat (``dashboard.session_folder_root``).

Three layers, each tested where it decides:

* ``session_folders`` makes the folder: a dated, random-suffixed, owner-only
  directory under a root that must pass the same rules as a hand-picked project
  directory, re-checked on every use.
* ``POST /api/chat/slots`` decides WHEN a folder is made: only for a brand-new
  unnamed owner chat with no explicit project, and a failure falls back to the
  default project with a notice in the response instead of failing the create.
* ``PUT /api/dashboard/config`` refuses a root the gateway would not accept as a
  project directory, and never re-judges an unchanged round-tripped value.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_folders, chat_handlers, session_folders
from kiro_crew.dashboard.session_folders import (
    SessionFolderError,
    create_session_folder,
    validate_session_folder_root,
)

_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$")


def _real(path: Any) -> str:
    return os.path.realpath(str(path))


# ── The folder itself ──


def test_create_makes_a_dated_private_folder_under_the_root(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()

    path = create_session_folder(str(root), today=dt.date(2026, 10, 12))

    assert os.path.dirname(path) == _real(root)
    name = os.path.basename(path)
    assert _NAME_RE.match(name), name
    assert name.startswith("2026-10-12-")
    assert os.path.isdir(path)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


def test_two_creates_on_one_day_get_different_folders(tmp_path):
    first = create_session_folder(str(tmp_path))
    second = create_session_folder(str(tmp_path))
    assert first != second
    assert os.path.isdir(first) and os.path.isdir(second)


def test_an_existing_name_is_never_adopted(tmp_path, monkeypatch):
    taken = tmp_path / "2026-10-12-aaaaaa"
    taken.mkdir()
    (taken / "someone-elses-work.txt").write_text("keep", encoding="utf-8")
    names = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr(session_folders.secrets, "token_hex", lambda _n: next(names))

    path = create_session_folder(str(tmp_path), today=dt.date(2026, 10, 12))

    assert os.path.basename(path) == "2026-10-12-bbbbbb"
    assert (taken / "someone-elses-work.txt").read_text(encoding="utf-8") == "keep"


def test_no_free_name_is_an_error_not_a_reuse(tmp_path, monkeypatch):
    (tmp_path / "2026-10-12-aaaaaa").mkdir()
    monkeypatch.setattr(session_folders.secrets, "token_hex", lambda _n: "aaaaaa")
    with pytest.raises(SessionFolderError, match="free session folder name"):
        create_session_folder(str(tmp_path), today=dt.date(2026, 10, 12))


def test_a_mkdir_failure_is_reported(tmp_path, monkeypatch):
    def refuse(path, mode=0o777):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(session_folders.os, "mkdir", refuse)
    with pytest.raises(SessionFolderError, match="Permission denied"):
        create_session_folder(str(tmp_path))


def test_the_folder_is_locked_to_its_owner(tmp_path, monkeypatch):
    locked: list[str] = []
    monkeypatch.setattr(session_folders.platform_compat, "restrict_dir_to_owner", locked.append)
    path = create_session_folder(str(tmp_path))
    assert locked == [path]


def test_a_folder_that_cannot_be_made_private_is_removed(tmp_path, monkeypatch):
    def refuse(_path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(session_folders.platform_compat, "restrict_dir_to_owner", refuse)
    with pytest.raises(SessionFolderError, match="private"):
        create_session_folder(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_a_folder_swapped_for_a_link_is_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "root"
    root.mkdir()

    def swap(path, mode=0o777):
        os.symlink(str(elsewhere), path)

    monkeypatch.setattr(session_folders.os, "mkdir", swap)
    with pytest.raises(SessionFolderError, match="replaced"):
        create_session_folder(str(root))


# ── The root ──


def test_empty_root_is_off():
    assert validate_session_folder_root("") == ("", None)
    assert validate_session_folder_root("   ") == ("", None)


def test_relative_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    resolved, err = validate_session_folder_root("rel")
    assert resolved == ""
    assert err and "absolute" in err


def test_missing_root_is_refused(tmp_path):
    resolved, err = validate_session_folder_root(str(tmp_path / "nope"))
    assert resolved == ""
    assert err and "existing directory" in err


def test_sensitive_root_is_refused_and_nothing_is_created(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    monkeypatch.setattr(chat_folders, "is_sensitive_path", lambda p: p == _real(root))

    resolved, err = validate_session_folder_root(str(root))
    assert resolved == ""
    assert err and "sensitive" in err
    with pytest.raises(SessionFolderError, match="sensitive"):
        create_session_folder(str(root))
    assert list(root.iterdir()) == []


def test_a_nul_in_the_root_is_refused_not_raised(tmp_path):
    root = f"{tmp_path}/\x00"
    resolved, err = validate_session_folder_root(root)
    assert resolved == ""
    assert err and "NUL" in err
    with pytest.raises(SessionFolderError, match="NUL"):
        create_session_folder(root)


def test_unc_root_is_refused_before_resolution(monkeypatch):
    def must_not_resolve(_raw):
        raise AssertionError("a share path must not be resolved")

    monkeypatch.setattr(chat_folders, "_validate_project_dir", must_not_resolve)
    resolved, err = validate_session_folder_root("\\\\server\\share\\sessions")
    assert resolved == ""
    assert err and "local directory" in err


def test_data_home_overlap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_folders, "voice_runtime_workspace_conflict", lambda _p: "overlaps the data home"
    )
    resolved, err = validate_session_folder_root(str(tmp_path))
    assert (resolved, err) == ("", "overlaps the data home")


def test_config_reads_a_non_string_root_as_off(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"dashboard": {"session_folder_root": ["/tmp"]}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        assert KiroCrewConfig.load().dashboard.session_folder_root == ""
    assert KiroCrewConfig().dashboard.session_folder_root == ""


# ── POST /api/chat/slots ──


@pytest.fixture
def owner_cfg(tmp_path, monkeypatch):
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    default_dir = tmp_path / "default-project"
    default_dir.mkdir()
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _ws: str(default_dir))
    cfg._test_default_dir = str(default_dir)  # type: ignore[attr-defined]
    return cfg


@pytest.fixture
def folder_root(tmp_path):
    root = tmp_path / "session-root"
    root.mkdir()
    return root


async def _create(
    state: Any, payload: dict[str, Any], *, owner: bool = True
) -> tuple[int, dict[str, Any]]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app) if owner else app)) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_new_chat_gets_a_fresh_folder_as_its_project(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    state = _make_state(tmp_path)

    status, data = await _create(state, {})

    assert status == 200, data
    project = data["project"]
    assert os.path.dirname(project) == _real(folder_root)
    assert _NAME_RE.match(os.path.basename(project))
    assert os.path.isdir(project)
    assert state._slots[data["key"]].project == project
    assert "session_folder_notice" not in data


@pytest.mark.asyncio
async def test_root_unset_keeps_the_default_project(tmp_path, owner_cfg, folder_root):
    state = _make_state(tmp_path)
    status, data = await _create(state, {})
    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert "session_folder_notice" not in data
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_caller_opt_out_wins(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    state = _make_state(tmp_path)

    status, data = await _create(state, {"no_session_folder": True})

    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_an_artifact_companion_chat_gets_no_folder(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    state = _make_state(tmp_path)
    status, data = await _create(state, {"artifact": "release-notes"})
    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_project_linked_folder_wins(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    linked = tmp_path / "linked"
    linked.mkdir()
    state = _make_state(tmp_path)
    state._folders = [{"id": "f1", "name": "Work", "order": 0, "project_dir": str(linked)}]

    status, data = await _create(state, {"folder_id": "f1"})

    assert status == 200, data
    assert data["project"] == _real(linked)
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_named_create_gets_no_folder(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    state = _make_state(tmp_path)
    status, data = await _create(state, {"name": "reopened"})
    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_non_owner_create_gets_no_folder(tmp_path, owner_cfg, folder_root):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    state = _make_state(tmp_path)
    status, data = await _create(state, {}, owner=False)
    assert status == 200, data
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_refused_root_falls_back_with_a_notice(
    tmp_path, owner_cfg, folder_root, monkeypatch
):
    owner_cfg.dashboard.session_folder_root = str(folder_root)
    monkeypatch.setattr(chat_folders, "is_sensitive_path", lambda p: p == _real(folder_root))
    state = _make_state(tmp_path)

    status, data = await _create(state, {})

    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert data["session_folder_notice"] == "session_folder_unavailable"
    assert list(folder_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_create_failure_falls_back_with_a_notice(tmp_path, owner_cfg, monkeypatch):
    owner_cfg.dashboard.session_folder_root = str(tmp_path / "gone")
    state = _make_state(tmp_path)

    status, data = await _create(state, {})

    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert data["session_folder_notice"] == "session_folder_unavailable"


@pytest.mark.asyncio
async def test_a_hand_edited_nul_root_falls_back_with_a_notice(tmp_path, owner_cfg):
    owner_cfg.dashboard.session_folder_root = f"{tmp_path}/\x00"
    state = _make_state(tmp_path)

    status, data = await _create(state, {})

    assert status == 200, data
    assert data["project"] == owner_cfg._test_default_dir
    assert data["session_folder_notice"] == "session_folder_unavailable"


# ── PUT /api/dashboard/config ──


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def config_app(cfg_file):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    m = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        app = web.Application()
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        yield as_owner(app)


@pytest.mark.asyncio
async def test_put_stores_the_resolved_root_and_get_echoes_it(config_app, folder_root):
    async with TestClient(TestServer(config_app)) as client:
        resp = await client.put(
            "/api/dashboard/config", json={"session_folder_root": f"  {folder_root}  "}
        )
        assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().dashboard.session_folder_root == _real(folder_root)
        body = await (await client.get("/api/dashboard/config")).json()
        assert body["session_folder_root"] == _real(folder_root)


@pytest.mark.asyncio
async def test_put_empty_turns_it_off(config_app, cfg_file, folder_root):
    cfg_file.write_text(
        json.dumps({"dashboard": {"session_folder_root": _real(folder_root)}}), encoding="utf-8"
    )
    async with TestClient(TestServer(config_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"session_folder_root": ""})
        assert resp.status == 200, await resp.text()
    assert KiroCrewConfig.load().dashboard.session_folder_root == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["relative", "missing", "sensitive", "nul", "not-a-string", "too-long"]
)
async def test_put_refuses_a_root_that_is_not_a_usable_project_dir(
    config_app, folder_root, monkeypatch, kind
):
    value: Any = {
        "relative": "some/dir",
        "missing": str(folder_root / "missing"),
        "sensitive": str(folder_root),
        "nul": f"{folder_root}/\x00",
        "not-a-string": 7,
        "too-long": "/" + "a" * session_folders.SESSION_FOLDER_ROOT_MAX_CHARS,
    }[kind]
    if kind == "sensitive":
        monkeypatch.setattr(chat_folders, "is_sensitive_path", lambda p: p == _real(folder_root))
    async with TestClient(TestServer(config_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"session_folder_root": value})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_session_folder_root"
    assert KiroCrewConfig.load().dashboard.session_folder_root == ""


@pytest.mark.asyncio
async def test_an_unchanged_stale_root_does_not_block_other_saves(config_app, cfg_file, tmp_path):
    stale = str(tmp_path / "deleted-since")
    cfg_file.write_text(json.dumps({"dashboard": {"session_folder_root": stale}}), encoding="utf-8")
    async with TestClient(TestServer(config_app)) as client:
        resp = await client.put(
            "/api/dashboard/config",
            json={"session_folder_root": stale, "auto_open_git_panel": True},
        )
        assert resp.status == 200, await resp.text()
    cfg = KiroCrewConfig.load()
    assert cfg.dashboard.auto_open_git_panel is True
    assert cfg.dashboard.session_folder_root == stale
