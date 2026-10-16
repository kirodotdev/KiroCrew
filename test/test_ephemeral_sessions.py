"""Tests for incognito/temporary session support.

Non-persistent sessions disable memory consolidation while keeping
conversation log persistence intact for tab recovery and gateway restart.

``test_ephemeral_sessions_recognition.py`` covers the session-recognition
gate behind the lessons and memory write routes: evicted and archived slots,
durable Slack privacy flags, cron keys, forged keys and path-traversal keys,
and the lessons delete and memory routes that apply the same gate. The state
and app builders that both files use live in ``ephemeral_sessions_helpers.py``.
"""

from __future__ import annotations

import json as _json
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from ephemeral_sessions_helpers import (
    _make_app,
    _make_state,
)

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


def _write_session(log, key, messages, *, memory_mode="persistent", mode=""):
    """Write a JSONL session file with optional memory_mode / mode metadata."""
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}
    if memory_mode != "persistent":
        meta["memory_mode"] = memory_mode
    if mode:
        meta["mode"] = mode
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
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_restricted_session_writes_transcript_for_history(self, tmp_path, monkeypatch, mode):
        """Every memory mode writes its transcript: the user can reopen it from History.

        What a restricted mode withholds is learning FROM the conversation
        (consolidation, lessons, memory injection), never the conversation
        itself. Those readers gate on the ``memory_mode`` the metadata line
        carries, which is asserted alongside the body.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("e1", memory_mode=mode)
        slot.append("user", "secret tax info")
        slot.append("assistant", "noted")

        assert _save_slot_to_history(state, slot)

        msgs = state.conversation_log.read_messages("dashboard:e1")
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "secret tax info"),
            ("assistant", "noted"),
        ]
        assert state.conversation_log.get_metadata("dashboard:e1").get("memory_mode") == mode
        assert len(slot.messages) == 2

    def test_new_restricted_session_metadata_carries_its_mode(self, tmp_path, monkeypatch):
        """The metadata line is the file's privacy contract, so it names the mode."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("e1", memory_mode="incognito")
        slot.append("user", "hello")

        _save_slot_to_history(state, slot)

        meta = state.conversation_log.get_metadata("dashboard:e1")
        assert meta == {
            "_type": "metadata",
            "created_at": slot.created_at,
            "last_consolidated": 0,
            "memory_mode": "incognito",
            "model": "",
            "autocompact_pct": None,
            "tab_id": slot._tab_id,
        }
        assert "memory_store" not in meta
        assert slot.memory_mode == "incognito"
        assert "dashboard:e1" in state._restricted_keys

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
        (docs/system-specs/modules/history.md): the session JSONL -- full user
        and assistant content -- is written by the ordinary flush path regardless
        of mode. A persisted title is therefore a summary of content already in
        that same file, not a new disclosure. If this ever starts asserting
        False, `_maybe_auto_title` must be re-gated on memory_mode.
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
        listed = state.conversation_log.list_sessions()
        assert len(listed) == 1
        assert listed[0]["memory_mode"] == "temporary"


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

        assert restored == 1
        assert "e1" in state2._slots
        assert state2._slots["e1"].memory_mode == "incognito"
        assert "dashboard:e1" in state2._restricted_keys
        assert [m["content"] for m in state2._slots["e1"].messages] == ["private stuff", "ok"]


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
    async def test_resume_folds_retired_crew_mode_to_plain_chat(self, tmp_path, monkeypatch):
        """A session persisted under the retired Crew Mode resumes as plain chat.

        This is the third restore path (History browser → resume, no live slot);
        the two persistence loaders fold the same way. Without the fold the
        response advertises ``surface: "crew"``, a surface the chat page does
        not render, so the resume silently fails to open.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _write_session(state.conversation_log, "old-crew", [("user", "hi")], mode="crew")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/old-crew/resume", json={"key": "old-crew"})
            data = await resp.json()

        assert data["ok"] is True
        assert data["surface"] == ""
        assert state._slots["old-crew"].mode == ""

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
            caller="dashboard:e1",
            operation="consolidate",
            outcome="denied",
            source="dashboard",
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
            MagicMock(return_value=MagicMock(agents={}, default_agent="")),
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
            MagicMock(return_value=MagicMock(agents={}, default_agent="")),
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
            MagicMock(return_value=MagicMock(agents={}, default_agent="")),
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
        # ``api_lessons_create`` lives in handlers/cron_lessons.py, which did
        # ``from ._shared import _get_memory``, so the name it calls is cron_lessons.py's
        # OWN global. Patching the handlers package re-export (or _shared) leaves
        # that global untouched: the route then builds a real ``MemoryStore`` and
        # runs ``init()``, and reaches the JSONL branch only by the accident that
        # a fresh store's ``vector_store`` is also None — which stops being true
        # the day it isn't, turning the session-acceptance assertion below into a
        # 500 about something else entirely.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
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
        NO persisted JSONL — the regression it guards, live-reproduced from a
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
            "kiro_crew.dashboard.handlers.cron._get_memory",
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
    async def test_learn_add_blocked_by_slot_fallback_on_restricted_key_desync(
        self, tmp_path, monkeypatch
    ):
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
    async def test_learn_add_allowed_for_browser_ui_despite_restricted_slot(
        self, tmp_path, monkeypatch
    ):
        """Browser Memory page sends 'dashboard:ui' — allowed even when restricted slots exist."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.cron._get_memory",
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
                url="http://x/api/lessons",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error": "unknown session"}'),
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
                url="http://x/api/lessons",
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=io.BytesIO(b"upstream exploded"),
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
                url="http://x/api/lessons",
                code=502,
                msg="Bad Gateway",
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
                url="http://x/api/lessons",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error": "unknown session"}'),
            )
            from kiro_crew.mcp_core import _call_tool_inner

            out = _call_tool_inner("learn_add", {"rule": "use tool-b for auth", "category": "tool"})

        assert "not saved" in out.lower()
        assert "HTTP Error 400" not in out


# ── Cross-tab privacy filtering (history.py) ──


class TestCrossTabPrivacy:
    def test_recent_from_source_skips_restricted_sessions(self, tmp_path):
        """Restricted session messages must not leak into 'Other chat tabs' context."""
        log = ConversationLog(base_dir=tmp_path)

        _write_session(
            log, "dashboard:e1", [("user", "secret private data")], memory_mode="incognito"
        )
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
            _write_session(
                log, f"dashboard:e{i}", [("user", f"secret-{i}")], memory_mode="temporary"
            )
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
            _write_session(
                log, f"dashboard:e{i}", [("user", f"secret-{i}")], memory_mode="incognito"
            )
            p = log._path(f"dashboard:e{i}")
            os.utime(p, (time.time() + 200 + i, time.time() + 200 + i))
        for i in range(5):
            _write_session(log, f"dashboard:n{i}", [("user", f"normal-{i}")])
            p = log._path(f"dashboard:n{i}")
            os.utime(p, (time.time() + i, time.time() + i))

        results = log.recent_from_source("dashboard:", max_messages=50)
        texts = [m.get("content", "") for m in results]
        included = sum(1 for t in texts if t.startswith("normal-"))
        assert included == 5


# ── Suggestions builder: restricted transcripts never ground the prompt ──


class TestSuggestionsContext:
    @pytest.mark.parametrize("mode", ["incognito", "temporary", "Incognito"])
    def test_build_context_skips_restricted_sessions(self, tmp_path, monkeypatch, mode):
        """A restricted transcript's rows never reach the suggestions prompt.

        ``_build_context`` walks ``list_sessions()`` and pulls each session's
        last user messages through ``recent()`` into a prompt that is shipped to a
        model and cached for the dashboard. The transcript is on disk for the
        user to reopen, so the gate has to be here, on the reader -- mirroring
        ``chat_folder_suggest._folder_sample_titles``. The restricted key must
        not be READ at all, not merely dropped from the output.
        """
        from kiro_crew import suggestions

        monkeypatch.setattr(
            suggestions.ContextBuilder,
            "get_memory_for",
            MagicMock(side_effect=RuntimeError("no memory in this test")),
        )
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "dashboard:e1", [("user", "SECRET tax question")], memory_mode=mode)
        _write_session(log, "dashboard:n1", [("user", "public refactor plan")])
        read_keys: list[str] = []
        real_recent = log.recent

        def _recent(key, *args, **kwargs):
            read_keys.append(key)
            return real_recent(key, *args, **kwargs)

        monkeypatch.setattr(log, "recent", _recent)
        state = MagicMock(conversation_log=log)
        state.crons.list_jobs.return_value = []

        context = suggestions._build_context(state)

        assert "public refactor plan" in context
        assert "SECRET" not in context
        assert "e1" not in context
        # ``list_sessions`` yields the file stem as the key; that is what ``recent``
        # is handed, so the read log is checked in that spelling.
        assert read_keys == ["dashboard_n1"], "the restricted transcript was read"

    def test_build_context_drops_a_session_tightened_during_its_row_read(
        self, tmp_path, monkeypatch
    ):
        """The listing row is a snapshot; the line is re-asked once the rows are read.

        A same-key hand-over can land a restricted tab's rows under a line that
        was persistent when ``list_sessions()`` ran. The rows just read must not
        ground the prompt if the line now says restricted.
        """
        from kiro_crew import suggestions

        monkeypatch.setattr(
            suggestions.ContextBuilder,
            "get_memory_for",
            MagicMock(side_effect=RuntimeError("no memory in this test")),
        )
        log = ConversationLog(base_dir=tmp_path)
        _write_session(log, "dashboard:t1", [("user", "SECRET landed late")])
        _write_session(log, "dashboard:n1", [("user", "public refactor plan")])
        real_locked_stems = type(log).locked_stems
        fired: set[str] = set()

        def _tighten_then_lock(self, stems):
            # The tightening writer takes the transcript locks the seam holds, so
            # it lands before the seam's hold (modelled here) or after -- never
            # inside; landing first, it is what the seam's own check sees.
            stems = list(stems)
            if any("dashboard_t1" in stem for stem in stems) and "done" not in fired:
                fired.add("done")
                self.update_metadata("dashboard_t1", {"memory_mode": "incognito"})
            return real_locked_stems(self, stems)

        monkeypatch.setattr(type(log), "locked_stems", _tighten_then_lock)
        state = MagicMock(conversation_log=log)
        state.crons.list_jobs.return_value = []

        context = suggestions._build_context(state)

        assert "public refactor plan" in context
        assert (
            "SECRET" not in context
        ), "rows read under a line tightened mid-read reached the prompt"


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
