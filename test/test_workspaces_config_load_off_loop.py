"""The four workspace handlers read their configuration snapshot off the event loop.

``KiroCrewConfig.load()`` is a blocking file read; on the gateway loop it pauses
every chat stream and handler until it returns. Each handler is driven through
its success path with a wrapped loader that records the thread it ran on.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.config.loader import KiroCrewConfig, WorkspaceConfig
from kiro_crew.dashboard.handlers import (
    api_workspaces,
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

_SEL = "kiro_crew.sel.sel"
_LOAD = "kiro_crew.config.loader.KiroCrewConfig.load"
_CFGDIR = "kiro_crew.config.loader.config_dir"
_DATAHOME = "kiro_crew.dashboard.handlers.files.data_home"


def _req(method: str, body: dict | None = None, match_info: dict | None = None) -> web.Request:
    """An owner-authorized mocked request carrying real body bytes."""
    raw = json.dumps(body if body is not None else {}).encode()
    app = web.Application()
    app["state"] = NoConfiguredOwner()
    request = make_mocked_request(
        method,
        "/api/workspaces",
        match_info=match_info or {},
        headers={"Content-Length": str(len(raw)), "Content-Type": "application/json"},
        payload=BodyStreamPayload(raw),
        app=app,
    )
    request["user"] = "local-app"
    request["app"] = ""
    return request


def _seed(directory: Path, workspaces: dict[str, str]) -> KiroCrewConfig:
    """Write *workspaces* as the on-disk config and return the matching snapshot."""
    doc = {
        "workspaces": {n: {"dir": d} for n, d in workspaces.items()},
        "default_workspace": "default",
        "agents": {},
    }
    (directory / "config.json").write_text(json.dumps(doc), encoding="utf-8")
    return KiroCrewConfig(
        workspaces={n: WorkspaceConfig(dir=d) for n, d in workspaces.items()},
        default_workspace="default",
        agents={},
    )


class _RecordingLoad:
    """Stands in for ``KiroCrewConfig.load``; records the thread of each call."""

    def __init__(self, cfg: KiroCrewConfig) -> None:
        self._cfg = cfg
        self.threads: list[int] = []

    def __call__(self, *args: object, **kwargs: object) -> KiroCrewConfig:
        self.threads.append(threading.get_ident())
        return self._cfg


async def _drive(tmp_path: Path, workspaces: dict[str, str], handler, request):
    load = _RecordingLoad(_seed(tmp_path, workspaces))
    with (
        patch(_LOAD, new=load),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
        patch(_SEL),
    ):
        resp = await handler(request)
    return resp, load.threads


def _assert_off_loop(threads: list[int], loop_thread: int) -> None:
    assert threads, "the handler never read its configuration snapshot"
    assert loop_thread not in threads, "KiroCrewConfig.load ran on the event loop thread"


@pytest.mark.asyncio
async def test_list_reads_config_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    resp, threads = await _drive(tmp_path, {"default": "workspace"}, api_workspaces, _req("GET"))
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "workspaces": [{"name": "default", "path": "workspace", "is_default": True}],
        "default": "default",
    }
    _assert_off_loop(threads, loop_thread)


@pytest.mark.asyncio
async def test_create_reads_config_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    resp, threads = await _drive(
        tmp_path,
        {"default": "workspace"},
        api_workspaces_create,
        _req("POST", {"name": "staging"}),
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "name": "staging"}
    _assert_off_loop(threads, loop_thread)


@pytest.mark.asyncio
async def test_update_reads_config_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    (tmp_path / "new-dir").mkdir()
    resp, threads = await _drive(
        tmp_path,
        {"default": "workspace"},
        api_workspaces_update,
        _req("PUT", {"dir": "new-dir"}, match_info={"name": "default"}),
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "name": "default"}
    _assert_off_loop(threads, loop_thread)


@pytest.mark.asyncio
async def test_delete_reads_config_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    resp, threads = await _drive(
        tmp_path,
        {"default": "workspace", "staging": "workspace-staging"},
        api_workspaces_delete,
        _req("DELETE", match_info={"name": "staging"}),
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True}
    _assert_off_loop(threads, loop_thread)
