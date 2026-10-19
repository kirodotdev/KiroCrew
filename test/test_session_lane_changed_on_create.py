"""``SessionLaneChanged`` from folder inheritance on slot create — the third site.

This is the emit site a two-handler design misses, and the reason the RFC's
write-surface table is wider than the issue that prompted it: a folder carrying a
STATUS tag puts a brand-new session into a lane without either tag handler running.
``validate_folder_tag_ids`` screens a folder tag id's shape and vocabulary but NOT
its ``status`` field, so nothing else on that path notices the lane entry.

Separate module from the route tests because the create handler needs its own app
and a seeded folder store, not the tags app.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew import hooks as H
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    ScriptHookStore,
    set_global_hook_store,
)

FOLDER_ID = "f-design"


@pytest.fixture(autouse=True)
def _reset_global_store(monkeypatch):
    """Unbind the process-global hook store, and permit the governance capability.

    Same two reasons as the emitter module: a store rooted at this test's
    ``tmp_path`` left bound is reachable by any later test that moves a status
    tag, and ``capabilities.script_hooks`` defaults to OFF, so without this the
    fire assertions pass only on a host whose config happens to enable it.
    """
    set_global_hook_store(None)  # type: ignore[arg-type]
    monkeypatch.setattr("kiro_crew.hooks._script_hooks_capability_denied", lambda sk="": None)
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


def _make_state(tmp_path, folder_tags=None):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    folder = {"id": FOLDER_ID, "name": "Design Review", "order": 0}
    if folder_tags is not None:
        folder["tags"] = list(folder_tags)
    state._folders.append(folder)
    return state


def _seed_vocabulary(state, tags):
    """``state._tags`` holds the tag OBJECTS; a slot holds only their ids."""
    state._tags = list(tags)
    # The constructor fails closed until load_tags() runs, so a directly seeded
    # vocabulary has to declare itself authoritative or the id screen drops it.
    state._tags_authoritative = True


def _make_app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    return app


def _recording_store(tmp_path):
    out = tmp_path / "fired.jsonl"
    script = tmp_path / "lane_hook.sh"
    script.write_text(f"#!/bin/bash\ncat >> {out}\n", encoding="utf-8")
    script.chmod(0o755)
    # Built through the real write path, not by assigning `_hooks`. That
    # attribute is a dict keyed by id, so a list assigned to it makes every
    # reader iterate ID STRINGS -- which is exactly the production defect these
    # tests exist to catch, and a fixture that fakes the shape cannot see it.
    store = ScriptHookStore(tmp_path)
    store.create(
        {
            "name": "lane",
            "event": HOOK_EVENT_SESSION_LANE_CHANGED,
            "command": str(script),
            "enabled": True,
        }
    )
    set_global_hook_store(store)
    return store, out


async def _fired(out, *, timeout=30.0):
    """The payloads the hook recorded, once every dispatched task has finished.

    Awaits the dispatcher's own task set rather than sleeping. A fixed sleep is not
    a completion barrier in either direction: a loaded runner makes the positive
    assertion flake, and an absence assertion passes before the work it claims to
    exclude has started, so a dispatcher that fired late would read as one that
    correctly stayed silent.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [t for t in list(H._lane_dispatch_tasks) if not t.done()]
        if not pending:
            break
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"{len(pending)} lane dispatch task(s) still running")
        await asyncio.wait(pending, timeout=remaining)

    if not out.exists():
        return []
    return [json.loads(x) for x in out.read_text().splitlines() if x.strip()]


class TestFolderInheritanceFires:
    @pytest.mark.asyncio
    async def test_a_folders_status_tag_fires_on_the_new_session(self, tmp_path):
        """Creating a chat inside a folder whose tags include a lane fires once.

        Neither tag handler runs here, so this passing is the whole point: the event
        covers the path that reaches a lane through creation rather than through a
        move.
        """
        done = {"id": "done12", "name": "Done", "status": True, "order": 0}
        state = _make_state(tmp_path, folder_tags=[done["id"]])
        _seed_vocabulary(state, [done])
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})
            assert resp.status == 200
        assert state._slots["s1"].tags == [done["id"]]

        rows = await _fired(out)
        assert len(rows) == 1, f"expected exactly one fire, got {rows}"
        assert rows[0]["hook_event_name"] == HOOK_EVENT_SESSION_LANE_CHANGED
        assert rows[0]["added"] == [done["id"]]
        assert rows[0]["removed"] == []

    @pytest.mark.asyncio
    async def test_a_folders_plain_label_does_not_fire(self, tmp_path):
        """The control: an inherited NON-status tag is not a lane entry.

        Without this, an emitter that fired on any inherited tag would pass the test
        above while making the event chatty for exactly the auto-tag traffic the
        status-only contract exists to exclude.
        """
        label = {"id": "label56", "name": "plain", "order": 0}
        state = _make_state(tmp_path, folder_tags=[label["id"]])
        _seed_vocabulary(state, [label])
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})
            assert resp.status == 200
        assert state._slots["s1"].tags == [label["id"]]
        assert await _fired(out) == []

    @pytest.mark.asyncio
    async def test_a_folder_with_no_tags_fires_nothing(self, tmp_path):
        """``lane_move`` stays None when nothing is appended.

        It is bound at function scope rather than inside the inheritance branch, and
        this is the case that would raise ``NameError`` if it were not.
        """
        state = _make_state(tmp_path)
        _seed_vocabulary(state, [])
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1", "folder_id": FOLDER_ID})
            assert resp.status == 200
        assert await _fired(out) == []
