"""POST /api/chat/slots/{slot}/project probes the caller's path off the event loop.

The project path is the caller's, so a dead mount there must not stall every
session's turn: its ``realpath`` and ``isdir`` run on the request-path probe
pool, and a full pool answers the shared busy response instead of waiting.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_chat_slot_project import _make_app, _mock_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.file_api.path_probe import _PathProbeBusy
from kiro_crew.dashboard.state import _ChatSlot


@pytest.mark.asyncio
async def test_the_project_path_is_resolved_and_checked_off_the_loop(tmp_path) -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real_realpath, real_isdir = os.path.realpath, os.path.isdir

    def realpath(path, *args, **kwargs):
        if str(path) == str(tmp_path):
            seen["realpath"] = threading.get_ident()
        return real_realpath(path, *args, **kwargs)

    def isdir(path):
        if str(path) == real_realpath(str(tmp_path)):
            seen.setdefault("isdir", threading.get_ident())
        return real_isdir(path)

    slot = _ChatSlot("test")
    state = _mock_state(slot)
    with (
        patch("kiro_crew.dashboard.chat_handlers._save_recent_project"),
        patch.object(os.path, "realpath", realpath),
        patch.object(os.path, "isdir", isdir),
    ):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project", json={"project": str(tmp_path)}
            )
            assert resp.status == 200
    assert set(seen) == {"realpath", "isdir"}
    assert seen["realpath"] != loop_thread, "the project path was resolved on the event loop"
    assert seen["isdir"] != loop_thread, "the project folder was checked on the event loop"
    assert slot.project == real_realpath(str(tmp_path))


@pytest.mark.asyncio
async def test_a_full_probe_pool_answers_busy_and_leaves_the_slot_alone(tmp_path) -> None:
    async def busy(*_args, **_kwargs):
        raise _PathProbeBusy()

    slot = _ChatSlot("test")
    state = _mock_state(slot)
    with patch.object(chat_handlers, "_run_path_probe", busy):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/project", json={"project": str(tmp_path)}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "path_probe_busy"
    assert slot.project in ("", None)


@pytest.mark.asyncio
async def test_a_file_is_still_refused_as_not_a_directory(tmp_path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/test/project", json={"project": str(target)})
        assert resp.status == 400
        assert (await resp.json())["error"] == "Not a directory"
