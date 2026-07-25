"""Tests for session restore on startup and dashboard config API."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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
    return app


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
            meta={"title": "Test Chat", "agent": "kirocrew", "workspace": "myws", "mode": "orchestrator"},
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

    def test_skips_non_dashboard_sessions(self, tmp_path, monkeypatch):
        """Sessions not prefixed with 'dashboard' are skipped."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(
            tmp_path,
            "slack_thread123",
            [{"role": "user", "content": "slack msg", "ts": "2026-03-23T10:00:00"}],
        )
        path = tmp_path / "slack_thread123.jsonl"
        path.touch()

        state = _make_state(tmp_path)
        restored = restore_recent_sessions(state, window_minutes=60)
        assert restored == 0

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
        """LLM-sourced content is redacted before being added to dashboard slots."""
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
        # User content should be preserved as-is
        assert slot.messages[0]["content"] == "show me the key"
        # Assistant content should have the AWS key redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[1]["content"]
        assert "[REDACTED" in slot.messages[1]["content"]

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
