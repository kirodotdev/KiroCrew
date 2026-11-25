"""``kirocrew.app.present``: one row per installed app per UTC day (``metrics/app_present.py``)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from kiro_crew import beacon
from kiro_crew.metrics import app_present
from kiro_crew.metrics import feature_usage as fu

_APPS = [
    # A backend process this gateway runs.
    {"name": "weather-app", "enabled": True, "manifest": {"backend": {"entryPoint": "b.py"}}},
    # A backend process that is not running, on a disabled app.
    {"name": "notes", "enabled": False, "manifest": {"backend": {"entryPoint": "b.py"}}},
    # No backend process at all (in-gateway hooks only).
    {"name": "mine", "enabled": True, "manifest": {"backend": {"hooks": {}}}},
]
_IDS = {"weather-app": "1.2.0", "notes": "2.0.0-beta"}


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        if name == fu.APP_PRESENT:
            self.rows.append(dict(attrs or {}))


@pytest.fixture
def day(monkeypatch, tmp_path):
    """The UTC day the stamp compares against; set ``day[0]`` to move it."""
    today = ["2026-11-20"]
    monkeypatch.setattr(beacon, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(beacon, "_today", lambda: today[0])
    monkeypatch.setattr(fu, "refresh_public_app_ids", lambda: dict(_IDS))
    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", lambda: [dict(a) for a in _APPS])
    monkeypatch.setattr(
        "kiro_crew.apps.backend.list_app_processes",
        lambda: [{"app_name": "weather-app", "running": True}],
    )
    return today


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def test_one_row_per_app_with_status_backend_and_version(day, rec):
    assert app_present.record_today() == 3
    assert rec.rows == [
        {
            "app": "weather-app",
            "entity_version": "1.2.0",
            "status": "enabled",
            "backend": "running",
        },
        {"app": "notes", "entity_version": "other", "status": "disabled", "backend": "stopped"},
        {"app": "private", "entity_version": "private", "status": "enabled", "backend": "none"},
    ]


def test_two_restarts_on_one_day_record_one_row_per_app(day, rec):
    """The day is read from the stamp on disk, so a new process the same day records nothing."""
    assert app_present.record_today() == 3
    assert (beacon.config_dir() / beacon.APP_PRESENT_STAMP_FILE).read_text() == "2026-11-20"
    assert app_present.record_today() == 0
    assert app_present.record_today() == 0
    assert len(rec.rows) == 3


def test_a_long_running_gateway_records_once_more_after_utc_midnight(day, rec, monkeypatch):
    """The task itself: a check after UTC midnight records the new day, a later one nothing."""
    waits: list[int] = []

    def next_wait(now=None):
        waits.append(1)
        if len(waits) == 1:
            day[0] = "2026-11-21"  # the wait crossed UTC midnight
        elif len(waits) == 3:
            raise asyncio.CancelledError
        return 0.0

    monkeypatch.setattr(app_present, "FIRST_PASS_DELAY_SECS", 0.0)
    monkeypatch.setattr(app_present, "seconds_until_next_check", next_wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_present.run_daily())
    # Day 1 once, day 2 once, and the third pass (still day 2) nothing.
    assert len(rec.rows) == 6
    assert [r["app"] for r in rec.rows[3:]] == ["weather-app", "notes", "private"]


def test_a_pass_that_crosses_utc_midnight_stamps_the_day_it_recorded(day, rec, monkeypatch):
    """The clock moves to the next day mid-pass: the next day is still due afterwards."""

    def listing_across_midnight():
        day[0] = "2026-11-21"
        return [dict(a) for a in _APPS]

    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", listing_across_midnight)
    assert app_present.record_today() == 3
    assert (beacon.config_dir() / beacon.APP_PRESENT_STAMP_FILE).read_text() == "2026-11-20"
    assert app_present.record_today() == 3


def test_the_wait_ends_just_after_utc_midnight_and_never_exceeds_an_hour():
    late = datetime(2026, 11, 20, 23, 59, 30, tzinfo=timezone.utc)
    early = datetime(2026, 11, 21, 0, 0, 1, tzinfo=timezone.utc)
    assert app_present.seconds_until_next_check(late) == 31.0
    assert app_present.seconds_until_next_check(early) == app_present.MAX_SLEEP_SECS


def test_telemetry_off_records_nothing_and_writes_no_stamp(day, rec):
    rec.enabled = False
    assert app_present.record_today() == 0
    assert rec.rows == []
    assert not (beacon.config_dir() / beacon.APP_PRESENT_STAMP_FILE).exists()


def test_a_raising_recorder_never_breaks_the_pass(day, rec, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(rec, "counter", boom)
    assert app_present.record_today() == 3
    assert app_present.record_today() == 0  # the stamp was still written


def test_a_failed_listing_writes_no_stamp_and_retries(day, rec, monkeypatch):
    def broken():
        raise OSError("apps dir unreadable")

    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", broken)
    assert app_present.record_today() == 0
    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", lambda: [dict(a) for a in _APPS])
    assert app_present.record_today() == 3
