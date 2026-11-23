"""The workspace create and update handlers stat and resolve paths off the event loop.

``Path.is_dir`` and ``Path.resolve`` are blocking filesystem calls; on a mount that
stopped answering they would stall every chat stream. Each handler is driven through
its success path with the two calls wrapped to record the thread they ran on.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from test_workspaces_config_load_off_loop import _CFGDIR, _DATAHOME, _LOAD, _SEL, _req, _seed

from kiro_crew.dashboard.handlers import api_workspaces_create, api_workspaces_update


async def _drive(
    tmp_path: Path, handler, request, watched: Path
) -> tuple[web.Response, list[int], list[int]]:
    """Run *handler*; return the response and the threads that resolved or stat'ed *watched*."""
    resolve_threads: list[int] = []
    is_dir_threads: list[int] = []
    real_resolve = Path.resolve
    real_is_dir = Path.is_dir

    def resolve(self: Path) -> Path:
        if self == tmp_path:
            resolve_threads.append(threading.get_ident())
        return real_resolve(self)

    def is_dir(self: Path) -> bool:
        if self == watched:
            is_dir_threads.append(threading.get_ident())
        return real_is_dir(self)

    cfg = _seed(tmp_path, {"default": "workspace"})
    with (
        patch(_LOAD, return_value=cfg),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
        patch(_SEL),
        patch.object(Path, "resolve", resolve),
        patch.object(Path, "is_dir", is_dir),
    ):
        resp = await handler(request)
    return resp, resolve_threads, is_dir_threads


@pytest.mark.asyncio
async def test_create_resolves_the_data_home_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    resp, resolves, _ = await _drive(
        tmp_path, api_workspaces_create, _req("POST", {"name": "staging"}), tmp_path
    )
    assert resp.status == 200
    assert resolves, "the create never resolved the data home"
    assert loop_thread not in resolves, "the data home was resolved on the event loop thread"


@pytest.mark.asyncio
async def test_copy_create_checks_the_source_folder_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "notes.txt").write_text("kept", encoding="utf-8")
    resp, _, is_dirs = await _drive(
        tmp_path,
        api_workspaces_create,
        _req("POST", {"name": "copy", "copy_from": "default"}),
        source,
    )
    assert resp.status == 200
    assert (tmp_path / "workspace-copy" / "notes.txt").read_text(encoding="utf-8") == "kept"
    assert is_dirs, "the create never checked the source folder"
    assert loop_thread not in is_dirs, "the source folder was checked on the event loop thread"


@pytest.mark.asyncio
async def test_update_resolves_the_data_home_off_the_loop(tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    (tmp_path / "new-dir").mkdir()
    resp, resolves, _ = await _drive(
        tmp_path,
        api_workspaces_update,
        _req("PUT", {"dir": "new-dir"}, match_info={"name": "default"}),
        tmp_path,
    )
    assert resp.status == 200
    assert resolves, "the update never resolved the data home"
    assert loop_thread not in resolves, "the data home was resolved on the event loop thread"
