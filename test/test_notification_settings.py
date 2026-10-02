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
from kiro_crew.notifications.bus import MONITOR_CHANNEL
from kiro_crew.notifications.settings import (
    PROTECTED_CHANNELS,
    ChannelSettings,
    ChannelSettingsError,
    parse_imported_settings,
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
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
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
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
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
            {
                "ts": "t1",
                "kind": "heartbeat",
                "channel": "system.heartbeat",
                "priority": "default",
                "title": "hb",
                "body": "b",
            }
        )
        state._deliver_note(
            {
                "ts": "t2",
                "kind": "cron",
                "channel": "system.cron",
                "priority": "default",
                "title": "job",
                "body": "b",
            }
        )
        assert state._unread_count == 1  # only the cron note counts
        hb = state._notification_log[0]
        assert hb["silenced"] is True and hb["priority"] == "passive"
        # Still in history (mute silences, it does not destroy)
        assert len(state._notification_log) == 2

    def test_passive_priority_never_counts_toward_badge(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._deliver_note(
            {
                "ts": "t1",
                "kind": "subagent",
                "channel": "system.subagent",
                "priority": "passive",
                "title": "s",
                "body": "b",
            }
        )
        assert state._unread_count == 0

    def test_approval_still_interrupts(self, monkeypatch, tmp_path):
        """system.approval cannot be silenced even with a rogue settings row
        on disk (protected at apply time too)."""
        state = _make_state(monkeypatch, tmp_path)
        # Simulate a hand-edited settings file muting approval
        state.notification_channel_settings._settings["system.approval"] = {"muted": True}
        state._deliver_note(
            {
                "ts": "t1",
                "kind": "approval",
                "channel": "system.approval",
                "priority": "critical",
                "title": "a",
                "body": "b",
            }
        )
        note = state._notification_log[0]
        assert "silenced" not in note
        assert note["priority"] == "critical"
        assert state._unread_count == 1


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/notifications/channels", api_notification_channels)
    app.router.add_put("/api/notifications/channels/settings", api_notification_channel_settings)
    return app


class TestChannelSettingsApi:
    @pytest.mark.asyncio
    async def test_list_channels_includes_settings_and_protection(self, monkeypatch, tmp_path):
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
        assert state.notification_channel_settings.get("system.heartbeat") == {"muted": True}
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
            r1 = await client.put("/api/notifications/channels/settings", json={"muted": True})
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
        data = json.loads((tmp_path / "notification_settings.json").read_text(encoding="utf-8"))
        assert data == {
            "channel_settings": {
                "a.b": {"muted": True},
                MONITOR_CHANNEL: {},
            }
        }


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


def _write_settings(tmp_path, channel_settings) -> None:
    data = {"channel_settings": channel_settings}
    (tmp_path / "notification_settings.json").write_text(json.dumps(data), encoding="utf-8")


def _read_settings(tmp_path) -> dict:
    return json.loads((tmp_path / "notification_settings.json").read_text(encoding="utf-8"))


class TestSeedMonitorFromAgent:
    """One-time seed of system.monitor from a stored system.agent entry."""

    @pytest.fixture(autouse=True)
    def _config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)

    def test_agent_mute_seeds_monitor_without_writing_at_load(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"muted": True}})
        before = _read_settings(tmp_path)
        settings = ChannelSettings()
        assert settings.get(MONITOR_CHANNEL) == {"muted": True}
        # The load never writes; a second load derives the same seed.
        assert _read_settings(tmp_path) == before
        assert ChannelSettings().get(MONITOR_CHANNEL) == {"muted": True}

    def test_seed_persists_with_the_next_update(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"muted": True}})
        ChannelSettings().update("a.b", muted=True)
        data = _read_settings(tmp_path)
        assert data == {
            "channel_settings": {
                "system.agent": {"muted": True},
                MONITOR_CHANNEL: {"muted": True},
                "a.b": {"muted": True},
            }
        }
        assert "monitor_seeded_from_agent" not in data

    def test_unmuting_agent_after_seed_keeps_monitor_muted(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"muted": True}})
        ChannelSettings().update("system.agent", muted=False)
        reloaded = ChannelSettings()
        assert reloaded.get("system.agent") == {}
        assert reloaded.get(MONITOR_CHANNEL) == {"muted": True}

    def test_agent_priority_override_is_copied(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"priority": "passive"}})
        settings = ChannelSettings()
        assert settings.get(MONITOR_CHANNEL) == {"priority": "passive"}
        assert settings.get("system.agent") == {"priority": "passive"}

    def test_unmuting_monitor_persists_empty_entry_and_hides_it(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"muted": True}})
        settings = ChannelSettings()
        settings.update(MONITOR_CHANNEL, muted=False)
        assert _read_settings(tmp_path)["channel_settings"][MONITOR_CHANNEL] == {}

        reloaded = ChannelSettings()
        assert reloaded.get(MONITOR_CHANNEL) == {}
        assert MONITOR_CHANNEL not in reloaded.all_settings()
        assert reloaded.get("system.agent") == {"muted": True}

    def test_unmuted_monitor_survives_downgrade_rewrite(self, tmp_path):
        _write_settings(tmp_path, {"system.agent": {"muted": True}})
        ChannelSettings().update(MONITOR_CHANNEL, muted=False)

        # A build that preserves only channel_settings keeps the empty sentinel.
        downgraded = {"channel_settings": _read_settings(tmp_path)["channel_settings"]}
        (tmp_path / "notification_settings.json").write_text(
            json.dumps(downgraded), encoding="utf-8"
        )

        reloaded = ChannelSettings()
        assert reloaded.get(MONITOR_CHANNEL) == {}
        assert MONITOR_CHANNEL not in reloaded.all_settings()
        assert reloaded.get("system.agent") == {"muted": True}

    def test_fresh_install_agent_mute_does_not_seed_monitor(self, tmp_path):
        settings = ChannelSettings()
        assert settings.all_settings() == {}
        assert not (tmp_path / "notification_settings.json").exists()

        settings.update("system.agent", muted=True)
        data = _read_settings(tmp_path)
        assert data["channel_settings"] == {
            "system.agent": {"muted": True},
            MONITOR_CHANNEL: {},
        }

        reloaded = ChannelSettings()
        assert reloaded.get(MONITOR_CHANNEL) == {}
        assert MONITOR_CHANNEL not in reloaded.all_settings()
        assert reloaded.get("system.agent") == {"muted": True}

    def test_existing_monitor_entry_is_untouched(self, tmp_path):
        _write_settings(
            tmp_path,
            {
                "system.agent": {"muted": True},
                MONITOR_CHANNEL: {"priority": "critical"},
            },
        )
        before = _read_settings(tmp_path)
        settings = ChannelSettings()
        assert settings.get(MONITOR_CHANNEL) == {"priority": "critical"}
        assert _read_settings(tmp_path) == before

    def test_empty_monitor_entry_blocks_seed(self, tmp_path):
        _write_settings(
            tmp_path,
            {"system.agent": {"muted": True}, MONITOR_CHANNEL: {}},
        )
        settings = ChannelSettings()
        assert settings.get(MONITOR_CHANNEL) == {}
        assert MONITOR_CHANNEL not in settings.all_settings()

    def test_corrupt_file_still_falls_back_and_seeds_nothing(self, tmp_path):
        path = tmp_path / "notification_settings.json"
        path.write_text("{not json", encoding="utf-8")
        settings = ChannelSettings()
        assert settings.all_settings() == {}
        assert path.read_text(encoding="utf-8") == "{not json"


class TestImportedSettings:
    """`parse_imported_settings` + `install_imported` + `reload`: the settings import."""

    def test_the_live_writer_rules_apply_to_an_archive(self):
        channels, dropped = parse_imported_settings(
            json.dumps(
                {
                    "channel_settings": {
                        "system.heartbeat": {"muted": True, "priority": "loud"},
                        "system.approval": {"muted": True, "priority": "passive"},
                        "app.x": "not-an-entry",
                        "app.y": {"priority": "critical", "colour": "red"},
                    }
                }
            )
        )
        assert channels["system.heartbeat"] == {"muted": True}
        # Both of system.approval's fields were refused, so it gets no row at all:
        # an empty entry would still land on the install through replace mode.
        assert "system.approval" not in channels
        assert channels["app.y"] == {"priority": "critical"}
        assert "app.x" not in channels
        assert dropped == 5

    @pytest.mark.parametrize("text", ["{bad", "[]", '{"channel_settings": []}'])
    def test_a_document_of_the_wrong_shape_is_refused(self, text):
        with pytest.raises(ChannelSettingsError):
            parse_imported_settings(text)

    def test_an_all_refused_channel_gets_no_row_at_all(self):
        """A channel keeping nothing must not occupy a row.

        Kept as ``{}`` it would still act on the install: replace writes it out,
        and a planted empty ``system.monitor`` marks the one-time seed complete.
        A legitimately empty entry (``{}``, or only ``muted: false``) still rides.
        """
        channels, dropped = parse_imported_settings(
            json.dumps(
                {
                    "channel_settings": {
                        "system.monitor": {"colour": "red"},
                        "app.unmuted": {"muted": False},
                        "app.empty": {},
                    }
                }
            )
        )
        assert "system.monitor" not in channels
        assert channels["app.unmuted"] == {}
        assert channels["app.empty"] == {}
        assert dropped == 1

    def test_channels_past_the_cap_are_dropped_and_counted(self):
        from kiro_crew.notifications.settings import _MAX_IMPORT_CHANNELS

        extra = 7
        raw = {f"app.c{i:04d}": {"muted": True} for i in range(_MAX_IMPORT_CHANNELS + extra)}
        channels, dropped = parse_imported_settings(json.dumps({"channel_settings": raw}))
        assert len(channels) == _MAX_IMPORT_CHANNELS
        assert dropped == extra
        # The admission gate ran before any field was retained: a refused channel
        # has no row, not an empty one.
        assert all(entry == {"muted": True} for entry in channels.values())

    def test_an_archive_is_installed_only_where_no_file_exists(self, settings, tmp_path):
        assert settings.install_imported({"app.new": {"muted": True}}) is True
        assert settings.get("app.new") == {"muted": True}
        assert ChannelSettings().get("app.new") == {"muted": True}  # persisted

    def test_an_existing_file_is_kept_whole(self, settings, tmp_path):
        settings.update("system.heartbeat", muted=True)
        before = (tmp_path / "notification_settings.json").read_bytes()
        assert settings.install_imported({"app.new": {"muted": True}}) is False
        assert (tmp_path / "notification_settings.json").read_bytes() == before
        assert settings.get("app.new") == {}

    def test_a_file_swapped_underneath_is_picked_up(self, settings, tmp_path):
        settings.update("system.heartbeat", muted=True)
        with settings.replacing_file():
            (tmp_path / "notification_settings.json").write_text(
                json.dumps({"channel_settings": {"app.z": {"muted": True}}}), encoding="utf-8"
            )
        assert settings.get("system.heartbeat") == {}
        assert settings.get("app.z") == {"muted": True}

    def test_an_update_cannot_land_between_the_swap_and_the_reread(self, settings, tmp_path):
        """The reported loss: an update during the swap wrote its pre-import mapping
        over the restored file, and the re-read then picked that up."""
        import threading

        settings.update("system.heartbeat", muted=True)
        landed = threading.Event()

        def _update() -> None:
            settings.update("app.other", muted=True)
            landed.set()

        with settings.replacing_file():
            worker = threading.Thread(target=_update)
            worker.start()
            assert not landed.wait(0.3)  # held off by the writer lock
            (tmp_path / "notification_settings.json").write_text(
                json.dumps({"channel_settings": {"app.z": {"muted": True}}}), encoding="utf-8"
            )
        worker.join(5)
        assert landed.is_set()
        stored = json.loads((tmp_path / "notification_settings.json").read_text(encoding="utf-8"))
        # The restored mute survived; the update was applied ON TOP of it.
        assert stored["channel_settings"]["app.z"] == {"muted": True}
        assert stored["channel_settings"]["app.other"] == {"muted": True}
        assert "system.heartbeat" not in stored["channel_settings"]

    def test_a_failed_reread_does_not_empty_the_restored_mapping(
        self, settings, tmp_path, monkeypatch
    ):
        """A transient read error after the swap read as {} and the next update wrote
        that empty baseline over the restored mutes."""
        from kiro_crew.notifications import settings as mod

        settings.update("system.heartbeat", muted=True)
        restored = {"app.z": {"muted": True}}
        with settings.replacing_file(restored):
            (tmp_path / "notification_settings.json").write_text(
                json.dumps({"channel_settings": restored}), encoding="utf-8"
            )
            monkeypatch.setattr(mod, "_read_stored", lambda: {})
        assert settings.get("app.z") == {"muted": True}
        settings.update("app.other", muted=True)
        stored = json.loads((tmp_path / "notification_settings.json").read_text(encoding="utf-8"))
        assert stored["channel_settings"]["app.z"] == {"muted": True}

    def test_the_store_rereads_even_when_the_swap_fails(self, settings, tmp_path):
        settings.update("system.heartbeat", muted=True)
        with pytest.raises(RuntimeError):
            with settings.replacing_file():
                (tmp_path / "notification_settings.json").write_text(
                    json.dumps({"channel_settings": {}}), encoding="utf-8"
                )
                raise RuntimeError("swap failed")
        assert settings.get("system.heartbeat") == {}

    def test_an_unreadable_file_after_a_refused_swap_keeps_the_loaded_mapping(
        self, settings, tmp_path, monkeypatch
    ):
        """A refused swap left the file alone; if the re-read then fails, falling back
        to {} would drop every mute and the next update would persist that loss."""
        from pathlib import Path

        settings.update("system.heartbeat", muted=True)
        real_read_text = Path.read_text

        def _eio(self, *a, **k):
            if self.name == "notification_settings.json":
                raise OSError(5, "Input/output error")
            return real_read_text(self, *a, **k)

        with pytest.raises(RuntimeError), monkeypatch.context() as mp:
            with settings.replacing_file({"app.z": {"muted": True}}):
                mp.setattr(Path, "read_text", _eio)
                raise RuntimeError("replace refused: stores in use")

        assert settings.get("system.heartbeat") == {"muted": True}
        settings.update("app.other", muted=True)
        stored = json.loads((tmp_path / "notification_settings.json").read_text(encoding="utf-8"))
        assert stored["channel_settings"]["system.heartbeat"] == {"muted": True}
