"""Old relay chats load as read-only archives and every run path refuses them.

The turn relay that ran a local slot on a peer crew is gone. A slot whose
metadata line still says ``executor == "remote"`` comes back as an archive:
it reads like it did, and the send, turn-restarting, chokepoint and peer-side
paths all refuse to run it. The MCP session tools, the OpenAI-compatible
endpoint, the header picks and the checklist pill pin the same 409 in their own
test files (``test_session_control*``, ``test_openai_compat``,
``test_chat_agent_kind``, ``test_todo_pill_resync``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.peer_redaction import redact_peer_text
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    is_relay_archive,
    relay_archive_refusal,
)
from kiro_crew.dashboard.state import _ChatSlot


def _archive(key: str = "chat-1", *, complete: bool = True) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.executor = "remote"
    if complete:
        slot.instance_id = "peer-1"
        slot.remote_slot = "peer-chat-9"
    return slot


def _app(state, *, app_name: str = "") -> web.Application:
    """The send and turn-restarting handlers behind one owner-authenticated server."""
    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_continue
    from kiro_crew.dashboard.chat_regenerate import (
        api_chat_slot_edit_resend,
        api_chat_slot_regenerate,
    )
    from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

    @web.middleware
    async def _auth(request, handler):
        request["app"] = app_name
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


@pytest.fixture
def ready(monkeypatch):
    """Readiness is orthogonal to the archive guard; stub it so a 503 cannot mask it."""

    async def _ok(_request):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified", _ok)
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind.reject_if_kiro_unverified", _ok)


def _with_a_turn(state, slot: _ChatSlot) -> _ChatSlot:
    slot.append("user", "first question", "msg msg-u")
    slot.append("assistant", "first answer", "msg msg-a")
    state._slots[slot.key] = slot
    return slot


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "half-bound"])
def test_the_refusal_keys_on_executor_alone(complete):
    assert is_relay_archive(_archive(complete=complete))
    refusal = relay_archive_refusal(_archive(complete=complete))
    assert refusal is not None and refusal.status == 409
    assert relay_archive_refusal(_ChatSlot("local")) is None


@pytest.mark.asyncio
async def test_a_send_is_refused_before_the_user_row_is_recorded(tmp_path):
    state = _make_state(tmp_path)
    slot = _with_a_turn(state, _archive())
    before = list(slot.messages)

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "again"})
        assert resp.status == 409
        assert (await resp.json())["code"] == RELAY_ARCHIVE_CODE

    assert slot.messages == before
    assert slot.task is None


@pytest.mark.asyncio
async def test_a_relayed_send_from_an_older_owner_is_refused_before_any_slot_exists(tmp_path):
    state = _make_state(tmp_path)

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/send?relay=1", json={"slot": "peer-chat-9", "message": "run it"}
        )
        assert resp.status == 410
        assert (await resp.json())["code"] == "remote_relay_retired"

    assert "peer-chat-9" not in state._slots


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/chat/slots/chat-1/regenerate", None),
        ("/api/chat/slots/chat-1/edit-resend", {"index": 0, "content": "edited"}),
        ("/api/chat/slots/chat-1/rewind", {"at_message_index": 0, "content": "edited"}),
        ("/api/chat/slots/chat-1/continue", None),
    ],
)
@pytest.mark.asyncio
async def test_each_turn_restarting_action_is_refused_and_nothing_is_truncated(
    tmp_path, ready, path, body
):
    state = _make_state(tmp_path)
    slot = _with_a_turn(state, _archive())
    before = list(slot.messages)

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(path, json=body)
        assert resp.status == 409, path
        assert (await resp.json())["code"] == RELAY_ARCHIVE_CODE

    assert slot.messages == before
    assert slot.task is None


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/chat/slots/chat-1/rewind", {"at_message_index": 0, "content": "edited"}),
        ("/api/chat/slots/chat-1/continue", None),
    ],
)
@pytest.mark.asyncio
async def test_a_foreign_app_gets_the_anti_enumeration_404_not_the_409(tmp_path, ready, path, body):
    state = _make_state(tmp_path)
    _with_a_turn(state, _archive())

    async with TestClient(TestServer(_app(state, app_name="notes"))) as client:
        resp = await client.post(path, json=body)
        assert resp.status == 404, path
        assert (await resp.json())["code"] == "slot_not_found"


@pytest.mark.asyncio
async def test_run_chat_refuses_an_archive_before_any_execution(tmp_path):
    from kiro_crew.dashboard.chat_runner import _run_chat

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _archive()
    state._slots[slot.key] = slot

    await _run_chat(state, slot, "run this here")

    assert slot.messages[-1]["role"] == "error"
    assert "read-only" in slot.messages[-1]["content"]
    assert any(c.args[0] == "chat_done" for c in state.broadcast_ws.call_args_list)
    assert not any(m["role"] == "assistant" for m in slot.messages)


def test_an_old_relay_slot_loads_as_a_read_only_archive(tmp_path):
    """A line written by the relay build, mid-relay marker and all, restores whole."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "inspect the page", "msg msg-u")
    slot.append("assistant", "the crew's answer", "msg msg-a")
    assert _save_slot_to_history(state, slot, force=True)
    assert state.conversation_log is not None
    state.conversation_log.update_metadata(
        "dashboard:chat-1",
        {
            "executor": "remote",
            "instance_id": "peer-1",
            "remote_slot": "peer-chat-9",
            "relay_in_flight": True,
        },
    )
    del state._slots["chat-1"]

    restored = _rehydrate_slot_from_history(state, "chat-1")

    assert restored is not None
    assert (restored.executor, restored.instance_id, restored.remote_slot) == (
        "remote",
        "peer-1",
        "peer-chat-9",
    )
    assert [m["role"] for m in restored.messages] == ["user", "assistant"]
    assert state.serialize_slot(restored)["row_identity"] == "chat-1"
    assert relay_archive_refusal(restored) is not None


def test_peer_text_is_redacted_and_clean_text_passes_unchanged():
    secret = "key AKIAIOSFODNN7EXAMPLE here"
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_peer_text(secret)
    assert redact_peer_text("plain words") == "plain words"
