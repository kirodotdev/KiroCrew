"""Tests for incognito/temporary session support.

Non-persistent sessions disable memory consolidation while keeping
conversation log persistence intact for tab recovery and gateway restart.
"""

from __future__ import annotations

import json as _json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog

# ── Helpers ──


def _make_state(tmp_path, **kwargs):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state):
    from kiro_crew.dashboard.chat import (
        api_chat_slot_create,
        api_chat_slot_delete,
        api_chat_slot_resume,
        api_chat_slots,
    )
    from kiro_crew.dashboard.handlers import api_lessons_create

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_post("/api/lessons", api_lessons_create)
    return app


def _write_session(log, key, messages, *, memory_mode="persistent"):
    """Write a JSONL session file with optional memory_mode metadata."""
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}
    if memory_mode != "persistent":
        meta["memory_mode"] = memory_mode
    lines = [_json.dumps(meta)]
    for role, content in messages:
        lines.append(_json.dumps({"role": role, "content": content, "ts": "2026-01-01T00:00:01"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Slot model tests ──


class TestSlotMemoryMode:
    def test_default_persistent(self):
        slot = _ChatSlot("s1")
        assert slot.memory_mode == "persistent"
        assert not slot.is_restricted
        assert not slot.blocks_reads

    def test_incognito(self):
        slot = _ChatSlot("s1", memory_mode="incognito")
        assert slot.memory_mode == "incognito"
        assert slot.is_restricted
        assert not slot.blocks_reads

    def test_temporary(self):
        slot = _ChatSlot("s1", memory_mode="temporary")
        assert slot.memory_mode == "temporary"
        assert slot.is_restricted
        assert slot.blocks_reads

    def test_to_dict_includes_memory_mode(self):
        slot = _ChatSlot("s1", memory_mode="incognito")
        d = slot.to_dict()
        assert d["memory_mode"] == "incognito"

    def test_to_dict_persistent(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert d["memory_mode"] == "persistent"


class TestSlotCreation:
    def test_get_or_create_slot_incognito(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("e1", memory_mode="incognito")
        assert slot.memory_mode == "incognito"
        assert "dashboard:e1" in state._restricted_keys

    def test_get_or_create_slot_temporary(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t1", memory_mode="temporary")
        assert slot.memory_mode == "temporary"
        assert "dashboard:t1" in state._restricted_keys

    def test_get_or_create_slot_persistent(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("n1")
        assert slot.memory_mode == "persistent"
        assert "dashboard:n1" not in state._restricted_keys

    @pytest.mark.asyncio
    async def test_restricted_key_cleaned_on_slot_delete(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("reuse", memory_mode="incognito")
        assert "dashboard:reuse" in state._restricted_keys

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/reuse")
            assert resp.status == 200

        assert "dashboard:reuse" not in state._restricted_keys
        slot = state.get_or_create_slot("reuse")
        assert slot.memory_mode == "persistent"

    def test_get_or_create_slot_memory_mode_mismatch_raises(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("x")
        with pytest.raises(ValueError, match="memory_mode="):
            state.get_or_create_slot("x", memory_mode="incognito")


# ── Conversation log persistence ──


class TestHistoryPersistence:
    def test_restricted_session_still_saves_conversation_log(self, tmp_path, monkeypatch):
        """All memory modes write conversation log for tab recovery."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("e1", memory_mode="temporary")
        slot.append("user", "secret tax info")
        slot.append("assistant", "noted")

        _save_slot_to_history(state, slot)

        msgs = state.conversation_log.read_messages("dashboard:e1")
        assert len(msgs) == 2

    def test_restricted_metadata_flag_persisted(self, tmp_path, monkeypatch):
        """Conversation log metadata includes memory_mode for restricted sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("e1", memory_mode="incognito")
        slot.append("user", "hello")

        _save_slot_to_history(state, slot)

        meta = state.conversation_log.get_metadata("dashboard:e1")
        assert meta.get("memory_mode") == "incognito"

    def test_persistent_session_no_memory_mode_metadata(self, tmp_path, monkeypatch):
        """Persistent sessions don't have memory_mode in metadata."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("n1")
        slot.append("user", "hello")

        _save_slot_to_history(state, slot)

        meta = state.conversation_log.get_metadata("dashboard:n1")
        assert "memory_mode" not in meta or meta.get("memory_mode") == "persistent"

    def test_temporary_transcript_on_disk_predates_any_titling(self, tmp_path, monkeypatch):
        """A temporary slot's transcript reaches disk with NO titling involved.

        Locks in the premise behind "titling is independent of memory_mode"
        (docs/system-specs/modules/history.md): the session JSONL — full user and
        assistant content — is written by the ordinary flush path regardless of
        mode. A persisted title is therefore a summary of content already in that
        same file, not a new disclosure. If this ever starts asserting False,
        `_maybe_auto_title` must be re-gated on memory_mode.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t-disk", memory_mode="temporary")
        slot.append("user", "my private question")
        slot.append("assistant", "the answer")

        # No _maybe_auto_title / _persist_title call anywhere in this test.
        _save_slot_to_history(state, slot)

        path = state.conversation_log._path("dashboard:t-disk")
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        assert "my private question" in body
        assert "the answer" in body


# ── Restore on gateway restart ──


class TestRestore:
    def test_restore_rebuilds_memory_mode(self, tmp_path, monkeypatch):
        """Gateway restart restores restricted sessions with memory_mode intact."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history, restore_recent_sessions

        state1 = _make_state(tmp_path)
        slot = state1.get_or_create_slot("e1", memory_mode="incognito")
        slot.append("user", "private stuff")
        slot.append("assistant", "ok")
        _save_slot_to_history(state1, slot)

        state2 = _make_state(tmp_path)
        restored = restore_recent_sessions(state2, window_minutes=0)

        assert restored >= 1
        assert "e1" in state2._slots
        assert state2._slots["e1"].memory_mode == "incognito"
        assert "dashboard:e1" in state2._restricted_keys


# ── User-initiated resume from History tab ──


class TestResumeFromHistory:
    """Resume endpoint (POST /api/chat/slots/{slot}/resume) must restore memory_mode.

    Regression test: prior to the fix, this path restored agent/workspace/mode/folder_id
    etc. but not memory_mode, causing reloaded incognito/temporary sessions to become
    persistent and allow memory writes.
    """

    @pytest.mark.asyncio
    async def test_resume_restores_incognito(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "e1", [("user", "hi")], memory_mode="incognito")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/e1/resume", json={"key": "e1"})
            data = await resp.json()

        assert data["ok"] is True
        assert data["memory_mode"] == "incognito"
        assert state._slots["e1"].memory_mode == "incognito"
        assert "dashboard:e1" in state._restricted_keys

    @pytest.mark.asyncio
    async def test_resume_restores_temporary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "t1", [("user", "hi")], memory_mode="temporary")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/t1/resume", json={"key": "t1"})
            data = await resp.json()

        assert data["memory_mode"] == "temporary"
        assert state._slots["t1"].memory_mode == "temporary"
        assert state._slots["t1"].blocks_reads is True
        assert "dashboard:t1" in state._restricted_keys

    @pytest.mark.asyncio
    async def test_resume_persistent_leaves_restricted_keys_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "p1", [("user", "hi")])

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/p1/resume", json={"key": "p1"})
            data = await resp.json()

        assert data["memory_mode"] == "persistent"
        assert state._slots["p1"].memory_mode == "persistent"
        assert "dashboard:p1" not in state._restricted_keys

    @pytest.mark.asyncio
    async def test_resume_missing_memory_mode_defaults_persistent(self, tmp_path, monkeypatch):
        """Legacy sessions (pre-) without memory_mode metadata default to persistent."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "legacy", [("user", "hi")])

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/legacy/resume", json={"key": "legacy"})
            data = await resp.json()

        assert data["memory_mode"] == "persistent"

    @pytest.mark.asyncio
    async def test_learn_add_blocked_after_resume_incognito(self, tmp_path, monkeypatch):
        """Core regression: learn_add must be blocked on a resumed incognito session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "e1", [("user", "hi")], memory_mode="incognito")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/e1/resume", json={"key": "e1"})
            resp = await client.post(
                "/api/lessons",
                json={"rule": "secret", "category": "preference"},
                headers={"X-Session-Key": "dashboard:e1"},
            )

        assert resp.status == 403


# ── Consolidation gate ──


class TestConsolidation:
    def test_consolidation_not_triggered_for_restricted(self, tmp_path):
        """maybe_consolidate must not be called for restricted sessions."""
        from kiro_crew.dashboard.chat import _maybe_consolidate

        state = _make_state(tmp_path)
        state.consolidator = MagicMock()
        slot = state.get_or_create_slot("e1", memory_mode="incognito")

        with patch("kiro_crew.dashboard.chat_utils.sel") as mock_sel:
            _maybe_consolidate(state, slot)

        state.consolidator.maybe_consolidate.assert_not_called()
        mock_sel().log_api_access.assert_called_once_with(
            caller="dashboard:e1", operation="consolidate",
            outcome="denied", source="dashboard",
            resources="restricted_session_block",
        )

    def test_consolidation_triggered_for_persistent(self, tmp_path):
        """maybe_consolidate must be called for persistent sessions."""
        from kiro_crew.dashboard.chat import _maybe_consolidate

        state = _make_state(tmp_path)
        state.consolidator = MagicMock()
        slot = state.get_or_create_slot("n1")

        _maybe_consolidate(state, slot)

        state.consolidator.maybe_consolidate.assert_called_once()


# ── API: create slot with memory_mode ──


class TestSlotAPI:
    @pytest.mark.asyncio
    async def test_create_incognito_slot_via_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.KiroCrewConfig.load",
            MagicMock(return_value=MagicMock(agents={})),
        )
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"memory_mode": "incognito"},
            )
            data = await resp.json()

        assert data["memory_mode"] == "incognito"
        slot_key = data["key"]
        assert state._slots[slot_key].memory_mode == "incognito"
        assert f"dashboard:{slot_key}" in state._restricted_keys

    @pytest.mark.asyncio
    async def test_create_persistent_slot_via_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.KiroCrewConfig.load",
            MagicMock(return_value=MagicMock(agents={})),
        )
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={})
            data = await resp.json()

        assert data["memory_mode"] == "persistent"

    @pytest.mark.asyncio
    async def test_create_slot_memory_mode_mismatch_returns_409(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.KiroCrewConfig.load",
            MagicMock(return_value=MagicMock(agents={})),
        )
        state = _make_state(tmp_path)
        state.get_or_create_slot("conflict")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "conflict", "memory_mode": "incognito"},
            )
            assert resp.status == 409


# ── API: lessons blocked for restricted sessions ──


class TestLessonsGate:
    @pytest.mark.asyncio
    async def test_learn_add_blocked_for_restricted_session(self, tmp_path, monkeypatch):
        """POST /api/lessons returns 403 when X-Session-Key is restricted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("e1", memory_mode="incognito")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:e1"},
            )
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_persistent_session(self, tmp_path, monkeypatch):
        """POST /api/lessons succeeds for persistent sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        state.get_or_create_slot("n1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:n1"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_channel_namespace_session(self, tmp_path, monkeypatch):
        """POST /api/lessons succeeds for a channel session key with NO slot and
        NO persisted JSONL — the #1268 regression, live-reproduced from a
        Telegram forum topic.

        Post-#232 the transport publishes ``session_pid`` so the gateway
        resolves the ``X-Session-Key`` (e.g. ``telegram:kirocrew:forum:…``), but
        the acceptance gate recognised only the ``slack:`` namespace, so every
        OTHER channel fell through to the ``_session_has_persisted_history``
        fallback — which can never match a channel key (``sk.split(':',1)[-1]``
        keeps inner colons while the on-disk file folds them to ``_``) — and was
        rejected with HTTP 400 ``unknown session``. The gate now recognises the
        whole channel-namespace family via ``is_channel_session_key``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Deliberately create NO slot and write NO JSONL: acceptance must come
        # purely from the channel namespace, exactly as it does for Slack.
        for channel_key in (
            "telegram:kirocrew:forum:-1004326574849:18:gen3",
            "discord:kirocrew:dm:123456789:gen1",
            "webex:kirocrew:dm:user@example.com",
            "wecom:kirocrew:dm:wuser",
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "remember this", "category": "knowledge"},
                    headers={"X-Session-Key": channel_key},
                )
                assert resp.status == 200, (channel_key, await resp.text())

    @pytest.mark.asyncio
    async def test_learn_add_rejected_without_session_header(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_learn_add_rejected_for_unknown_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:deleted-slot"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_learn_add_blocked_by_slot_fallback_on_restricted_key_desync(self, tmp_path, monkeypatch):
        """Defense-in-depth: even if _restricted_keys loses the key, the slot's own flag blocks writes."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("e1", memory_mode="incognito")
        state._restricted_keys.discard("dashboard:e1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:e1"},
            )
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_browser_ui_despite_restricted_slot(self, tmp_path, monkeypatch):
        """Browser Memory page sends 'dashboard:ui' — allowed even when restricted slots exist."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        state.get_or_create_slot("e1", memory_mode="incognito")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:ui"},
            )
            assert resp.status == 200


# ── MCP core: session_key passthrough ──


class TestMcpCoreSessionKeyPassthrough:
    def test_learn_add_sends_session_key_header(self):
        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:e1"}),
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"error": "Incognito mode"}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            from kiro_crew.mcp_core import _post
            _post("/api/lessons", {"rule": "test", "category": "knowledge"})

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-session-key") == "dashboard:e1"

    def test_learn_add_no_session_key_header_when_unset(self):
        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
        ):
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok": true}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            from kiro_crew.mcp_core import _post
            _post("/api/lessons", {"rule": "test", "category": "knowledge"})

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-session-key") is None

    def test_post_surfaces_http_error_json_body(self):
        """Regression: on an HTTP 400 the structured
        ``{"error": ...}`` body lives in ``HTTPError.read()``, not ``str(e)``
        (which is only ``"HTTP Error 400: Bad Request"``). ``_post`` must
        decode the body so the learn_add wrapper's ``unknown session`` mapping
        can match instead of leaking an opaque transport error.
        """
        import io
        import urllib.error

        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch("kiro_crew.mcp_core._resolve_session_key", return_value="1781215864.487849"),
        ):
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="http://x/api/lessons", code=400, msg="Bad Request",
                hdrs=None, fp=io.BytesIO(b'{"error": "unknown session"}'),
            )
            from kiro_crew.mcp_core import _post
            result = _post("/api/lessons", {"rule": "x", "category": "knowledge"})

        assert result == {"error": "unknown session"}

    def test_post_falls_back_to_raw_body_for_non_json_error(self):
        """A non-JSON error body still yields a usable ``{"error": ...}``
        rather than crashing the JSON decode.
        """
        import io
        import urllib.error

        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
        ):
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="http://x/api/lessons", code=500, msg="Internal Server Error",
                hdrs=None, fp=io.BytesIO(b"upstream exploded"),
            )
            from kiro_crew.mcp_core import _post
            result = _post("/api/lessons", {"rule": "x"})

        assert "error" in result
        assert result["error"] == "upstream exploded"

    def test_post_redacts_credentials_in_http_error_body(self):
        """An HTTP error body is untrusted external content — credentials in it
        must be redacted before the error dict reaches a caller that may echo
        it to the LLM/dashboard/Slack (review-bot security-controls rule).
        """
        import io
        import urllib.error

        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch("kiro_crew.mcp_core._resolve_session_key", return_value=""),
        ):
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="http://x/api/lessons", code=502, msg="Bad Gateway",
                hdrs=None,
                fp=io.BytesIO(b'{"error": "upstream rejected key AKIAIOSFODNN7EXAMPLE"}'),
            )
            from kiro_crew.mcp_core import _post
            result = _post("/api/lessons", {"rule": "x"})

        assert "AKIAIOSFODNN7EXAMPLE" not in result["error"]
        assert "REDACTED" in result["error"]

    def test_learn_add_tool_maps_unknown_session_to_friendly_message(self):
        """End-to-end: the learn_add tool dispatch turns a backend
        ``unknown session`` (now correctly surfaced by ``_post``) into the
        user-actionable message instead of ``Error: HTTP Error 400``.
        """
        import io
        import urllib.error

        with (
            patch("kiro_crew.mcp_core.loopback_urlopen") as mock_urlopen,
            patch("kiro_crew.mcp_core._resolve_session_key", return_value="1781215864.487849"),
        ):
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="http://x/api/lessons", code=400, msg="Bad Request",
                hdrs=None, fp=io.BytesIO(b'{"error": "unknown session"}'),
            )
            from kiro_crew.mcp_core import _call_tool_inner
            out = _call_tool_inner(
                "learn_add", {"rule": "use tool-b for auth", "category": "tool"}
            )

        assert "not saved" in out.lower()
        assert "HTTP Error 400" not in out


# ── Cross-tab privacy filtering (history.py) ──


class TestCrossTabPrivacy:
    def test_recent_from_source_skips_restricted_sessions(self, tmp_path):
        """Restricted session messages must not leak into 'Other chat tabs' context."""
        log = ConversationLog(base_dir=tmp_path)

        _write_session(log, "dashboard:e1", [("user", "secret private data")], memory_mode="incognito")
        _write_session(log, "dashboard:n1", [("user", "normal public data")])

        results = log.recent_from_source("dashboard:", max_messages=50)
        texts = [m.get("content", "") for m in results]
        assert "normal public data" in texts
        assert "secret private data" not in texts

    def test_recent_from_source_includes_persistent_sessions(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "dashboard:n1", [("user", "visible message")])

        results = log.recent_from_source("dashboard:", max_messages=50)
        texts = [m.get("content", "") for m in results]
        assert "visible message" in texts

    def test_restricted_sessions_do_not_consume_budget(self, tmp_path):
        """4 restricted + 3 persistent: all 3 persistent sessions included."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(4):
            _write_session(log, f"dashboard:e{i}", [("user", f"secret-{i}")], memory_mode="temporary")
            p = log._path(f"dashboard:e{i}")
            os.utime(p, (time.time() + 100 + i, time.time() + 100 + i))
        for i in range(3):
            _write_session(log, f"dashboard:n{i}", [("user", f"normal-{i}")])
            p = log._path(f"dashboard:n{i}")
            os.utime(p, (time.time() + i, time.time() + i))

        results = log.recent_from_source("dashboard:", max_messages=50)
        texts = [m.get("content", "") for m in results]
        for i in range(3):
            assert f"normal-{i}" in texts
        for i in range(4):
            assert f"secret-{i}" not in texts

    def test_many_restricted_do_not_crowd_out_persistent_sessions(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(18):
            _write_session(log, f"dashboard:e{i}",
                           [("user", f"secret-{i}")], memory_mode="incognito")
            p = log._path(f"dashboard:e{i}")
            os.utime(p, (time.time() + 200 + i, time.time() + 200 + i))
        for i in range(5):
            _write_session(log, f"dashboard:n{i}",
                           [("user", f"normal-{i}")])
            p = log._path(f"dashboard:n{i}")
            os.utime(p, (time.time() + i, time.time() + i))

        results = log.recent_from_source("dashboard:", max_messages=50)
        texts = [m.get("content", "") for m in results]
        included = sum(1 for t in texts if t.startswith("normal-"))
        assert included == 5


# ── Soft gate: incognito prompt prefix (chat.py) ──


class TestSoftGatePrompt:
    def test_incognito_prefix_injected(self):
        from kiro_crew.dashboard.chat import _apply_incognito_prefix

        slot = _ChatSlot("e1", memory_mode="incognito")
        result = _apply_incognito_prefix(slot, "Hello world")
        assert result.startswith("[INCOGNITO SESSION]")
        assert "Hello world" in result

    def test_temporary_prefix_injected(self):
        from kiro_crew.dashboard.chat import _apply_incognito_prefix

        slot = _ChatSlot("t1", memory_mode="temporary")
        result = _apply_incognito_prefix(slot, "Hello world")
        assert "[TEMPORARY SESSION]" in result or "[INCOGNITO" in result
        assert "Hello world" in result

    def test_no_prefix_for_persistent_session(self):
        from kiro_crew.dashboard.chat import _apply_incognito_prefix

        slot = _ChatSlot("n1")
        result = _apply_incognito_prefix(slot, "Hello world")
        assert result == "Hello world"

    def test_prefix_injected_for_resumed_restricted(self):
        from kiro_crew.dashboard.chat import _apply_incognito_prefix

        slot = _ChatSlot("e1", memory_mode="incognito")
        result = _apply_incognito_prefix(slot, "Follow-up question")
        assert result.startswith("[INCOGNITO SESSION]")
        assert "Follow-up question" in result


# ── History file integrity ──


class TestHistoryFileIntegrity:
    """list_sessions() must surface memory_mode; rewrite_session() must preserve it."""

    def test_list_sessions_includes_memory_mode(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "e1", [("user", "hi")], memory_mode="incognito")
        _write_session(log, "t1", [("user", "hi")], memory_mode="temporary")
        _write_session(log, "p1", [("user", "hi")])

        by_key = {s["key"]: s for s in log.list_sessions()}

        assert by_key["e1"].get("memory_mode") == "incognito"
        assert by_key["t1"].get("memory_mode") == "temporary"
        assert by_key["p1"].get("memory_mode") == "persistent"

    def test_rewrite_session_preserves_memory_mode(self, tmp_path):
        """Compaction must not drop memory_mode from metadata."""
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "e1", [("user", "a"), ("assistant", "b")], memory_mode="incognito")

        kept = [{"role": "user", "content": "a", "ts": "2026-01-01T00:00:01"}]
        log.rewrite_session("e1", kept)

        meta = log.get_metadata("e1")
        assert meta.get("memory_mode") == "incognito"

    def test_rewrite_session_persistent_has_no_memory_mode(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "p1", [("user", "a")])

        log.rewrite_session("p1", [{"role": "user", "content": "a", "ts": "2026-01-01T00:00:01"}])

        meta = log.get_metadata("p1")
        assert "memory_mode" not in meta


# ── Context builder: blocks_reads skips memory ──


class TestBlocksReadsContext:
    def test_blocks_reads_skips_memory_and_lessons(self, tmp_path):
        """build_session_context(blocks_reads=True) must not inject memory or lessons."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore

        ws_dir = tmp_path / "workspace"
        mem_dir = ws_dir / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "preferences.md").write_text("# User Preferences\n\n- Likes pizza\n")

        mem = MemoryStore(workspace=ws_dir)
        cb = ContextBuilder(memory=mem)

        ctx_normal = cb.build_session_context(
            session_key="dashboard:test-normal",
            workspace="default",
        )
        ctx_blocked = cb.build_session_context(
            session_key="dashboard:test-blocked",
            workspace="default",
            blocks_reads=True,
        )

        assert "Likes pizza" in ctx_normal
        assert "Likes pizza" not in ctx_blocked


# ── Session slot recovery via persisted JSONL ──


class TestSessionSlotRecovery:
    """learn_add must accept keys whose slot was evicted from memory but whose
    JSONL file still exists in the data home's sessions/ — this covers the
    long-lived Slack thread / reopened dashboard tab cases where the MCP
    subprocess holds a stale KIROCREW_SESSION_KEY env var that maps to a swept
    slot.
    """

    @pytest.fixture(autouse=True)
    def _persisted_history_dir_tracks_patched_home(self, monkeypatch):
        """Point ``_shared.config_dir()`` at ``<patched home>/.kirocrew``.

        The data home moved from ``~/.kirocrew`` to ``~/.kiro/crew``
        (``config_dir()``); ``_session_has_persisted_history`` now probes
        ``config_dir()/sessions`` rather than ``Path.home()/".kirocrew"/"sessions"``.
        Each test here patches ``Path.home()`` and seeds JSONL under
        ``<home>/.kirocrew/sessions``, but ``config_dir()`` reads ``KIROCREW_HOME``
        (a different tmp dir pinned by conftest), so redirect ``_shared``'s
        ``config_dir`` to ``Path.home()/".kirocrew"`` (lazy, tracks the per-test
        ``Path.home`` patch) to keep the seeded layout authoritative. Applied
        first so a test's own patches still win.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.config_dir",
            lambda: Path.home() / ".kirocrew",
        )

    def _write_sessions_jsonl(self, tmp_path, stem: str) -> None:
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / f"{stem}.jsonl").write_text(
            '{"_type": "metadata", "created_at": "2026-01-01T00:00:00"}\n',
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_learn_add_allowed_when_slot_evicted_but_jsonl_persists(
        self, tmp_path, monkeypatch
    ):
        """Core fix: evicted slot + existing JSONL → learn_add proceeds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Slot was evicted — state._slots is empty — but JSONL exists.
        self._write_sessions_jsonl(tmp_path, "1776000000.123456")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:1776000000.123456"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_when_slot_evicted_dashboard_prefix_jsonl(
        self, tmp_path, monkeypatch
    ):
        """dashboard_{stem}.jsonl fallback path from slack/interactions.py."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        self._write_sessions_jsonl(tmp_path, "dashboard_chat-1-1776000000")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:chat-1-1776000000"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_evicted_cron_session_underscore_jsonl(
        self, tmp_path, monkeypatch
    ):
        """A cron session keys off ``cron:{id}`` and persists its transcript as
        ``cron_{id}.jsonl`` (``_safe_key`` maps ``:`` → ``_``). The resolver must
        probe that name so an idle-evicted-but-real cron's learn_add succeeds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # cron:abc123 → _safe_key → cron_abc123.jsonl on disk; slot evicted.
        self._write_sessions_jsonl(tmp_path, "cron_abc123")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:abc123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_evicted_cron_session_dashboard_jsonl(
        self, tmp_path, monkeypatch
    ):
        """A cron's linked dashboard slot keys off ``dashboard:cron-{id}`` and
        persists as ``dashboard_cron-{id}.jsonl``; probing that name also
        recovers the evicted cron session for learn_add."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # dashboard:cron-abc123 → _safe_key → dashboard_cron-abc123.jsonl
        self._write_sessions_jsonl(tmp_path, "dashboard_cron-abc123")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:abc123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_learn_add_rejects_forged_cron_key_without_jsonl(
        self, tmp_path, monkeypatch
    ):
        """Regression guard: a ``cron:`` key with no backing JSONL is still
        rejected. The cron probes only ADD positive matches — they must not
        relax the deny path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Empty sessions dir so the path-exists check is meaningful.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "cron:forged123"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_path_traversal_in_cron_slot_name(
        self, tmp_path, monkeypatch
    ):
        """AC #4: the path-traversal guard still runs first on the slot_name
        even for a ``cron:``-prefixed key, so a traversal attempt is rejected
        even when a file exists at the resolved target."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        # Seed files at each resolved traversal target so the guard — NOT the
        # missing-file fallback — is what rejects each request.
        # "../escape" → sess_dir/../escape.jsonl → ~/.kirocrew/escape.jsonl
        (tmp_path / ".kirocrew" / "escape.jsonl").write_text("{}\n")
        # "a/b" → sess_dir/a/b.jsonl
        sub = sess_dir / "a"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "b.jsonl").write_text("{}\n")
        # "a\\b" (Windows separator) → literal single-name file on Linux.
        (sess_dir / "a\\b.jsonl").write_text("{}\n")

        async with TestClient(TestServer(_make_app(state))) as client:
            for bad_key in (
                "cron:../escape",
                "cron:a/b",
                "cron:a\\b",
            ):
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": bad_key},
                )
                assert resp.status == 400, f"cron path-traversal passed: {bad_key!r}"

    @pytest.mark.asyncio
    async def test_learn_add_still_rejected_when_no_jsonl_exists(self, tmp_path, monkeypatch):
        """Forged/stale keys with no backing JSONL are still rejected as unknown."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Create an empty sessions dir so the path-exists check is meaningful.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:forged-key"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_path_traversal_in_slot_name(self, tmp_path, monkeypatch):
        """Defence-in-depth: slot names with path separators, null bytes, or
        leading dots are rejected even when a matching file happens to exist
        at the resolved traversal target. This proves the guard itself blocks
        the request — without creating the target files, the test would pass
        even if the guard were removed because ``Path.exists()`` would return
        ``False`` for the missing file."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)

        # Seed files at every resolved traversal target so that the guard —
        # NOT the missing-file fallback — is what rejects each request.
        # "../escape" → sess_dir/../escape.jsonl → ~/.kirocrew/escape.jsonl
        (tmp_path / ".kirocrew" / "escape.jsonl").write_text("{}\n")
        # ".hidden" → sess_dir/.hidden.jsonl
        (sess_dir / ".hidden.jsonl").write_text("{}\n")
        # "a/b" → sess_dir/a/b.jsonl
        sub = sess_dir / "a"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "b.jsonl").write_text("{}\n")
        # "a\\b" (Windows path separator) → nominally sess_dir/a\b.jsonl.
        # Create a literal single-filename entry with an embedded backslash
        # so that, on Linux, a JSONL with that exact name exists — proving
        # the guard rejects backslash independent of platform behaviour.
        (sess_dir / "a\\b.jsonl").write_text("{}\n")

        async with TestClient(TestServer(_make_app(state))) as client:
            # These keys must be *rejected* — either by the server guard
            # (status 400) or by aiohttp's own header validation before the
            # request ever reaches the server (ValueError on newline / CR
            # / null byte). Both outcomes prove the traversal attempt is
            # blocked end-to-end.
            for bad_key in (
                "dashboard:../escape",
                "dashboard:.hidden",
                "dashboard:a/b",
                "dashboard:a\\b",
            ):
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": bad_key},
                )
                assert resp.status == 400, f"path-traversal attempt passed: {bad_key!r}"

            # Null byte: blocked at transport level. Older aiohttp raises
            # ValueError client-side; newer versions reject at the HTTP parser
            # (ServerDisconnectedError or similar). Either way, the request
            # cannot reach the handler — verify it doesn't succeed.
            import aiohttp
            try:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:bad\x00key"},
                )
                assert resp.status == 400, "null byte header reached handler"
            except (ValueError, aiohttp.ServerDisconnectedError, aiohttp.ClientConnectionError):
                pass  # transport-level rejection — acceptable

    def test_session_has_persisted_history_unit(self, tmp_path, monkeypatch):
        """Direct unit test for the helper."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _session_has_persisted_history

        assert _session_has_persisted_history("1776000000.123456") is False
        assert _session_has_persisted_history("") is False
        assert _session_has_persisted_history("../escape") is False
        assert _session_has_persisted_history(".hidden") is False
        assert _session_has_persisted_history("a\\b") is False
        assert _session_has_persisted_history("bad\x00key") is False

        sess_dir = tmp_path / ".kirocrew" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "1776000000.123456.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("1776000000.123456") is True

        # dashboard_ prefix fallback
        (sess_dir / "dashboard_chat-1.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("chat-1") is True

        # Even if a file exists at a traversal-style path, the guard still
        # rejects it — this is what actually proves defence-in-depth.
        (sess_dir / ".hidden.jsonl").write_text("{}\n")
        assert _session_has_persisted_history(".hidden") is False
        (sess_dir / "a\\b.jsonl").write_text("{}\n")
        assert _session_has_persisted_history("a\\b") is False

    # ── Audit events on positive-match paths (security-controls rule) ──

    @pytest.mark.asyncio
    async def test_learn_add_audits_live_slot_allow_path(self, tmp_path, monkeypatch):
        """Live in-memory slot → audit event with resources='live_slot'."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Seed a live slot so in_slots=True on the guard.
        state.get_or_create_slot("live1")

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "remember this", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:live1"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:live1", operation="learn_add", outcome="allowed",
            source="dashboard", resources="live_slot",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_restricted_key_allow_path(self, tmp_path, monkeypatch):
        """Key present in _restricted_keys → audit event with resources='restricted_key'.

        Restricted keys are blocked *later* in the handler by the
        ``_is_restricted_session`` guard (403), but the session-scope check
        itself permits them through — that positive-match decision must be
        audited for the security-controls rule even though the downstream
        write is denied.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Populate _restricted_keys without a live slot so in_slots=False
        # and in_restricted=True on the guard.
        state._restricted_keys.add("dashboard:r1")

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:r1"},
                )
                # Downstream _is_restricted_session still blocks the write
                # with 403, but the session-scope allow decision fires first.
                assert resp.status in (200, 403)

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:r1", operation="learn_add", outcome="allowed",
            source="dashboard", resources="restricted_key",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_channel_namespace_allow_path(self, tmp_path, monkeypatch):
        """Key in a channel namespace (here ``slack:``) → audit event with
        resources='channel_namespace' (the tag now covers every channel, not
        just Slack; see #1268)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "slack:C123:1777000000.000000"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="slack:C123:1777000000.000000", operation="learn_add", outcome="allowed",
            source="dashboard", resources="channel_namespace",
        )

    @pytest.mark.asyncio
    async def test_learn_add_audits_dashboard_ui_allow_path(self, tmp_path, monkeypatch):
        """Browser UI's static ``dashboard:ui`` key → audit event with
        resources='dashboard_ui'. This key bypasses the slot-scope block
        entirely; the allow decision still needs its own SEL event.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "x", "category": "knowledge"},
                    headers={"X-Session-Key": "dashboard:ui"},
                )
                assert resp.status == 200

        mock_sel().log_api_access.assert_any_call(
            caller="dashboard:ui", operation="learn_add", outcome="allowed",
            source="dashboard", resources="dashboard_ui",
        )

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_bare_slack_thread_ts_without_jsonl(
        self, tmp_path, monkeypatch
    ):
        """Regression: a Slack thread keys its session off the
        bare ``thread_ts`` (e.g. ``1781215864.487849``), not a ``slack:``
        prefix. The first ``learn_add`` in a fresh thread arrives *before*
        the session JSONL is flushed (the transcript is written only after
        the LLM turn completes), so without recognising the bare-ts shape it
        would race the flush and 400 with ``unknown session``. The key must be
        allowed via the Slack namespace even with an empty sessions dir.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        # Empty sessions dir: no JSONL fallback is possible, so the allow can
        # only come from the bare-thread_ts namespace recognition.
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        with patch("kiro_crew.dashboard.handlers.cron._sel") as mock_sel:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "remember this", "category": "knowledge"},
                    headers={"X-Session-Key": "1781215864.487849"},
                )
                assert resp.status == 200

        # Audited as a channel-namespace allow (bare Slack thread_ts), not a JSONL recovery.
        mock_sel().log_api_access.assert_any_call(
            caller="1781215864.487849", operation="learn_add", outcome="allowed",
            source="dashboard", resources="channel_namespace",
        )

    @pytest.mark.asyncio
    async def test_learn_add_rejects_non_slack_bare_numeric_key(self, tmp_path, monkeypatch):
        """The bare-ts heuristic must not widen authorization: a numeric key
        that does not match the Slack ``thread_ts`` shape (too-short
        sub-second component) with no backing JSONL is still ``unknown
        session``. Guards against the regex matching arbitrary ``N.M`` keys.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": "123.45"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"

    @pytest.mark.asyncio
    async def test_learn_add_rejects_unicode_digit_thread_ts_lookalike(self, tmp_path, monkeypatch):
        """Security: the Slack-ts regex gates an authorization decision, so it
        must match ASCII digits only. A key built from non-ASCII Unicode
        decimal digits (Arabic-Indic) that is otherwise thread_ts-shaped must
        NOT be granted channel_namespace access — `[0-9]` (not `\\d`) enforces
        this. With no backing JSONL the call is rejected as unknown session.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        state = _make_state(tmp_path)
        (tmp_path / ".kirocrew" / "sessions").mkdir(parents=True, exist_ok=True)

        # Arabic-Indic digits forming "١٧٨١٢١٥٨٦٤.٤٨٧٨٤٩" — \d would match this,
        # [0-9] does not.
        unicode_ts = "١٧٨١٢١٥٨٦٤.٤٨٧٨٤٩"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "remember this", "category": "knowledge"},
                headers={"X-Session-Key": unicode_ts},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "unknown session"


class TestArchivedRestrictedSessionRecovery:
    """An archived incognito/temporary tab must stay restricted.

    ``api_chat_slot_close`` drops the slot from ``state._slots`` AND discards
    its ``state._restricted_keys`` entry, while ``_save_slot_to_history``
    writes the transcript — including its ``memory_mode`` marker — to disk. A
    still-live MCP subprocess keeps sending the original session key, so both
    in-memory signals miss and the gate must fall back to the persisted mode.
    Without that fallback the establish-session probe (which only tests file
    EXISTENCE) reads the archived transcript as proof of an ordinary session
    and memory writes are allowed.
    """

    @staticmethod
    def _archive(tmp_path, slot_name, mode):
        """Write the JSONL an archived session in *mode* leaves behind."""
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        path = sess_dir / f"dashboard_{slot_name}.jsonl"
        meta = {"_type": "metadata", "created_at": "2026-01-01T00:00:00", "closed": True}
        if mode != "persistent":
            meta["memory_mode"] = mode
        path.write_text(
            _json.dumps(meta)
            + "\n"
            + _json.dumps({"role": "user", "content": "secret", "ts": "2026-01-01T00:00:01"})
            + "\n",
            encoding="utf-8",
        )
        return path

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_learn_add_denied_for_archived_restricted_session(
        self, tmp_path, monkeypatch, mode
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _session_has_persisted_history

        state = _make_state(tmp_path)
        self._archive(tmp_path, "e1", mode)
        # Preconditions: neither in-memory signal survives the archive, and the
        # establish-session probe DOES accept the key — so the only thing that
        # can deny the write is the persisted-mode fallback. Asserting this
        # keeps the test from passing for the wrong reason (a 400 "unknown
        # session") if the probe's path resolution ever changes.
        assert "e1" not in state._slots
        assert "dashboard:e1" not in state._restricted_keys
        assert _session_has_persisted_history("e1") is True

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked from ephemeral session", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:e1"},
            )
            assert resp.status == 403, await resp.text()
            assert "not allowed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_learn_add_still_allowed_for_archived_persistent_session(
        self, tmp_path, monkeypatch
    ):
        """The recovery path this rides on must keep working for normal sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._get_memory",
            MagicMock(return_value=MagicMock(vector_store=None)),
        )
        state = _make_state(tmp_path)
        self._archive(tmp_path, "p1", "persistent")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "legitimate lesson", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:p1"},
            )
            assert resp.status == 200, await resp.text()

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("incognito", "incognito"), ("temporary", "temporary"), ("persistent", "persistent")],
    )
    def test_persisted_memory_mode_reader(self, tmp_path, monkeypatch, mode, expected):
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        self._archive(tmp_path, "s1", mode)
        assert _persisted_session_memory_mode("s1") == expected

    def test_persisted_memory_mode_unknown_is_none_not_persistent(self, tmp_path, monkeypatch):
        """Unreadable metadata reads as None (unknown) — never as a mode.

        A valid header that merely LACKS ``memory_mode`` is a legacy persistent
        session and must read as ``persistent``; anything unparseable must read
        as ``None`` so the write gate fails closed instead of allowing.
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        assert _persisted_session_memory_mode("missing") is None
        assert _persisted_session_memory_mode("../escape") is None
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_corrupt.jsonl").write_text("not json\n{{\n", encoding="utf-8")
        assert _persisted_session_memory_mode("corrupt") is None
        # A non-string mode must not be coerced into a truthy value.
        (sess_dir / "dashboard_weird.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": 42}) + "\n", encoding="utf-8"
        )
        assert _persisted_session_memory_mode("weird") is None
        # Legacy header without the field -> persistent (writes stay allowed).
        (sess_dir / "dashboard_legacy.jsonl").write_text(
            _json.dumps({"_type": "metadata", "created_at": "2026-01-01T00:00:00"}) + "\n",
            encoding="utf-8",
        )
        assert _persisted_session_memory_mode("legacy") == "persistent"
        # A metadata object that is NOT the first line must not define the mode:
        # append() writes the header first, so a later one is message content.
        (sess_dir / "dashboard_late.jsonl").write_text(
            _json.dumps({"role": "user", "content": "hi"})
            + "\n"
            + _json.dumps({"_type": "metadata", "memory_mode": "persistent"})
            + "\n",
            encoding="utf-8",
        )
        assert _persisted_session_memory_mode("late") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "incognito ",
            " incognito",
            "\tincognito\n",
            "INCOGNITO",
            "Incognito",
            "temporary ",
            "TEMPORARY",
        ],
    )
    def test_whitespace_or_case_variant_modes_do_not_fail_open(
        self, tmp_path, monkeypatch, raw
    ):
        """A restricted mode must not slip through on casing/whitespace.

        The downstream comparison is set membership against
        INCOGNITO_MEMORY_MODES, so `"incognito "` would lower() to itself, miss
        the set, and read as unrestricted. Every variant must normalize to the
        restricted mode (never to None, which would also be wrong here: the
        header IS parseable and DOES name a restricted mode).
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode
        from kiro_crew.history import INCOGNITO_MEMORY_MODES

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_v1.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": raw}) + "\n", encoding="utf-8"
        )
        got = _persisted_session_memory_mode("v1")
        assert got == raw.strip().lower()
        assert got in INCOGNITO_MEMORY_MODES, f"{raw!r} escaped the restricted set as {got!r}"

    @pytest.mark.parametrize("raw", ["", "  ", "bogus", "persistent-ish", "incognito2"])
    def test_unrecognized_mode_reads_as_unknown(self, tmp_path, monkeypatch, raw):
        """An unrecognised value is unknown (None), not silently permissive."""
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import _persisted_session_memory_mode

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_v2.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": raw}) + "\n", encoding="utf-8"
        )
        assert _persisted_session_memory_mode("v2") is None

    @pytest.mark.asyncio
    async def test_learn_add_denied_for_whitespace_bearing_incognito(self, tmp_path, monkeypatch):
        """End-to-end: the padded value must still produce a 403, not a 200."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "dashboard_pad.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "incognito "}) + "\n",
            encoding="utf-8",
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked via a padded mode value", "category": "knowledge"},
                headers={"X-Session-Key": "dashboard:pad"},
            )
            assert resp.status == 403, await resp.text()

    @pytest.mark.asyncio
    async def test_ambiguous_stem_denies_instead_of_picking_a_winner(self, tmp_path, monkeypatch):
        """Two transcripts can claim one stem — the gate must not guess.

        ``slot_name`` arrives with its transport namespace stripped
        (``sk.split(":", 1)[-1]``), so a legacy Slack transcript at
        ``<ts>.jsonl`` and an archived dashboard slot named after that same ts at
        ``dashboard_<ts>.jsonl`` both match. Taking the first candidate lets the
        PERSISTENT Slack file answer for the INCOGNITO dashboard session and the
        lesson is stored. Existence stays true; the mode must read unknown.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import (
            _persisted_session_memory_mode,
            _probe_persisted_session,
        )

        ts = "1785861252.833429"
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        # Bare stem = legacy Slack transcript, persistent. Probed FIRST.
        (sess_dir / f"{ts}.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n",
            encoding="utf-8",
        )
        # Same stem under the dashboard prefix = archived INCOGNITO slot.
        (sess_dir / f"dashboard_{ts}.jsonl").write_text(
            _json.dumps({"_type": "metadata", "memory_mode": "incognito"}) + "\n",
            encoding="utf-8",
        )
        # First-match would report "persistent" here; ambiguity must win.
        assert _persisted_session_memory_mode(ts) == "persistent"  # first-match, unsafe alone
        exists, mode = _probe_persisted_session(ts)
        assert exists is True, "the session does exist — only the mode is unknown"
        assert mode is None, "ambiguous stem must not resolve to a mode"

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked via an ambiguous stem", "category": "knowledge"},
                headers={"X-Session-Key": f"dashboard:{ts}"},
            )
            assert resp.status == 403, await resp.text()

    def test_colon_slot_name_cannot_escape_sessions_dir(self, tmp_path, monkeypatch):
        """A colon is rejected: on Windows it yields a drive-relative escape.

        ``WindowsPath('.../sessions') / 'D:foo.jsonl'`` evaluates to
        ``D:foo.jsonl``, outside the sessions directory entirely (POSIX joins it
        literally and is unaffected), and it also spells an NTFS alternate data
        stream.

        The rejection is name-based and happens before any filesystem access, so
        the assertion below is meaningful on every platform. The *plant* is
        POSIX-only on purpose: on Windows the very path expression under test
        would write to another drive — i.e. outside ``tmp_path`` — which is
        exactly the escape being guarded against (and a colon is not a legal
        NTFS filename character anyway, so the file could not be created there).
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers._shared import (
            _persisted_session_memory_mode,
            _persisted_session_path,
            _session_has_persisted_history,
        )

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            # Plant a real file at the literal POSIX name, so the guard — not a
            # missing file — is provably what rejects it.
            (sess_dir / "D:foo.jsonl").write_text(
                _json.dumps({"_type": "metadata", "memory_mode": "persistent"}) + "\n",
                encoding="utf-8",
            )
            assert (sess_dir / "D:foo.jsonl").exists()
        for hostile in ("D:foo", "C:evil", "file:stream"):
            assert _persisted_session_path(hostile) is None, hostile
            assert _session_has_persisted_history(hostile) is False, hostile
            assert _persisted_session_memory_mode(hostile) is None, hostile


class TestDurableSlackFlagsAtHttpGate:
    """The HTTP gate must honour a Slack thread's DURABLE privacy flag.

    ``_thread_incognito``/``_thread_temporary`` are process-local and are only
    populated by ``_hydrate_conv_flags`` on an INBOUND Slack message. A turn no
    inbound message drove — a cron with ``session="origin"``, a webhook-resumed
    session, a monitor/autonudge re-injection, a subagent — reaches the gate
    with empty maps after a gateway restart, even though the user's
    ``!incognito`` is on disk. The gate must restore before it decides.
    """

    SLACK_KEY = "slack:1785861252.833429"

    @pytest.fixture()
    def durable(self, tmp_path, monkeypatch):
        """A real SessionMap in tmp_path, with the in-memory LRUs emptied."""
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers._shared.config_dir", lambda: tmp_path)
        from kiro_crew.slack import handler as _h

        _h._thread_temporary.clear()
        _h._thread_incognito.clear()
        yield
        _h._thread_temporary.clear()
        _h._thread_incognito.clear()

    @pytest.mark.parametrize("flag", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_learn_add_denied_for_durable_slack_flag(self, tmp_path, durable, flag):
        from kiro_crew.session_map import SessionMap
        from kiro_crew.slack import handler as _h

        sm = SessionMap()
        sm.set_flag(self.SLACK_KEY, flag, True)
        # Preconditions: durable on disk, absent from this process's maps.
        assert SessionMap().get_flag(self.SLACK_KEY, flag) is True
        assert _h.is_thread_incognito(self.SLACK_KEY) is False
        assert _h.is_thread_temporary(self.SLACK_KEY) is False

        state = _make_state(tmp_path)
        state.sessions._session_map = sm

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "leaked from a slack privacy thread", "category": "knowledge"},
                headers={"X-Session-Key": self.SLACK_KEY},
            )
            assert resp.status == 403, await resp.text()

    @pytest.mark.asyncio
    async def test_learn_add_allowed_for_unflagged_slack_thread(self, tmp_path, durable):
        """An ordinary Slack thread must stay writable — no over-blocking."""
        from unittest.mock import MagicMock as _MM

        from kiro_crew.session_map import SessionMap

        state = _make_state(tmp_path)
        state.sessions._session_map = SessionMap()
        with patch(
            "kiro_crew.dashboard.handlers._get_memory",
            _MM(return_value=_MM(vector_store=None)),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/lessons",
                    json={"rule": "legitimate slack lesson", "category": "knowledge"},
                    headers={"X-Session-Key": self.SLACK_KEY},
                )
                assert resp.status == 200, await resp.text()
