"""Tests for session restore on startup and dashboard config API."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.chat import restore_recent_sessions
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

# ── Helpers ──


def _make_state(tmp_path, **kwargs):
    """Create a DashboardState with mocked services and real ConversationLog."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )


def _write_session(
    tmp_path: Path, key: str, messages: list[dict], meta: dict | None = None
) -> None:
    """Write a JSONL session file directly for test setup."""
    path = tmp_path / f"{key}.jsonl"
    lines = []
    meta_line = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "last_consolidated": 0}
    if meta:
        meta_line.update(meta)
    lines.append(json.dumps(meta_line))
    for m in messages:
        lines.append(json.dumps(m))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_config_app(tmp_path):
    """Minimal aiohttp app with dashboard config endpoint."""
    from kiro_crew.dashboard.handlers import api_dashboard_config

    state = _make_state(tmp_path)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


# ── restore_recent_sessions tests ──


class TestRestoreRecentSessions:
    def test_returns_zero_when_no_conversation_log(self, tmp_path):
        """Returns 0 when state has no conversation_log."""
        state = _make_state(tmp_path)
        state.conversation_log = None
        assert restore_recent_sessions(state) == 0

    def test_restores_recent_dashboard_session(self, tmp_path, monkeypatch):
        """Restores a dashboard session modified within the time window."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_chat1",
            [
                {"role": "user", "content": "hello", "ts": "2026-03-23T10:00:00"},
                {"role": "assistant", "content": "hi there", "ts": "2026-03-23T10:00:01"},
            ],
            meta={
                "title": "Test Chat",
                "agent": "kirocrew",
                "workspace": "myws",
                "mode": "orchestrator",
            },
        )
        # Touch the file to make it recent
        path = tmp_path / "dashboard_chat1.jsonl"
        path.touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        assert "chat1" in state._slots
        slot = state._slots["chat1"]
        assert slot.title == "Test Chat"
        assert slot.agent == "kirocrew"
        assert slot.workspace == "myws"
        assert slot.mode == "orchestrator"
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "hello"
        assert slot.messages[1]["content"] == "hi there"
        assert all(not (message.get("meta") or {}).get("mid") for message in slot.messages)
        assert slot._dirty is False
        assert slot._resumed_count == 2

    def test_restores_mode_empty_by_default(self, tmp_path, monkeypatch):
        """Sessions without mode in metadata default to empty string."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_nomode",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "No Mode"},
        )
        (tmp_path / "dashboard_nomode.jsonl").touch()
        state = _make_state(tmp_path)
        restore_recent_sessions(state, window_minutes=60)
        assert state._slots["nomode"].mode == ""

    def test_trust_flags_not_restored(self, tmp_path, monkeypatch):
        """Trust flags in metadata are NOT restored — security boundary."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_trusted",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Trusted", "trust": True, "trust_reads": True},
        )
        (tmp_path / "dashboard_trusted.jsonl").touch()
        state = _make_state(tmp_path)
        restore_recent_sessions(state, window_minutes=60)
        assert state._slots["trusted"]._trust is False
        assert state._slots["trusted"]._trust_reads is False

    def test_skips_old_sessions(self, tmp_path, monkeypatch):
        """Sessions older than the window are not restored."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_old",
            [{"role": "user", "content": "old msg", "ts": "2026-03-20T10:00:00"}],
        )
        # Set mtime to 2 hours ago
        path = tmp_path / "dashboard_old.jsonl"
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=30)
        assert restored == 0
        assert "old" not in state._slots

    def test_leaves_channel_sessions_to_the_reconciler(self, tmp_path, monkeypatch):
        """This loop runs ON the event loop, so it must not read channel transcripts.

        Channel-born tabs are restored by ``channel_slot_reconciler``, which
        reads in an executor. Pulling them in here would put a large
        transcript's read in front of the whole gateway at startup.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "slack_thread123",
            [{"role": "user", "content": "slack msg", "ts": "2026-03-23T10:00:00"}],
        )
        (tmp_path / "slack_thread123.jsonl").touch()

        state = _make_state(tmp_path)
        assert restore_recent_sessions(state, window_minutes=60) == 0

    def test_skips_sessions_with_no_dashboard_surface(self, tmp_path, monkeypatch):
        """Keys owned by another surface (cron, sub-agent) never become tabs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        for stem in ("cron_nightly", "subagent_abc123"):
            _write_session(
                tmp_path,
                stem,
                [{"role": "user", "content": "x", "ts": "2026-03-23T10:00:00"}],
            )
            (tmp_path / f"{stem}.jsonl").touch()

        state = _make_state(tmp_path)
        assert restore_recent_sessions(state, window_minutes=60) == 0

    def test_skips_already_existing_slots(self, tmp_path, monkeypatch):
        """Does not overwrite slots that already exist in state."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_existing",
            [{"role": "user", "content": "msg", "ts": "2026-03-23T10:00:00"}],
        )
        path = tmp_path / "dashboard_existing.jsonl"
        path.touch()

        state = _make_state(tmp_path)
        # Pre-create the slot
        slot = state.get_or_create_slot("existing")
        slot.append("user", "already here")
        slot.drain()

        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 0
        assert len(state._slots["existing"].messages) == 1
        assert state._slots["existing"].messages[0]["content"] == "already here"

    def test_limits_to_500_messages(self, tmp_path, monkeypatch):
        """Only the last 500 messages are loaded from a session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        messages = [
            {"role": "user", "content": f"msg {i}", "ts": f"2026-03-23T10:{i:04d}"}
            for i in range(600)
        ]
        _write_session(tmp_path, "dashboard_big", messages)
        path = tmp_path / "dashboard_big.jsonl"
        path.touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        slot = state._slots["big"]
        assert len(slot.messages) == 500
        assert slot.messages[0]["content"] == "msg 100"
        assert slot._disk_older_count == 100  # 600 total - 500 loaded = 100 older on disk

    def test_restore_computes_the_durable_prefix_count_from_disk(self, tmp_path, monkeypatch):
        """A trimmed prefix holding transient-role lines splits the two counters.

        ``_disk_older_count`` counts every on-disk line older than the window
        (the frozen prefix the save model preserves); the durable counter must
        count only the rows a durable read returns, and must be RECOMPUTED from
        the messages on disk — a slot restored by an older build has no durable
        counter persisted anywhere to trust.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        messages = [
            {"role": "user", "content": f"msg {i}", "ts": f"2026-03-23T10:{i:04d}"}
            for i in range(600)
        ]
        # 10 transient-role lines inside the trimmed prefix (a legacy or foreign
        # writer put them on disk; the save path itself never does).
        for i in range(10):
            messages[i * 5] = {
                "role": "permission",
                "content": "approve?",
                "ts": messages[i * 5]["ts"],
            }
        _write_session(tmp_path, "dashboard_big", messages)
        (tmp_path / "dashboard_big.jsonl").touch()

        state = _make_state(tmp_path)
        assert restore_recent_sessions(state, window_minutes=60) == 1
        slot = state._slots["big"]
        assert slot._disk_older_count == 100
        assert (
            slot._disk_older_durable_count == 90
        ), "the 10 transient prefix lines must not count as durable"

    def test_channel_rebuild_window_computes_the_durable_prefix_count(self, tmp_path, monkeypatch):
        """The channel restore path draws the same durable/all-rows line."""
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.channel_slots import _rebuild_window

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1", linked_session_key="slack:1.1")
        n = channel_slots._RESTORE_WINDOW
        messages = [
            {"role": "user", "content": f"msg {i}", "ts": f"2026-03-23T10:{i:04d}"}
            for i in range(n + 40)
        ]
        # 7 transient lines inside the 40-row trimmed prefix.
        for i in range(7):
            messages[i * 3] = {"role": "queued", "content": "q", "ts": messages[i * 3]["ts"]}

        _rebuild_window(slot, messages)

        assert slot._disk_older_count == 40
        assert slot._disk_older_durable_count == 33
        assert len(slot.messages) == n

    def test_restores_multiple_sessions(self, tmp_path, monkeypatch):
        """Multiple recent dashboard sessions are all restored."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        for name in ["dashboard_a", "dashboard_b", "dashboard_c"]:
            _write_session(
                tmp_path,
                name,
                [{"role": "user", "content": f"from {name}", "ts": "2026-03-23T10:00:00"}],
            )
            (tmp_path / f"{name}.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 3
        assert "a" in state._slots
        assert "b" in state._slots
        assert "c" in state._slots

    def test_dashboard_underscore_key_derives_correct_slot_name(self, tmp_path, monkeypatch):
        """Underscore-format key (dashboard_mychat) derives slot name 'mychat'."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_mychat",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
        )
        (tmp_path / "dashboard_mychat.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        assert "mychat" in state._slots
        assert state._slots["mychat"].messages[0]["content"] == "hi"

    def test_dashboard_colon_key_derives_correct_slot_name(self, tmp_path, monkeypatch):
        """Colon-format key (dashboard:mychat) derives slot name 'mychat'.

        list_sessions() returns keys from filenames, but the colon-stripping
        branch in restore_recent_sessions handles keys like 'dashboard:xyz'.
        We mock list_sessions() to return a colon-format key to exercise that path.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_mychat",
            [{"role": "user", "content": "colon test", "ts": "2026-03-23T10:00:00"}],
        )
        (tmp_path / "dashboard_mychat.jsonl").touch()

        state = _make_state(tmp_path)
        # Patch list_sessions to return the colon-format key
        original_list = state.conversation_log.list_sessions

        def patched_list():
            sessions = original_list()
            for s in sessions:
                if s["key"] == "dashboard_mychat":
                    s["key"] = "dashboard:mychat"
            return sessions

        monkeypatch.setattr(state.conversation_log, "list_sessions", patched_list)

        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        assert "mychat" in state._slots
        assert state._slots["mychat"].messages[0]["content"] == "colon test"

    def test_redacts_credentials_in_restored_messages(self, tmp_path, monkeypatch):
        """LLM-sourced content is redacted before it can reach a client.

        Redaction moved from load time to display time: the restore now loads
        stored bytes as-is and every EMIT site cleans them. The security property
        is unchanged (a credential must never reach a client) but the enforcement
        point moved, so this asserts the emit path rather than the slot contents.
        See test_display_time_redaction.py for the per-site coverage.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_redact",
            [
                {"role": "user", "content": "show me the key", "ts": "2026-03-23T10:00:00"},
                {
                    "role": "assistant",
                    "content": "Here: AKIAIOSFODNN7EXAMPLE",
                    "ts": "2026-03-23T10:00:01",
                },
            ],
        )
        (tmp_path / "dashboard_redact.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        slot = state._slots["redact"]
        # User content is preserved as-is, on load and on emit.
        assert slot.messages[0]["content"] == "show me the key"

        # The credential is gone from what the slot-detail endpoint returns...
        from kiro_crew.dashboard.chat_utils import _prepare_messages

        emitted = _prepare_messages(slot.messages, False, live_child="")
        rendered = " ".join(m.get("content", "") for m in emitted)
        assert "AKIAIOSFODNN7EXAMPLE" not in rendered
        assert "[REDACTED" in rendered
        # ...and from the prompt-building paths that leave the process.
        from kiro_crew.dashboard.chat_persistence import _build_history_prefix
        from kiro_crew.dashboard.side_context import _format_parent_snapshot

        assert "AKIAIOSFODNN7EXAMPLE" not in _build_history_prefix(slot)
        assert "AKIAIOSFODNN7EXAMPLE" not in _format_parent_snapshot(slot)

    def test_zero_window_restores_all_sessions(self, tmp_path, monkeypatch):
        """window_minutes=0 means infinite — restores sessions regardless of age."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_ancient",
            [{"role": "user", "content": "old msg", "ts": "2025-01-01T10:00:00"}],
        )
        # Set mtime to 30 days ago
        path = tmp_path / "dashboard_ancient.jsonl"
        old_time = time.time() - (30 * 24 * 3600)
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=0)
        assert restored == 1
        assert "ancient" in state._slots

    def test_negative_window_restores_all_sessions(self, tmp_path, monkeypatch):
        """Negative window_minutes is treated the same as 0 (restore all)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_neg",
            [{"role": "user", "content": "msg", "ts": "2025-01-01T10:00:00"}],
        )
        path = tmp_path / "dashboard_neg.jsonl"
        old_time = time.time() - (30 * 24 * 3600)
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=-5)
        assert restored == 1
        assert "neg" in state._slots

    def test_removeprefix_preserves_interior_dashboard(self, tmp_path, monkeypatch):
        """Slot name 'my_dashboard_session' is not mangled by prefix stripping."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_my_dashboard_session",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
        )
        (tmp_path / "dashboard_my_dashboard_session.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 1
        # The old .replace() would produce "my_session"; removeprefix gives "my_dashboard_session"
        assert "my_dashboard_session" in state._slots


# ── api_dashboard_config tests ──


class TestDashboardConfigAPI:
    @pytest.mark.asyncio
    async def test_get_defaults(self, tmp_path, monkeypatch):
        """GET returns default config values."""
        monkeypatch.setattr(
            "kiro_crew.config.loader.config_path", lambda: tmp_path / "nonexistent.json"
        )
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/dashboard/config")
                assert resp.status == 200
                data = await resp.json()
                assert data["restore_sessions"] is False
                assert data["restore_window_minutes"] == 30

    @pytest.mark.asyncio
    async def test_put_updates_config(self, tmp_path, monkeypatch):
        """PUT updates restore settings and persists to disk."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_sessions": True, "restore_window_minutes": 120},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                # Verify it was persisted
                assert cfg_file.exists()
                saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                assert saved["dashboard"]["restore_sessions"] is True
                assert saved["dashboard"]["restore_window_minutes"] == 120

    @pytest.mark.asyncio
    async def test_put_clamps_window_minutes(self, tmp_path, monkeypatch):
        """PUT clamps restore_window_minutes to [0, 1440] range."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                # Too high
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_window_minutes": 9999},
                )
                assert resp.status == 200
                saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                assert saved["dashboard"]["restore_window_minutes"] == 1440

                # Negative clamps to 0
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_window_minutes": -5},
                )
                assert resp.status == 200
                saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                assert saved["dashboard"]["restore_window_minutes"] == 0

    @pytest.mark.asyncio
    async def test_put_accepts_zero_window(self, tmp_path, monkeypatch):
        """PUT accepts restore_window_minutes=0 (infinite restore)."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_window_minutes": 0},
                )
                assert resp.status == 200
                saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                assert saved["dashboard"]["restore_window_minutes"] == 0

    @pytest.mark.asyncio
    async def test_put_invalid_json(self, tmp_path, monkeypatch):
        """PUT with invalid JSON returns 400."""
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/dashboard/config",
                    data="not json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_invalid_window_type(self, tmp_path, monkeypatch):
        """PUT with non-integer restore_window_minutes returns 400."""
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_window_minutes": "not a number"},
                )
                assert resp.status == 400
                data = await resp.json()
                assert "integer" in data["error"]

    @pytest.mark.asyncio
    async def test_put_invalid_restore_sessions_type(self, tmp_path, monkeypatch):
        """PUT with non-boolean restore_sessions returns 400."""
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/dashboard/config",
                    json={"restore_sessions": "false"},
                )
                assert resp.status == 400
                data = await resp.json()
                assert "boolean" in data["error"]

    @pytest.mark.asyncio
    async def test_get_after_put_reflects_changes(self, tmp_path, monkeypatch):
        """GET after PUT returns the updated values."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_config_app(tmp_path)
            async with TestClient(TestServer(app)) as client:
                await client.put(
                    "/api/dashboard/config",
                    json={"restore_sessions": True, "restore_window_minutes": 60},
                )
                resp = await client.get("/api/dashboard/config")
                data = await resp.json()
                assert data["restore_sessions"] is True
                assert data["restore_window_minutes"] == 60


# ── Config loader roundtrip tests ──


class TestConfigRestoreFields:
    def test_defaults_have_restore_fields(self):
        """Default config has restore_sessions=False and restore_window_minutes=30."""
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        assert cfg.dashboard.restore_sessions is False
        assert cfg.dashboard.restore_window_minutes == 30

    def test_load_restore_fields_from_file(self, tmp_path, monkeypatch):
        """Config loader reads dashboard restore fields from JSON."""
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "dashboard": {
                        "url": "http://localhost:9120",
                        "restore_sessions": True,
                        "restore_window_minutes": 120,
                    }
                }
            )
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)

        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.restore_sessions is True
        assert cfg.dashboard.restore_window_minutes == 120
        assert cfg.dashboard.url == "http://localhost:9120"

    def test_to_dict_includes_restore_fields(self):
        """to_dict() serializes restore fields under dashboard key."""
        from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig

        cfg = KiroCrewConfig(
            dashboard=DashboardConfig(restore_sessions=True, restore_window_minutes=60)
        )
        d = cfg.to_dict()
        assert d["dashboard"]["restore_sessions"] is True
        assert d["dashboard"]["restore_window_minutes"] == 60

    def test_save_and_reload_roundtrip(self, tmp_path, monkeypatch):
        """save() then load() preserves restore fields."""
        from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig

        cfg_file = tmp_path / ".kirocrew" / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)

        cfg = KiroCrewConfig(
            dashboard=DashboardConfig(restore_sessions=True, restore_window_minutes=720)
        )
        cfg.save()

        loaded = KiroCrewConfig.load()
        assert loaded.dashboard.restore_sessions is True
        assert loaded.dashboard.restore_window_minutes == 720

    def test_missing_restore_fields_use_defaults(self, tmp_path, monkeypatch):
        """Config without dashboard restore fields falls back to defaults."""
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"url": "http://localhost:9120"}}))
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)

        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.restore_sessions is False
        assert cfg.dashboard.restore_window_minutes == 30

    def test_restores_foldered_session_regardless_of_age(self, tmp_path, monkeypatch):
        """Sessions with folder_id are restored even when older than the window."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_foldered",
            [{"role": "user", "content": "in folder", "ts": "2026-03-20T10:00:00"}],
            meta={"folder_id": "f1"},
        )
        path = tmp_path / "dashboard_foldered.jsonl"
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=30)
        assert restored == 1
        assert "foldered" in state._slots
        assert state._slots["foldered"].folder_id == "f1"

    def test_closed_foldered_session_not_restored(self, tmp_path, monkeypatch):
        """Closed sessions are NOT restored even with folder_id — explicit close always wins."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_closedfolder",
            [{"role": "user", "content": "closed but foldered", "ts": "2026-03-23T10:00:00"}],
            meta={"closed": True, "folder_id": "f2"},
        )
        path = tmp_path / "dashboard_closedfolder.jsonl"
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 0
        assert "closedfolder" not in state._slots

    def test_skips_closed_session_without_folder(self, tmp_path, monkeypatch):
        """Closed sessions without folder_id are not restored."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_closednofolder",
            [{"role": "user", "content": "closed no folder", "ts": "2026-03-23T10:00:00"}],
            meta={"closed": True},
        )
        (tmp_path / "dashboard_closednofolder.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 0

    def test_folders_only_skips_non_foldered(self, tmp_path, monkeypatch):
        """folders_only=True skips sessions without folder_id."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_nofolder",
            [{"role": "user", "content": "no folder", "ts": "2026-03-23T10:00:00"}],
        )
        (tmp_path / "dashboard_nofolder.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60, folders_only=True)
        assert restored == 0

    def test_folders_only_restores_foldered(self, tmp_path, monkeypatch):
        """folders_only=True restores sessions with folder_id."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_withfolder",
            [{"role": "user", "content": "has folder", "ts": "2026-03-23T10:00:00"}],
            meta={"folder_id": "f3"},
        )
        (tmp_path / "dashboard_withfolder.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60, folders_only=True)
        assert restored == 1
        assert state._slots["withfolder"].folder_id == "f3"

    def test_folder_id_persisted_in_flush(self, tmp_path, monkeypatch):
        """folder_id is written to JSONL metadata when slot is saved."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("testslot")
        slot.folder_id = "f-abc"
        slot.append("user", "hello")
        slot.drain()

        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)

        path = tmp_path / "dashboard_testslot.jsonl"
        assert path.exists()
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta["folder_id"] == "f-abc"

    def test_restores_pinned_session_regardless_of_age(self, tmp_path, monkeypatch):
        """Pinned sessions are restored even when older than the window."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_pinnedold",
            [{"role": "user", "content": "pinned", "ts": "2026-03-20T10:00:00"}],
            meta={"pinned": True},
        )
        path = tmp_path / "dashboard_pinnedold.jsonl"
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=30)
        assert restored == 1
        assert state._slots["pinnedold"].pinned is True

    def test_folders_only_restores_pinned(self, tmp_path, monkeypatch):
        """folders_only=True also restores pinned sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_pinnedonly",
            [{"role": "user", "content": "pinned", "ts": "2026-03-23T10:00:00"}],
            meta={"pinned": True},
        )
        (tmp_path / "dashboard_pinnedonly.jsonl").touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60, folders_only=True)
        assert restored == 1
        assert state._slots["pinnedonly"].pinned is True

    def test_pinned_persisted_in_save(self, tmp_path, monkeypatch):
        """pinned is written to JSONL metadata when slot is saved."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("pinslot")
        slot.pinned = True
        slot.append("user", "hello")
        slot.drain()

        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)

        path = tmp_path / "dashboard_pinslot.jsonl"
        assert path.exists()
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta["pinned"] is True


# ── _rehydrate_slot_from_history tests ──


class TestRehydrateSlotFromHistory:
    """Integration tests for the per-slot rehydrate path used by cron→origin
    injection. Unlike restore_recent_sessions (bulk startup), this helper
    rehydrates a single slot on demand and must preserve metadata (memory_mode,
    title, agent, messages) so the revived slot is not a phantom empty tab."""

    def test_returns_none_when_session_not_on_disk(self, tmp_path, monkeypatch):
        """Returns None for a slot name that has no persisted session file.

        The messaging handler relies on this to distinguish "slot truly gone
        → fall through to Slack DM" from "slot on disk but unloaded → revive"."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        assert _rehydrate_slot_from_history(state, "missing-slot") is None
        assert "missing-slot" not in state._slots

    def test_returns_existing_slot_without_reloading(self, tmp_path, monkeypatch):
        """Hot-path: when slot is already in memory, return it as-is."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        existing = state.get_or_create_slot("hot-slot")
        existing.title = "Original Title"
        result = _rehydrate_slot_from_history(state, "hot-slot")
        assert result is existing
        # No double-registration
        assert state._slots["hot-slot"] is existing
        assert existing.title == "Original Title"

    def test_rehydrates_slot_with_metadata_and_messages(self, tmp_path, monkeypatch):
        """Rehydrate restores title, agent, model, memory_mode and message history
        from the persisted JSONL — not just an empty shell.

        Pin the config provider so the assertion is deterministic: rehydration
        canonicalizes the stored model per-provider via
        ``model_registry.canonicalize_for_provider`` (so a raw provider id like
        ``claude-opus-4.7`` maps back to the canonical dropdown key for CC). On
        a kiro provider it is a no-op; previously this test read the ambient
        on-disk config and so passed or failed depending on the dev machine.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Force a kiro (acp) provider so canonicalize_for_provider is a no-op and
        # the stored model round-trips unchanged, independent of the dev config.
        _acp_cfg = KiroCrewConfig()
        _acp_cfg.agent.provider = "acp"
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _acp_cfg))
        _write_session(
            tmp_path,
            "dashboard_originchat",
            [
                {"role": "user", "content": "first", "ts": "2026-03-23T10:00:00"},
                {"role": "assistant", "content": "reply", "ts": "2026-03-23T10:00:01"},
            ],
            meta={"title": "Cron Owner Tab", "agent": "general", "model": "claude-opus-4.7"},
        )
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "originchat")
        assert slot is not None
        assert slot.title == "Cron Owner Tab"
        assert slot.agent == "general"
        assert slot.model == "claude-opus-4.7"
        # Message history restored — not a phantom empty tab.
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "first"
        assert slot.messages[1]["content"] == "reply"
        # Registered in _slots so subsequent send_message calls hit the hot path.
        assert state._slots["originchat"] is slot

    def test_rehydrate_canonicalizes_model_for_claude_code(self, tmp_path, monkeypatch):
        """For a claude_code session, a stored raw provider id is mapped back to
        the canonical registry key so it matches the canonical-keyed dropdown."""
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _cc_cfg = KiroCrewConfig()
        _cc_cfg.agent.provider = "claude_code"
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cc_cfg))
        _write_session(
            tmp_path,
            "dashboard_ccchat",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "CC", "model": "claude-opus-4.7"},
        )
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "ccchat")
        assert slot is not None
        # Raw provider id canonicalized to the registry key for CC.
        assert slot.model == "opus-4.7-1m"

    def test_rehydrates_incognito_memory_mode(self, tmp_path, monkeypatch):
        """Rehydrated slot preserves non-persistent memory_mode from metadata.

        Regression guard for the phantom-slot bug: naive get_or_create_slot
        would default to memory_mode='persistent', so an incognito cron message
        would leak content to disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_incog",
            [{"role": "user", "content": "secret", "ts": "2026-03-23T10:00:00"}],
            meta={"memory_mode": "off", "title": "Private Tab"},
        )
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "incog")
        assert slot is not None
        assert slot.memory_mode == "off"
        # Restricted keys marker is set so consolidation respects the mode.
        assert "dashboard:incog" in state._restricted_keys

    def test_rehydrates_folder_and_pin_metadata(self, tmp_path, monkeypatch):
        """Folder, pin, and color metadata are preserved across rehydrate."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_foldered",
            [{"role": "user", "content": "x", "ts": "2026-03-23T10:00:00"}],
            meta={
                "title": "Foldered",
                "folder_id": "work",
                "pinned": True,
                "color_index": 3,
            },
        )
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "foldered")
        assert slot is not None
        assert slot.folder_id == "work"
        assert slot.pinned is True
        assert slot.color_index == 3

    def test_skips_closed_session(self, tmp_path, monkeypatch):
        """Explicitly closed sessions are NOT rehydrated — cron messages fall
        through to Slack DM instead of resurrecting a tab the user closed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_closed",
            [{"role": "user", "content": "bye", "ts": "2026-03-23T10:00:00"}],
            meta={"closed": True, "title": "Done"},
        )
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        assert _rehydrate_slot_from_history(state, "closed") is None
        assert "closed" not in state._slots

    def test_returns_none_when_no_conversation_log(self, tmp_path):
        """Without a conversation_log, rehydrate is a no-op returning None."""
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        state.conversation_log = None
        assert _rehydrate_slot_from_history(state, "anything") is None


# ── rehydrate_slot_from_history_async (async twin) ──


class TestAsyncRehydrateSlotFromHistory:
    """The async twin used by app backends resolving a cold worker slot on the
    request path. Same result as the sync helper, but the transcript read happens
    in a worker thread: a multi-megabyte session file read on the event loop
    stalls chat, WebSockets and the heartbeat for every connected client.

    The slot mutation deliberately stays on the loop — ``slot.append`` sets an
    ``asyncio.Event``, which is not safe to touch from another thread.
    """

    @pytest.mark.asyncio
    async def test_matches_the_sync_helper(self, tmp_path, monkeypatch):
        """Parity: title, metadata and full message history, same as sync."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_asyncchat",
            [
                {"role": "user", "content": "first", "ts": "2026-03-23T10:00:00"},
                {"role": "assistant", "content": "reply", "ts": "2026-03-23T10:00:01"},
            ],
            meta={"title": "Async Tab", "agent": "general"},
        )
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        state = _make_state(tmp_path)
        slot = await rehydrate_slot_from_history_async(state, "asyncchat")
        assert slot is not None
        assert slot.title == "Async Tab"
        assert [m["content"] for m in slot.messages] == ["first", "reply"]
        assert state._slots["asyncchat"] is slot

    @pytest.mark.asyncio
    async def test_the_transcript_read_is_off_the_loop(self, tmp_path, monkeypatch):
        """The whole point: no message read may run on the event-loop thread."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_offloop",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Off Loop"},
        )
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        state = _make_state(tmp_path)
        loop_thread = threading.get_ident()
        read_threads: list[int] = []
        real_read = state.conversation_log.read_messages_chained

        def _spy(key):
            read_threads.append(threading.get_ident())
            return real_read(key)

        monkeypatch.setattr(state.conversation_log, "read_messages_chained", _spy)
        slot = await rehydrate_slot_from_history_async(state, "offloop")

        assert slot is not None
        assert read_threads, "the transcript was never read"
        assert loop_thread not in read_threads, "the transcript was read ON the event loop"

    @pytest.mark.asyncio
    async def test_closed_sessions_need_an_explicit_opt_in(self, tmp_path, monkeypatch):
        """A session the user closed stays closed by default. ``adopt_closed``
        exists for app-owned worker slots, whose lifecycle belongs to the app —
        idle-slot cleanup archives them with closed=True without the user ever
        asking, and that must not permanently hide the transcript."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_archived",
            [{"role": "user", "content": "mid-build", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Spec: x", "app": "spec-builder", "closed": True},
        )
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        state = _make_state(tmp_path)
        assert await rehydrate_slot_from_history_async(state, "archived") is None
        assert "archived" not in state._slots, "a closed session created a phantom slot"

        revived = await rehydrate_slot_from_history_async(state, "archived", adopt_closed=True)
        assert revived is not None
        assert [m["content"] for m in revived.messages] == ["mid-build"]
        # Ownership travels with the transcript so callers can police it.
        assert revived._app == "spec-builder"

    @pytest.mark.asyncio
    async def test_a_slot_created_during_the_read_is_not_double_populated(
        self, tmp_path, monkeypatch
    ):
        """The await opens a window the sync helper never had: another task can
        materialize this slot while we are reading. Applying the snapshot on top
        would duplicate its messages, so the post-await recheck returns the live
        slot untouched."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "dashboard_racy",
            [{"role": "user", "content": "from disk", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Racy"},
        )
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        state = _make_state(tmp_path)
        real_read = state.conversation_log.read_messages_chained

        def _read_then_race(key):
            out = real_read(key)
            # Simulate the concurrent creation landing while we are off-loop.
            state.get_or_create_slot("racy").append("user", "from the other task", "msg msg-u")
            return out

        monkeypatch.setattr(state.conversation_log, "read_messages_chained", _read_then_race)
        slot = await rehydrate_slot_from_history_async(state, "racy")

        assert slot is state._slots["racy"]
        assert [m["content"] for m in slot.messages] == [
            "from the other task"
        ], "the snapshot was applied on top of a concurrently created slot"


class TestPartialRehydrateRollsBack:
    """A raise partway through populating a slot must leave NOTHING registered.

    ``_rehydrate_slot_from_history`` registers the slot via ``get_or_create_slot`` before
    any of its fallible work (title redaction, model canonicalization, the message
    replay). A failure after that used to leave a half-populated slot in
    ``state._slots`` -- and every rehydrate entry point short-circuits on
    ``slot_name in state._slots``, so the next caller received the partial slot as
    a complete restore with ``_disk_older_count`` still 0, after which a save
    rewrote the frozen on-disk prefix it had never loaded.
    """

    def _boom(self, *_a, **_k):
        raise RuntimeError("malformed persisted content")

    def test_sync_rehydrate_leaves_no_partial_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        _write_session(
            tmp_path,
            "dashboard_doomed",
            [{"role": "user", "content": "hi"}],
            meta={"title": "T", "memory_mode": "ephemeral"},
        )
        # Fail inside the population, after the slot is registered.
        monkeypatch.setattr(chat_persistence, "redact_credentials", self._boom)

        with pytest.raises(RuntimeError):
            _rehydrate_slot_from_history(state, "doomed")

        assert "doomed" not in state._slots, "a partial slot was left registered"
        assert "dashboard:doomed" not in state._restricted_keys, (
            "restricted key left behind: a later persistent slot would inherit it "
            "and silently lose consolidation + lessons"
        )

    @pytest.mark.asyncio
    async def test_async_rehydrate_leaves_no_partial_slot(self, tmp_path, monkeypatch):
        """The async twin -- the path app-owned worker slots use. This is the one
        that had no protection at all before the rollback moved into the callee."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence

        state = _make_state(tmp_path)
        _write_session(
            tmp_path,
            "dashboard_doomed-async",
            [{"role": "user", "content": "hi"}],
            meta={"title": "T", "memory_mode": "ephemeral"},
        )
        monkeypatch.setattr(chat_persistence, "redact_credentials", self._boom)

        with pytest.raises(RuntimeError):
            await chat_persistence.rehydrate_slot_from_history_async(state, "doomed-async")

        assert "doomed-async" not in state._slots
        assert "dashboard:doomed-async" not in state._restricted_keys

    def test_a_preexisting_restricted_key_is_never_discarded(self, tmp_path, monkeypatch):
        """The rollback must undo only what its own call added.

        A restricted key can outlive the slot it was recorded for, so a failed
        restore must leave one it found in place. Discarding it would let a later
        get_or_create_slot (default memory_mode 'persistent') treat an ephemeral
        session as a normal one.

        The slot half needs no equivalent test: _rehydrate_slot_from_history
        returns early when the slot already exists, so its body only ever runs
        for a slot the call created itself.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        state._restricted_keys.add("dashboard:keeper")
        _write_session(
            tmp_path,
            "dashboard_keeper",
            [{"role": "user", "content": "hi"}],
            meta={"title": "T", "memory_mode": "ephemeral"},
        )
        monkeypatch.setattr(chat_persistence, "redact_credentials", self._boom)

        with pytest.raises(RuntimeError):
            _rehydrate_slot_from_history(state, "keeper")

        assert (
            "dashboard:keeper" in state._restricted_keys
        ), "rollback discarded a restricted key it did not add"

    def test_the_rollback_lives_in_the_callee_not_the_caller(self):
        """Source guard: the rollback belongs at the creation site so EVERY caller
        gets it. restore_open_slots used to carry its own copy, which protected
        only itself -- the async twin had none. Asserts on the code, not comments."""
        import inspect

        from kiro_crew.dashboard import chat_persistence

        callee = inspect.getsource(chat_persistence._rehydrate_slot_from_history)
        assert "state._slots.pop(" in callee, "the callee no longer rolls back its own slot"
        assert "_restricted_keys.discard(" in callee, "the callee no longer rolls back its key"

        caller = inspect.getsource(chat_persistence.restore_open_slots)
        body = caller.split('"""')[-1]
        assert "state._slots.pop(" not in body, (
            "restore_open_slots re-grew its own rollback: two copies of the same "
            "undo will drift, and the callee's is the one every caller reaches"
        )


# ── restore_recent_sessions_async: the reads must leave the loop (#895) ──
#
# This driver is the slower of the two startup restores: it calls list_sessions()
# (a glob + stat + first-line read of EVERY session file) and then per selected
# session a get_metadata plus a chained transcript walk — measured at 13.6s for
# 76 sessions, all of it on the event loop between per-session yields. The yield
# bounded one session's share; it did not stop a single large transcript from
# stalling the stall-watchdog heartbeat. So every read is hoisted into
# asyncio.to_thread and only the slot build stays on the loop, which is
# mandatory: slot creation broadcasts through asyncio.Queue.put_nowait /
# Event.set, neither of which is thread-safe.


class TestAsyncRestoreRecentSessionsOffLoop:
    @pytest.mark.asyncio
    async def test_matches_the_sync_driver(self, tmp_path, monkeypatch):
        """Parity guard: the two drivers no longer share a generator body."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        for name in ("dashboard_par1", "dashboard_par2"):
            _write_session(
                tmp_path,
                name,
                [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
                meta={"title": name, "agent": "kirocrew"},
            )
            (tmp_path / f"{name}.jsonl").touch()

        sync_state = _make_state(tmp_path)
        async_state = _make_state(tmp_path)
        sync_n = restore_recent_sessions(sync_state, window_minutes=60)
        async_n = await restore_recent_sessions_async(async_state, window_minutes=60)

        assert sync_n == async_n == 2
        assert set(sync_state._slots) == set(async_state._slots) == {"par1", "par2"}
        assert [m["content"] for m in async_state._slots["par1"].messages] == ["hi"]

    @pytest.mark.asyncio
    async def test_every_read_runs_off_the_loop_and_the_build_runs_on_it(
        self, tmp_path, monkeypatch
    ):
        """list_sessions, get_metadata and the transcript walk must all be offloaded."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        for i in range(3):
            name = f"dashboard_off{i}"
            _write_session(
                tmp_path,
                name,
                [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
                meta={"title": name},
            )
            (tmp_path / f"{name}.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        reads: list[str] = []
        builds: list[str] = []

        real_list = log.list_sessions
        real_meta = log.get_metadata
        real_chained = log.read_messages_chained
        real_apply = chat_persistence._apply_recent_session

        def _list():
            reads.append(threading.current_thread().name)
            return real_list()

        def _meta(key):
            reads.append(threading.current_thread().name)
            return real_meta(key)

        def _chained(key):
            reads.append(threading.current_thread().name)
            return real_chained(key)

        def _apply(*a, **kw):
            builds.append(threading.current_thread().name)
            return real_apply(*a, **kw)

        monkeypatch.setattr(log, "list_sessions", _list)
        monkeypatch.setattr(log, "get_metadata", _meta)
        monkeypatch.setattr(log, "read_messages_chained", _chained)
        monkeypatch.setattr(chat_persistence, "_apply_recent_session", _apply)

        main = threading.current_thread().name
        restored = await restore_recent_sessions_async(state, window_minutes=60)

        assert restored == 3
        assert reads, "nothing was read — the test would not detect the bug"
        assert all(t != main for t in reads), (
            "a startup restore read ran ON the event-loop thread "
            f"(threads seen: {sorted(set(reads))})"
        )
        assert builds == [main] * 3, (
            "slot construction left the event-loop thread; it broadcasts through "
            f"asyncio.Queue.put_nowait / Event.set (threads seen: {builds})"
        )

    @pytest.mark.asyncio
    async def test_a_skipped_session_never_pays_for_its_transcript(self, tmp_path, monkeypatch):
        """The filters run BETWEEN the two reads, on the cheap one.

        Reading a multi-megabyte transcript and then discarding it because the
        session is closed or outside the window is pure waste, and it is the read
        that dominates startup.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_keep",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "keep"},
        )
        _write_session(
            tmp_path,
            "dashboard_shut",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "shut", "closed": True},
        )
        for name in ("dashboard_keep", "dashboard_shut"):
            (tmp_path / f"{name}.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        walked: list[str] = []
        real_chained = log.read_messages_chained

        def _chained(key):
            walked.append(key)
            return real_chained(key)

        monkeypatch.setattr(log, "read_messages_chained", _chained)

        assert await restore_recent_sessions_async(state, window_minutes=60) == 1
        assert set(state._slots) == {"keep"}
        assert not any(
            "shut" in k for k in walked
        ), f"read the transcript of a session it then skipped: {walked}"

    @pytest.mark.asyncio
    async def test_the_restoring_guard_is_released_even_on_failure(self, tmp_path, monkeypatch):
        """A stuck flag silently disables open-tab persistence for the process."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        state = _make_state(tmp_path)
        with patch.object(
            state.conversation_log, "list_sessions", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError):
                await restore_recent_sessions_async(state, window_minutes=60)
        assert state.restoring_open_slots is False

    @pytest.mark.asyncio
    async def test_a_channel_born_session_is_left_to_the_reconciler(self, tmp_path, monkeypatch):
        """Non-dashboard keys stay out of this path — the reconciler owns them."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "slack_1712793600.123456",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "thread"},
        )
        (tmp_path / "slack_1712793600.123456.jsonl").touch()

        state = _make_state(tmp_path)
        assert await restore_recent_sessions_async(state, window_minutes=60) == 0
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_a_slot_published_during_the_read_is_not_replayed_over(
        self, tmp_path, monkeypatch
    ):
        """Post-hop re-check: the pre-hop ``_slots`` answer is seconds stale.

        Offloading the reads opened a window that did not exist before — the
        check and the apply used to run atomically on the loop. If a resume, a
        nudge or the user opening the tab publishes the slot while its transcript
        loads, ``_apply_recent_session`` -> ``get_or_create_slot`` returns that
        LIVE slot and the replay appends the on-disk messages a second time, then
        persists the duplicates. Flagged as blocking by GPT 5.6 review and by
        Design review on the first CI round.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_racy",
            [
                {"role": "user", "content": "first", "ts": "2026-03-23T10:00:00"},
                {"role": "assistant", "content": "reply", "ts": "2026-03-23T10:00:01"},
            ],
            meta={"title": "Racy"},
        )
        (tmp_path / "dashboard_racy.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        real_chained = log.read_messages_chained

        def _publish_then_read(key):
            # Stand in for a resume publishing the slot mid-read: the live slot
            # already holds the transcript, which is exactly the state a replay
            # would double.
            if "racy" in key and "racy" not in state._slots:
                live = state.get_or_create_slot("racy")
                live.append("user", "first", "msg msg-u", ts="2026-03-23T10:00:00")
                live.append("assistant", "reply", "msg msg-a", ts="2026-03-23T10:00:01")
                live.drain()
            return real_chained(key)

        monkeypatch.setattr(log, "read_messages_chained", _publish_then_read)

        restored = await restore_recent_sessions_async(state, window_minutes=60)

        assert restored == 0, "restored a session that was already live"
        assert [m["content"] for m in state._slots["racy"].messages] == [
            "first",
            "reply",
        ], "the transcript was replayed onto a live slot — duplicated history"

    @pytest.mark.asyncio
    async def test_a_tab_closed_during_the_read_is_not_resurrected(self, tmp_path, monkeypatch):
        """✕ clicked mid-read must win over the metadata snapshot.

        The close pops the slot and records a tombstone synchronously, but
        persists the ``closed`` flag only after its own awaits — so the metadata
        read before the click still says open. Rebuilding from it would re-create
        a dismissed tab and then fire a nudge turn into it. Mirrors the guard
        ``rehydrate_slot_from_history_async`` applies after its own hop.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_dismissed",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Dismissed"},
        )
        (tmp_path / "dashboard_dismissed.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        real_chained = log.read_messages_chained

        def _close_then_read(key):
            channel_slots.note_slot_closed(state, "dismissed")
            return real_chained(key)

        monkeypatch.setattr(log, "read_messages_chained", _close_then_read)

        assert await restore_recent_sessions_async(state, window_minutes=60) == 0
        assert "dismissed" not in state._slots, "resurrected a tab the user closed"

    @pytest.mark.asyncio
    async def test_an_older_tombstone_does_not_block_a_reopened_tab(self, tmp_path, monkeypatch):
        """Only a close DURING the read is the race; older tombstones are inert.

        Without the ``>= started`` comparison the guard would make a reopened tab
        un-restorable for the tombstone's whole lifetime.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_reopened",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Reopened"},
        )
        (tmp_path / "dashboard_reopened.jsonl").touch()

        state = _make_state(tmp_path)
        channel_slots.note_slot_closed(state, "reopened")  # then reopened
        time.sleep(0.01)

        assert await restore_recent_sessions_async(state, window_minutes=60) == 1
        assert "reopened" in state._slots

    @pytest.mark.asyncio
    async def test_a_session_deleted_during_the_read_is_not_restored(self, tmp_path, monkeypatch):
        """Third post-hop window: permanent deletion during the offloaded read.

        ``delete_session`` leaves no tombstone, so a slot published from content
        already in hand rewrites the deleted file on its next flush. The
        dashboard's listener is bound before startup restore runs, so a user
        delete really can land here. Raised as blocking by GPT 5.6 review.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_doomed",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Doomed", "created_at": "2026-03-23T10:00:00"},
        )
        (tmp_path / "dashboard_doomed.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        real_chained = log.read_messages_chained

        def _delete_then_read(key):
            msgs = real_chained(key)
            log.delete_session(key)
            return msgs

        monkeypatch.setattr(log, "read_messages_chained", _delete_then_read)

        assert await restore_recent_sessions_async(state, window_minutes=60) == 0
        assert "doomed" not in state._slots, (
            "restored a slot for a permanently deleted session; its flush would "
            "rewrite the deleted transcript"
        )
        assert not (
            tmp_path / "dashboard_doomed.jsonl"
        ).exists(), "the deleted transcript came back"

    @pytest.mark.asyncio
    async def test_an_intact_session_is_not_refused_by_the_deletion_guard(
        self, tmp_path, monkeypatch
    ):
        """Negative control: the guard must not refuse an untouched session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_intact",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Intact", "created_at": "2026-03-23T10:00:00"},
        )
        (tmp_path / "dashboard_intact.jsonl").touch()

        state = _make_state(tmp_path)
        assert await restore_recent_sessions_async(state, window_minutes=60) == 1
        assert "intact" in state._slots

    @pytest.mark.asyncio
    async def test_a_session_deleted_before_its_metadata_read_builds_no_phantom(
        self, tmp_path, monkeypatch
    ):
        """An empty metadata line must skip, not build a phantom slot.

        ``list_sessions()`` is a snapshot taken one thread hop earlier, so a
        session deleted in that gap still appears in the list while its metadata
        reads back ``{}``. An empty dict sails past every filter (folder, pin,
        closed, cutoff all read falsy) and used to reach ``get_or_create_slot`` —
        registering a phantom slot whose flush RECREATES the deleted transcript.
        Raised as blocking by GPT 5.6 review, round 3.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import restore_recent_sessions_async

        _write_session(
            tmp_path,
            "dashboard_vanished",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Vanished", "created_at": "2026-03-23T10:00:00"},
        )
        _write_session(
            tmp_path,
            "dashboard_survivor",
            [{"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}],
            meta={"title": "Survivor", "created_at": "2026-03-23T10:00:00"},
        )
        for name in ("dashboard_vanished", "dashboard_survivor"):
            (tmp_path / f"{name}.jsonl").touch()

        state = _make_state(tmp_path)
        log = state.conversation_log
        real_list = log.list_sessions

        def _list_then_delete():
            listed = real_list()
            # The delete lands after the snapshot, before the per-session read.
            log.delete_session("dashboard:vanished")
            return listed

        monkeypatch.setattr(log, "list_sessions", _list_then_delete)

        restored = await restore_recent_sessions_async(state, window_minutes=60)

        assert restored == 1
        assert set(state._slots) == {"survivor"}, (
            "built a phantom slot for a deleted session; its flush would "
            f"recreate the transcript (slots: {sorted(state._slots)})"
        )
        assert not (
            tmp_path / "dashboard_vanished.jsonl"
        ).exists(), "the deleted transcript came back"
