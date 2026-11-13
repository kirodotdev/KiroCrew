"""The slot payload carries the transcript key reopen-closed-session resumes by.

``to_dict()["history_key"]`` is ``slot_history_key`` passed through unfolded,
the session key ``/api/chat/resume`` reads. The dashboard's Cmd/Ctrl+Shift+T
reopen resumes by it; the client cannot rebuild it for an unbound channel-born
slot, which carries no ``linked_session_key`` and no origin link.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


def test_dashboard_slot_history_key_is_its_dashboard_transcript_key():
    assert _ChatSlot("chat-1").to_dict()["history_key"] == "dashboard:chat-1"


def test_linked_slot_history_key_is_its_linked_session_key():
    slot = _ChatSlot("chat-2")
    slot.linked_session_key = "slack:C0ABC:1790411186.442029"
    assert slot.to_dict()["history_key"] == "slack:C0ABC:1790411186.442029"


def test_unbound_channel_slot_history_key_is_the_channel_transcript_key():
    # The case the client cannot derive: channel-born, no binding, no link.
    slot = _ChatSlot("slack_1785370133.085469")
    slot.channel_origin = True
    assert slot.linked_session_key == ""
    assert slot.to_dict()["history_key"] == slot_history_key(slot)
    assert not slot.to_dict()["history_key"].startswith("dashboard")


@pytest.mark.asyncio
async def test_fresh_resume_returns_the_projected_history_key(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.conversation_log.append("dashboard:fresh", "user", "hello")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/fresh/resume", json={"key": "dashboard:fresh"})
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert body["history_key"] == state._slots["fresh"].to_dict()["history_key"]


@pytest.mark.asyncio
async def test_already_live_resume_returns_the_projected_history_key(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("live")
    slot.linked_session_key = "slack:C0ABC:1790411186.442029"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/live/resume", json={"key": slot.linked_session_key}
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert body["history_key"] == slot.to_dict()["history_key"]
    assert body["history_key"] == slot.linked_session_key


def test_legacy_slack_thread_history_key_resolves_to_its_legacy_transcript(tmp_path):
    # A Slack thread from before the ``slack:<ts>`` key migration keeps its
    # transcript under the bare ``<ts>.jsonl``. ``ConversationLog._path`` only
    # falls back to that file from the colon spelling, so the key reopen sends
    # must keep the colon: the folded ``slack_<ts>`` names a file that does not
    # exist, and resuming it would rebind an empty slot onto the real thread.
    ts = "1790411186.442029"
    legacy = tmp_path / f"{ts}.jsonl"
    legacy.write_text("")
    slot = _ChatSlot("chat-3")
    slot.linked_session_key = f"slack:{ts}"

    key = slot.to_dict()["history_key"]

    log = ConversationLog(base_dir=tmp_path)
    assert key == f"slack:{ts}"
    assert log._path(key) == legacy
    assert log._path(f"slack_{ts}") != legacy


@pytest.mark.asyncio
async def test_reopen_by_history_key_restores_a_legacy_slack_transcript(tmp_path, monkeypatch):
    # End to end through the resume route the shortcut calls
    # (``resumeChatSlot``: the key is both the URL slot and the body key).
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    ts = "1790411186.442029"
    log.append(ts, "user", "legacy-question")
    log.append(ts, "assistant", "legacy-answer")
    assert log._path(f"slack:{ts}").name == f"{ts}.jsonl"

    slot = _ChatSlot("chat-4")
    slot.linked_session_key = f"slack:{ts}"
    key = slot.to_dict()["history_key"]

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{quote(key, safe='')}/resume",
            json={"name": key, "key": key, "title": "t"},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    contents = [m.get("content") for m in body.get("messages", [])]
    assert "legacy-question" in contents
    assert "legacy-answer" in contents
