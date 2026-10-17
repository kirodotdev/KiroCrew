"""Every session-resetting switch leaves one boundary row in a started conversation.

The agent picker's half lives in ``test_agent_slash_command.py`` beside the
``/agent`` announce line it must not duplicate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state

from kiro_crew.agent_switch_command import switch_announcement
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import (
    api_chat_slot_model,
    api_chat_slot_reasoning_effort,
    api_chat_slots_model,
)
from kiro_crew.dashboard.system_notices import (
    _SWITCH_VALUE_MAX,
    SESSION_SWITCH_KIND,
    SYSTEM_NOTICE_KINDS,
    is_speech_row,
    is_system_notice,
    session_switch_notice,
)


def _switch_rows(slot) -> list[dict]:
    return [m for m in slot.messages if (m.get("meta") or {}).get("kind") == SESSION_SWITCH_KIND]


def _start(slot) -> None:
    slot.append("user", "hello")
    slot.append("assistant", "hi there")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.sessions.reset = AsyncMock()
    return st


@web.middleware
async def _dashboard_user(request, handler):
    # Production's token middleware marks every authenticated request; the
    # bulk route refuses one without the marker.
    if "app" not in request:
        request["app"] = ""
    return await handler(request)


def _app(st) -> web.Application:
    app = _make_app_with_agent_routes(st)
    app.middlewares.insert(0, _dashboard_user)
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    return app


async def _post(st, path: str, body: dict) -> int:
    async with TestClient(TestServer(_app(st))) as client:
        resp = await client.post(path, json=body)
        return resp.status


# ── The row itself ──


def test_the_kind_is_a_system_notice_and_not_speech():
    assert SESSION_SWITCH_KIND in SYSTEM_NOTICE_KINDS
    meta = {"kind": SESSION_SWITCH_KIND}
    text = session_switch_notice("model", "auto")
    assert is_system_notice("assistant", meta)
    assert not is_speech_row("assistant", text, meta)


def test_the_wording_claims_only_the_next_reply():
    text = session_switch_notice("model", "claude-opus-4.8")
    assert text == "Model switched to claude-opus-4.8. Replies from here on use the new model."
    for switch in ("agent", "model", "reasoning_effort", "workspace"):
        line = session_switch_notice(switch, "x").lower()
        assert "forget" not in line and "restarted" not in line


def test_a_request_value_is_shown_on_one_capped_line():
    text = session_switch_notice("workspace", "a\nb  " + "w" * 200)
    assert "\n" not in text
    shown = text.removeprefix("Workspace switched to ").split(". Replies from here on")[0]
    assert shown.startswith("a b w") and len(shown) == 80 and shown.endswith("…")


def test_the_agent_row_reads_the_same_line_as_the_agent_command():
    assert session_switch_notice("agent", "kirocrew-worker") == switch_announcement(
        "kirocrew-worker"
    )


# ── Workspace ──


@pytest.mark.asyncio
async def test_a_workspace_switch_mid_conversation_appends_one_row(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    assert await _post(state, "/api/chat/slots/s1/workspace", {"workspace": "new-ws"}) == 200
    rows = _switch_rows(slot)
    assert [r["content"] for r in rows] == [session_switch_notice("workspace", "new-ws")]
    assert slot.messages[-1] is rows[0]


@pytest.mark.asyncio
async def test_a_workspace_notice_redacts_credentials_before_capping(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    prefix = "https://user:"
    password = "p" * (_SWITCH_VALUE_MAX - 1 - len(prefix))
    workspace = f"{prefix}{password}@example.test/workspace"
    assert workspace.index("@") == _SWITCH_VALUE_MAX - 1

    assert await _post(state, "/api/chat/slots/s1/workspace", {"workspace": workspace}) == 200

    rows = _switch_rows(slot)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert password not in rows[0]["content"]
    assert "[REDACTED" in rows[0]["content"]
    assert slot.messages[-1] is rows[0]


@pytest.mark.asyncio
async def test_a_same_workspace_pick_appends_nothing(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    slot.workspace = "new-ws"
    assert await _post(state, "/api/chat/slots/s1/workspace", {"workspace": "new-ws"}) == 200
    assert _switch_rows(slot) == []
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_switch_on_an_empty_chat_appends_nothing(state):
    slot = state.get_or_create_slot("s1")
    assert await _post(state, "/api/chat/slots/s1/workspace", {"workspace": "new-ws"}) == 200
    assert slot.messages == []


@pytest.mark.asyncio
async def test_a_refused_switch_appends_nothing(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    busy = MagicMock(spec=chat_handlers.LLMProvider)
    busy.has_active_turn.return_value = True
    state.sessions.get_provider = MagicMock(return_value=busy)
    assert await _post(state, "/api/chat/slots/s1/workspace", {"workspace": "new-ws"}) == 409
    assert _switch_rows(slot) == []


# ── Model, single and bulk ──


@pytest.mark.asyncio
async def test_a_model_reset_mid_conversation_appends_one_row(state):
    slot = state.get_or_create_slot("s1", model="claude-opus-4.6")
    _start(slot)
    assert await _post(state, "/api/chat/slots/s1/model", {"model": ""}) == 200
    assert [r["content"] for r in _switch_rows(slot)] == [session_switch_notice("model", "auto")]


@pytest.mark.asyncio
async def test_a_same_model_pick_appends_nothing(state):
    slot = state.get_or_create_slot("s1", model="claude-opus-4.6")
    _start(slot)
    assert await _post(state, "/api/chat/slots/s1/model", {"model": "claude-opus-4.6"}) == 200
    assert _switch_rows(slot) == []


@pytest.mark.asyncio
async def test_the_bulk_switch_marks_only_switched_started_slots(state):
    started = state.get_or_create_slot("a", model="claude-opus-4.6")
    _start(started)
    empty = state.get_or_create_slot("b", model="claude-sonnet-4.6")
    same = state.get_or_create_slot("c", model="claude-opus-4.8")
    _start(same)
    assert await _post(state, "/api/chat/slots/model", {"model": "claude-opus-4.8"}) == 200
    assert [r["content"] for r in _switch_rows(started)] == [
        session_switch_notice("model", "claude-opus-4.8")
    ]
    assert empty.messages == []
    assert _switch_rows(same) == []


@pytest.mark.asyncio
async def test_a_landed_live_model_with_declined_reset_appends_nothing(state, monkeypatch):
    slot = state.get_or_create_slot("s1", model="claude-opus-4.6")
    _start(slot)
    before = list(slot.messages)
    target = "claude-opus-4.8"
    provider = MagicMock(spec=chat_handlers.AcpProvider)
    provider.has_active_turn.return_value = False
    provider.model_pin_refused = ""
    provider.served_model = slot.model
    provider.client = MagicMock(_explicit_pick_epoch=0)

    async def land_model(model):
        provider.served_model = model

    provider.client.set_model = AsyncMock(side_effect=land_model)
    state.sessions.get_provider.return_value = provider
    state.sessions.reset.return_value = False
    monkeypatch.setattr(chat_handlers, "_wire_model_id", lambda *_args: target)
    reapply = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_handlers, "_reapply_effort_after_live_switch", reapply)

    assert await _post(state, "/api/chat/slots/s1/model", {"model": target}) == 200

    provider.client.set_model.assert_awaited_once_with(target)
    reapply.assert_awaited_once_with("s1", slot, provider)
    state.sessions.reset.assert_awaited_once()
    assert state.sessions.get_provider("dashboard:s1") is provider
    assert slot.model == provider.served_model == target
    assert slot.messages == before
    assert _switch_rows(slot) == []


# ── Reasoning effort ──


@pytest.mark.asyncio
async def test_an_effort_reset_mid_conversation_appends_one_row(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    status = await _post(state, "/api/chat/slots/s1/reasoning-effort", {"reasoning_effort": "high"})
    assert status == 200
    assert [r["content"] for r in _switch_rows(slot)] == [
        session_switch_notice("reasoning_effort", "high")
    ]


@pytest.mark.asyncio
async def test_a_same_effort_pick_appends_nothing(state):
    slot = state.get_or_create_slot("s1")
    _start(slot)
    slot.reasoning_effort = "high"
    status = await _post(state, "/api/chat/slots/s1/reasoning-effort", {"reasoning_effort": "high"})
    assert status == 200
    assert _switch_rows(slot) == []
