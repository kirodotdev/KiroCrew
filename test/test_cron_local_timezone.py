"""A cron job whose zone is ``local`` (or unset) follows the zone of the computer it runs on.

A laptop that travels switches its system zone; a job set for 09:00 should run at
09:00 wherever the laptop is. The host zone is published by ``kiro_crew.host_timezone``
(the conftest floor keeps it undetected, so each test publishes the zones it needs),
and every instant is built from a wall time with an explicit ``ZoneInfo``, so no test
depends on the zone of the machine running the suite. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import kiro_crew.cron as cron
from kiro_crew import host_timezone, platform_compat
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.testing.wait import await_until

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
NEW_YORK = ZoneInfo("America/New_York")

#: When the host was first seen in Shanghai, well before every run below.
_SEEN_IN_SHANGHAI = datetime(2026, 11, 1, 0, 0, tzinfo=SHANGHAI).timestamp()


def _at(tz: ZoneInfo, *parts: int) -> float:
    return datetime(*parts, tzinfo=tz).timestamp()


def _daily_nine(timezone: str = "", last_run_ts: float | None = None) -> CronJob:
    return CronJob(
        id="nine",
        name="morning brief",
        message="m",
        schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
        timezone=timezone,
        last_run_ts=last_run_ts,
    )


@pytest.fixture
def no_configured_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "published_config_timezone", lambda: "")


class TestTheZoneAJobResolvesTo:
    def test_an_unset_zone_with_no_configured_zone_is_the_hosts(self, no_configured_zone) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        assert cron.resolve_timezone_name("") == "Asia/Shanghai"
        assert cron.get_local_tz()[0] == "Asia/Shanghai"

    def test_local_is_the_hosts_zone_whatever_the_config_names(self, monkeypatch) -> None:
        monkeypatch.setattr(cron, "published_config_timezone", lambda: "Asia/Tokyo")
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        assert cron.resolve_timezone_name("local") == "Asia/Shanghai"
        assert cron.resolve_timezone_name("Local") == "Asia/Shanghai"
        assert cron.resolve_timezone_name("") == "Asia/Tokyo"

    def test_a_configured_local_default_is_the_hosts_zone(self, monkeypatch) -> None:
        monkeypatch.setattr(cron, "published_config_timezone", lambda: "local")
        host_timezone.publish_host_timezone("Europe/Paris", at=_SEEN_IN_SHANGHAI)
        assert cron.resolve_timezone_name("") == "Europe/Paris"
        assert cron.job_follows_host(_daily_nine(""))

    def test_an_iana_name_stands_for_itself(self, no_configured_zone) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        assert cron.resolve_timezone_name("America/New_York") == "America/New_York"
        assert not cron.job_follows_host(_daily_nine("America/New_York"))

    def test_an_undetected_host_zone_reads_as_utc(self, no_configured_zone) -> None:
        assert cron.resolve_timezone_name("local") == ""
        assert cron.get_local_tz()[0] == "UTC"
        assert getattr(cron._job_tz(_daily_nine("local")), "key", "UTC") == "UTC"

    def test_local_is_a_valid_zone_and_is_stored_in_one_spelling(self, tmp_path) -> None:
        assert cron.is_valid_timezone("local")
        assert cron.is_valid_timezone("LOCAL")
        # Not an alias: some Linux tzdata ships a real "localtime" key, so whether it
        # is a valid zone depends on the host; it is never read as "local".
        assert not cron.is_local_timezone("localtime")
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="n", message="m", cron_expr="0 9 * * *", timezone="Local")
        assert job.timezone == "local"
        updated = svc.update_job(job.id, timezone="LOCAL")
        assert updated is not None and updated.timezone == "local"

    def test_a_one_shot_wall_clock_in_local_is_read_in_the_hosts_zone(
        self, no_configured_zone, monkeypatch
    ) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        now = datetime(2026, 11, 3, 8, 0, tzinfo=SHANGHAI)
        monkeypatch.setattr(cron, "datetime", _FrozenDatetime.at(now))
        assert cron.parse_time_string("9am", "local") == _at(SHANGHAI, 2026, 11, 3, 9, 0)


class _FrozenDatetime(datetime):
    _now: datetime

    @classmethod
    def at(cls, now: datetime) -> type[_FrozenDatetime]:
        return type("_Frozen", (cls,), {"_now": now})

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls._now.astimezone(tz) if tz is not None else cls._now


class TestAJobThatFollowsTheHostMovesWithIt:
    def test_it_fires_at_nine_in_whichever_zone_the_host_is_in(self, no_configured_zone) -> None:
        job = _daily_nine("")
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        assert cron.is_due(job, _at(SHANGHAI, 2026, 11, 3, 9, 0) + 10)
        assert not cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 10)

        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(SHANGHAI, 2026, 11, 3, 12)
        )
        assert cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 4, 9, 0) + 10)
        assert not cron.is_due(job, _at(SHANGHAI, 2026, 11, 5, 9, 0) + 10)

    def test_flying_west_does_not_run_the_same_local_slot_twice(self, no_configured_zone) -> None:
        """09:00 in Shanghai ran; after landing in San Francisco the same date's 09:00
        comes round again, and must not run a second time. The next day's does."""
        ran_in_shanghai = _at(SHANGHAI, 2026, 11, 3, 9, 0) + 20
        job = _daily_nine("", last_run_ts=ran_in_shanghai)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(SHANGHAI, 2026, 11, 3, 14)
        )

        assert not cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 10)
        assert cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 4, 9, 0) + 10)
        assert cron.compute_next_run_ts(job, now=_at(LOS_ANGELES, 2026, 11, 3, 8, 0)) == _at(
            LOS_ANGELES, 2026, 11, 4, 9, 0
        )

    def test_a_frequent_job_keeps_running_after_flying_west(self, no_configured_zone) -> None:
        """Only the slot repeating the last run's local date and time is held back; an
        hourly job does not go quiet for the hours the clock went back."""
        ran = _at(SHANGHAI, 2026, 11, 3, 10, 0) + 20
        hourly = CronJob(
            id="hourly",
            name="hourly",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="0 * * * *"),
            last_run_ts=ran,
        )
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=ran + 3600)

        assert cron.is_due(hourly, _at(LOS_ANGELES, 2026, 11, 2, 20, 0) + 10)
        assert not cron.is_due(hourly, _at(LOS_ANGELES, 2026, 11, 3, 10, 0) + 10)
        assert cron.is_due(hourly, _at(LOS_ANGELES, 2026, 11, 3, 11, 0) + 10)

    def test_flying_east_does_not_catch_up_a_slot_the_clock_jumped_over(
        self, no_configured_zone
    ) -> None:
        ran_in_la = _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 20
        job = _daily_nine("", last_run_ts=ran_in_la)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=_SEEN_IN_SHANGHAI)
        landed = _at(SHANGHAI, 2026, 11, 4, 18, 0)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=landed)

        assert not cron.is_due(job, landed + 10)
        assert cron.compute_next_run_ts(job, now=landed + 10) == _at(SHANGHAI, 2026, 11, 5, 9, 0)

    def test_a_job_with_its_own_zone_ignores_the_host(self, no_configured_zone) -> None:
        job = _daily_nine("America/New_York", last_run_ts=_at(NEW_YORK, 2026, 11, 2, 9, 0) + 20)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(NEW_YORK, 2026, 11, 2, 12)
        )
        assert cron.is_due(job, _at(NEW_YORK, 2026, 11, 3, 9, 0) + 10)

    def test_a_last_run_from_before_this_process_saw_a_zone_is_not_compared(
        self, no_configured_zone
    ) -> None:
        """The history starts at the first detection; an older run has no known zone."""
        ran_before = _at(SHANGHAI, 2026, 11, 3, 9, 0) + 20
        job = _daily_nine("", last_run_ts=ran_before)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=ran_before + 3600)
        assert cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 10)


class TestTheHostZoneSnapshot:
    def test_a_failed_read_keeps_the_last_zone(self) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone("", at=_SEEN_IN_SHANGHAI + 60)
        assert host_timezone.host_timezone_at(_SEEN_IN_SHANGHAI + 120) == "Asia/Shanghai"

    def test_the_history_names_the_zone_in_effect_at_an_instant(self) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=100.0)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=150.0)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=200.0)
        assert host_timezone.host_timezone_at(99.0) == ""
        assert host_timezone.host_timezone_at(199.0) == "Asia/Shanghai"
        assert host_timezone.host_timezone_at(200.0) == "America/Los_Angeles"

    def test_the_history_is_bounded(self) -> None:
        zones = ["Asia/Shanghai", "America/Los_Angeles"]
        for i in range(host_timezone._HISTORY_MAX + 10):
            host_timezone.publish_host_timezone(zones[i % 2], at=float(i))
        state = host_timezone._STATE
        assert state is not None and len(state.history) == host_timezone._HISTORY_MAX

    @pytest.mark.asyncio
    async def test_a_reader_on_the_event_loop_refreshes_on_a_worker_thread(
        self, monkeypatch
    ) -> None:
        """No read of the host on the loop's thread, and no dependence on the cron
        scheduler running: an on-loop reader starts the refresh off the loop. The
        test waits on the loop for the refresh to finish, never in a pool thread
        the refresh itself may need."""
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        read_on: list[int] = []
        refreshed = asyncio.Event()
        real_refresh = host_timezone.refresh_host_timezone

        def _detect() -> str:
            read_on.append(threading.get_ident())
            return "Europe/Paris"

        def _refresh() -> str:
            try:
                return real_refresh()
            finally:
                loop.call_soon_threadsafe(refreshed.set)

        monkeypatch.setattr(host_timezone, "detect_host_timezone", _detect)
        monkeypatch.setattr(host_timezone, "refresh_host_timezone", _refresh)
        host_timezone.freeze_host_timezone(None)
        assert host_timezone.published_host_timezone() == ""
        await await_until(refreshed, what="the background host-zone refresh")
        assert read_on and read_on[0] != loop_thread
        assert host_timezone.published_host_timezone() == "Europe/Paris"

    @pytest.mark.parametrize(
        ("named", "level"), [("", logging.INFO), ("Not/A_Zone", logging.WARNING)]
    )
    def test_an_undetected_zone_is_logged_once(self, named, level, monkeypatch, caplog) -> None:
        """UTC is the normal default on a server or a container, so a host with no
        configured zone gets an info line; a zone name that does not load still warns."""
        monkeypatch.setattr(host_timezone, "detect_host_timezone", lambda: named)
        host_timezone.freeze_host_timezone(None)
        with caplog.at_level(logging.INFO, logger="kiro_crew.host_timezone"):
            host_timezone.refresh_host_timezone()
            host_timezone.refresh_host_timezone()
        hits = [r for r in caplog.records if "Could not detect" in r.getMessage()]
        assert [r.levelno for r in hits] == [level]

    def test_windows_reloads_tzlocal_so_a_running_gateway_sees_a_new_zone(
        self, monkeypatch
    ) -> None:
        calls: list[str] = []
        fake = types.SimpleNamespace(
            reload_localzone=lambda: calls.append("reload"),
            get_localzone_name=lambda: "Asia/Tokyo",
        )
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(host_timezone.Path, "is_symlink", lambda self: False)
        monkeypatch.setitem(sys.modules, "tzlocal", fake)
        assert host_timezone.detect_host_timezone() == "Asia/Tokyo"
        assert calls == ["reload"]


def _write_store(path, version, jobs) -> None:
    document = {"jobs": jobs} if version is None else {"version": version, "jobs": jobs}
    (path / "crons.json").write_text(json.dumps(document), encoding="utf-8")


def _record(job_id: str, schedule: dict, **extra) -> dict:
    return {"id": job_id, "name": job_id, "message": "m", "schedule": schedule, **extra}


_CRON = {"kind": "cron", "cron_expr": "0 9 * * *"}
_EVERY = {"kind": "every", "every_secs": 3600}


def _config_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.cron_service import zone_default_migration

    def _unknown() -> str:
        raise zone_default_migration.ConfigZoneUnknown("config.json did not parse")

    monkeypatch.setattr(zone_default_migration, "configured_default_zone", _unknown)


def _saved(path) -> dict:
    return json.loads((path / "crons.json").read_text(encoding="utf-8"))


class TestTheConfigZoneTheUpgradeReads:
    @pytest.mark.parametrize(("base", "overlay"), [(False, False), (True, False), (False, True)])
    def test_the_config_zone_comes_from_a_load_that_read_the_files(
        self, monkeypatch, base, overlay
    ) -> None:
        """A config that did not parse loads as defaults; its empty zone is not the
        zone the jobs inherited, so it is refused rather than read as "none"."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.cron_service import zone_default_migration

        loaded = SimpleNamespace(
            timezone="Asia/Tokyo", _base_unreadable=base, _overlay_unreadable=overlay
        )
        monkeypatch.setattr(KiroCrewConfig, "load", lambda *a, **k: loaded)
        if base or overlay:
            with pytest.raises(zone_default_migration.ConfigZoneUnknown):
                zone_default_migration.configured_default_zone()
        else:
            assert zone_default_migration.configured_default_zone() == "Asia/Tokyo"


class TestAStoreFromBeforeLocalKeepsItsJobsOnUtc:
    @pytest.fixture(autouse=True)
    def _configured_empty(self, _floor_monkeypatch) -> None:
        from kiro_crew.cron_service import zone_default_migration

        _floor_monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "")

    @pytest.mark.parametrize("version", [2, None])
    def test_an_unzoned_job_whose_wall_clock_matters_is_stamped_utc(
        self, tmp_path, version
    ) -> None:
        _write_store(
            tmp_path,
            version,
            [
                _record("cron", _CRON),
                _record("skips", _EVERY, skip_dates=["2026-12-25"]),
                _record("interval", _EVERY),
                _record("tokyo", _CRON, timezone="Asia/Tokyo"),
            ],
        )
        zones = {j.id: j.timezone for j in CronService(base_dir=tmp_path).list_jobs(True)}
        assert zones == {"cron": "UTC", "skips": "UTC", "interval": "", "tokyo": "Asia/Tokyo"}

    def test_the_next_save_writes_the_stamp_and_the_new_version(self, tmp_path) -> None:
        _write_store(tmp_path, 2, [_record("cron", _CRON)])
        svc = CronService(base_dir=tmp_path)
        added = svc.add_job(name="new", message="m", cron_expr="0 9 * * *")
        saved = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
        assert saved["version"] == 3
        zones = {rec["id"]: rec.get("timezone", "") for rec in saved["jobs"]}
        assert zones == {"cron": "UTC", added.id: ""}

    def test_a_skipped_record_the_save_keeps_is_stamped_too(self, tmp_path) -> None:
        """It is written under the new version too, so without the stamp a record fixed
        by hand later would fire in the host's zone instead of UTC."""
        broken = {"id": "broken", "message": "m", "schedule": dict(_CRON)}  # no "name"
        _write_store(tmp_path, 2, [_record("cron", _CRON), broken])
        CronService(base_dir=tmp_path).add_job(name="new", message="m", cron_expr="0 9 * * *")
        kept = [rec for rec in _saved(tmp_path)["jobs"] if rec["id"] == "broken"]
        assert kept == [{**broken, "timezone": "UTC"}]

    def test_a_kept_record_with_a_list_id_does_not_fail_the_load(self, tmp_path) -> None:
        """The stamp's follows-default lookup takes any id a skipped record carries."""
        broken = {"id": ["x"], "message": "m", "schedule": dict(_CRON)}  # no "name"
        _write_store(tmp_path, 2, [_record("cron", _CRON), broken])
        assert [j.id for j in CronService(base_dir=tmp_path).list_jobs(True)] == ["cron"]

    def test_a_current_store_is_not_stamped(self, tmp_path) -> None:
        _write_store(tmp_path, 3, [_record("cron", _CRON)])
        assert CronService(base_dir=tmp_path).list_jobs(True)[0].timezone == ""

    def test_a_configured_zone_is_left_to_be_inherited(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.cron_service import zone_default_migration

        monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "Asia/Tokyo")
        _write_store(tmp_path, 2, [_record("cron", _CRON)])
        assert CronService(base_dir=tmp_path).list_jobs(True)[0].timezone == ""

    def test_an_unreadable_config_defers_the_upgrade(
        self, tmp_path, monkeypatch, no_configured_zone
    ) -> None:
        """Nothing is stamped and saves keep the older version, so a later load with
        a readable config settles the jobs; the store stays readable throughout.
        Meanwhile the older job keeps its UTC time, in memory only, rather than
        running at the host's (Shanghai's) as the defaults-only config would."""
        from kiro_crew.cron_service import zone_default_migration

        def _unknown() -> str:
            raise zone_default_migration.ConfigZoneUnknown("config.json did not parse")

        monkeypatch.setattr(zone_default_migration, "configured_default_zone", _unknown)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        _write_store(tmp_path, 2, [_record("cron", _CRON)])
        svc = CronService(base_dir=tmp_path)
        assert not svc._load_failed
        older = svc.list_jobs(True)[0]
        assert older.timezone == ""
        assert cron.job_zone_name(older) == "UTC"
        assert cron.is_due(older, _at(ZoneInfo("UTC"), 2026, 11, 3, 9, 0) + 10)
        assert not cron.is_due(older, _at(SHANGHAI, 2026, 11, 3, 9, 0) + 10)
        svc.add_job(name="new", message="m", cron_expr="0 9 * * *")
        saved = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
        assert saved["version"] == 2
        assert {rec["id"]: rec.get("timezone", "") for rec in saved["jobs"]}["cron"] == ""

        monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "")
        zones = {j.name: j.timezone for j in CronService(base_dir=tmp_path).list_jobs(True)}
        assert zones["cron"] == "UTC"

    def test_a_job_added_while_the_upgrade_waits_keeps_following_the_default(
        self, tmp_path, monkeypatch, no_configured_zone
    ) -> None:
        """Version 2 cannot tell a job added during the wait from an older one, so the
        save lists it: neither a reload nor the later upgrade holds or stamps it."""
        from kiro_crew.cron_service import zone_default_migration

        key = zone_default_migration.FOLLOWS_DEFAULT_KEY
        _config_unreadable(monkeypatch)
        _write_store(tmp_path, 2, [_record("cron", _CRON)])
        added = CronService(base_dir=tmp_path).add_job(
            name="new", message="m", cron_expr="0 9 * * *"
        )
        assert _saved(tmp_path)["version"] == 2
        assert _saved(tmp_path)[key] == [added.id]
        reloaded = CronService(base_dir=tmp_path).list_jobs(True)
        assert {j.name: cron.job_zone_name(j) for j in reloaded} == {"cron": "UTC", "new": ""}

        monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "")
        svc = CronService(base_dir=tmp_path)
        assert {j.name: j.timezone for j in svc.list_jobs(True)} == {"cron": "UTC", "new": ""}
        svc.add_job(name="later", message="m", cron_expr="0 10 * * *")
        assert _saved(tmp_path)["version"] == 3
        assert key not in _saved(tmp_path)

    def test_a_skipped_record_listed_as_following_the_default_stays_listed(
        self, tmp_path, monkeypatch
    ) -> None:
        """A save while the upgrade waits keeps the listing of a record it could not
        load, so once the record is fixed by hand the upgrade does not stamp it. An
        unlisted one stays unlisted, and one with a list id does not fail the save."""
        from kiro_crew.cron_service import zone_default_migration

        key = zone_default_migration.FOLLOWS_DEFAULT_KEY
        _config_unreadable(monkeypatch)
        broken = [
            {"id": job_id, "message": "m", "schedule": dict(_CRON)}  # no "name"
            for job_id in ("listed", "older", ["x"])
        ]
        document = {"version": 2, "jobs": [_record("cron", _CRON), *broken], key: ["listed"]}
        (tmp_path / "crons.json").write_text(json.dumps(document), encoding="utf-8")
        CronService(base_dir=tmp_path).enable_job("cron", enabled=False)
        saved = _saved(tmp_path)
        assert saved[key] == ["listed"]

        saved["jobs"][1]["name"] = "listed"
        saved["jobs"][2]["name"] = "older"
        (tmp_path / "crons.json").write_text(json.dumps(saved), encoding="utf-8")
        monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "")
        zones = {j.id: j.timezone for j in CronService(base_dir=tmp_path).list_jobs(True)}
        assert zones == {"cron": "UTC", "listed": "", "older": "UTC"}

    def test_a_kept_record_sharing_a_jobs_id_is_listed_once(self) -> None:
        """A kept record with a loaded job's id must not double the listing."""
        from kiro_crew.cron_service import zone_default_migration

        key = zone_default_migration.FOLLOWS_DEFAULT_KEY
        job = _daily_nine("")
        job.id = "dup"
        kept = zone_default_migration.store_extra_for_save(
            {key: ["dup"]}, [job], deferred=True, skipped=({"id": "dup", "message": "m"},)
        )
        assert kept[key] == ["dup"]

    def test_an_update_that_sets_a_held_jobs_zone_releases_it(
        self, tmp_path, monkeypatch, no_configured_zone
    ) -> None:
        """A zone set and then cleared during the wait leaves the job on the default,
        not on UTC, and the save lists it so the upgrade does not stamp it later. An
        update that leaves the zone alone keeps the hold."""
        from kiro_crew.cron_service import zone_default_migration

        _config_unreadable(monkeypatch)
        _write_store(tmp_path, 2, [_record("cron", _CRON), _record("kept", _CRON)])
        svc = CronService(base_dir=tmp_path)
        svc.update_job("cron", timezone="Europe/Paris")
        cleared = svc.update_job("cron", timezone="")
        assert cleared is not None and cron.job_zone_name(cleared) == ""
        renamed = svc.update_job("kept", name="renamed")
        assert renamed is not None and cron.job_zone_name(renamed) == "UTC"
        assert _saved(tmp_path)[zone_default_migration.FOLLOWS_DEFAULT_KEY] == ["cron"]


class TestTheScheduleApiSaysWhereLocalIs:
    @pytest.mark.asyncio
    async def test_it_reports_the_host_zone_the_default_and_each_jobs_zone(
        self, no_configured_zone
    ) -> None:
        from kiro_crew.dashboard.handlers.cron import api_crons

        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        jobs = [_daily_nine("local"), _daily_nine("America/New_York"), _daily_nine("")]
        for i, job in enumerate(jobs):
            job.id = f"j{i}"
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs_async = AsyncMock(return_value=jobs)
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}

        data = json.loads((await api_crons(request)).body)

        assert data["host_tz"] == "Asia/Shanghai"
        assert data["default_tz"] == "local"
        assert [j["effective_tz"] for j in data["jobs"]] == [
            "Asia/Shanghai",
            "America/New_York",
            "Asia/Shanghai",
        ]
        assert [j["timezone"] for j in data["jobs"]] == ["local", "America/New_York", None]

    @pytest.mark.asyncio
    async def test_a_configured_zone_that_cannot_load_is_reported_as_utc(self, monkeypatch) -> None:
        """The page formats with ``effective_tz``, so it is the zone the scheduler
        uses (UTC for a name that cannot load), never a typo the browser refuses."""
        from kiro_crew.dashboard.handlers.cron import api_crons

        monkeypatch.setattr(cron, "published_config_timezone", lambda: "America/NewYork")
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs_async = AsyncMock(return_value=[_daily_nine("")])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}

        data = json.loads((await api_crons(request)).body)

        assert [j["effective_tz"] for j in data["jobs"]] == ["UTC"]
        assert data["default_tz"] == "UTC"

    @pytest.mark.asyncio
    async def test_a_configured_zone_is_the_default_the_form_offers(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.cron import api_crons

        monkeypatch.setattr(cron, "published_config_timezone", lambda: "Europe/London")
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs_async = AsyncMock(return_value=[])
        request = MagicMock()
        request.app = {"state": state}

        data = json.loads((await api_crons(request)).body)

        assert data["default_tz"] == "Europe/London"
        assert data["host_tz"] is None


class TestAZoneChangeWhileAJobRuns:
    def test_a_run_that_ends_after_the_zone_changed_counts_where_it_began(
        self, no_configured_zone
    ) -> None:
        """``last_run_ts`` is stamped when a run ENDS. A run that began at 09:00 in
        Shanghai, lid closed, and ended after landing in San Francisco has run that
        date's 09:00, so San Francisco's 09:00 on the same date does not run."""
        landed = _at(SHANGHAI, 2026, 11, 3, 9, 0) + 13 * 3600
        job = _daily_nine("", last_run_ts=landed + 60)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=landed)

        assert not cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 10)
        assert cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 4, 9, 0) + 10)

    def test_a_zone_it_cannot_load_or_a_cron_it_cannot_step_holds_nothing_back(
        self, no_configured_zone
    ) -> None:
        from kiro_crew.cron_service import schedule

        ran = _at(SHANGHAI, 2026, 11, 3, 9, 0) + 20
        host_timezone.publish_host_timezone("Not/A_Zone", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=ran + 3600)
        slot = datetime(2026, 11, 3, 9, 0, tzinfo=LOS_ANGELES)
        assert not schedule._already_ran_by_wall_clock(_daily_nine("", last_run_ts=ran), slot)

        host_timezone.reset_host_timezone()
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=ran + 3600)
        unsteppable = _daily_nine("", last_run_ts=ran)
        unsteppable.schedule = CronSchedule(kind="cron", cron_expr="not a cron")
        assert not schedule._already_ran_by_wall_clock(unsteppable, slot)

    def test_a_zone_change_well_before_the_run_does_not_hold_later_slots_back(
        self, no_configured_zone
    ) -> None:
        """Only the zones the host was in within the run's time limit count."""
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(SHANGHAI, 2026, 11, 2, 12)
        )
        hourly = CronJob(
            id="hourly",
            name="hourly",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="0 * * * *"),
            last_run_ts=_at(LOS_ANGELES, 2026, 11, 3, 10, 0) + 20,
        )
        assert cron.is_due(hourly, _at(LOS_ANGELES, 2026, 11, 3, 11, 0) + 10)

    def test_a_zone_the_host_left_before_the_slot_came_round_never_ran_it(
        self, no_configured_zone
    ) -> None:
        """New York to Los Angeles at 08:45 LA, then a 09:00 run ending at 09:01: the
        run's span reaches back into New York, but New York's noon came round after
        the host had left it, so Los Angeles's noon still runs."""
        host_timezone.publish_host_timezone("America/New_York", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(LOS_ANGELES, 2026, 11, 3, 8, 45)
        )
        twice = CronJob(
            id="twice",
            name="twice",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="0 9,12 * * *"),
            last_run_ts=_at(LOS_ANGELES, 2026, 11, 3, 9, 1),
        )
        assert cron.is_due(twice, _at(LOS_ANGELES, 2026, 11, 3, 12, 0) + 10)

    def test_a_zone_change_during_the_jitter_wait_still_holds_the_slot(
        self, no_configured_zone
    ) -> None:
        """Dispatched at 09:00 New York, 50 minutes of jitter, the host moves to Los
        Angeles at 09:10 and the run ends at 09:51: the span reaches back through the
        jitter wait to New York, so Los Angeles's 09:00 on that date does not run."""
        host_timezone.publish_host_timezone("America/New_York", at=_SEEN_IN_SHANGHAI)
        host_timezone.publish_host_timezone(
            "America/Los_Angeles", at=_at(NEW_YORK, 2026, 11, 3, 9, 10)
        )
        job = _daily_nine("", last_run_ts=_at(NEW_YORK, 2026, 11, 3, 9, 51))
        assert not cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 3, 9, 0) + 10)
        assert cron.is_due(job, _at(LOS_ANGELES, 2026, 11, 4, 9, 0) + 10)

    def test_a_sub_hourly_job_across_a_zone_change_skips_no_slot(self, no_configured_zone) -> None:
        """A */15 job has no jitter, so its look-back is only its time limit; after a
        westward change every slot that did not run still runs, and the preview names
        the very next one."""
        from kiro_crew.cron_service import schedule

        ran = _at(SHANGHAI, 2026, 11, 3, 10, 0) + 30
        quarterly = CronJob(
            id="quarter",
            name="quarter",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="*/15 * * * *"),
            last_run_ts=ran,
        )
        assert schedule.jitter_bound(quarterly) == 0
        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        landed = _at(SHANGHAI, 2026, 11, 3, 10, 5)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=landed)

        first = _at(LOS_ANGELES, 2026, 11, 2, 18, 15)
        for step in range(8):
            assert cron.is_due(quarterly, first + step * 15 * 60 + 10), step
        assert cron.compute_next_run_ts(quarterly, now=landed + 60) == first

    def test_the_look_back_uses_the_jobs_own_jitter_band(self) -> None:
        from kiro_crew.cron_service import schedule

        def job(expr: str, strict: bool = False) -> CronJob:
            return CronJob(
                id=expr,
                name=expr,
                message="m",
                schedule=CronSchedule(kind="cron", cron_expr=expr),
                strict_schedule=strict,
            )

        assert schedule.jitter_bound(job("0 9 * * *")) == schedule._JITTER_DAILY_MAX
        assert schedule.jitter_bound(job("0 * * * *")) == schedule._JITTER_HOURLY_MAX
        assert schedule.jitter_bound(job("*/15 * * * *")) == 0
        assert schedule.jitter_bound(job("0 9 * * *", strict=True)) == 0

    def test_the_zones_in_effect_over_a_span(self) -> None:
        host_timezone.publish_host_timezone("Asia/Shanghai", at=100.0)
        host_timezone.publish_host_timezone("America/Los_Angeles", at=200.0)
        host_timezone.publish_host_timezone("Asia/Tokyo", at=300.0)
        assert host_timezone.host_timezones_between(150.0, 250.0) == {
            "Asia/Shanghai",
            "America/Los_Angeles",
        }
        assert host_timezone.host_timezones_between(210.0, 250.0) == {"America/Los_Angeles"}
        assert host_timezone.host_timezones_between(50.0, 90.0) == frozenset()


class TestReadingTheHostZone:
    def test_a_refresh_already_in_progress_is_not_overtaken(self, monkeypatch) -> None:
        """Two reads finishing out of order would publish the older zone last."""
        reads: list[str] = []

        def _detect() -> str:
            reads.append("read")
            return "Asia/Tokyo"

        monkeypatch.setattr(host_timezone, "detect_host_timezone", _detect)
        host_timezone.freeze_host_timezone(None)
        host_timezone.publish_host_timezone("Asia/Shanghai", at=100.0)
        with host_timezone._REFRESH_LOCK:
            assert host_timezone.refresh_host_timezone() == "Asia/Shanghai"
        assert reads == []
        assert host_timezone.refresh_host_timezone() == "Asia/Tokyo"

    def test_a_copied_localtime_file_falls_back_to_etc_timezone(
        self, monkeypatch, tmp_path
    ) -> None:
        localtime = tmp_path / "localtime"
        localtime.write_bytes(b"TZif2")
        named = tmp_path / "timezone"
        named.write_text("Asia/Tokyo\n", encoding="utf-8")
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(host_timezone, "_LOCALTIME", localtime)
        monkeypatch.setattr(host_timezone, "_ETC_TIMEZONE", named)
        assert host_timezone.detect_host_timezone() == "Asia/Tokyo"


class TestOtherWritersAndReadersOfTheDefault:
    def test_a_schedule_card_names_the_zone_the_browser_can_format(
        self, no_configured_zone
    ) -> None:
        from kiro_crew import change_card_catalog as catalog
        from kiro_crew.dashboard import change_cards as cards

        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        for zone in ("local", ""):
            params = {"name": "brief", "message": "m", "cron_expr": "0 9 * * *", "timezone": zone}
            ctx = asyncio.run(
                cards.read_context(catalog.KIND_SCHEDULE_CREATE, params, {}, state=None, app=None)
            )
            assert ctx["timezone"] == "Asia/Shanghai"

    def test_a_goal_check_in_keeps_following_the_computer(
        self, no_configured_zone, tmp_path
    ) -> None:
        from kiro_crew.dashboard import goal_reminder

        host_timezone.publish_host_timezone("Asia/Shanghai", at=_SEEN_IN_SHANGHAI)
        svc = CronService(base_dir=tmp_path)
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {"daily": True, "time": "09:00"})
        )
        assert [j.timezone for j in svc.list_jobs(True)] == ["local"]

    def test_a_goal_check_in_saved_on_utc_is_left_alone_by_an_unrelated_save(
        self, no_configured_zone, tmp_path
    ) -> None:
        """An older build saved check-ins on UTC; saving the Goals tab again keeps each
        untouched one, id and zone, instead of recreating it on the new default."""
        from kiro_crew.dashboard import goal_reminder

        svc = CronService(base_dir=tmp_path)
        schedules = {"daily": True, "weekly": True, "time": "09:00", "weekday": 1}
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "timezone": "UTC"})
        )
        before = {j.id: j.timezone for j in svc.list_jobs(True)}
        assert set(before.values()) == {"UTC"}

        asyncio.run(goal_reminder.sync_schedule(svc, "mate", "Mate", schedules))
        assert {j.id: j.timezone for j in svc.list_jobs(True)} == before

        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "time": "10:00"})
        )
        assert {j.timezone for j in svc.list_jobs(True)} == {"local"}

    def test_clearing_a_check_ins_zone_moves_it_to_the_default(
        self, no_configured_zone, tmp_path
    ) -> None:
        from kiro_crew.dashboard import goal_reminder

        svc = CronService(base_dir=tmp_path)
        schedules = {"daily": True, "time": "09:00"}
        asyncio.run(
            goal_reminder.sync_schedule(
                svc, "mate", "Mate", {**schedules, "timezone": "Asia/Tokyo"}
            )
        )
        asyncio.run(goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "timezone": ""}))
        assert [j.timezone for j in svc.list_jobs(True)] == ["local"]

    def test_a_configured_zone_change_moves_every_check_in(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard import goal_reminder

        svc = CronService(base_dir=tmp_path)
        schedules = {"daily": True, "weekly": True, "time": "09:00", "weekday": 1}
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "timezone": "UTC"})
        )
        monkeypatch.setattr(cron, "published_config_timezone", lambda: "Europe/London")
        asyncio.run(goal_reminder.sync_schedule(svc, "mate", "Mate", schedules))
        assert {j.timezone for j in svc.list_jobs(True)} == {"Europe/London"}

    def test_a_configured_zone_that_cannot_load_never_loses_a_check_in(
        self, monkeypatch, tmp_path
    ) -> None:
        """A typo in the config zone is stored as UTC, the zone the scheduler runs it
        in; stored as is, the replacement would be refused after the old check-in
        was already removed."""
        from kiro_crew.dashboard import goal_reminder

        monkeypatch.setattr(cron, "published_config_timezone", lambda: "America/NewYork")
        svc = CronService(base_dir=tmp_path)
        schedules = {"daily": True, "time": "09:00"}
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "timezone": "UTC"})
        )
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "time": "10:00"})
        )
        jobs = svc.list_jobs(True)
        assert [(j.timezone, j.schedule.cron_expr) for j in jobs] == [("UTC", "0 10 * * *")]

    def test_a_goal_check_in_takes_a_zone_the_record_names(
        self, no_configured_zone, tmp_path
    ) -> None:
        from kiro_crew.dashboard import goal_reminder

        svc = CronService(base_dir=tmp_path)
        schedules = {"daily": True, "time": "09:00"}
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {**schedules, "timezone": "UTC"})
        )
        asyncio.run(
            goal_reminder.sync_schedule(
                svc, "mate", "Mate", {**schedules, "timezone": "Asia/Tokyo"}
            )
        )
        assert [j.timezone for j in svc.list_jobs(True)] == ["Asia/Tokyo"]

    def test_a_goal_check_in_takes_a_configured_zone_by_name(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard import goal_reminder

        monkeypatch.setattr(cron, "published_config_timezone", lambda: "Europe/London")
        svc = CronService(base_dir=tmp_path)
        asyncio.run(
            goal_reminder.sync_schedule(svc, "mate", "Mate", {"daily": True, "time": "09:00"})
        )
        assert [j.timezone for j in svc.list_jobs(True)] == ["Europe/London"]


class TestMergingABackupIntoTheLiveStore:
    @pytest.fixture(autouse=True)
    def _configured_empty(self, _floor_monkeypatch) -> None:
        from kiro_crew.cron_service import zone_default_migration

        _floor_monkeypatch.setattr(zone_default_migration, "configured_default_zone", lambda: "")

    def _merge(self, tmp_path, src_doc: dict, dst_doc: dict) -> dict:
        from kiro_crew import snapshot_merge

        src, dst = tmp_path / "backup.json", tmp_path / "crons.json"
        src.write_text(json.dumps(src_doc), encoding="utf-8")
        dst.write_text(json.dumps(dst_doc), encoding="utf-8")
        assert snapshot_merge._merge_crons(src, dst)
        return json.loads(dst.read_text(encoding="utf-8"))

    def test_an_older_backups_unzoned_job_keeps_firing_in_utc(self, tmp_path) -> None:
        merged = self._merge(
            tmp_path,
            {"version": 2, "jobs": [_record("old", _CRON)]},
            {"version": 3, "jobs": [_record("new", _CRON)]},
        )
        zones = {rec["name"]: rec.get("timezone", "") for rec in merged["jobs"]}
        assert zones == {"new": "", "old": "UTC"}
        assert merged["version"] == 3

    def test_with_the_config_unreadable_an_older_backup_is_not_merged(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew import snapshot_merge
        from kiro_crew.cron_service import zone_default_migration

        def _unknown() -> str:
            raise zone_default_migration.ConfigZoneUnknown("config.json did not parse")

        monkeypatch.setattr(zone_default_migration, "configured_default_zone", _unknown)
        src, dst = tmp_path / "backup.json", tmp_path / "crons.json"
        src.write_text(
            json.dumps({"version": 2, "jobs": [_record("old", _CRON)]}), encoding="utf-8"
        )
        before = json.dumps({"version": 3, "jobs": [_record("new", _CRON)]})
        dst.write_text(before, encoding="utf-8")
        assert not snapshot_merge._merge_crons(src, dst)
        assert dst.read_text(encoding="utf-8") == before

    def test_a_current_backup_merged_into_an_older_store_is_not_stamped(self, tmp_path) -> None:
        merged = self._merge(
            tmp_path,
            {"version": 3, "jobs": [_record("new", _CRON)]},
            {"version": 2, "jobs": [_record("old", _CRON)]},
        )
        zones = {rec["name"]: rec.get("timezone", "") for rec in merged["jobs"]}
        assert zones == {"old": "UTC", "new": ""}
        assert merged["version"] == 3

    def test_a_job_an_older_store_lists_as_following_the_default_is_not_stamped(
        self, tmp_path
    ) -> None:
        from kiro_crew.cron_service import zone_default_migration

        key = zone_default_migration.FOLLOWS_DEFAULT_KEY
        merged = self._merge(
            tmp_path,
            {
                "version": 2,
                "jobs": [_record("old", _CRON), _record("added", _CRON)],
                key: ["added"],
            },
            {
                "version": 2,
                "jobs": [_record("live", _CRON), _record("fresh", _CRON)],
                key: ["fresh"],
            },
        )
        zones = {rec["name"]: rec.get("timezone", "") for rec in merged["jobs"]}
        assert zones == {"live": "UTC", "fresh": "", "old": "UTC", "added": ""}
        assert merged["version"] == 3 and key not in merged


class _SignallingLock:
    """A lock that reports a thread finding it held, so a test can wait for that."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self._lock.acquire(timeout=60) if blocking else False

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class TestAColdGatewayReadsTheHostZoneBeforeSavingAnInstant:
    def test_an_off_loop_parse_waits_for_a_refresh_in_progress(
        self, no_configured_zone, monkeypatch
    ) -> None:
        """``cron_add`` on a worker thread of a cold gateway, while a refresh another
        reader started holds the lock: answering around it with the empty snapshot
        reads the clock in UTC, for good. The parse waits, then reads the zone."""
        tokyo = ZoneInfo("Asia/Tokyo")
        lock = _SignallingLock()
        monkeypatch.setattr(host_timezone, "_REFRESH_LOCK", lock)
        monkeypatch.setattr(host_timezone, "detect_host_timezone", lambda: "Asia/Tokyo")
        host_timezone.freeze_host_timezone(None)
        parsed: list[float | str] = []
        worker = threading.Thread(
            target=lambda: parsed.append(cron.parse_time_string("2030-01-15 09:00", "local"))
        )
        lock.acquire()
        worker.start()
        try:
            assert lock.contended.wait(timeout=60), "the parse never reached the refresh"
        finally:
            lock.release()
            worker.join(timeout=60)
        assert not worker.is_alive()
        assert parsed == [_at(tokyo, 2030, 1, 15, 9, 0)]

    @pytest.mark.asyncio
    async def test_a_local_one_shot_on_a_cold_gateway_is_read_in_the_hosts_zone(
        self, no_configured_zone, monkeypatch, tmp_path
    ) -> None:
        """A gateway that never read the host zone (no cron scheduler running) must
        not read a "local" clock as UTC: the instant it saves is never corrected."""
        from body_stream_helpers import attach_body

        from kiro_crew.dashboard.handlers import api_crons_create

        tokyo = ZoneInfo("Asia/Tokyo")
        monkeypatch.setattr(host_timezone, "detect_host_timezone", lambda: "Asia/Tokyo")
        host_timezone.freeze_host_timezone(None)
        crons = CronService(base_dir=tmp_path)
        state = MagicMock()
        state.crons = crons
        request = MagicMock()
        request.app = {"state": state}
        attach_body(
            request,
            {"name": "brief", "message": "m", "at_time": "tomorrow 09:00", "timezone": "local"},
        )

        resp = await api_crons_create(request)

        assert resp.status == 200, resp.body
        job = crons.list_jobs()[0]
        assert job.timezone == "local"
        ran = datetime.fromtimestamp(job.schedule.at_ts, tz=tokyo)
        assert (ran.hour, ran.minute) == (9, 0)

    @pytest.mark.asyncio
    async def test_ensure_reads_the_zone_afresh_off_the_loop(self, monkeypatch) -> None:
        """A snapshot the host has since left is never trusted: each call reads again,
        always on a worker thread."""
        loop_thread = threading.get_ident()
        read_on: list[int] = []
        zone = ["America/New_York"]

        def _detect() -> str:
            read_on.append(threading.get_ident())
            return zone[0]

        monkeypatch.setattr(host_timezone, "detect_host_timezone", _detect)
        host_timezone.freeze_host_timezone(None)
        assert await host_timezone.ensure_host_timezone() == "America/New_York"
        zone[0] = "America/Los_Angeles"
        assert await host_timezone.ensure_host_timezone() == "America/Los_Angeles"
        assert len(read_on) == 2 and loop_thread not in read_on
