"""Tests for per-channel notification settings (RFC Phase 3)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import (
    api_notification_channel_settings,
    api_notification_channels,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.notifications.settings import (
    PROTECTED_CHANNELS,
    ChannelSettings,
    ChannelSettingsError,
)


@pytest.fixture()
def settings(monkeypatch, tmp_path) -> ChannelSettings:
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return ChannelSettings()


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestChannelSettingsStore:
    def test_mute_persists_and_reloads(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.notifications.settings.config_dir", lambda: tmp_path
        )
        s = ChannelSettings()
        s.update("system.heartbeat", muted=True)
        # Fresh instance reads back from disk
        s2 = ChannelSettings()
        assert s2.get("system.heartbeat") == {"muted": True}

    def test_unmute_removes_empty_entry(self, settings):
        settings.update("a.b", muted=True)
        settings.update("a.b", muted=False)
        assert settings.get("a.b") == {}
        assert settings.all_settings() == {}

    def test_priority_override_and_clear(self, settings):
        settings.update("a.b", priority="critical")
        assert settings.get("a.b") == {"priority": "critical"}
        settings.update("a.b", clear_priority=True)
        assert settings.get("a.b") == {}

    def test_invalid_priority_rejected(self, settings):
        with pytest.raises(ChannelSettingsError, match="priority"):
            settings.update("a.b", priority="urgent")

    def test_protected_channel_cannot_be_muted_or_lowered(self, settings):
        with pytest.raises(ChannelSettingsError, match="muted"):
            settings.update("system.approval", muted=True)
        with pytest.raises(ChannelSettingsError, match="lowered"):
            settings.update("system.approval", priority="passive")
        # Explicit critical is a no-op but allowed
        settings.update("system.approval", priority="critical")

    def test_corrupt_file_falls_back_to_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.notifications.settings.config_dir", lambda: tmp_path
        )
        (tmp_path / "notification_settings.json").write_text("{not json", encoding="utf-8")
        s = ChannelSettings()
        assert s.all_settings() == {}

    def test_apply_mute_forces_passive_and_silenced(self, settings):
        settings.update("system.heartbeat", muted=True)
        note = {"channel": "system.heartbeat", "priority": "default"}
        settings.apply(note)
        assert note["silenced"] is True
        assert note["priority"] == "passive"

    def test_apply_priority_override(self, settings):
        settings.update("app.chan", priority="critical")
        note = {"channel": "app.chan", "priority": "default"}
        settings.apply(note)
        assert note["priority"] == "critical"
        assert "silenced" not in note

    def test_apply_untouched_without_settings(self, settings):
        note = {"channel": "app.chan", "priority": "default"}
        settings.apply(note)
        assert note == {"channel": "app.chan", "priority": "default"}


class TestSinkIntegration:
    def test_muted_channel_excluded_from_badge(self, monkeypatch, tmp_path):
        """RFC exit criteria: muting system.heartbeat silences it everywhere
        while badge count excludes passive rows."""
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.heartbeat", muted=True)
        state._deliver_note(
            {"ts": "t1", "kind": "heartbeat", "channel": "system.heartbeat",
             "priority": "default", "title": "hb", "body": "b"}
        )
        state._deliver_note(
            {"ts": "t2", "kind": "cron", "channel": "system.cron",
             "priority": "default", "title": "job", "body": "b"}
        )
        assert state._unread_count == 1  # only the cron note counts
        hb = state._notification_log[0]
        assert hb["silenced"] is True and hb["priority"] == "passive"
        # Still in history (mute silences, it does not destroy)
        assert len(state._notification_log) == 2

    def test_passive_priority_never_counts_toward_badge(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._deliver_note(
            {"ts": "t1", "kind": "subagent", "channel": "system.subagent",
             "priority": "passive", "title": "s", "body": "b"}
        )
        assert state._unread_count == 0

    def test_approval_still_interrupts(self, monkeypatch, tmp_path):
        """system.approval cannot be silenced even with a rogue settings row
        on disk (protected at apply time too)."""
        state = _make_state(monkeypatch, tmp_path)
        # Simulate a hand-edited settings file muting approval
        state.notification_channel_settings._settings["system.approval"] = {"muted": True}
        state._deliver_note(
            {"ts": "t1", "kind": "approval", "channel": "system.approval",
             "priority": "critical", "title": "a", "body": "b"}
        )
        note = state._notification_log[0]
        assert "silenced" not in note
        assert note["priority"] == "critical"
        assert state._unread_count == 1


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/notifications/channels", api_notification_channels)
    app.router.add_put(
        "/api/notifications/channels/settings", api_notification_channel_settings
    )
    return app


class TestChannelSettingsApi:
    @pytest.mark.asyncio
    async def test_list_channels_includes_settings_and_protection(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_bus.register_channel("my-app.alerts", "default")
        state.notification_channel_settings.update("my-app.alerts", muted=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/notifications/channels")
            body = await resp.json()
        assert resp.status == 200
        by_name = {c["channel"]: c for c in body["channels"]}
        assert by_name["my-app.alerts"]["settings"] == {"muted": True}
        assert by_name["my-app.alerts"]["source"] == "my-app"
        assert by_name["system.approval"]["protected"] is True
        assert by_name["system.cron"]["default_priority"] == "default"

    @pytest.mark.asyncio
    async def test_stored_setting_for_unregistered_channel_still_listed(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("gone-app.chan", muted=True)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/notifications/channels")
            body = await resp.json()
        by_name = {c["channel"]: c for c in body["channels"]}
        assert by_name["gone-app.chan"]["registered"] is False

    @pytest.mark.asyncio
    async def test_put_mute_roundtrip(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.heartbeat", "muted": True},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["settings"] == {"muted": True}
        assert state.notification_channel_settings.get("system.heartbeat") == {
            "muted": True
        }
        state.broadcast_ws.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_priority_null_clears_override(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        state.notification_channel_settings.update("a.b", priority="critical")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "a.b", "priority": None},
            )
        assert resp.status == 200
        assert state.notification_channel_settings.get("a.b") == {}

    @pytest.mark.asyncio
    async def test_put_protected_channel_mute_rejected(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.approval", "muted": True},
            )
            body = await resp.json()
        assert resp.status == 400
        assert "muted" in body["error"]

    @pytest.mark.asyncio
    async def test_put_validation_errors(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            r1 = await client.put(
                "/api/notifications/channels/settings", json={"muted": True}
            )
            r2 = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "a.b", "priority": "urgent"},
            )
            r3 = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "a.b", "muted": "yes"},
            )
        assert r1.status == 400
        assert r2.status == 400
        assert r3.status == 400

    @pytest.mark.asyncio
    async def test_settings_file_written_atomically(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "a.b", "muted": True},
            )
        data = json.loads((tmp_path / "notification_settings.json").read_text())
        assert data == {"channel_settings": {"a.b": {"muted": True}}}


class TestProtectedConstant:
    def test_approval_is_protected(self):
        assert "system.approval" in PROTECTED_CHANNELS


class TestReviewRegressions:
    def test_apply_ignores_noncritical_override_on_protected_channel(self, settings):
        """Hand-edited {'system.approval': {'priority': 'passive'}} must NOT
        lower approval's priority -- the apply-time floor covers the
        priority branch, not just mute."""
        settings._settings["system.approval"] = {"priority": "passive"}
        note = {"channel": "system.approval", "priority": "critical"}
        settings.apply(note)
        assert note["priority"] == "critical"
        assert "silenced" not in note

    def test_apply_allows_critical_override_on_protected_channel(self, settings):
        settings._settings["system.approval"] = {"priority": "critical"}
        note = {"channel": "system.approval", "priority": "critical"}
        settings.apply(note)
        assert note["priority"] == "critical"

    def test_persist_failure_leaves_memory_unchanged(self, settings, monkeypatch):
        """A failed write must not leave the rejected setting active in
        memory: persist the candidate first, commit only on success."""
        settings.update("a.b", muted=True)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.notifications.settings.atomic_write", boom)
        with pytest.raises(OSError):
            settings.update("a.b", muted=False)
        # Memory still reflects the last successfully persisted state
        assert settings.get("a.b") == {"muted": True}

    @pytest.mark.asyncio
    async def test_put_oversized_channel_name_rejected(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "x" * 300, "muted": True},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_non_object_json_returns_400(self, monkeypatch, tmp_path):
        """Valid-but-non-object JSON must be a validation 400, not a 500."""
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            for payload in ("[]", "null", '"str"'):
                resp = await client.put(
                    "/api/notifications/channels/settings",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400, payload

    def test_readers_are_lock_free(self, settings):
        """apply()/get() must not block on the writer lock: with the lock
        held (simulating a worker-thread update mid-write), reads and
        apply still complete."""
        import kiro_crew.notifications.settings as mod

        settings.update("a.b", muted=True)
        with mod._lock:  # writer holds the lock across its file write
            assert settings.get("a.b") == {"muted": True}
            assert settings.all_settings() == {"a.b": {"muted": True}}
            note = {"channel": "a.b", "priority": "default"}
            settings.apply(note)
            assert note["silenced"] is True
