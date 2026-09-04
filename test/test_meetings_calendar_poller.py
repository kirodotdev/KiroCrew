"""The calendar poller: background sync plus pre-creating the meeting that is about to start.

Two properties are behaviour rather than plumbing and are what the file exists to
pin:

1. Pre-creation is exactly ``init`` — a meeting directory with ``idle`` metadata,
   a tasks file and seeded outputs — and NEVER a start. A poller that activated a
   session would open the microphone binding in the dashboard with nobody in the
   room.
2. The loop is unkillable by one bad tick and silent when there is nothing to do:
   provider ``none`` and a disabled app cost no fetch and no log line per tick.

Every test drives ``poll_once`` directly, or the loop with the sleeps patched to
zero; nothing waits on wall-clock time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    client_for,
    enabled_fixture,
    make_app,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import calendar_poller as poller
from kiro_crew.apps.builtins.meetings.backend import calendar_sync
from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.routes import _common

BASE = k.API_BASE


def _stamp(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y%m%dT%H%M%SZ")


def _ics(*events: tuple[str, str, str, str | None]) -> str:
    """A calendar of ``(uid, summary, dtstart, dtend-or-None)`` rows."""
    body = ""
    for uid, summary, start, end in events:
        body += f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{summary}\nDTSTART:{start}\n"
        if end is not None:
            body += f"DTEND:{end}\n"
        body += "END:VEVENT\n"
    return f"BEGIN:VCALENDAR\n{body}END:VCALENDAR\n"


def _configure_ics(root: Path, ics: Path, **calendar_overrides: object) -> None:
    config = store.read_config(root)
    config["calendar"] = {
        **config["calendar"],
        "provider": k.CALENDAR_PROVIDER_ICS,
        "source": str(ics),
        **calendar_overrides,
    }
    store.write_config(config, root)


@pytest.fixture(name="calendar")
def calendar_fixture(root: Path, tmp_path: Path) -> Path:
    """An ``.ics`` with one meeting starting in five minutes and one in three hours."""
    ics = tmp_path / "cal.ics"
    ics.write_text(
        _ics(
            ("soon", "Design Review", _stamp(timedelta(minutes=5)), _stamp(timedelta(minutes=35))),
            ("later", "Retro", _stamp(timedelta(hours=3)), _stamp(timedelta(hours=4))),
        )
    )
    _configure_ics(root, ics)
    return ics


@pytest.fixture(autouse=True)
def _no_live_loop():
    """No test may leave the module-level task running into the next one."""
    yield
    poller._poll_task = None


def _meeting_dirs(root: Path) -> set[str]:
    return {p.name for p in store.meetings_root(root).iterdir() if p.is_dir()}


# ── due_events: the pure selection rule ─────────────────────────────────────


class TestDueEvents:
    NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def _event(self, start: timedelta, end: timedelta | None = None, **extra) -> dict:
        row = {
            "event_id": "e",
            "title": "t",
            "start": (self.NOW + start).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if end is not None:
            row["end"] = (self.NOW + end).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {**row, **extra}

    def _due(self, *events: dict, lead: int = 15) -> list[dict]:
        return poller.due_events(list(events), now=self.NOW, lead_minutes=lead)

    def test_an_event_inside_the_lead_window_is_due(self):
        assert self._due(self._event(timedelta(minutes=10), timedelta(minutes=40))) != []

    def test_an_event_past_the_lead_window_is_not_due(self):
        assert self._due(self._event(timedelta(minutes=16), timedelta(minutes=40))) == []

    def test_the_window_edge_is_inclusive(self):
        assert self._due(self._event(timedelta(minutes=15), timedelta(minutes=40))) != []

    def test_a_meeting_already_under_way_is_due(self):
        """A late poll must still prepare the meeting that has started."""
        assert self._due(self._event(timedelta(minutes=-10), timedelta(minutes=20))) != []

    def test_an_ended_meeting_is_not_due(self):
        assert self._due(self._event(timedelta(hours=-2), timedelta(hours=-1))) == []

    def test_an_endless_event_long_past_its_start_is_not_due(self):
        """No DTEND: the `.ics` default duration is an hour, so past that it is over."""
        assert self._due(self._event(timedelta(minutes=-61))) == []
        assert self._due(self._event(timedelta(minutes=-59))) != []

    def test_an_all_day_event_is_never_due(self):
        """Its start is a date anchor, not an instant a meeting begins at."""
        assert self._due(self._event(timedelta(0), timedelta(days=1), all_day=True)) == []

    def test_unreadable_rows_are_skipped_not_guessed(self):
        assert self._due({"event_id": "x", "start": "not a time"}, "junk", {"event_id": "y"}) == []

    def test_lead_zero_means_only_meetings_that_have_started(self):
        assert self._due(self._event(timedelta(minutes=1), timedelta(minutes=30)), lead=0) == []
        assert self._due(self._event(timedelta(minutes=-1), timedelta(minutes=30)), lead=0) != []


# ── poll_once: one tick against a real .ics ─────────────────────────────────


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_a_tick_syncs_the_cache_and_pre_creates_the_imminent_meeting(
        self, root: Path, enabled, calendar: Path
    ):
        app = make_app(root)
        delay = await poller.poll_once(app)

        assert delay == k.CALENDAR_POLL_INTERVAL_SECS
        cached = store.read_calendar_cache(root)
        assert sorted(e["title"] for e in cached) == ["Design Review", "Retro"]

        created = _meeting_dirs(root)
        assert len(created) == 1
        (meeting_id,) = created
        assert meeting_id.startswith("soon-")
        meta = store.read_meeting_meta(meeting_id, root)
        assert meta is not None
        assert meta["title"] == "Design Review"
        # Pre-created, NOT started: the dashboard's transcription binding keys
        # off `active`, so a poller that started a meeting would open the mic.
        assert meta["status"] == k.STATUS_IDLE
        assert store.tasks_path(meeting_id, root).is_file()

    @pytest.mark.asyncio
    async def test_the_pre_created_meeting_matches_what_the_dashboard_would_init(
        self, root: Path, enabled, calendar: Path
    ):
        """Same id, same folder: opening the row later finds THIS meeting, not a twin."""
        app = make_app(root)
        await poller.poll_once(app)
        (meeting_id,) = _meeting_dirs(root)
        event_id = next(
            e["event_id"] for e in store.read_calendar_cache(root) if e["title"] == "Design Review"
        )
        assert meeting_id == store.safe_meeting_id(event_id)

        async with client_for(app) as client:
            resp = await client.post(
                f"{BASE}/meetings/{event_id}/init", json={"title": "Design Review"}
            )
            assert resp.status == 200
        assert _meeting_dirs(root) == {meeting_id}

    @pytest.mark.asyncio
    async def test_a_second_tick_creates_nothing_new_and_audits_nothing_new(
        self, root: Path, enabled, calendar: Path, monkeypatch: pytest.MonkeyPatch
    ):
        audits: list[tuple] = []
        monkeypatch.setattr(_common, "audit", lambda op, res, **kw: audits.append((op, res, kw)))
        app = make_app(root)
        await poller.poll_once(app)
        first = [a for a in audits if a[0] == "meetings.calendar_precreate"]
        assert len(first) == 1 and first[0][2] == {"outcome": "ok"}

        await poller.poll_once(app)
        assert len([a for a in audits if a[0] == "meetings.calendar_precreate"]) == 1
        assert len(_meeting_dirs(root)) == 1

    @pytest.mark.asyncio
    async def test_an_existing_meeting_is_left_untouched(self, root: Path, enabled, calendar: Path):
        """The user renamed it; a sync must not overwrite their metadata."""
        event_id = next(
            e.event_id for e in cal.parse_ics(calendar.read_text()) if e.title == "Design Review"
        )
        meeting_id = store.safe_meeting_id(event_id)
        store.meeting_dir(meeting_id, root).mkdir(parents=True)
        store.write_meeting_meta(
            meeting_id, store.new_meeting_meta(meeting_id, "My own title"), root
        )

        await poller.poll_once(make_app(root))
        meta = store.read_meeting_meta(meeting_id, root)
        assert meta is not None and meta["title"] == "My own title"

    @pytest.mark.asyncio
    async def test_lead_zero_syncs_but_pre_creates_nothing(
        self, root: Path, enabled, calendar: Path
    ):
        _configure_ics(root, calendar, precreate_lead_minutes=0)
        await poller.poll_once(make_app(root))
        assert store.read_calendar_cache(root) != []
        assert _meeting_dirs(root) == set()

    @pytest.mark.asyncio
    async def test_provider_none_costs_no_fetch_and_no_cache_write(self, root: Path, enabled):
        """The default install: five-minute CalendarErrors would be a log flood about nothing."""
        app = make_app(root)
        delay = await poller.poll_once(app)
        assert delay == k.CALENDAR_POLL_INTERVAL_SECS
        assert not store.calendar_cache_path(root).exists()

    @pytest.mark.asyncio
    async def test_auto_sync_off_skips_the_tick(self, root: Path, enabled, calendar: Path):
        _configure_ics(root, calendar, auto_sync=False)
        await poller.poll_once(make_app(root))
        assert not store.calendar_cache_path(root).exists()
        assert _meeting_dirs(root) == set()

    @pytest.mark.asyncio
    async def test_a_disabled_app_skips_the_tick(
        self, root: Path, calendar: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(_common, "is_app_enabled", lambda _name: False)
        await poller.poll_once(make_app(root))
        assert not store.calendar_cache_path(root).exists()

    @pytest.mark.asyncio
    async def test_a_provider_failure_is_logged_and_the_tick_still_returns_a_delay(
        self, root: Path, enabled, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        _configure_ics(root, tmp_path / "missing.ics")
        with caplog.at_level(logging.INFO, logger="kirocrew.app.meetings"):
            delay = await poller.poll_once(make_app(root))
        assert delay == k.CALENDAR_POLL_INTERVAL_SECS
        assert any("background calendar sync failed" in r.getMessage() for r in caplog.records)
        assert _meeting_dirs(root) == set()

    @pytest.mark.asyncio
    async def test_the_configured_cadence_is_returned_and_clamped(
        self, root: Path, enabled, calendar: Path
    ):
        _configure_ics(root, calendar, poll_interval_secs=10)
        assert await poller.poll_once(make_app(root)) == k.CALENDAR_POLL_MIN_SECS
        _configure_ics(root, calendar, poll_interval_secs=900)
        assert await poller.poll_once(make_app(root)) == 900

    @pytest.mark.asyncio
    async def test_a_corrupt_cache_id_is_skipped(
        self, root: Path, enabled, monkeypatch: pytest.MonkeyPatch
    ):
        """The provider always emits a safe id; a bad row is corruption, not a path."""
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        created = await poller.precreate_due_meetings(
            [{"event_id": "../escape", "title": "x", "start": soon}], lead_minutes=15, root=root
        )
        assert created == []
        assert _meeting_dirs(root) == set()


# ── the loop and its hooks ──────────────────────────────────────────────────


class TestLoopAndHooks:
    def test_register_routes_installs_the_hooks_in_teardown_order(self, root: Path):
        from kiro_crew.apps.builtins.meetings.backend import routes as routes_pkg

        app = make_app(root)
        assert poller.start_poller in app.on_startup
        # The poller stops BEFORE the live session is torn down, so no tick can
        # pre-create a meeting mid-shutdown.
        assert app.on_cleanup.index(poller.stop_poller) < app.on_cleanup.index(
            routes_pkg._on_cleanup
        )

    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_stop_cancels(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(k, "CALENDAR_POLL_STARTUP_DELAY_SECS", 3600)
        app = make_app(root)
        await poller.start_poller(app)
        first = poller._poll_task
        await poller.start_poller(app)
        assert poller._poll_task is first
        assert poller.is_running()

        await poller.stop_poller(app)
        assert not poller.is_running()
        assert first.cancelled()
        # Stopping again is a no-op, not an error.
        await poller.stop_poller(app)

    @pytest.mark.asyncio
    async def test_the_loop_ticks_and_uses_each_ticks_delay(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(k, "CALENDAR_POLL_STARTUP_DELAY_SECS", 0)
        ticks = 0
        done = asyncio.Event()

        async def fake_tick(_app) -> int:
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                done.set()
            return 0

        monkeypatch.setattr(poller, "poll_once", fake_tick)
        app = make_app(root)
        await poller.start_poller(app)
        await asyncio.wait_for(done.wait(), timeout=5)
        await poller.stop_poller(app)
        assert ticks >= 3

    @pytest.mark.asyncio
    async def test_one_bad_tick_does_not_end_the_loop(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setattr(k, "CALENDAR_POLL_STARTUP_DELAY_SECS", 0)
        monkeypatch.setattr(k, "CALENDAR_POLL_INTERVAL_SECS", 0)
        calls = 0
        recovered = asyncio.Event()

        async def flaky_tick(_app) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            recovered.set()
            return 3600

        monkeypatch.setattr(poller, "poll_once", flaky_tick)
        app = make_app(root)
        with caplog.at_level(logging.WARNING, logger="kirocrew.app.meetings"):
            await poller.start_poller(app)
            await asyncio.wait_for(recovered.wait(), timeout=5)
            await poller.stop_poller(app)
        assert calls == 2
        assert any("calendar poll tick failed" in r.getMessage() for r in caplog.records)


# ── the settings round trip and the shared sync ─────────────────────────────


class TestSettingsAndSync:
    def test_read_config_fills_the_poller_keys_for_an_older_calendar_block(self, root: Path):
        store.write_config(
            {**store.read_config(root), "calendar": {"provider": "none", "source": ""}}, root
        )
        calendar = store.read_config(root)["calendar"]
        assert calendar["auto_sync"] is True
        assert calendar["poll_interval_secs"] == k.CALENDAR_POLL_INTERVAL_SECS
        assert calendar["precreate_lead_minutes"] == k.CALENDAR_PRECREATE_LEAD_MINUTES

    def test_calendar_settings_defaults_and_clamps_hostile_values(self):
        settings = calendar_sync.calendar_settings(
            {
                "calendar": {
                    "auto_sync": "yes",
                    "poll_interval_secs": 1,
                    "precreate_lead_minutes": 10**9,
                }
            }
        )
        assert settings["auto_sync"] is False  # a string is not a consent
        assert settings["poll_interval_secs"] == k.CALENDAR_POLL_MIN_SECS
        assert settings["precreate_lead_minutes"] == k.CALENDAR_PRECREATE_LEAD_MAX_MINUTES
        assert calendar_sync.calendar_settings({})["provider"] == k.DEFAULT_CALENDAR_PROVIDER
        assert calendar_sync.calendar_settings({"calendar": "junk"})["auto_sync"] is True

    @pytest.mark.asyncio
    async def test_put_config_round_trips_and_bounds_the_poller_keys(self, root: Path, enabled):
        app = make_app(root)
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/config")
            config = (await resp.json())["config"]
            config["calendar"] = {
                **config["calendar"],
                "auto_sync": False,
                "poll_interval_secs": 5,
                "precreate_lead_minutes": 99999,
            }
            resp = await client.put(f"{BASE}/config", json={"config": config})
            assert resp.status == 200
            saved = (await resp.json())["config"]["calendar"]
        assert saved["auto_sync"] is False
        assert saved["poll_interval_secs"] == k.CALENDAR_POLL_MIN_SECS
        assert saved["precreate_lead_minutes"] == k.CALENDAR_PRECREATE_LEAD_MAX_MINUTES

    @pytest.mark.asyncio
    async def test_put_config_treats_a_non_boolean_auto_sync_as_the_default(
        self, root: Path, enabled
    ):
        app = make_app(root)
        async with client_for(app) as client:
            resp = await client.get(f"{BASE}/config")
            config = (await resp.json())["config"]
            config["calendar"] = {**config["calendar"], "auto_sync": "false"}
            resp = await client.put(f"{BASE}/config", json={"config": config})
            assert (await resp.json())["config"]["calendar"]["auto_sync"] is True

    @pytest.mark.asyncio
    async def test_the_sync_route_and_the_poller_share_one_sync(
        self, root: Path, enabled, calendar: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One implementation: a manual Sync and a scheduled one write the same cache."""
        seen: list[tuple] = []
        real = calendar_sync.sync_calendar

        async def spy(root_arg, **kw):
            seen.append(kw)
            return await real(root_arg, **kw)

        monkeypatch.setattr(calendar_sync, "sync_calendar", spy)
        app = make_app(root)
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/calendar/sync?days=3")
            assert resp.status == 200
        await poller.poll_once(app)
        assert seen == [{"days": 3}, {}]
