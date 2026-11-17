"""The owner's new chat starts its session with no debounce.

A message sent soon after "New chat" waits for whatever is left of the session
start, so the 1.5 s debounce before that start is time the person waits. The
slot-create handler passes ``debounce=0.0`` for the owner's own create; every
other caller keeps ``_EAGER_SPAWN_DEBOUNCE_SECS``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _bindings,
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _pin_prewarm_allowance,
    _prime,
)

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat_runner import schedule_eager_spawn
from kiro_crew.dashboard.state import _ChatSlot

#: Long enough that a start which honoured the module debounce could not finish
#: inside the hang ceiling below.
_LONG_DEBOUNCE = 3600.0
#: Only catches a hang; a start with no debounce finishes in milliseconds.
_HANG_CEILING = 60.0


def _unwaitable_debounce(mp: pytest.MonkeyPatch) -> None:
    """Set a module debounce no run can wait out, so only ``debounce=0.0`` passes."""
    mp.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", _LONG_DEBOUNCE)  # flake-ok: never waited


async def _create(
    state: Any, *, owner: bool, body: dict[str, Any], crew_mode: bool = False
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    real_load = chat_handlers.KiroCrewConfig.load

    def load(*a: Any, **kw: Any) -> Any:
        cfg = real_load(*a, **kw)
        cfg.dashboard.new_chat_crew_mode = crew_mode
        return cfg

    with (
        patch.object(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: calls.append(kw)),
        patch.object(chat_handlers.KiroCrewConfig, "load", load),
    ):
        async with TestClient(TestServer(as_owner(app) if owner else app)) as client:
            resp = await client.post("/api/chat/slots", json={"name": "fresh-chat", **body})
            assert resp.status < 300, await resp.text()
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner, body, crew_mode, expected",
    [
        (True, {}, False, 0.0),
        (False, {}, False, None),
        # A binding change follows the create, so the debounce absorbs it and it
        # never lands on a start holding the key: the client sets the project
        # (the project side is pinned in test_chat_slot_project.py) ...
        (True, {"no_session_folder": True}, False, None),
        # ... or switches the new chat to the conductor (Crew Mode).
        (True, {}, True, None),
    ],
)
async def test_only_the_owners_plain_create_skips_the_debounce(
    tmp_path, owner, body, crew_mode, expected
):
    state = _make_state(tmp_path)
    state.sessions.get_provider.return_value = None
    state.sessions.resumable_sid.return_value = ""
    calls = await _create(state, owner=owner, body=body, crew_mode=crew_mode)
    assert calls, "the create armed no start"
    assert calls[-1].get("debounce") == expected


@pytest.mark.asyncio
async def test_schedule_hands_the_debounce_to_the_start(monkeypatch):
    _unwaitable_debounce(monkeypatch)
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    _prime(True)
    with (
        patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
        patch.object(chat_runner, "resolve_agent_bindings", return_value=_bindings()),
    ):
        task = schedule_eager_spawn(state, slot, debounce=0.0)
        assert task is not None
        await asyncio.wait_for(task, _HANG_CEILING)
    state.sessions.get_or_create.assert_awaited_once()
