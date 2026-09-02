"""Tests for file_send outbox notify real-time broadcast behaviour.

Verifies that api_outbox_notify broadcasts a chat_message event (not file_ready)
so the frontend receives the file card in real-time via the existing WebSocket handler.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tmpdir_helpers import short_tmp_base

from kiro_crew.dashboard.handlers import api_outbox_notify


def _make_app(state=None) -> web.Application:
    app = web.Application()
    app["state"] = state or MagicMock(_slots={})
    app.router.add_post("/api/outbox/notify", api_outbox_notify)
    return app


def _make_state_with_slot(has_reader=True):
    """Create a mock state with one active slot containing a message."""
    slot = MagicMock()
    slot.key = "chat-1"
    slot._has_reader = has_reader
    slot.messages = [{"role": "assistant", "content": "hello", "ts": "2026-05-27T20:00:00+00:00"}]

    def fake_append(role, content, **kwargs):
        msg = {"role": role, "content": content, "ts": "2026-05-27T20:42:33.357701+00:00"}
        slot.messages.append(msg)

    slot.append = MagicMock(side_effect=fake_append)
    state = MagicMock()
    state._slots = {"chat-1": slot}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    return state, slot


def _make_state_with_two_slots():
    """Create a mock state with two slots — chat-2 has a newer timestamp."""
    slot_1 = MagicMock()
    slot_1.key = "chat-1"
    slot_1.messages = [{"role": "assistant", "content": "older", "ts": "2026-05-27T20:00:00+00:00"}]

    def fake_append_1(role, content, **kwargs):
        msg = {"role": role, "content": content, "ts": "2026-05-27T20:42:33.357701+00:00"}
        slot_1.messages.append(msg)

    slot_1.append = MagicMock(side_effect=fake_append_1)

    slot_2 = MagicMock()
    slot_2.key = "chat-2"
    slot_2.messages = [{"role": "assistant", "content": "newer", "ts": "2026-05-27T21:00:00+00:00"}]

    def fake_append_2(role, content, **kwargs):
        msg = {"role": role, "content": content, "ts": "2026-05-27T21:42:33.357701+00:00"}
        slot_2.messages.append(msg)

    slot_2.append = MagicMock(side_effect=fake_append_2)

    state = MagicMock()
    state._slots = {"chat-1": slot_1, "chat-2": slot_2}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    return state, slot_1, slot_2


def _make_state_with_cron_and_chat_slots():
    """Create a mock state with a cron slot and a newer chat slot.

    The chat slot has a newer timestamp, so the max-heuristic fallback would
    pick it — the cron-key resolution must override that.
    """
    cron_slot = MagicMock()
    cron_slot.key = "cron-daily-digest"
    cron_slot.messages = [{"role": "assistant", "content": "digest", "ts": "2026-05-27T20:00:00+00:00"}]

    def fake_append_cron(role, content, **kwargs):
        cron_slot.messages.append(
            {"role": role, "content": content, "ts": "2026-05-27T20:42:33.357701+00:00"}
        )

    cron_slot.append = MagicMock(side_effect=fake_append_cron)

    chat_slot = MagicMock()
    chat_slot.key = "chat-2"
    chat_slot.messages = [{"role": "assistant", "content": "newer", "ts": "2026-05-27T21:00:00+00:00"}]

    def fake_append_chat(role, content, **kwargs):
        chat_slot.messages.append(
            {"role": role, "content": content, "ts": "2026-05-27T21:42:33.357701+00:00"}
        )

    chat_slot.append = MagicMock(side_effect=fake_append_chat)

    state = MagicMock()
    state._slots = {"cron-daily-digest": cron_slot, "chat-2": chat_slot}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    return state, cron_slot, chat_slot


def _make_state_with_empty_slot(has_reader=True):
    """Create a mock state with one header-targetable slot that has no messages yet."""
    slot = MagicMock()
    slot.key = "chat-1"
    slot._has_reader = has_reader
    slot.messages = []

    def fake_append(role, content, **kwargs):
        slot.messages.append(
            {"role": role, "content": content, "ts": "2026-05-27T20:42:33.357701+00:00"}
        )

    slot.append = MagicMock(side_effect=fake_append)
    state = MagicMock()
    state._slots = {"chat-1": slot}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    return state, slot


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def outbox(tmp_path):
    # Use /tmp as a stable base — macOS tmp_path contains high-entropy directory
    # IDs that trigger the bare-secret heuristic in redact_credentials(), causing
    # api_outbox_notify to reject the path with 400 before any test logic runs.
    #
    # Removed on teardown: `mkdtemp` does not register a finalizer, so without this
    # every test left a directory in /tmp forever (one per test, thousands over a
    # dev's history). tmp_path is still requested so pytest's own numbered-dir
    # retention policy keeps this fixture tied to the test that used it.
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
    odir = base / "outbox"
    odir.mkdir()
    try:
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestOutboxNotifyBroadcast:
    """Behaviour: file_send notify broadcasts chat_message for real-time rendering."""

    @pytest.mark.asyncio
    async def test_broadcasts_chat_message_type(self, outbox, mock_sel):
        """Happy path: broadcast_ws is called with type 'chat_message' not 'file_ready'."""
        wav = outbox / "test.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot = _make_state_with_slot()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(wav),
                "filename": "test.wav",
                "description": "audio clip",
                "size": 100,
            })
            assert resp.status == 200
            state.broadcast_ws.assert_called_once()
            call_args = state.broadcast_ws.call_args
            assert call_args[0][0] == "chat_message"
            payload = call_args[0][1]
            assert payload["role"] == "file"
            assert payload["slot"] == "chat-1"
            assert "test.wav" in payload["content"]

    @pytest.mark.asyncio
    async def test_broadcast_ts_matches_persisted_message(self, outbox, mock_sel):
        """Broadcast timestamp matches the persisted message for dedup consistency."""
        wav = outbox / "clip.wav"
        wav.write_bytes(b"\x00" * 50)
        state, slot = _make_state_with_slot()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(wav),
                "filename": "clip.wav",
                "description": "test",
                "size": 50,
            })
            assert resp.status == 200
            broadcast_ts = state.broadcast_ws.call_args[0][1]["ts"]
            persisted_ts = slot.messages[-1]["ts"]
            assert broadcast_ts == persisted_ts

    @pytest.mark.asyncio
    async def test_no_slot_no_broadcast_no_crash(self, outbox, mock_sel):
        """Unhappy path: no active slot means no broadcast and no crash."""
        wav = outbox / "orphan.wav"
        wav.write_bytes(b"\x00" * 50)
        state = MagicMock(_slots={})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(wav),
                "filename": "orphan.wav",
                "description": "no slot",
                "size": 50,
            })
            assert resp.status == 200
            state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_persisted_to_slot(self, outbox, mock_sel):
        """Regression guard: file message is appended to the active slot."""
        mp3 = outbox / "track.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        state, slot = _make_state_with_slot()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(mp3),
                "filename": "track.mp3",
                "description": "music",
                "size": 54,
            })
            assert resp.status == 200
            slot.append.assert_called_once()
            call_args = slot.append.call_args
            assert call_args[0][0] == "file"
            content = json.loads(call_args[0][1])
            assert content["filename"] == "track.mp3"
            assert content["content_type"] == "audio/mpeg"

    @pytest.mark.asyncio
    async def test_no_explicit_broadcast_when_reader_inactive(self, outbox, mock_sel):
        """No duplicate: when _has_reader=False, append's _on_message handles
        the broadcast — explicit broadcast_ws must NOT fire."""
        wav = outbox / "no_dup.wav"
        wav.write_bytes(b"\x00" * 50)
        state, slot = _make_state_with_slot(has_reader=False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/outbox/notify", json={
                "path": str(wav),
                "filename": "no_dup.wav",
                "description": "dedup test",
                "size": 50,
            })
            assert resp.status == 200
            state.broadcast_ws.assert_not_called()


class TestOutboxNotifySlotTargeting:
    """Behaviour: file_send targets the caller's slot via X-Session-Key header."""

    @pytest.mark.asyncio
    async def test_notify_targets_slot_from_session_key_header(self, outbox, mock_sel):
        """B1: Agent sends file → card appears in the caller's session, not the most recent."""
        wav = outbox / "voice.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        # slot_2 has newer timestamp — max heuristic would pick it
        # But header says dashboard:chat-1 — should target slot_1
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "voice.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200
            slot_1.append.assert_called_once()
            slot_2.append.assert_not_called()
            broadcast_payload = state.broadcast_ws.call_args[0][1]
            assert broadcast_payload["slot"] == "chat-1"

    @pytest.mark.asyncio
    async def test_notify_falls_back_to_max_heuristic_when_no_header(self, outbox, mock_sel):
        """B2: No X-Session-Key header → falls back to most-recently-active slot."""
        wav = outbox / "fallback.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "fallback.wav", "description": "test", "size": 100},
            )
            assert resp.status == 200
            # slot_2 has newer ts — max heuristic picks it
            slot_2.append.assert_called_once()
            slot_1.append.assert_not_called()
            broadcast_payload = state.broadcast_ws.call_args[0][1]
            assert broadcast_payload["slot"] == "chat-2"

    @pytest.mark.asyncio
    async def test_notify_falls_back_when_session_key_slot_not_found(self, outbox, mock_sel):
        """B3: Header present but slot doesn't exist → graceful fallback to max heuristic."""
        wav = outbox / "stale.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "stale.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "dashboard:nonexistent-slot"},
            )
            assert resp.status == 200
            # Slot not found → fallback picks slot_2 (newer ts)
            slot_2.append.assert_called_once()
            slot_1.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_targets_cron_slot_from_cron_session_key(self, outbox, mock_sel):
        """B4: cron:{id} session key resolves to the cron's own cron-{id} slot,
        not the most-recently-active dashboard slot."""
        wav = outbox / "digest.wav"
        wav.write_bytes(b"\x00" * 100)
        state, cron_slot, chat_slot = _make_state_with_cron_and_chat_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "digest.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "cron:daily-digest"},
            )
            assert resp.status == 200
            cron_slot.append.assert_called_once()
            chat_slot.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_falls_back_when_cron_slot_not_found(self, outbox, mock_sel):
        """B5: cron:{id} key with NO matching cron-{id} slot → graceful fallback
        to the max-heuristic (this state has no cron-daily-digest slot)."""
        wav = outbox / "cron.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "cron.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "cron:daily-digest"},
            )
            assert resp.status == 200
            # No cron-daily-digest slot → fallback picks slot_2 (newer ts)
            slot_2.append.assert_called_once()
            slot_1.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_appends_to_empty_header_targeted_slot(self, outbox, mock_sel):
        """B6: a header-targeted slot with no messages yet still receives the file
        (must not be silently dropped by the max-heuristic guard)."""
        wav = outbox / "first.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot = _make_state_with_empty_slot()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "first.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200
            slot.append.assert_called_once()


class TestOutboxNotifyRedaction:
    """Behaviour: broadcast payload is redacted before WebSocket emission."""

    @pytest.mark.asyncio
    async def test_broadcast_content_is_redacted(self, outbox, mock_sel):
        """Filename/description with sensitive content is redacted in broadcast."""
        wav = outbox / "test.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot = _make_state_with_slot()

        # Content egress is routed through the single context-aware redact()
        # shim (which runs both the exfil-URL and credential passes and applies
        # a loaded companion's extra regexes). Patch that one shim.
        with patch(
            "kiro_crew.dashboard.handlers.files.redact",
            side_effect=lambda s: s.replace("http://evil.com", "[REDACTED_URL]"),
        ) as mock_redact:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/outbox/notify", json={
                    "path": str(wav),
                    "filename": "test.wav",
                    "description": "exfil http://evil.com payload",
                    "size": 100,
                })
                assert resp.status == 200
                assert mock_redact.called
                # Both append and broadcast must receive redacted content
                append_content = slot.append.call_args[0][1]
                assert "http://evil.com" not in append_content
                assert "[REDACTED_URL]" in append_content
                broadcast_content = state.broadcast_ws.call_args[0][1]["content"]
                assert "http://evil.com" not in broadcast_content
                assert "[REDACTED_URL]" in broadcast_content
