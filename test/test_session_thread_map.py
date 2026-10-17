"""Tests for SessionManager thread tracking (set_thread / get_thread).

Thread tracking is now backed by SessionMap (persisted to disk) via
set_slack_link / get_slack_link.  The backward-compat wrappers
set_thread / get_thread delegate to the new methods.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        return provider

    return factory


@pytest.fixture()
def config():
    return KiroCrewConfig()


class TestSetThread:
    """set_thread stores thread_ts via SessionMap."""

    @pytest.mark.asyncio
    async def test_stores_thread_for_existing_session(self, config) -> None:
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j1")
        await session_manager.set_thread("cron:j1", "1711957800.001234")
        assert session_manager.get_thread("cron:j1") == "1711957800.001234"
        await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_set_thread_without_session_creates_entry(self, config) -> None:
        """set_slack_link creates an entry even without an active session."""
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.set_thread("cron:ghost", "1711957800.999")
        assert session_manager.get_thread("cron:ghost") == "1711957800.999"
        await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_get_thread_returns_none_when_unset(self, config) -> None:
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j2")
        assert session_manager.get_thread("cron:j2") is None
        await session_manager.close_all()


class TestThreadCleanup:
    """Thread map entries persist across reset/remove (backed by SessionMap)."""

    @pytest.mark.asyncio
    async def test_reset_preserves_thread(self, config) -> None:
        """reset() kills the session but preserves the SessionMap entry."""
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j1")
        await session_manager.set_thread("cron:j1", "1711957800.001234")
        await session_manager.reset("cron:j1")
        # Thread link persists in SessionMap after reset
        assert session_manager.get_thread("cron:j1") == "1711957800.001234"
        await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_remove_preserves_thread(self, config) -> None:
        """remove() preserves the SessionMap entry for future resume."""
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j1")
        await session_manager.set_thread("cron:j1", "1711957800.001234")
        await session_manager.remove("cron:j1")
        assert session_manager.get_thread("cron:j1") == "1711957800.001234"
        await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_destroy_clears_thread(self, config) -> None:
        """destroy() deletes the SessionMap entry permanently."""
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j1")
        await session_manager.set_thread("cron:j1", "1711957800.001234")
        await session_manager.destroy("cron:j1")
        assert session_manager.get_thread("cron:j1") is None
        await session_manager.close_all()

    @pytest.mark.asyncio
    async def test_close_all_preserves_threads(self, config) -> None:
        """close_all() saves session mappings — thread links persist."""
        session_manager = SessionManager(config, provider_factory=_mock_provider_factory())
        await session_manager.get_or_create("cron:j1")
        await session_manager.set_thread("cron:j1", "1711957800.001234")
        await session_manager.close_all()
        # Thread link persists in SessionMap after close_all
        assert session_manager.get_thread("cron:j1") == "1711957800.001234"


class TestSlackLink:
    """Direct set_slack_link / get_slack_link / get_session_for_thread tests."""

    @pytest.mark.asyncio
    async def test_set_and_get_slack_link(self, config) -> None:
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        sm.set_slack_link("dashboard:chat-1", "1711957800.001", "C123")
        ts, ch = sm.get_slack_link("dashboard:chat-1")
        assert ts == "1711957800.001"
        assert ch == "C123"
        await sm.close_all()

    @pytest.mark.asyncio
    async def test_get_session_for_thread(self, config) -> None:
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        sm.set_slack_link("dashboard:chat-1", "1711957800.001", "C123")
        assert sm.get_session_for_thread("1711957800.001") == "dashboard:chat-1"
        assert sm.get_session_for_thread("nonexistent") is None
        await sm.close_all()


class TestSessionMapMigration:
    """Tests for SessionMap backward-compat migration from string to dict format."""

    @pytest.mark.asyncio
    async def test_load_migrates_string_values(self, config, tmp_path, monkeypatch) -> None:
        """Old session_map.json with plain string values gets migrated to dict format."""
        import json

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        map_path = tmp_path / "session_map.json"
        map_path.write_text(json.dumps({"thread-1": "sid-abc", "thread-2": "sid-def"}))
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        # After migration, get_slack_link should return (None, None) for thread fields
        ts, ch = sm.get_slack_link("thread-1")
        assert ts is None
        assert ch is None
        # But the file should be rewritten in dict format
        raw = json.loads(map_path.read_text(encoding="utf-8"))
        assert isinstance(raw["thread-1"], dict)
        assert raw["thread-1"]["sid"] == "sid-abc"
        await sm.close_all()

    @pytest.mark.asyncio
    async def test_load_skips_corrupt_entries(self, config, tmp_path, monkeypatch) -> None:
        import json

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        map_path = tmp_path / "session_map.json"
        map_path.write_text(json.dumps({"good": {"sid": "s1"}, "bad": [1, 2, 3]}))
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        # Good entry loaded, bad entry skipped
        ts, ch = sm.get_slack_link("good")
        assert ts is None  # no slack link set
        ts2, ch2 = sm.get_slack_link("bad")
        assert ts2 is None
        await sm.close_all()


class TestSetSlackLinkUpdate:
    """Tests for set_slack_link updating an existing entry (lines 572-576)."""

    @pytest.mark.asyncio
    async def test_update_existing_link(self, config, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        sm.set_slack_link("k1", "ts-old", "C-old")
        sm.set_slack_link("k1", "ts-new", "C-new")
        ts, ch = sm.get_slack_link("k1")
        assert ts == "ts-new"
        assert ch == "C-new"
        # Old thread should be removed from reverse index
        assert sm.get_session_for_thread("ts-old") is None
        assert sm.get_session_for_thread("ts-new") == "k1"
        await sm.close_all()

    @pytest.mark.asyncio
    async def test_idempotent_set_same_link(self, config, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        sm.set_slack_link("k1", "ts-1", "C-1")
        sm.set_slack_link("k1", "ts-1", "C-1")  # same values — no-op
        ts, ch = sm.get_slack_link("k1")
        assert ts == "ts-1"
        assert ch == "C-1"
        await sm.close_all()


class TestBackwardCompatWrappers:
    """Tests for set_channel/get_channel backward-compat wrappers (lines 1168-1175)."""

    @pytest.mark.asyncio
    async def test_set_channel_updates_existing_link(self, config, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        sm.set_slack_link("k1", "ts-1", "C-old")
        await sm.set_channel("k1", "C-new")
        assert sm.get_channel("k1") == "C-new"
        # Thread should be preserved
        assert sm.get_thread("k1") == "ts-1"
        await sm.close_all()

    @pytest.mark.asyncio
    async def test_set_channel_stores_without_thread(self, config, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        await sm.set_channel("k1", "C-1")
        # set_channel now stores unconditionally (mirrors set_thread behavior)
        assert sm.get_channel("k1") == "C-1"
        await sm.close_all()

    @pytest.mark.asyncio
    async def test_get_channel_returns_none_for_unknown(self, config, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        sm = SessionManager(config, provider_factory=_mock_provider_factory())
        assert sm.get_channel("nonexistent") is None
        await sm.close_all()
