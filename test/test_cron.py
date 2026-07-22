"""Tests for the cron service."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import (
    _TIMER_POLL_SECS,
    CronJob,
    CronSchedule,
    CronService,
    _job_tz,
    compute_next_run_ts,
    cron_expr_matches,
    validate_cron_expr,
)


class TestCronExprMatching:
    def test_every_minute(self) -> None:
        dt = datetime(2026, 2, 15, 9, 30, tzinfo=timezone.utc)
        assert cron_expr_matches("* * * * *", dt)

    def test_specific_minute_hour(self) -> None:
        dt = datetime(2026, 2, 15, 9, 30, tzinfo=timezone.utc)
        assert cron_expr_matches("30 9 * * *", dt)
        assert not cron_expr_matches("0 9 * * *", dt)

    def test_step(self) -> None:
        dt = datetime(2026, 2, 15, 9, 0, tzinfo=timezone.utc)
        assert cron_expr_matches("*/5 * * * *", dt)
        dt2 = datetime(2026, 2, 15, 9, 3, tzinfo=timezone.utc)
        assert not cron_expr_matches("*/5 * * * *", dt2)

    def test_range(self) -> None:
        # 2026-02-16 is Monday, 2026-02-15 is Sunday
        dt_mon = datetime(2026, 2, 16, 9, 0, tzinfo=timezone.utc)  # Monday
        assert cron_expr_matches("0 9 * * 1-5", dt_mon)  # cron: 1=Mon..5=Fri
        dt_sun = datetime(2026, 2, 15, 9, 0, tzinfo=timezone.utc)  # Sunday
        assert not cron_expr_matches("0 9 * * 1-5", dt_sun)

    def test_named_days(self) -> None:
        dt_mon = datetime(2026, 2, 16, 9, 0, tzinfo=timezone.utc)  # Monday
        assert cron_expr_matches("0 9 * * MON-FRI", dt_mon)

    def test_comma_list(self) -> None:
        dt = datetime(2026, 2, 15, 9, 0, tzinfo=timezone.utc)
        assert cron_expr_matches("0 9,10,11 * * *", dt)
        assert not cron_expr_matches("0 10,11 * * *", dt)

    def test_invalid_expr(self) -> None:
        dt = datetime(2026, 2, 15, 9, 0, tzinfo=timezone.utc)
        assert not cron_expr_matches("bad", dt)


class TestValidateCronExpr:
    def test_valid(self) -> None:
        assert validate_cron_expr("0 9 * * *")
        assert validate_cron_expr("*/5 * * * MON-FRI")
        assert validate_cron_expr("0 9 1,15 * *")

    def test_invalid(self) -> None:
        assert not validate_cron_expr("bad")
        assert not validate_cron_expr("* * *")
        assert not validate_cron_expr("")


class TestCronService:
    def test_add_job_every(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", every_secs=300)
        assert job.id
        assert job.name == "test"
        assert job.schedule.kind == "every"
        assert job.schedule.every_secs == 300

    def test_add_job_at(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="once", message="do it", at_ts=9999999999.0)
        assert job.schedule.kind == "at"
        assert job.schedule.at_ts == 9999999999.0

    def test_add_job_cron_expr(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="daily", message="briefing", cron_expr="0 9 * * *")
        assert job.schedule.kind == "cron"
        assert job.schedule.cron_expr == "0 9 * * *"

    def test_add_job_invalid_cron_expr(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError, match="Invalid cron"):
            svc.add_job(name="bad", message="nope", cron_expr="invalid")

    def test_add_job_no_schedule_raises(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError, match="Must provide"):
            svc.add_job(name="bad", message="nope")

    def test_min_interval(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="fast", message="go", every_secs=5)
        assert job.schedule.every_secs == 60

    def test_remove_job(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="rm", message="bye", every_secs=300)
        assert svc.remove_job(job.id)
        assert not svc.remove_job("nonexistent")

    def test_list_jobs(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="a", message="1", every_secs=300)
        svc.add_job(name="b", message="2", every_secs=600)
        assert len(svc.list_jobs()) == 2

    def test_persistence(self, tmp_path: Path) -> None:
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        svc1.add_job(name="persist", message="test", every_secs=300)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert len(svc2.list_jobs()) == 1
        assert svc2.list_jobs()[0].name == "persist"

    def test_persistence_cron_expr(self, tmp_path: Path) -> None:
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        svc1.add_job(name="daily", message="hi", cron_expr="0 9 * * MON-FRI")

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        job = svc2.list_jobs()[0]
        assert job.schedule.kind == "cron"
        assert job.schedule.cron_expr == "0 9 * * MON-FRI"

    def test_status(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="s", message="m", every_secs=300)
        status = svc.status()
        assert status["jobs"] == 1
        assert status["enabled"] == 1

    def test_load_corrupted(self, tmp_path: Path) -> None:
        (tmp_path / "crons.json").write_text("not json")
        svc = CronService(base_dir=tmp_path)
        svc._load()
        assert svc.list_jobs() == []

    def test_add_job_default_not_silent(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        assert job.silent is False

    def test_silent_field_persists(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="t", message="m", every_secs=300)
        job.silent = True
        svc._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2.list_jobs()[0].silent is True

    def test_silent_field_default_false(self) -> None:
        job = CronJob(id="x", name="x", message="x")
        assert job.silent is False

    def test_add_job_with_channel(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="ops", message="check", every_secs=300, channel="C0AP77JJSN6")
        assert job.channel == "C0AP77JJSN6"

    def test_add_job_channel_persists(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="ops", message="check", every_secs=300, channel="C0AP77JJSN6")
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.channel == "C0AP77JJSN6"

    def test_add_job_channel_default_none(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="ops", message="check", every_secs=300)
        assert job.channel is None

    def test_approval_mode_default(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", every_secs=300)
        assert job.approval_mode == ""

    def test_approval_mode_persists(self, tmp_path: Path) -> None:
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        job = svc1.add_job(name="auto-job", message="go", every_secs=300)
        job.approval_mode = "auto"
        svc1._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        loaded = svc2.list_jobs()[0]
        assert loaded.approval_mode == "auto"

    def test_approval_mode_missing_in_json(self, tmp_path: Path) -> None:
        """Old crons.json without approval_mode should default to empty string."""
        import json

        data = {
            "version": 2,
            "jobs": [
                {
                    "id": "abc123",
                    "name": "legacy",
                    "message": "hi",
                    "schedule": {"kind": "every", "every_secs": 300},
                }
            ],
        }
        (tmp_path / "crons.json").write_text(json.dumps(data))
        svc = CronService(base_dir=tmp_path)
        svc._load()
        assert svc.list_jobs()[0].approval_mode == ""

    def test_model_default_empty(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", every_secs=300)
        assert job.model == ""

    def test_model_persists(self, tmp_path: Path) -> None:
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        job = svc1.add_job(name="model-job", message="go", every_secs=300)
        job.model = "sonnet"
        svc1._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        loaded = svc2.list_jobs()[0]
        assert loaded.model == "sonnet"

    def test_model_missing_in_json_defaults_empty(self, tmp_path: Path) -> None:
        """Old crons.json without model field should default to empty string."""
        data = {
            "version": 2,
            "jobs": [
                {
                    "id": "abc123",
                    "name": "legacy",
                    "message": "hi",
                    "schedule": {"kind": "every", "every_secs": 300},
                }
            ],
        }
        (tmp_path / "crons.json").write_text(json.dumps(data))
        svc = CronService(base_dir=tmp_path)
        svc._load()
        assert svc.list_jobs()[0].model == ""

    def test_update_job_model(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", every_secs=300)
        updated = svc.update_job(job.id, model="opus")
        assert updated is not None
        assert updated.model == "opus"

    def test_update_job_model_clear(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hello", every_secs=300)
        job.model = "sonnet"
        svc._save()
        updated = svc.update_job(job.id, model="")
        assert updated is not None
        assert updated.model == ""


class TestTimerRestoreOnLoad:
    """Verify that _load() restores timers for active jobs when running."""

    def _write_jobs(self, tmp_path: Path, jobs: list[dict]) -> None:
        (tmp_path / "crons.json").write_text(json.dumps({"version": 1, "jobs": jobs}))

    def _make_job(self, *, enabled: bool = True, job_id: str = "abc123") -> dict:
        return {
            "id": job_id,
            "name": "test",
            "message": "hello",
            "schedule": {"kind": "every", "every_secs": 300},
            "enabled": enabled,
            "created_ts": time.time(),
        }

    def test_load_active_jobs_arms_timer(self, tmp_path: Path) -> None:
        """Active jobs loaded from disk must trigger _arm_timer."""
        self._write_jobs(tmp_path, [self._make_job()])
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        with patch.object(svc, "_arm_timer") as mock_arm:
            svc._load()
            mock_arm.assert_called_once()

    def test_load_paused_jobs_no_timer(self, tmp_path: Path) -> None:
        """Paused (disabled) jobs must NOT trigger _arm_timer."""
        self._write_jobs(tmp_path, [self._make_job(enabled=False)])
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        with patch.object(svc, "_arm_timer") as mock_arm:
            svc._load()
            mock_arm.assert_not_called()

    def test_load_not_running_no_timer(self, tmp_path: Path) -> None:
        """Jobs loaded before start() must NOT trigger _arm_timer."""
        self._write_jobs(tmp_path, [self._make_job()])
        svc = CronService(base_dir=tmp_path)
        with patch.object(svc, "_arm_timer") as mock_arm:
            svc._load()
            mock_arm.assert_not_called()

    def test_load_logs_restored_count(self, tmp_path: Path, caplog) -> None:
        """Log message must include the count of restored timers."""
        self._write_jobs(
            tmp_path,
            [self._make_job(job_id="a"), self._make_job(job_id="b")],
        )
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        with patch.object(svc, "_arm_timer"):
            with caplog.at_level(logging.INFO, logger="kiro_crew.cron"):
                svc._load()
        assert "Restored 2 cron timer(s) from disk" in caplog.text


class TestUserPausedState:
    """Verify user_paused separates user-controlled pause from execution state."""

    def test_enable_job_sets_user_paused(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="test", message="hi", every_secs=300)
        assert job.user_paused is False

        svc.enable_job(job.id, enabled=False)
        assert job.user_paused is True
        assert job.enabled is False

        svc.enable_job(job.id, enabled=True)
        assert job.user_paused is False
        assert job.enabled is True

    def test_merge_result_preserves_enabled_for_recurring_jobs(self, tmp_path: Path) -> None:
        """_merge_job_result must not propagate enabled=False for recurring jobs."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="recurring", message="go", every_secs=60)
        # Simulate stale runtime state where enabled got corrupted
        job.enabled = False
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        # Reload and verify enabled was NOT persisted as False
        svc._load()
        reloaded = [j for j in svc._jobs if j.id == job.id][0]
        assert reloaded.enabled is True

    def test_merge_result_disables_at_job_with_user_paused(self, tmp_path: Path) -> None:
        """_merge_job_result sets user_paused=True when disabling at-jobs."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="once", message="fire", at_ts=time.time() + 9999)
        job.enabled = False  # at-job fired
        job.last_run_ts = time.time()
        job.last_status = "ok"
        svc._merge_job_result(job)
        # Reload and verify both enabled=False AND user_paused=True persisted
        svc._load()
        reloaded = [j for j in svc._jobs if j.id == job.id][0]
        assert reloaded.enabled is False
        assert reloaded.user_paused is True


class TestEffectiveDelay:
    """Tests for _effective_delay — the capped timer delay used by _arm_timer."""

    def test_far_future_at_job_capped_at_poll_interval(self, tmp_path: Path) -> None:
        """A one-shot job far in the future must not sleep beyond _TIMER_POLL_SECS."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="future", message="later", at_ts=9999999999.0)

        assert svc._effective_delay() == _TIMER_POLL_SECS

    def test_imminent_job_not_capped(self, tmp_path: Path) -> None:
        """A job due very soon should return its actual short delay, not the poll interval."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        svc.add_job(name="soon", message="now", at_ts=time.time() + 2)

        delay = svc._effective_delay()

        assert delay < _TIMER_POLL_SECS

    def test_no_jobs_defaults_to_poll_interval(self, tmp_path: Path) -> None:
        """With no jobs, _effective_delay returns the poll interval."""
        svc = CronService(base_dir=tmp_path)
        svc._load()

        assert svc._effective_delay() == _TIMER_POLL_SECS

    def test_disabled_jobs_default_to_poll_interval(self, tmp_path: Path) -> None:
        """Disabled jobs should not influence the delay — falls back to poll interval."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="off", message="skip", at_ts=9999999999.0)
        job.enabled = False

        assert svc._effective_delay() == _TIMER_POLL_SECS


class TestFormatSchedule:
    @pytest.fixture(autouse=False)
    def _utc_tz(self):
        """Pin TZ=UTC for tests that compare dates across today/future.

        ``time.tzset`` is Unix-only and absent from some interpreter builds;
        when it's missing we skip the call since CI fleets already run in UTC,
        so the pin is a no-op there.
        """
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        if hasattr(time, "tzset"):
            time.tzset()
        yield
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        if hasattr(time, "tzset"):
            time.tzset()

    def test_cron_expr_human_readable(self, monkeypatch) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        s = CronSchedule(kind="cron", cron_expr="0 22 * * 1-5")
        result = format_schedule(s, tz_name="")
        assert "Monday through Friday" in result
        assert "10:00 PM" in result

    def test_cron_expr_with_timezone(self) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        s = CronSchedule(kind="cron", cron_expr="0 22 * * 1-5")
        result = format_schedule(s, tz_name="America/Los_Angeles")
        # Expression is evaluated in job timezone (LA), so 22:00 = 10 PM local
        assert "10:00 PM" in result
        assert "PDT" in result or "PST" in result
        assert "Monday through Friday" in result

    def test_cron_expr_single_day(self, monkeypatch) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        s = CronSchedule(kind="cron", cron_expr="0 21 * * 5")
        result = format_schedule(s, tz_name="")
        assert "Friday" in result

    def test_single_digit_hour_with_timezone(self) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        # 03:00 in LA timezone = 3 AM local, no date boundary issue
        s = CronSchedule(kind="cron", cron_expr="0 3 * * *")
        result = format_schedule(s, tz_name="America/Los_Angeles")
        assert "PDT" in result or "PST" in result
        assert "3:00 AM" in result

    def test_every_secs(self) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        s = CronSchedule(kind="every", every_secs=300)
        assert format_schedule(s) == "every 300s"

    def test_every_hours(self) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        s = CronSchedule(kind="every", every_secs=7200)
        assert format_schedule(s) == "every 2h"

    def test_at_timestamp_today(self, monkeypatch, _utc_tz) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        # Mock "now" to 2026-04-10, job at 3PM same day
        fake_now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        # Mock only covers now() and fromtimestamp() — extend if format_schedule evolves.
        monkeypatch.setattr("kiro_crew.cron.datetime", type("D", (datetime,), {
            "now": classmethod(lambda cls, tz=None: fake_now),
            "fromtimestamp": staticmethod(lambda ts, tz=None: datetime.fromtimestamp(ts, tz)),
        }))
        job_ts = datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc).timestamp()
        result = format_schedule(CronSchedule(kind="at", at_ts=job_ts))
        assert result.startswith("at ")
        assert "," not in result  # no date for today

    def test_at_timestamp_future_date(self, monkeypatch, _utc_tz) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        # Mock "now" to 2026-04-10, job on Apr 17
        fake_now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        # Mock only covers now() and fromtimestamp() — extend if format_schedule evolves.
        monkeypatch.setattr("kiro_crew.cron.datetime", type("D", (datetime,), {
            "now": classmethod(lambda cls, tz=None: fake_now),
            "fromtimestamp": staticmethod(lambda ts, tz=None: datetime.fromtimestamp(ts, tz)),
        }))
        job_ts = datetime(2026, 4, 17, 8, 0, tzinfo=timezone.utc).timestamp()
        result = format_schedule(CronSchedule(kind="at", at_ts=job_ts))
        assert "Apr 17" in result

    def test_unknown_kind(self) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        s = CronSchedule(kind="unknown")
        assert format_schedule(s) == "unknown"

    def test_every_5_minutes(self, monkeypatch) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        s = CronSchedule(kind="cron", cron_expr="*/5 * * * *")
        result = format_schedule(s, tz_name="")
        assert "5 minutes" in result

    def test_invalid_timezone_falls_back(self, monkeypatch) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        s = CronSchedule(kind="cron", cron_expr="0 22 * * 1-5")
        result = format_schedule(s, tz_name="Invalid/Timezone")
        # Should still return a description, just without tz conversion
        assert "Monday through Friday" in result

    def test_config_timezone_fallback(self, monkeypatch) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": "America/New_York"})()),
        )
        s = CronSchedule(kind="cron", cron_expr="0 22 * * 1-5")
        result = format_schedule(s)
        # Expression is evaluated in job timezone (ET fallback), so 22:00 = 10 PM local
        assert "10:00 PM" in result
        assert "EDT" in result or "EST" in result
        assert "Monday through Friday" in result


class TestComputeNextRunTs:
    """Tests for compute_next_run_ts helper."""

    def test_every_schedule(self) -> None:
        now = 5000.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=300),
            created_ts=1000.0,
            last_run_ts=4800.0,
        )
        result = compute_next_run_ts(job, now=now)
        assert result == 5100.0

    def test_every_schedule_no_last_run(self) -> None:
        now = 5000.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=4970.0,
        )
        result = compute_next_run_ts(job, now=now)
        assert result == 5030.0

    def test_every_schedule_overdue_returns_now(self) -> None:
        now = 5000.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=1000.0,
            last_run_ts=1000.0,
        )
        result = compute_next_run_ts(job, now=now)
        assert result == now

    def test_at_schedule_future(self) -> None:
        now = 5000.0
        future_ts = 8600.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="at", at_ts=future_ts),
        )
        assert compute_next_run_ts(job, now=now) == future_ts

    def test_at_schedule_past_returns_none(self) -> None:
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="at", at_ts=1000.0),
        )
        assert compute_next_run_ts(job, now=5000.0) is None

    def test_cron_schedule(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        now = 1745000000.0  # 2025-04-18T18:13:20Z
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 12 * * *"),
        )
        result = compute_next_run_ts(job, now=now)
        # next "0 12 * * *" after 2025-04-18T18:13:20Z → 2025-04-19T12:00:00Z
        expected = datetime(2025, 4, 19, 12, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_disabled_job_returns_none(self) -> None:
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=300),
            enabled=False,
        )
        assert compute_next_run_ts(job, now=5000.0) is None

    def test_invalid_cron_expr_returns_none(self) -> None:
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="invalid"),
        )
        assert compute_next_run_ts(job, now=5000.0) is None

    def test_every_schedule_no_last_run_uses_created_ts_zero(self) -> None:
        """When last_run_ts is None and created_ts is 0.0 (default), uses 0.0 as base."""
        now = 5000.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=300),
            created_ts=0.0,
            last_run_ts=None,
        )
        # 0.0 + 300 = 300.0, which is < now, so returns now
        assert compute_next_run_ts(job, now=now) == now

    def test_at_schedule_exact_now_returns_none(self) -> None:
        """at_ts exactly equal to now is treated as expired."""
        now = 5000.0
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="at", at_ts=now),
        )
        assert compute_next_run_ts(job, now=now) is None


class TestTimezoneScheduling:
    """Tests for timezone-aware cron scheduling."""

    def test_job_tz_returns_zoneinfo(self) -> None:
        job = CronJob(id="j1", name="t", message="m", timezone="America/Toronto")
        tz = _job_tz(job)
        assert isinstance(tz, ZoneInfo)
        assert str(tz) == "America/Toronto"

    def test_job_tz_empty_returns_utc(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        job = CronJob(id="j1", name="t", message="m", timezone="")
        assert _job_tz(job) == ZoneInfo("UTC")

    def test_job_tz_invalid_falls_back_to_utc(self) -> None:
        job = CronJob(id="j1", name="t", message="m", timezone="Fake/Zone")
        assert _job_tz(job) == ZoneInfo("UTC")

    def test_compute_next_run_ts_with_timezone(self) -> None:
        """Job at 1pm Toronto should compute next fire at 17:00 UTC (EDT = UTC-4)."""
        # 2025-04-18T12:00:00 UTC = 2025-04-18T08:00:00 EDT
        # Next "0 13 * * *" in Toronto = 2025-04-18T13:00:00 EDT = 17:00:00 UTC
        now = datetime(2025, 4, 18, 12, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
            timezone="America/Toronto",
        )
        result = compute_next_run_ts(job, now=now)
        expected = datetime(2025, 4, 18, 17, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_compute_next_run_ts_no_timezone_stays_utc(self, monkeypatch) -> None:
        """Backward compat: no timezone means cron_expr evaluated as UTC."""
        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        now = datetime(2025, 4, 18, 12, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
        )
        result = compute_next_run_ts(job, now=now)
        expected = datetime(2025, 4, 18, 13, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_is_due_respects_timezone(self) -> None:
        """Job at 1pm Toronto should be due at 17:00 UTC, not 13:00 UTC."""
        # 17:00 UTC = 13:00 EDT → should match "0 13 * * *" in Toronto
        now_due = datetime(2025, 4, 18, 17, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
            timezone="America/Toronto",
        )
        assert CronService._is_due(job, now_due) is True

    def test_is_due_not_due_at_utc_time(self) -> None:
        """Job at 1pm Toronto should NOT be due at 13:00 UTC (= 9am EDT)."""
        now_not_due = datetime(2025, 4, 18, 13, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
            timezone="America/Toronto",
        )
        assert CronService._is_due(job, now_not_due) is False

    def test_is_due_no_timezone_fires_at_utc(self, monkeypatch) -> None:
        """Backward compat: no timezone fires at UTC time."""
        monkeypatch.setattr(
            "kiro_crew.cron.KiroCrewConfig.load",
            staticmethod(lambda: type("C", (), {"timezone": ""})()),
        )
        now = datetime(2025, 4, 18, 13, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
        )
        assert CronService._is_due(job, now) is True

    def test_is_due_dedup_uses_utc_minute(self) -> None:
        """Same UTC minute should be deduped regardless of timezone."""
        now = datetime(2025, 4, 18, 17, 0, 30, tzinfo=timezone.utc).timestamp()
        last = datetime(2025, 4, 18, 17, 0, 5, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="0 13 * * *"),
            timezone="America/Toronto",
            last_run_ts=last,
        )
        # Same UTC minute (both timestamps in 17:00 UTC), should be deduped
        assert CronService._is_due(job, now) is False

    def test_is_due_spring_forward_skipped_hour(self) -> None:
        """During spring forward, a job targeting the skipped hour still fires.

        2025-03-09: Toronto clocks jump 2:00 AM EST -> 3:00 AM EDT at 07:00 UTC,
        so the wall-clock 2:30 AM never occurs. The invariant we care about is
        that the daily job is NOT silently lost for the day: it still fires, in
        the resumed hour, and never before the jump. We assert that invariant
        rather than the exact resolved instant, because the precise UTC minute(s)
        croniter maps the skipped wall-time to are croniter-version-specific
        (e.g. 2.0.7 matches a two-minute window at 07:29-07:30 UTC).
        """
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="30 2 * * *"),
            timezone="America/Toronto",
        )
        # Scan every UTC minute across the spring-forward window (01:00-04:00
        # local) and collect the minutes the job is due.
        window_start = datetime(2025, 3, 9, 6, 0, tzinfo=timezone.utc)
        jump_utc = datetime(2025, 3, 9, 7, 0, tzinfo=timezone.utc).timestamp()
        resume_end = datetime(2025, 3, 9, 8, 0, tzinfo=timezone.utc).timestamp()
        fires = [
            ts
            for i in range(180)
            if CronService._is_due(job, (ts := (window_start.timestamp() + i * 60)))
        ]
        # Not silently skipped — it fires at least once on the DST day.
        assert fires, "daily job in the skipped DST hour must still fire"
        # Every fire lands in the resumed hour [03:00, 04:00) EDT, i.e. at/after
        # the jump and within the first resumed hour — never at the vanished
        # pre-jump wall-clock time.
        assert all(jump_utc <= ts < resume_end for ts in fires)

    def test_is_due_normal_day_fires_exactly_once(self) -> None:
        """On a non-DST day a daily cron job is due in exactly one UTC minute."""
        job = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="cron", cron_expr="30 2 * * *"),
            timezone="America/Toronto",
        )
        window_start = datetime(2025, 3, 10, 6, 0, tzinfo=timezone.utc)
        fires = [
            i
            for i in range(180)
            if CronService._is_due(job, window_start.timestamp() + i * 60)
        ]
        assert len(fires) == 1


class TestGetJob:
    """CronService.get_job(job_id) returns the CronJob by id."""

    def test_get_job_by_id(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="findme", message="go", every_secs=300)
        found = svc.get_job(job.id)
        assert found is not None
        assert found.id == job.id
        assert found.name == "findme"

    def test_get_job_unknown_id_returns_none(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="other", message="go", every_secs=300)
        assert svc.get_job("does-not-exist") is None
