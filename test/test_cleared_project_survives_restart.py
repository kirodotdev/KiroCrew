"""A project change on an idle chat must survive a restart.

The periodic flush writes a slot's metadata line only while ``_dirty`` is set. A project
clear or move on an idle chat appends no row, so unless the change itself marks the slot,
the line keeps the old ``project`` and the next restore spawns the chat in the directory
the user moved it off.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard.chat_utils import _history_key_for

KEY = "chat-1-idle"


def _restore(home):
    state = _make_state(home / "sessions")
    state.sessions.aprepare_cwd_clear = AsyncMock()
    state.sessions.retract_cwd = MagicMock()
    asyncio.run(cp.restore_open_slots_async(state))
    return state, state._slots[KEY]


@pytest.fixture
def scoped_idle_chat(tmp_path, monkeypatch):
    """An open tab with one finished exchange, scoped to ``old-project`` on disk."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    old = tmp_path / "old-project"
    old.mkdir()
    seed = _make_state(tmp_path / "sessions")
    history_key = _history_key_for(KEY)
    seed.conversation_log.append(history_key, "user", "question")
    seed.conversation_log.append(history_key, "assistant", "answer")
    seed.conversation_log.update_metadata(history_key, {"project": str(old)})
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [KEY], "ts": 0.0}), encoding="utf-8"
    )
    _, slot = _restore(tmp_path)
    assert slot.project == str(old), "precondition: the restore must bind the stored project"
    return tmp_path


async def _over_http(state, project: str) -> None:
    from kiro_crew.dashboard.chat import api_chat_slot_project

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{KEY}/project", json={"project": project})
        assert resp.status == 200, await resp.text()


async def _by_directive(state, project: str) -> None:
    from kiro_crew.dashboard.session_directive_apply import _set_project

    args = {"project": project} if project else {"clear": True}
    result = await _set_project(state, state._slots[KEY], args)
    assert not result.startswith("Error"), result


def _change_flush_restart(home, change, project: str) -> str:
    state, _ = _restore(home)
    asyncio.run(change(state, project))
    assert state._slots[KEY].project == project
    state._flush_dirty_slots()
    _, restored = _restore(home)
    return restored.project


@pytest.mark.parametrize("change", [_over_http, _by_directive], ids=["http", "directive"])
def test_an_idle_chats_cleared_project_stays_cleared_after_a_restart(scoped_idle_chat, change):
    restored = _change_flush_restart(scoped_idle_chat, change, "")

    assert restored == "", (
        "the clear never reached the slot's metadata line, so the restart reopened the "
        f"removed directory; project={restored!r}"
    )


@pytest.mark.parametrize("change", [_over_http, _by_directive], ids=["http", "directive"])
def test_an_idle_chats_new_project_survives_a_restart(scoped_idle_chat, change):
    new = scoped_idle_chat / "new-project"
    new.mkdir()

    restored = _change_flush_restart(scoped_idle_chat, change, str(new))

    assert restored == str(new), f"the restart reopened the old directory; project={restored!r}"
