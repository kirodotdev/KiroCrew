"""Tests for file_send outbox notify real-time broadcast behaviour.

Verifies that api_outbox_notify broadcasts a chat_message event (not file_ready)
so the frontend receives the file card in real-time via the existing WebSocket handler.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

    def fake_append(role, content, cls="", **kwargs):
        # Mirror the real ``_ChatSlot.append`` contract: mint ``meta.mid`` and
        # return the appended row (append_and_surface reads ts/meta off it).
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T20:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-test-{len(slot.messages)}"},
        }
        slot.messages.append(msg)
        return msg

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

    def fake_append_1(role, content, cls="", **kwargs):
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T20:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-t1-{len(slot_1.messages)}"},
        }
        slot_1.messages.append(msg)
        return msg

    slot_1.append = MagicMock(side_effect=fake_append_1)

    slot_2 = MagicMock()
    slot_2.key = "chat-2"
    slot_2.messages = [{"role": "assistant", "content": "newer", "ts": "2026-05-27T21:00:00+00:00"}]

    def fake_append_2(role, content, cls="", **kwargs):
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T21:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-t2-{len(slot_2.messages)}"},
        }
        slot_2.messages.append(msg)
        return msg

    slot_2.append = MagicMock(side_effect=fake_append_2)

    state = MagicMock()
    state._slots = {"chat-1": slot_1, "chat-2": slot_2}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    # ``_resolve_session_target`` iterates ``state.crons.list_jobs(...)``; a bare
    # MagicMock is not iterable, so cron-key tests need a real list here. Tests
    # that want an origin override it with their own job.
    state.crons = MagicMock()
    state.crons.list_jobs = MagicMock(return_value=[])
    return state, slot_1, slot_2


def _make_state_with_cron_and_chat_slots():
    """Create a mock state with a cron slot and a newer chat slot.

    The chat slot has a newer timestamp, so the max-heuristic fallback would
    pick it — the cron-key resolution must override that.
    """
    cron_slot = MagicMock()
    cron_slot.key = "cron-daily-digest"
    cron_slot.messages = [
        {"role": "assistant", "content": "digest", "ts": "2026-05-27T20:00:00+00:00"}
    ]

    def fake_append_cron(role, content, cls="", **kwargs):
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T20:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-tc-{len(cron_slot.messages)}"},
        }
        cron_slot.messages.append(msg)
        return msg

    cron_slot.append = MagicMock(side_effect=fake_append_cron)

    chat_slot = MagicMock()
    chat_slot.key = "chat-2"
    chat_slot.messages = [
        {"role": "assistant", "content": "newer", "ts": "2026-05-27T21:00:00+00:00"}
    ]

    def fake_append_chat(role, content, cls="", **kwargs):
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T21:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-tt-{len(chat_slot.messages)}"},
        }
        chat_slot.messages.append(msg)
        return msg

    chat_slot.append = MagicMock(side_effect=fake_append_chat)

    state = MagicMock()
    state._slots = {"cron-daily-digest": cron_slot, "chat-2": chat_slot}
    state.get_slot = MagicMock(side_effect=lambda slot_name: state._slots.get(slot_name))
    # See _make_state_with_two_slots: keep ``list_jobs`` iterable so a cron key
    # that misses its live slot reaches origin resolution instead of a TypeError.
    state.crons = MagicMock()
    state.crons.list_jobs = MagicMock(return_value=[])
    return state, cron_slot, chat_slot


def _make_state_with_empty_slot(has_reader=True):
    """Create a mock state with one header-targetable slot that has no messages yet."""
    slot = MagicMock()
    slot.key = "chat-1"
    slot._has_reader = has_reader
    slot.messages = []

    def fake_append(role, content, cls="", **kwargs):
        msg = {
            "role": role,
            "content": content,
            "ts": "2026-05-27T20:42:33.357701+00:00",
            "meta": {**(kwargs.get("meta") or {}), "mid": f"m-test-{len(slot.messages)}"},
        }
        slot.messages.append(msg)
        return msg

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
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "test.wav",
                    "description": "audio clip",
                    "size": 100,
                },
            )
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
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "clip.wav",
                    "description": "test",
                    "size": 50,
                },
            )
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
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "orphan.wav",
                    "description": "no slot",
                    "size": 50,
                },
            )
            assert resp.status == 200
            state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_message_persisted_to_slot(self, outbox, mock_sel):
        """Regression guard: file message is appended to the active slot."""
        mp3 = outbox / "track.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        state, slot = _make_state_with_slot()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(mp3),
                    "filename": "track.mp3",
                    "description": "music",
                    "size": 54,
                },
            )
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
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "no_dup.wav",
                    "description": "dedup test",
                    "size": 50,
                },
            )
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
                json={
                    "path": str(wav),
                    "filename": "voice.wav",
                    "description": "test",
                    "size": 100,
                },
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
                json={
                    "path": str(wav),
                    "filename": "fallback.wav",
                    "description": "test",
                    "size": 100,
                },
            )
            assert resp.status == 200
            # slot_2 has newer ts — max heuristic picks it
            slot_2.append.assert_called_once()
            slot_1.append.assert_not_called()
            broadcast_payload = state.broadcast_ws.call_args[0][1]
            assert broadcast_payload["slot"] == "chat-2"

    @pytest.mark.asyncio
    async def test_notify_suppresses_card_when_session_key_slot_not_found(self, outbox, mock_sel):
        """B3: Header present but slot doesn't exist → card is SUPPRESSED, not
        leaked to an unrelated slot. A present-but-unresolved X-Session-Key
        denotes a session with no live slot; falling back to the
        most-recently-active slot would surface the card in whatever session the
        user happens to have focused (the file_send card-leak bug)."""
        wav = outbox / "stale.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "stale.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": "dashboard:nonexistent-slot"},
            )
            assert resp.status == 200
            # Present-but-unresolved key → no slot receives the card
            slot_1.append.assert_not_called()
            slot_2.append.assert_not_called()
            state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_suppressed_card_is_distinguishable_in_the_audit(self, outbox, mock_sel):
        """Suppression is right, but it must not read as delivery.

        The endpoint answers 200 either way and records one ``completed`` event
        either way, so without a marker the vanished card and the delivered one
        are the same event and there is nothing to diagnose from.
        """
        wav = outbox / "vanished.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "vanished.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": "dashboard:nonexistent-slot"},
            )
            assert resp.status == 200
        notified = [
            call.kwargs
            for call in mock_sel.log_tool_invocation.call_args_list
            if call.kwargs.get("tool_kind") == "notify"
        ]
        assert len(notified) == 1
        assert notified[0]["outcome"] == "completed"
        assert "delivered=0" in notified[0]["resources"]

    @pytest.mark.asyncio
    async def test_a_delivered_card_says_so_in_the_same_field(self, outbox, mock_sel):
        """The marker's other value, so the pair is what distinguishes them."""
        wav = outbox / "landed.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "landed.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200
        slot_1.append.assert_called_once()
        notified = [
            call.kwargs
            for call in mock_sel.log_tool_invocation.call_args_list
            if call.kwargs.get("tool_kind") == "notify"
        ]
        assert len(notified) == 1
        assert "delivered=1" in notified[0]["resources"]

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
                json={
                    "path": str(wav),
                    "filename": "digest.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": "cron:daily-digest"},
            )
            assert resp.status == 200
            cron_slot.append.assert_called_once()
            chat_slot.append.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("suffix", [":run-42", ":team-alpha"])
    async def test_a_suffixed_cron_key_still_finds_its_own_tab(self, outbox, mock_sel, suffix):
        """A cron turn's key can carry a further segment.

        `cron:<job>:<run>` for a per-run session and `cron:<job>:<agent>` for a
        multi-agent one are both real shapes, and the slot is named for the JOB
        alone. Folding the whole tail asks for a `cron-<job>:<run>` slot that never
        exists, so every suffixed turn missed its own open tab and fell through to
        origin resolution or suppression.
        """
        wav = outbox / "digest.wav"
        wav.write_bytes(b"\x00" * 100)
        state, cron_slot, chat_slot = _make_state_with_cron_and_chat_slots()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "digest.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": f"cron:daily-digest{suffix}"},
            )
            assert resp.status == 200
            cron_slot.append.assert_called_once()
            chat_slot.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_routes_to_cron_origin_when_no_live_slot(self, outbox, mock_sel):
        """B5: cron:{id} key with NO matching cron-{id} slot (a headless script
        cron with no live dashboard slot) routes the card to the cron's ORIGIN
        dashboard session — the chat that created the cron — mirroring
        send_message(session="origin"). It must NOT leak into the
        most-recently-active dashboard slot."""
        wav = outbox / "cron.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        # The "daily-digest" cron was created from dashboard chat-1 (the older
        # slot). slot_2 is newer, so the old max-heuristic would have leaked the
        # card there — origin resolution must override that.
        job = MagicMock(id="daily-digest", session_key="dashboard:chat-1")
        state.crons.list_jobs = MagicMock(return_value=[job])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "cron.wav", "description": "test", "size": 100},
                headers={"X-Session-Key": "cron:daily-digest"},
            )
            assert resp.status == 200
            # Origin is chat-1 → slot_1 receives the card, not the newer slot_2
            slot_1.append.assert_called_once()
            slot_2.append.assert_not_called()
            broadcast_payload = state.broadcast_ws.call_args[0][1]
            assert broadcast_payload["slot"] == "chat-1"

    @pytest.mark.asyncio
    async def test_notify_suppresses_card_when_cron_origin_unresolvable(self, outbox, mock_sel):
        """B5b: cron:{id} key with NO live cron-{id} slot AND no resolvable origin
        (a cron created from the dashboard UI with no originating chat, or an
        unknown job) → card SUPPRESSED, not leaked into the most-recently-active
        dashboard slot."""
        wav = outbox / "orphan_cron.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        # No job matches the cron id → origin unresolvable
        state.crons.list_jobs = MagicMock(return_value=[])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "orphan_cron.wav",
                    "description": "test",
                    "size": 100,
                },
                headers={"X-Session-Key": "cron:unknown-job"},
            )
            assert resp.status == 200
            slot_1.append.assert_not_called()
            slot_2.append.assert_not_called()
            state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_rehydrates_cron_origin_slot_on_get_slot_miss(self, outbox, mock_sel):
        """B5c: the COMMON headless case — a script cron running with NO dashboard
        tab open, so state._slots is EMPTY. The origin slot is not loaded, so on
        the get_slot miss the handler rehydrates it from history (off the loop,
        mirroring send_message(session="origin")) and delivers the card there. A
        cold-rehydrated slot has no live reader, so delivery goes through append's
        own _on_message callback, not an explicit broadcast."""
        wav = outbox / "cold.wav"
        wav.write_bytes(b"\x00" * 100)
        # No dashboard tabs open at all → state._slots is empty
        state = MagicMock()
        state._slots = {}
        state.get_slot = MagicMock(return_value=None)
        job = MagicMock(id="daily-digest", session_key="dashboard:chat-cold")
        state.crons = MagicMock()
        state.crons.list_jobs = MagicMock(return_value=[job])
        # The origin session exists on disk; rehydration returns a COLD slot
        # (no live reader — faithful to a session with no open tab).
        cold = MagicMock()
        cold.key = "chat-cold"
        cold._has_reader = False
        cold.messages = [
            {"role": "assistant", "content": "prev", "ts": "2026-05-27T19:00:00+00:00"}
        ]

        def fake_append_cold(role, content, cls="", **kwargs):
            msg = {
                "role": role,
                "content": content,
                "ts": "2026-05-27T22:00:00+00:00",
                "meta": {**(kwargs.get("meta") or {}), "mid": f"m-cold-{len(cold.messages)}"},
            }
            cold.messages.append(msg)
            return msg

        cold.append = MagicMock(side_effect=fake_append_cold)
        with patch(
            "kiro_crew.dashboard.handlers.files.rehydrate_slot_from_history_async",
            new_callable=AsyncMock,
            return_value=cold,
        ) as mock_rehydrate:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/outbox/notify",
                    json={
                        "path": str(wav),
                        "filename": "cold.wav",
                        "description": "test",
                        "size": 100,
                    },
                    headers={"X-Session-Key": "cron:daily-digest"},
                )
                assert resp.status == 200
                # get_slot("chat-cold") missed → rehydrated from history
                mock_rehydrate.assert_awaited_once_with(state, "chat-cold")
                cold.append.assert_called_once()
                content = json.loads(cold.append.call_args[0][1])
                assert content["filename"] == "cold.wav"
                state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_suppresses_when_cron_origin_slot_gone(self, outbox, mock_sel):
        """B5d: origin resolves to a slot key, but the session is truly gone
        (never persisted, deleted, or closed) so rehydration also returns None →
        card SUPPRESSED. Two live slots are seeded to prove the card is not leaked
        into a focused slot either."""
        wav = outbox / "gone_cron.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        job = MagicMock(id="daily-digest", session_key="dashboard:chat-gone")
        state.crons.list_jobs = MagicMock(return_value=[job])
        with patch(
            "kiro_crew.dashboard.handlers.files.rehydrate_slot_from_history_async",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_rehydrate:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/outbox/notify",
                    json={
                        "path": str(wav),
                        "filename": "gone_cron.wav",
                        "description": "test",
                        "size": 100,
                    },
                    headers={"X-Session-Key": "cron:daily-digest"},
                )
                assert resp.status == 200
                mock_rehydrate.assert_awaited_once_with(state, "chat-gone")
                slot_1.append.assert_not_called()
                slot_2.append.assert_not_called()
                state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_routes_a_subagent_card_to_its_parent_slot(self, outbox, mock_sel):
        """B7: a dedicated-process sub-agent has no tab of its own, so its card
        belongs to the PARENT's tab — the surface its completion injection and its
        ``subagent_*`` WS frames already route to. Suppressing it instead would let
        ``file_send`` answer "File sent" with nothing on screen anywhere."""
        wav = outbox / "sub.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        # A PLAIN LIST, because the real ``all_agents`` is a @property. Doubling it
        # as a callable is what let the production `manager.all_agents()` bug pass:
        # the double accepted the call, the real object raised TypeError, and the
        # card was suppressed for every sub-agent.
        state.subagents.all_agents = [
            MagicMock(id="a1", conversation_key="", parent_session_key="dashboard:chat-1")
        ]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "sub.wav", "description": "t", "size": 100},
                headers={"X-Session-Key": "subagent:a1"},
            )
            assert resp.status == 200
            slot_1.append.assert_called_once()
            slot_2.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_routes_a_continuation_to_its_own_parent(self, outbox, mock_sel):
        """Two records can carry ONE key, and the card belongs to the LIVE one.

        A continuation is minted as a new run whose ``conversation_key`` is the
        original's ``subagent:<id>``, and the original resolves to that same string.
        When a DIFFERENT session continued the conversation the two parents differ,
        so taking the first match routes the card to the PREVIOUS owner's chat --
        another user's tab. The newest active run is the right answer.
        """
        wav = outbox / "cont.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        # Roster order puts the ORIGINAL first, so a first-match scan picks it.
        state.subagents.all_agents = [
            MagicMock(
                id="orig",
                conversation_key="",
                parent_session_key="dashboard:chat-1",
                done=True,
                started=100.0,
            ),
            MagicMock(
                id="cont",
                conversation_key="subagent:orig",
                parent_session_key="dashboard:chat-2",
                done=False,
                started=200.0,
            ),
        ]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(wav), "filename": "cont.wav", "description": "t", "size": 100},
                headers={"X-Session-Key": "subagent:orig"},
            )
            assert resp.status == 200
            slot_2.append.assert_called_once()
            slot_1.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_suppresses_an_unknown_subagents_card(self, outbox, mock_sel):
        """B7b: an unknown run (reaped, or a manager that cannot answer) yields no
        parent, so the card is suppressed rather than guessed into a focused tab."""
        wav = outbox / "sub_unknown.wav"
        wav.write_bytes(b"\x00" * 100)
        state, slot_1, slot_2 = _make_state_with_two_slots()
        state.subagents.all_agents = []
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(wav),
                    "filename": "sub_unknown.wav",
                    "description": "t",
                    "size": 100,
                },
                headers={"X-Session-Key": "subagent:gone"},
            )
            assert resp.status == 200
            slot_1.append.assert_not_called()
            slot_2.append.assert_not_called()
            state.broadcast_ws.assert_not_called()

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
                json={
                    "path": str(wav),
                    "filename": "first.wav",
                    "description": "test",
                    "size": 100,
                },
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
                resp = await client.post(
                    "/api/outbox/notify",
                    json={
                        "path": str(wav),
                        "filename": "test.wav",
                        "description": "exfil http://evil.com payload",
                        "size": 100,
                    },
                )
                assert resp.status == 200
                assert mock_redact.called
                # Both append and broadcast must receive redacted content
                append_content = slot.append.call_args[0][1]
                assert "http://evil.com" not in append_content
                assert "[REDACTED_URL]" in append_content
                broadcast_content = state.broadcast_ws.call_args[0][1]["content"]
                assert "http://evil.com" not in broadcast_content
                assert "[REDACTED_URL]" in broadcast_content
