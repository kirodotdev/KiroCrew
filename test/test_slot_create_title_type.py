"""``POST /api/chat/slots`` checks the ``title`` type before it makes a slot.

The title is applied after the mint, where ``.strip()`` on a non-string value
would raise. The create answers 400 ``invalid_field_type`` for such a title
and builds nothing. A string, ``null``, a missing title and every other falsy
value give an untitled or titled chat as usual.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers


def _state(tmp_path, monkeypatch) -> Any:
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    state = _make_state(tmp_path)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    return state


async def _post(state: Any, body: dict) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body)
        if response.content_type != "application/json":
            # A 500 is plain text; keep it so the status assert reports it.
            return response.status, {"text": await response.text()}
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [123, True, ["a"], {"a": 1}])
@pytest.mark.parametrize("name", [None, "named-chat"])
async def test_a_non_string_title_is_refused_before_any_slot_is_made(
    tmp_path, monkeypatch, title, name
) -> None:
    """Fails on main: the title was read after the mint, so ``.strip()`` raised
    and the create answered 500, after it had already logged a session-create
    audit row for a chat that never opened. Now it is a 400, the slot table is
    unchanged and no create is audited.
    """
    from kiro_crew.shared_crew import audit

    audited: list[Any] = []
    monkeypatch.setattr(audit, "schedule_action_audit", lambda *a, **kw: audited.append(a))
    state = _state(tmp_path, monkeypatch)
    slots_before = set(state._slots)
    body: dict[str, Any] = {"title": title}
    if name:
        body["name"] = name

    status, data = await _post(state, body)

    assert status == 400, data
    assert data == {"error": "title must be a string", "code": "invalid_field_type"}
    assert set(state._slots) == slots_before
    assert audited == []


@pytest.mark.asyncio
async def test_a_string_title_is_pinned_as_before(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)

    status, data = await _post(state, {"title": "  My chat  "})

    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot.title == "My chat"
    assert slot._title_origin == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"title": None}, {"title": ""}, {"title": 0}, {"title": []}])
async def test_a_missing_or_empty_title_still_creates_an_untitled_slot(
    tmp_path, monkeypatch, body
) -> None:
    """Back-compat: every value that worked before still makes a slot with no
    pinned title."""
    state = _state(tmp_path, monkeypatch)

    status, data = await _post(state, body)

    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot._title_origin != "user"
