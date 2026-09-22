"""Tests for the cron service."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from kiro_crew.cron import (
    _TERMINAL_OWED_CLEAR_RETRY_DELAYS,
    _TIMER_POLL_SECS,
    CronJob,
    CronSchedule,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
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
        # Feb 16 is Monday, Feb 15 is Sunday
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

    def test_add_job_enabled_false_registers_paused(self, tmp_path: Path) -> None:
        """enabled=False creates the job paused (user_paused=True) at creation."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(
            name="shipped-disabled",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        )
        assert job.enabled is False
        assert job.user_paused is True
        # A fresh service reloading the store must also see it paused.
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        loaded = [j for j in svc2.list_jobs(include_disabled=True) if j.id == job.id]
        assert loaded and loaded[0].enabled is False

    def test_add_job_enabled_false_never_persisted_enabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The paused state is part of the FIRST persist — no save may ever
        capture the disabled-by-manifest job in an enabled state (a crash or a
        concurrent store reader between an enabled-then-paused save pair would
        make the wrong state permanent via the startup skip-by-name)."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        snapshots: list[tuple[bool, bool]] = []
        real_save = svc._save

        def spy_save(*a, **k):
            for j in svc._jobs:
                if j.name == "shipped-disabled":
                    snapshots.append((j.enabled, j.user_paused))
            return real_save(*a, **k)

        monkeypatch.setattr(svc, "_save", spy_save)
        svc.add_job(
            name="shipped-disabled",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        )
        assert snapshots, "add_job must persist the new job"
        assert all(
            s == (False, True) for s in snapshots
        ), f"a save captured the job enabled: {snapshots}"

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
        assert svc.remove_job(job.id, actor="test", source="test")
        assert not svc.remove_job("nonexistent", actor="test", source="test")

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

    def test_load_invalid_utf8_is_reported_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid UTF-8 must degrade to an empty store, not abort the caller.

        ``json.loads`` on bytes raises ``UnicodeDecodeError``, which is a
        SIBLING subclass of ``ValueError`` rather than an ancestor of
        ``json.JSONDecodeError`` — so a decode-error-only handler lets it
        escape into ``_sync`` and gateway startup.
        """
        (tmp_path / "crons.json").write_bytes(b'{"jobs": [], "note": "\xff\xfe\xfd"}')
        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs() == []
        assert "Failed to load cron store" in caplog.text

    def test_load_deeply_nested_is_reported_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deeply nested JSON must degrade to an empty store, not abort the caller.

        ``RecursionError`` subclasses ``RuntimeError``, NOT ``ValueError``, so
        it escapes a decode-error tuple entirely.
        """
        depth = 100_000
        (tmp_path / "crons.json").write_bytes(b"[" * depth + b"]" * depth)
        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs() == []
        assert "Failed to load cron store" in caplog.text

    def test_load_directory_at_store_path_is_reported_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A directory where crons.json belongs must degrade, not abort startup.

        ``exists()`` is True for a directory, so the fresh-install early return
        does not fire and ``read_bytes()`` raises ``IsADirectoryError``. ``_sync``
        guards its own read, but the constructor's ``_load()`` — and so gateway
        startup — does not.
        """
        (tmp_path / "crons.json").mkdir()
        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs() == []
        assert "Failed to load cron store" in caplog.text

    def test_load_absent_store_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing crons.json is the fresh-install case: no fault, no warning."""
        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs() == []
        assert "Failed to load cron store" not in caplog.text

    def test_load_honestly_empty_store_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty-but-valid store is not a fault either."""
        (tmp_path / "crons.json").write_text('{"jobs": []}')
        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
        assert svc.list_jobs() == []
        assert "Failed to load cron store" not in caplog.text

    # ── an unloadable store must not be OVERWRITTEN by a later mutation ──

    @staticmethod
    def _keeper_record() -> dict:
        """One real, loadable job record that proves survival on disk."""
        return {
            "id": "j-keep",
            "name": "keep-me",
            "message": "m",
            "schedule": {"kind": "every", "every_secs": 3600},
            "enabled": True,
        }

    def test_mutation_after_invalid_utf8_load_does_not_overwrite_the_store(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed load must REFUSE to be persisted over.

        ``_load`` degrades an unreadable store to ``self._jobs = []``, which is
        indistinguishable to every later writer from an honestly empty store.
        ``_save()`` serialises ``self._jobs`` wholesale, so one mutation after a
        failed load persists the empty list over a store that still held jobs.
        """
        path = tmp_path / "crons.json"
        body = {"version": 2, "jobs": [self._keeper_record()], "note": "XX"}
        path.write_bytes(json.dumps(body).encode("utf-8").replace(b"XX", b"\xff\xfe"))
        before = path.read_bytes()

        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
            with pytest.raises(CronStoreUnreadable):
                svc.add_job(name="new-job", message="m", every_secs=300)

        after = path.read_bytes()
        assert b'"j-keep"' in after, "the pre-existing job was erased from disk"
        assert after == before, "the unloadable store was overwritten"

    def test_mutation_after_deeply_nested_load_does_not_overwrite_the_store(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same guarantee for the ``RecursionError`` corruption class."""
        path = tmp_path / "crons.json"
        depth = 50_000
        head = json.dumps({"version": 2, "jobs": [self._keeper_record()]})[:-1]
        path.write_bytes((head + ',"deep":' + "[" * depth + "]" * depth + "}").encode("utf-8"))
        before = path.read_bytes()

        with caplog.at_level(logging.WARNING):
            svc = CronService(base_dir=tmp_path)
            with pytest.raises(CronStoreUnreadable):
                svc.add_job(name="new-job", message="m", every_secs=300)

        after = path.read_bytes()
        assert b'"j-keep"' in after, "the pre-existing job was erased from disk"
        assert after == before, "the unloadable store was overwritten"

    def test_a_background_writer_degrades_instead_of_crashing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raise must reach USER mutations only, never the scheduler loop.

        `_save` raising is what stops a mutation reporting false success, but a
        corrupt store must not abort the reaper, the due-scan or the job runner.
        `_merge_job_result` runs after a job has already executed, so it catches
        and degrades: the run result is lost, which beats clobbering the store.
        """
        path = tmp_path / "crons.json"
        body = {"version": 2, "jobs": [self._keeper_record()], "note": "XX"}
        path.write_bytes(json.dumps(body).encode("utf-8").replace(b"XX", b"\xff\xfe"))
        before = path.read_bytes()
        svc = CronService(base_dir=tmp_path)
        job = CronJob(id="j-bg", name="bg", message="m")

        with caplog.at_level(logging.WARNING):
            svc._merge_job_result(job)  # must NOT raise

        assert "not persisted" in caplog.text
        assert path.read_bytes() == before, "the unloadable store was overwritten"

    def test_mutation_on_a_fresh_install_still_saves(self, tmp_path: Path) -> None:
        """NC2. A missing store is not a load failure: the write must go through.

        Blocking this would be worse than the defect -- every fresh install
        would be unable to create its first cron job.
        """
        assert not (tmp_path / "crons.json").exists()
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="first", message="m", every_secs=300)

        assert (tmp_path / "crons.json").exists()
        assert b'"first"' in (tmp_path / "crons.json").read_bytes()
        assert [j.name for j in CronService(base_dir=tmp_path).list_jobs()] == ["first"]

    def test_mutation_on_an_honestly_empty_store_still_saves(self, tmp_path: Path) -> None:
        """NC2, other half. ``{"jobs": []}`` loads fine, so it stays writable."""
        (tmp_path / "crons.json").write_text('{"jobs": []}', encoding="utf-8")
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="first", message="m", every_secs=300)

        assert [j.name for j in CronService(base_dir=tmp_path).list_jobs()] == ["first"]

    def test_a_repaired_store_becomes_writable_again(self, tmp_path: Path) -> None:
        """NC2, third half. The refusal must not latch.

        A guard that survives the repair would leave the store permanently
        unwritable -- the same class of harm as blocking a fresh install.
        """
        path = tmp_path / "crons.json"
        path.write_bytes(b'{"jobs": [], "note": "\xff\xfe"}')
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(CronStoreUnreadable):
            svc.add_job(name="refused", message="m", every_secs=300)
        assert b'"refused"' not in path.read_bytes(), "the write should have been refused"

        path.write_text('{"jobs": []}', encoding="utf-8")
        svc._load()
        svc.add_job(name="accepted", message="m", every_secs=300)

        assert [j.name for j in CronService(base_dir=tmp_path).list_jobs()] == ["accepted"]

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


class TestUpdateDurableOwedOccurrence:
    """Schedule/calendar edits revalidate only canonical persisted debt."""

    _OCCURRENCE_TS = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()

    @classmethod
    def _service(cls, tmp_path: Path, occurrence_id: str | None = None):
        owed = occurrence_id or str(int(cls._OCCURRENCE_TS) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="UTC",
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=owed,
        )
        svc._jobs = [job]
        svc._save()
        return svc, job, owed

    @pytest.mark.parametrize(
        "update",
        [
            {"cron_expr": "*/5 6 * * *"},
            {"timezone": "UTC"},
            {"skip_dates": []},
        ],
        ids=["cron-expression", "timezone", "skip-dates"],
    )
    def test_schedule_edit_retains_still_current_canonical_debt(
        self, tmp_path: Path, update: dict[str, object]
    ) -> None:
        svc, job, occurrence_id = self._service(tmp_path)

        updated = svc.update_job(job.id, **update)

        assert updated is not None
        assert updated.owed_occurrence() == occurrence_id
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.parametrize("paused", [False, True], ids=["enabled", "paused"])
    def test_unchanged_calendar_payload_preserves_debt_despite_dispatch_state(
        self, tmp_path: Path, paused: bool
    ) -> None:
        svc, job, occurrence_id = self._service(tmp_path)
        job.last_run_ts = self._OCCURRENCE_TS + 120
        job.user_paused = paused
        job.enabled = not paused
        svc._save()

        with patch.object(
            svc,
            "_occurrence_matches_calendar",
            wraps=svc._occurrence_matches_calendar,
        ) as calendar_check:
            updated = svc.update_job(
                job.id,
                cron_expr="0 6 * * *",
                timezone="UTC",
                skip_dates=[],
            )

        calendar_check.assert_not_called()
        assert updated is not None
        assert updated.owed_occurrence() == occurrence_id
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_actual_valid_calendar_change_ignores_pause_and_last_run(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path)
        job.last_run_ts = self._OCCURRENCE_TS + 120
        job.user_paused = True
        job.enabled = False
        svc._save()

        updated = svc.update_job(job.id, skip_dates=["2099-01-01"])

        assert updated is not None
        assert updated.skip_dates == ["2099-01-01"]
        assert updated.owed_occurrence() == occurrence_id

    @pytest.mark.parametrize(
        "update",
        [
            {"cron_expr": "1 6 * * *"},
            {"every_secs": 60},
            {"timezone": "America/Los_Angeles"},
            {"skip_dates": ["2026-01-02"]},
        ],
        ids=["cron-expression", "cron-to-every", "timezone", "skip-date"],
    )
    def test_schedule_edit_clears_obsolete_canonical_debt(
        self, tmp_path: Path, update: dict[str, object]
    ) -> None:
        svc, job, _occurrence_id = self._service(tmp_path)

        updated = svc.update_job(job.id, **update)

        assert updated is not None
        assert updated.owed_occurrence() is None
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    def test_name_only_update_retains_canonical_debt(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path)

        updated = svc.update_job(job.id, name="renamed")

        assert updated is not None
        assert updated.name == "renamed"
        assert updated.owed_occurrence() == occurrence_id

    @pytest.mark.parametrize("occurrence_id", ["legacy", "future-format"])
    def test_unknown_debt_is_retained_after_schedule_edit(
        self, tmp_path: Path, occurrence_id: str
    ) -> None:
        svc, job, _owed = self._service(tmp_path, occurrence_id)

        updated = svc.update_job(job.id, every_secs=60)

        assert updated is not None
        assert updated.owed_occurrence() == occurrence_id

    def test_user_pause_retains_canonical_debt(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path)

        assert svc.enable_job(job.id, False) is True

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.parametrize(
        "update",
        [
            {"name": "changed", "cron_expr": "not a cron"},
            {"name": "changed", "timezone": "Not/AZone"},
            {"name": "changed", "skip_dates": ["2026-02-30"]},
            {"name": "changed", "cron_expr": "* * * * *", "every_secs": 60},
        ],
        ids=["cron-expression", "timezone", "skip-date", "conflicting-schedule"],
    )
    def test_rejected_update_preserves_fields_and_debt(
        self, tmp_path: Path, update: dict[str, object]
    ) -> None:
        svc, job, occurrence_id = self._service(tmp_path)
        original_schedule = job.schedule

        with pytest.raises(ValueError):
            svc.update_job(job.id, **update)

        current = svc.get_job(job.id)
        assert current is job
        assert current.name == "daily"
        assert current.schedule == original_schedule
        assert current.timezone == "UTC"
        assert current.skip_dates == []
        assert current.owed_occurrence() == occurrence_id
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.name == "daily"
        assert stored.schedule == original_schedule
        assert stored.timezone == "UTC"
        assert stored.skip_dates == []
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.parametrize(
        "failure_kind",
        ["pre-commit", "commit-then-raise"],
    )
    def test_schedule_save_failure_reloads_authoritative_calendar_and_debt(
        self, tmp_path: Path, failure_kind: str
    ) -> None:
        svc, job, occurrence_id = self._service(tmp_path)
        old_schedule = job.schedule
        new_schedule = CronSchedule(kind="cron", cron_expr="1 6 * * *")
        real_save = svc._save

        def fail_save() -> None:
            if failure_kind == "commit-then-raise":
                real_save()
            raise OSError(failure_kind)

        with (
            patch.object(svc, "_save", side_effect=fail_save),
            pytest.raises(OSError, match=failure_kind),
        ):
            svc.update_job(job.id, cron_expr="1 6 * * *")

        svc._sync()
        current = svc.get_job(job.id)
        assert current is not None
        expected_schedule = new_schedule if failure_kind == "commit-then-raise" else old_schedule
        expected_owed = None if failure_kind == "commit-then-raise" else occurrence_id
        assert current.schedule == expected_schedule
        assert current.owed_occurrence() == expected_owed

        updated = svc.update_job(job.id, name="renamed")
        assert updated is not None
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.name == "renamed"
        assert stored.schedule == expected_schedule
        assert stored.owed_occurrence() == expected_owed


class TestInheritedDefaultOwedOccurrence:
    """Runtime default-timezone publication revalidates inherited debt."""

    _OCCURRENCE_TS = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()

    @classmethod
    def _service(
        cls,
        tmp_path: Path,
        *,
        timezone_name: str = "",
        cron_expr: str = "0 6 * * *",
        occurrence_id: str | None = None,
    ) -> tuple[CronService, CronJob, str]:
        owed = occurrence_id or str(int(cls._OCCURRENCE_TS) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr=cron_expr),
            timezone=timezone_name,
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=owed,
        )
        svc._jobs = [job]
        svc._save()
        return svc, job, owed

    @pytest.mark.asyncio
    async def test_inherited_default_change_clears_invalid_debt_without_dispatch(
        self, tmp_path: Path
    ) -> None:
        svc, job, _occurrence_id = self._service(tmp_path)
        payloads: list[str] = []

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        svc._on_job = payload
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        with (
            patch(
                "kiro_crew.cron.published_config_timezone",
                return_value="America/Los_Angeles",
            ),
            patch(
                "kiro_crew.cron.time.time",
                return_value=self._OCCURRENCE_TS + 120,
            ),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
        ):
            await svc._on_timer()
            tasks = list(svc._running_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)

        assert payloads == []
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    def test_default_change_preserves_debt_matching_both_calendars(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path, cron_expr="* * * * *")

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            snapshot = svc._tick_scan_locked()

        assert snapshot.inherited_owed_validated is True
        assert snapshot.config_timezone == "America/Los_Angeles"
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_explicit_timezone_debt_ignores_global_default(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path, timezone_name="UTC")

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            snapshot = svc._tick_scan_locked()

        assert snapshot.inherited_owed_validated is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_unknown_inherited_debt_is_retained(self, tmp_path: Path) -> None:
        svc, job, _occurrence_id = self._service(
            tmp_path,
            occurrence_id="future-format",
        )

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            snapshot = svc._tick_scan_locked()

        assert snapshot.inherited_owed_validated is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "future-format"

    def test_precommit_clear_failure_restores_debt_and_retries(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path)

        with (
            patch(
                "kiro_crew.cron.published_config_timezone",
                return_value="America/Los_Angeles",
            ),
            patch.object(svc, "_save", side_effect=OSError("before commit")),
        ):
            snapshot = svc._tick_scan_locked()

        assert snapshot.inherited_owed_validated is False
        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() == occurrence_id
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            retry = svc._tick_scan_locked()
        assert retry.inherited_owed_validated is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    def test_commit_then_raise_keeps_committed_clear(self, tmp_path: Path) -> None:
        svc, job, _occurrence_id = self._service(tmp_path)
        real_save = svc._save

        def commit_then_raise() -> None:
            real_save()
            raise OSError("after commit")

        with (
            patch(
                "kiro_crew.cron.published_config_timezone",
                return_value="America/Los_Angeles",
            ),
            patch.object(svc, "_save", side_effect=commit_then_raise),
        ):
            snapshot = svc._tick_scan_locked()

        assert snapshot.inherited_owed_validated is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    async def test_concurrent_invalid_to_valid_default_preserves_debt(self, tmp_path: Path) -> None:
        occurrence_ts = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(occurrence_ts) // 60)
        svc, job, _owed = self._service(tmp_path, occurrence_id=occurrence_id)
        current = {"timezone": "UTC"}
        validation_entered = threading.Event()
        release_validation = threading.Event()
        blocked_once = False
        real_matches = svc._occurrence_matches_calendar

        def blocked_matches(*args, **kwargs):
            nonlocal blocked_once
            if not blocked_once and kwargs.get("default_timezone") == "UTC":
                blocked_once = True
                validation_entered.set()
                assert release_validation.wait(5), "calendar validation was not released"
            return real_matches(*args, **kwargs)

        scan_task: asyncio.Task[list[CronJob]] | None = None
        try:
            with (
                patch(
                    "kiro_crew.cron.published_config_timezone",
                    side_effect=lambda: current["timezone"],
                ),
                patch.object(
                    svc,
                    "_occurrence_matches_calendar",
                    side_effect=blocked_matches,
                ),
            ):
                scan_task = asyncio.create_task(asyncio.to_thread(svc._tick_scan_locked))
                assert await asyncio.to_thread(validation_entered.wait, 2)
                current["timezone"] = "America/Los_Angeles"
                release_validation.set()
                snapshot = await asyncio.wait_for(scan_task, 2)
        finally:
            release_validation.set()
            if scan_task is not None and not scan_task.done():
                scan_task.cancel()
                await asyncio.gather(scan_task, return_exceptions=True)

        assert snapshot.inherited_owed_validated is True
        assert snapshot.config_timezone == "America/Los_Angeles"
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_publication_change_during_clear_commit_restores_debt(self, tmp_path: Path) -> None:
        occurrence_ts = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(occurrence_ts) // 60)
        svc, job, _owed = self._service(tmp_path, occurrence_id=occurrence_id)
        current = {"timezone": "UTC"}
        real_save = svc._save
        save_calls = 0

        def publish_then_save() -> None:
            nonlocal save_calls
            save_calls += 1
            current["timezone"] = "America/Los_Angeles"
            real_save()

        with (
            patch(
                "kiro_crew.cron.published_config_timezone",
                side_effect=lambda: current["timezone"],
            ),
            patch.object(svc, "_save", side_effect=publish_then_save),
        ):
            snapshot = svc._tick_scan_locked()

        assert save_calls == 2
        assert snapshot.inherited_owed_validated is False
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_timezone_aba_during_validation_preserves_debt(self, tmp_path: Path) -> None:
        occurrence_ts = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(occurrence_ts) // 60)
        svc, job, _owed = self._service(tmp_path, occurrence_id=occurrence_id)
        stable = ("UTC", 41)
        after_aba = ("UTC", 43)

        with (
            patch(
                "kiro_crew.cron._published_timezone_authority",
                side_effect=[stable, stable, after_aba, after_aba],
            ),
            patch.object(svc, "_save", wraps=svc._save) as save,
        ):
            snapshot = svc._tick_scan_locked()

        save.assert_not_called()
        assert snapshot.inherited_owed_validated is False
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    def test_timezone_aba_during_clear_commit_restores_debt(self, tmp_path: Path) -> None:
        occurrence_ts = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(occurrence_ts) // 60)
        svc, job, _owed = self._service(tmp_path, occurrence_id=occurrence_id)
        stable = ("UTC", 51)
        after_aba = ("UTC", 53)

        with (
            patch(
                "kiro_crew.cron._published_timezone_authority",
                side_effect=[stable, stable, stable, after_aba],
            ),
            patch.object(svc, "_save", wraps=svc._save) as save,
        ):
            snapshot = svc._tick_scan_locked()

        assert save.call_count == 2
        assert snapshot.inherited_owed_validated is False
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_concurrent_default_publication_cannot_dispatch_old_debt(
        self, tmp_path: Path
    ) -> None:
        svc, job, _occurrence_id = self._service(tmp_path)
        current = {"timezone": "UTC"}
        validation_entered = threading.Event()
        release_validation = threading.Event()
        blocked_once = False
        payloads: list[str] = []
        real_matches = svc._occurrence_matches_calendar

        def blocked_matches(*args, **kwargs):
            nonlocal blocked_once
            if not blocked_once and kwargs.get("default_timezone") == "UTC":
                blocked_once = True
                validation_entered.set()
                assert release_validation.wait(5), "calendar validation was not released"
            return real_matches(*args, **kwargs)

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        svc._on_job = payload
        timer_task: asyncio.Task[None] | None = None
        try:
            with (
                patch(
                    "kiro_crew.cron.published_config_timezone",
                    side_effect=lambda: current["timezone"],
                ),
                patch.object(
                    svc,
                    "_occurrence_matches_calendar",
                    side_effect=blocked_matches,
                ),
                patch(
                    "kiro_crew.cron.time.time",
                    return_value=self._OCCURRENCE_TS + 120,
                ),
                patch("kiro_crew.cron.admission_check", return_value=admitted),
            ):
                timer_task = asyncio.create_task(svc._on_timer())
                assert await asyncio.to_thread(validation_entered.wait, 2)
                current["timezone"] = "America/Los_Angeles"
                release_validation.set()
                await asyncio.wait_for(timer_task, 2)
        finally:
            release_validation.set()
            if timer_task is not None and not timer_task.done():
                timer_task.cancel()
                await asyncio.gather(timer_task, return_exceptions=True)

        assert payloads == []
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    async def test_timezone_aba_around_token_mint_blocks_dispatch(self, tmp_path: Path) -> None:
        svc, job, occurrence_id = self._service(tmp_path)
        current = {"authority": ("UTC", 61)}
        payloads: list[str] = []
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        real_mint = svc._mint_run_token

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def publish_aba_after_mint(job_id: str) -> object:
            token = real_mint(job_id)
            current["authority"] = ("UTC", 63)
            return token

        svc._on_job = payload
        with (
            patch(
                "kiro_crew.cron._published_timezone_authority",
                side_effect=lambda: current["authority"],
            ),
            patch.object(svc, "_mint_run_token", side_effect=publish_aba_after_mint),
            patch(
                "kiro_crew.cron.time.time",
                return_value=self._OCCURRENCE_TS + 120,
            ),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
        ):
            await asyncio.wait_for(svc._on_timer(), 2)

        assert payloads == []
        assert job.id not in svc._run_tokens
        assert job.id not in svc._executing
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id


class TestLastResultTimestamp:
    """``last_result_ts`` identifies WHICH run produced ``last_result``.

    The dashboard injection stamps its rows from this field, so every injection
    site for one run renders byte-identical content (keeping ``/to-chat``
    idempotent against the executor's auto-inject) while two different runs stay
    two distinct rows instead of collapsing into one undated pile.
    """

    def test_set_run_result_stamps_the_run(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="stamped", message="go", every_secs=300)
        assert job.last_result_ts == 0.0
        before = time.time()
        job.set_run_result("output")
        assert job.last_result_ts >= before

    def test_stamp_persists(self, tmp_path: Path) -> None:
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        job = svc1.add_job(name="stamped", message="go", every_secs=300)
        job.set_run_result("output")
        stamped_at = job.last_result_ts
        rendered = job.last_result_stamp
        svc1._save()

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        # A later /to-chat re-surfacing reads the job back from disk and must
        # reproduce the same stamp the run's own injection used.
        reloaded = svc2.list_jobs()[0]
        assert reloaded.last_result_ts == stamped_at
        assert reloaded.last_result_stamp == rendered

    def test_merge_job_result_persists_the_stamp(self, tmp_path: Path) -> None:
        """The run-result merge is the writer a real run goes through.

        ``_merge_job_result`` ``_sync()``s first, so it copies field by field
        onto a RELOADED job object rather than saving the in-memory one. A stamp
        left out of that copy list was never persisted for the run that produced
        it: the disk record paired the new result with a PREVIOUS run's stamp, so
        after a reload ``/to-chat`` rendered a header the executor never wrote
        and ``append_if_absent`` appended a duplicate instead of collapsing onto
        the existing row.
        """
        svc1 = CronService(base_dir=tmp_path)
        svc1._load()
        job = svc1.add_job(name="stamped", message="go", every_secs=300)
        job.set_run_result("output")
        svc1._merge_job_result(job)

        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        merged = svc2.list_jobs()[0]
        assert merged.last_result == "output"
        assert merged.last_result_ts == job.last_result_ts
        assert merged.last_result_stamp == job.last_result_stamp

    def test_stamp_is_rendered_in_the_job_timezone_to_the_second(self) -> None:
        """The rendered stamp is a snapshot, and it resolves to seconds.

        Resolution is load-bearing rather than cosmetic: the stamp sits inside
        the row content the dedup compares, so anything coarser merges two runs
        that finished within the same interval.
        """
        job = CronJob(
            id="tz1", name="tz", message="go", schedule=CronSchedule(kind="every", every_secs=300)
        )
        job.timezone = "UTC"
        job.set_run_result("output")
        assert job.last_result_stamp.startswith(" | ")
        # ' | YYYY-MM-DD HH:MM:SS UTC'
        assert job.last_result_stamp.endswith("UTC")
        stamped = job.last_result_stamp[len(" | ") : -len(" UTC")]
        datetime.strptime(stamped, "%Y-%m-%d %H:%M:%S")

    def test_an_unknown_timezone_still_renders_via_the_utc_fallback(self) -> None:
        """An unresolvable zone is ``_job_tz``'s own fallback, not an error.

        It resolves job zone -> config zone -> UTC, so a typo'd zone yields a UTC
        stamp rather than raising. Asserted here because the value is what later
        rows dedup against: a run must not lose its stamp over a config typo.
        """
        job = CronJob(
            id="tz2",
            name="tz",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=300),
        )
        job.timezone = "Not/AZone"
        job.set_run_result("output")
        assert job.last_result == "output"
        assert job.last_result_stamp.endswith("UTC")

    def test_an_unrenderable_epoch_degrades_to_no_stamp(self) -> None:
        """A stamp is display-only: rendering it must never fail the run.

        Falling back to the UNSTAMPED header is deliberate -- that is the
        spelling a legacy row already carries, so the dedup stays coherent
        instead of gaining a third variant of the same row.
        """
        job = CronJob(
            id="tz3",
            name="tz",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=300),
        )
        # Beyond what the platform can turn into a date, which is what the
        # renderer's except branch exists for.
        assert job._render_run_stamp(1e300) == ""

    def test_missing_in_json_defaults_zero(self, tmp_path: Path) -> None:
        """A store written by an older build carries a result but no stamp.

        Zero means "unknown" and renders the pre-stamp header, so a row already
        on disk still dedups against its historical spelling instead of being
        re-appended beside a stamped twin.
        """
        data = {
            "version": 2,
            "jobs": [
                {
                    "id": "abc123",
                    "name": "legacy",
                    "message": "hi",
                    "schedule": {"kind": "every", "every_secs": 300},
                    "last_result": "from an older build",
                }
            ],
        }
        (tmp_path / "crons.json").write_text(json.dumps(data))
        svc = CronService(base_dir=tmp_path)
        svc._load()
        loaded = svc.list_jobs()[0]
        assert loaded.last_result == "from an older build"
        assert loaded.last_result_ts == 0.0
        assert loaded.last_result_stamp == ""

    def test_clear_carried_result_does_not_stamp(self, tmp_path: Path) -> None:
        """Clearing a carried result is not a run producing one."""
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="stamped", message="go", every_secs=300)
        job.last_result = "previous run's output"
        job.clear_carried_result()
        assert job.last_result == ""
        assert job.last_result_ts == 0.0
        assert job.last_result_stamp == ""


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


class TestJobCompletionRearmsTimer:
    """A job that ran for most of its interval must not have to wait out a
    stale wake (up to _TIMER_POLL_SECS) before its next tick is dispatched:
    completion re-arms the timer with the job's real next-due delay."""

    @pytest.mark.asyncio
    async def test_run_job_isolated_rearms_the_timer(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        svc._jobs = [job]
        svc._save()
        svc._running = True

        with (
            patch.object(svc, "_execute_with_timeout", return_value=None),
            patch.object(svc, "_arm_timer") as mock_arm,
        ):
            await svc._run_job_isolated(job)

        mock_arm.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [CronStoreBusy("busy"), OSError("disk full")],
        ids=["store-busy", "save-oserror"],
    )
    async def test_result_merge_failure_releases_claim_and_rearms(
        self, tmp_path: Path, failure: Exception
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        svc._jobs = [job]
        svc._save()
        svc._running = True
        svc._executing.add(job.id)

        with (
            patch.object(svc, "_execute_with_timeout", return_value=None),
            patch.object(svc, "_merge_job_result", side_effect=failure),
            patch.object(svc, "_arm_timer") as mock_arm,
        ):
            await svc._run_job_isolated(job)

        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        mock_arm.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_job_isolated_does_not_rearm_a_stopped_service(self, tmp_path: Path) -> None:
        """A job finishing during/after shutdown must not spin up a fresh
        timer task behind close_all()'s back."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        svc._jobs = [job]
        svc._save()
        svc._running = False

        with (
            patch.object(svc, "_execute_with_timeout", return_value=None),
            patch.object(svc, "_arm_timer") as mock_arm,
        ):
            await svc._run_job_isolated(job)

        mock_arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_job_replaces_a_longer_sleeping_timer_task(
        self, tmp_path: Path
    ) -> None:
        """_arm_timer() must run on job completion, or a job that becomes due
        again sooner than the CURRENTLY armed (long) sleep waits out that stale
        wake -- up to _TIMER_POLL_SECS late. Simulates that exact
        situation: a timer task already sleeping for a long time is armed
        when the job finishes; completion must cancel it and arm a fresh,
        shorter one instead of leaving the stale one in place."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        svc._jobs = [job]
        svc._save()
        svc._running = True
        svc._loop = asyncio.get_running_loop()

        async def _sleep_forever() -> None:
            await asyncio.sleep(9999)

        stale_timer_task = asyncio.create_task(_sleep_forever())
        svc._timer_task = stale_timer_task
        await asyncio.sleep(0)  # let it actually start sleeping

        with patch.object(svc, "_execute_with_timeout", return_value=None):
            await svc._run_job_isolated(job)
        await asyncio.sleep(0)  # let the cancellation propagate

        assert stale_timer_task.cancelled()
        assert svc._timer_task is not None
        assert svc._timer_task is not stale_timer_task

        svc._timer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await svc._timer_task


class TestArmTimerDuringOnTimer:
    """_arm_timer(), called from a job's own completion handler, must not
    cancel self._timer_task while _on_timer is still mid-sweep on it (the
    yield at its own to_thread scan) -- that's a DIFFERENT task calling in
    than the timer's own, so the pre-existing self-referential guard alone
    doesn't cover it. See _arm_timer's second guard clause."""

    @pytest.mark.asyncio
    async def test_arm_timer_does_not_cancel_the_timer_task_mid_sweep(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        svc._loop = asyncio.get_running_loop()

        async def _sleep_forever() -> None:
            await asyncio.sleep(9999)

        fake_timer_task = asyncio.create_task(_sleep_forever())
        svc._timer_task = fake_timer_task
        svc._on_timer_running = True
        try:
            svc._arm_timer()  # called from THIS task, not svc._timer_task
            await asyncio.sleep(0)
            assert not fake_timer_task.cancelled()
            assert not fake_timer_task.done()
            assert svc._timer_task is fake_timer_task
        finally:
            svc._on_timer_running = False
            fake_timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fake_timer_task

    @pytest.mark.asyncio
    async def test_arm_timer_still_replaces_the_task_once_the_sweep_is_done(
        self, tmp_path: Path
    ) -> None:
        """The guard is scoped to the sweep window only -- once _on_timer
        has returned (the common case: the timer task is just sleeping,
        not mid-dispatch), a completion-triggered re-arm still cancels and
        replaces it immediately, which is the actual fix for the reported
        lateness."""
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        svc._loop = asyncio.get_running_loop()

        async def _sleep_forever() -> None:
            await asyncio.sleep(9999)

        fake_timer_task = asyncio.create_task(_sleep_forever())
        svc._timer_task = fake_timer_task
        svc._on_timer_running = False
        try:
            svc._arm_timer()
            await asyncio.sleep(0)
            assert fake_timer_task.cancelled()
            assert svc._timer_task is not fake_timer_task
        finally:
            if svc._timer_task and not svc._timer_task.done():
                svc._timer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await svc._timer_task


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

    @pytest.mark.parametrize(
        ("every_secs", "expected"),
        [
            (60, "every 1m"),
            (90, "every 90s"),
            (300, "every 5m"),
            (3599, "every 3599s"),
            (3600, "every 1h"),
            (3601, "every 3601s"),
            (3660, "every 61m"),
            (5400, "every 90m"),
            (5401, "every 5401s"),
            (7200, "every 2h"),
            (9000, "every 150m"),
        ],
    )
    def test_every_preserves_interval(self, every_secs: int, expected: str) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        s = CronSchedule(kind="every", every_secs=every_secs)
        assert format_schedule(s) == expected

    def test_at_timestamp_today(self, monkeypatch, _utc_tz) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        # Mock "now" to Apr 10, job at 3PM same day
        fake_now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        # Mock only covers now() and fromtimestamp() — extend if format_schedule evolves.
        monkeypatch.setattr(
            "kiro_crew.cron.datetime",
            type(
                "D",
                (datetime,),
                {
                    "now": classmethod(lambda cls, tz=None: fake_now),
                    "fromtimestamp": staticmethod(
                        lambda ts, tz=None: datetime.fromtimestamp(ts, tz)
                    ),
                },
            ),
        )
        job_ts = datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc).timestamp()
        result = format_schedule(CronSchedule(kind="at", at_ts=job_ts))
        assert result.startswith("at ")
        assert "," not in result  # no date for today

    def test_at_timestamp_future_date(self, monkeypatch, _utc_tz) -> None:
        from kiro_crew.cron import CronSchedule, format_schedule

        # Mock "now" to Apr 10, job on Apr 17
        fake_now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        # Mock only covers now() and fromtimestamp() — extend if format_schedule evolves.
        monkeypatch.setattr(
            "kiro_crew.cron.datetime",
            type(
                "D",
                (datetime,),
                {
                    "now": classmethod(lambda cls, tz=None: fake_now),
                    "fromtimestamp": staticmethod(
                        lambda ts, tz=None: datetime.fromtimestamp(ts, tz)
                    ),
                },
            ),
        )
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
        """An omitted tz_name falls back to the PUBLISHED config default.

        Also pins that the fallback reads the snapshot rather than loading
        ``config.json``: that is what lets a loop-side caller omit tz_name at
        all, and it replaced a comment telling those callers to pass one.
        """
        from kiro_crew.cron import CronSchedule, format_schedule

        loads: list[int] = []

        def _record_load():
            loads.append(1)
            return type("C", (), {"timezone": "Bad/Zone"})()

        monkeypatch.setattr("kiro_crew.cron.KiroCrewConfig.load", staticmethod(_record_load))
        monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "America/New_York")
        s = CronSchedule(kind="cron", cron_expr="0 22 * * 1-5")
        result = format_schedule(s)
        # Expression is evaluated in job timezone (ET fallback), so 22:00 = 10 PM local
        assert "10:00 PM" in result
        assert "EDT" in result or "EST" in result
        assert "Monday through Friday" in result
        assert not loads, "format_schedule loaded config.json for its tz fallback"


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
        """No job zone and no published default resolves to UTC."""
        monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "")
        job = CronJob(id="j1", name="t", message="m", timezone="")
        assert _job_tz(job) == ZoneInfo("UTC")

    def test_job_tz_never_loads_the_config_file(self, monkeypatch) -> None:
        """Resolution reads the published snapshot, never ``config.json``.

        ``_job_tz`` runs on the event loop from two directions:
        ``CronService._on_timer``'s due-scan reaches it for EVERY
        cron-expression job on every tick, and ``CronJob.set_run_result``
        reaches it again when a completed run renders its stamp. A
        ``KiroCrewConfig.load()`` here stats and validates ``config.json`` on
        the loop, which ``no-blocking-call-on-event-loop`` forbids.

        Asserted on a recorded call rather than by raising from the fake:
        ``_job_tz`` catches ``Exception`` to degrade to UTC, so a raise would be
        swallowed and show up only as a wrong return value.
        """
        loads: list[int] = []

        def _record_load():
            loads.append(1)
            return type("C", (), {"timezone": "Bad/Zone"})()

        monkeypatch.setattr("kiro_crew.cron.KiroCrewConfig.load", staticmethod(_record_load))
        monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "America/Toronto")

        assert _job_tz(CronJob(id="j1", name="t", message="m", timezone="")) == ZoneInfo(
            "America/Toronto"
        )
        assert _job_tz(CronJob(id="j2", name="t", message="m", timezone="Asia/Tokyo")) == ZoneInfo(
            "Asia/Tokyo"
        )
        assert not loads, "_job_tz loaded config.json on the event loop"

    def test_get_local_tz_never_loads_the_config_file(self, monkeypatch) -> None:
        """Same rule, same reason: prompt assembly and the dashboard cron
        handler both reach ``get_local_tz`` from the event loop."""
        from kiro_crew.cron import get_local_tz

        loads: list[int] = []

        def _record_load():
            loads.append(1)
            return type("C", (), {"timezone": "Bad/Zone"})()

        monkeypatch.setattr("kiro_crew.cron.KiroCrewConfig.load", staticmethod(_record_load))
        monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "Asia/Tokyo")

        tz_name, tz = get_local_tz()
        assert tz_name == "Asia/Tokyo"
        assert tz == ZoneInfo("Asia/Tokyo")
        assert not loads, "get_local_tz loaded config.json on the event loop"

    def test_get_local_tz_unset_default_reads_as_utc(self, monkeypatch) -> None:
        """An unset default is not an error -- it resolves to UTC by name."""
        from kiro_crew.cron import get_local_tz

        monkeypatch.setattr("kiro_crew.cron.published_config_timezone", lambda: "")
        assert get_local_tz() == ("UTC", ZoneInfo("UTC"))

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

        On the spring-forward day, Toronto clocks jump 2:00 AM EST -> 3:00 AM EDT at 07:00 UTC,
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
            i for i in range(180) if CronService._is_due(job, window_start.timestamp() + i * 60)
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


class TestPrunedInstallCronService:
    """The scheduler side of the pruned-install / update-handoff contract:
    owed-fire make-up scheduling, keep-overdue schedule preservation, the
    quiesce registry, and one-shot retention."""

    @pytest.mark.parametrize(
        (
            "case",
            "job_timezone",
            "capture_authority",
            "live_authority",
            "completed",
            "newer_debt",
            "expected_status",
            "expected_owed_offset",
        ),
        [
            ("inherited-stable", "", ("UTC", 10), ("UTC", 10), True, False, "ok", None),
            (
                "inherited-a-to-b-complete",
                "",
                ("UTC", 10),
                ("America/Los_Angeles", 11),
                True,
                False,
                "ok",
                None,
            ),
            (
                "inherited-a-to-b-incomplete",
                "",
                ("UTC", 10),
                ("America/Los_Angeles", 11),
                False,
                False,
                "error",
                None,
            ),
            ("inherited-a-to-b-to-a", "", ("UTC", 10), ("UTC", 12), True, False, "ok", None),
            (
                "explicit-zone",
                "UTC",
                ("America/New_York", 10),
                ("America/Los_Angeles", 11),
                True,
                False,
                "ok",
                None,
            ),
            (
                "newer-replacement-debt",
                "",
                ("UTC", 10),
                ("America/Los_Angeles", 11),
                True,
                True,
                "ok",
                1,
            ),
        ],
        ids=[
            "inherited-stable",
            "inherited-a-to-b-complete",
            "inherited-a-to-b-incomplete",
            "inherited-a-to-b-to-a",
            "explicit-zone",
            "newer-replacement-debt",
        ],
    )
    def test_replacement_completion_uses_occurrence_capture_timezone(
        self,
        tmp_path: Path,
        case: str,
        job_timezone: str,
        capture_authority: tuple[str, int],
        live_authority: tuple[str, int],
        completed: bool,
        newer_debt: bool,
        expected_status: str,
        expected_owed_offset: int | None,
    ) -> None:
        """A drained run cannot reinterpret its occurrence under a later default."""
        occurrence_ts = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(occurrence_ts) // 60)
        svc = CronService(base_dir=tmp_path)
        running = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone=job_timezone,
            strict_schedule=True,
            last_status="error",
            last_error="stale drained runtime",
            last_run_ts=occurrence_ts + 60,
            run_never_started=True,
            keep_overdue=True,
        )
        svc._jobs = [running]
        svc._save()
        running.set_owed_occurrence(occurrence_id)
        svc._run_occurrence_ids[running.id] = occurrence_id
        svc._capture_run_occurrence_timezone_authority(
            running,
            occurrence_id,
            capture_authority,
        )

        replacement = CronService(base_dir=tmp_path)
        target = replacement.get_job(running.id)
        assert target is not None
        target.last_status = "ok"
        target.last_error = None
        target.last_run_ts = occurrence_ts if completed else occurrence_ts - 60
        if newer_debt:
            target.set_owed_occurrence(str(int(occurrence_id) + 1))
        replacement._save()

        with (
            patch("kiro_crew.cron._published_timezone_authority", return_value=live_authority),
            patch("kiro_crew.cron.published_config_timezone", return_value=live_authority[0]),
        ):
            svc._merge_job_result(running)

        stored = CronService(base_dir=tmp_path).get_job(running.id)
        assert stored is not None, case
        assert stored.last_status == expected_status, case
        expected_owed = (
            None if expected_owed_offset is None else str(int(occurrence_id) + expected_owed_offset)
        )
        assert stored.owed_occurrence() == expected_owed, case
        expected_capture = None if job_timezone else capture_authority
        assert svc._run_occurrence_timezone_authorities.get((running.id, occurrence_id)) == (
            expected_capture
        ), case

    @pytest.mark.asyncio
    async def test_scheduled_capture_rejects_a_stale_timezone_due_snapshot(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        calls: list[str] = []

        class Snapshot(list[CronJob]):
            config_timezone_authority = ("UTC", 10)
            inherited_owed_validated = True

        async def payload(running_job: CronJob) -> None:
            calls.append(running_job.id)

        svc._on_job = payload
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        with (
            patch.object(svc, "_tick_scan_locked", return_value=Snapshot([job])),
            patch("kiro_crew.cron.time.time", return_value=now),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
            patch("kiro_crew.cron.published_config_timezone", return_value="UTC"),
            patch(
                "kiro_crew.cron._published_timezone_authority",
                return_value=("America/Los_Angeles", 11),
            ),
        ):
            await svc._on_timer()

        assert calls == []
        assert job.id not in svc._run_tokens
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_occurrence_ids
        assert svc._run_occurrence_timezone_authorities == {}

    @pytest.mark.asyncio
    async def test_owed_fire_makes_a_cron_job_due_off_minute_and_is_consumed(
        self, tmp_path: Path
    ) -> None:
        """An owed occurrence is due regardless of the current minute, and the
        make-up run consumes the marker exactly once."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            # A minute that is (almost surely) not now; owed_fire overrides it.
            schedule=CronSchedule(kind="cron", cron_expr="3 3 29 2 *"),
        )
        job.owed_fire = True

        assert svc._is_due(job, time.time()) is True

        async def clean_cb(j):
            return None

        svc._jobs = [job]
        svc._save()
        claimed = job.owed_occurrence()
        svc._owed_fire_runs[job.id] = claimed
        svc._on_job = clean_cb
        await svc._execute(job)
        await asyncio.to_thread(svc._merge_job_result, job, claimed)

        assert job.owed_fire is False
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_fire is False
        assert svc._is_due(job, time.time()) is False

    @pytest.mark.asyncio
    async def test_owed_completion_keeps_admission_fenced_until_merge_settles(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=occurrence_id,
        )
        svc._jobs = [job]
        svc._save()
        payloads: list[str] = []
        merge_entered = threading.Event()
        release_merge = threading.Event()
        real_merge = svc._merge_job_result

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def delayed_merge(running_job: CronJob) -> None:
            merge_entered.set()
            assert release_merge.wait(timeout=5)
            real_merge(running_job)

        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        svc._on_job = payload
        try:
            with (
                patch("kiro_crew.cron.time.time", return_value=now),
                patch("kiro_crew.cron.admission_check", return_value=admitted),
                patch.object(svc, "_merge_job_result", side_effect=delayed_merge),
            ):
                await svc._on_timer()
                assert await asyncio.to_thread(merge_entered.wait, 2)
                run_task = svc._running_tasks[job.id]

                assert job.id in svc._executing
                assert await svc.run_job(job.id) is False
                await svc._on_timer()
                assert payloads == [job.id]

                release_merge.set()
                await asyncio.wait_for(run_task, 2)
        finally:
            release_merge.set()
            for task in list(svc._running_tasks.values()):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert payloads == [job.id]
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks

    @pytest.mark.asyncio
    async def test_successful_owed_result_retries_store_busy_without_rerun(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=occurrence_id,
        )
        svc._jobs = [job]
        svc._save()
        svc._owed_fire_runs[job.id] = occurrence_id
        svc._run_occurrence_ids[job.id] = occurrence_id
        svc._executing.add(job.id)
        payloads: list[str] = []
        retry_entered = threading.Event()
        release_retry = threading.Event()
        real_merge = svc._merge_job_result
        merge_calls = 0

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def fail_then_settle(running_job: CronJob) -> None:
            nonlocal merge_calls
            merge_calls += 1
            if merge_calls == 1:
                raise CronStoreBusy("busy")
            retry_entered.set()
            assert release_retry.wait(5)
            real_merge(running_job)

        svc._on_job = payload
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        try:
            with (
                patch.object(svc, "_merge_job_result", side_effect=fail_then_settle),
                patch("kiro_crew.cron.admission_check", return_value=admitted),
                patch("kiro_crew.cron.time.time", return_value=now + 120),
            ):
                run_task = asyncio.create_task(svc._run_job_isolated(job))
                svc._running_tasks[job.id] = run_task
                assert await asyncio.to_thread(retry_entered.wait, 2)
                assert job.id in svc._executing
                assert svc._running_tasks[job.id] is run_task
                assert await svc.run_job(job.id) is False
                await svc._on_timer()
                assert payloads == [job.id]
                release_retry.set()
                await asyncio.wait_for(run_task, 2)
        finally:
            release_retry.set()
            for task in list(svc._running_tasks.values()):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert payloads == [job.id]
        assert merge_calls == 2
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens

    @pytest.mark.asyncio
    async def test_successful_owed_result_keeps_retry_ownership_while_unreadable(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        healthy = (tmp_path / "crons.json").read_bytes()
        svc._owed_fire_runs[job.id] = "100"
        svc._executing.add(job.id)
        payloads: list[str] = []

        async def corrupt_after_payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)
            (tmp_path / "crons.json").write_bytes(b"\xff\xfe")

        svc._on_job = corrupt_after_payload
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        for _ in range(100):
            if svc._load_failed:
                break
            await asyncio.sleep(0.01)

        assert svc._load_failed is True
        assert run_task.done() is False
        assert job.id in svc._executing
        assert svc._running_tasks[job.id] is run_task
        assert payloads == [job.id]

        (tmp_path / "crons.json").write_bytes(healthy)
        await asyncio.wait_for(run_task, 3)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert payloads == [job.id]
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens

    @staticmethod
    def _assert_no_run_state(svc: CronService, job_id: str) -> None:
        assert job_id not in svc._running_tasks
        assert job_id not in svc._executing
        assert job_id not in svc._run_tokens
        assert job_id not in svc._result_merge_relinquished_runs
        assert job_id not in svc._owed_fire_runs
        assert job_id not in svc._terminal_settling
        assert job_id not in svc._terminal_retryable
        assert job_id not in svc._run_occurrence_ids
        assert job_id not in svc._job_run_meta
        assert job_id not in svc._job_start_times
        assert job_id not in svc._job_start_monotonic
        assert job_id not in svc._job_jitter

    @pytest.mark.asyncio
    async def test_stop_rejects_run_job_crossing_synced_snapshot(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="manual", message="go", every_secs=300)
        payloads: list[str] = []
        snapshot_entered = threading.Event()
        release_snapshot = threading.Event()
        real_snapshot = svc._synced_snapshot

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def blocked_snapshot(include_disabled: bool = False) -> list[CronJob]:
            snapshot_entered.set()
            assert release_snapshot.wait(5), "run snapshot was not released"
            return real_snapshot(include_disabled)

        svc._on_job = payload
        run_task: asyncio.Task[bool] | None = None
        stop_task: asyncio.Task[None] | None = None
        try:
            with patch.object(svc, "_synced_snapshot", side_effect=blocked_snapshot):
                run_task = asyncio.create_task(svc.run_job(job.id))
                assert await asyncio.to_thread(snapshot_entered.wait, 2)
                stop_task = asyncio.create_task(svc.stop())
                for _ in range(100):
                    if svc._stopping:
                        break
                    await asyncio.sleep(0)
                assert svc._stopping is True
                release_snapshot.set()
                assert await asyncio.wait_for(run_task, 2) is False
                await asyncio.wait_for(stop_task, 2)
        finally:
            release_snapshot.set()
            tasks = [task for task in (run_task, stop_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        assert payloads == []
        self._assert_no_run_state(svc, job.id)

    @pytest.mark.asyncio
    async def test_stop_rejects_run_job_scheduled_after_task_snapshot(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="manual", message="go", every_secs=300)
        payloads: list[str] = []
        snapshot_crossed = asyncio.Event()
        release_stop = asyncio.Event()

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        async def block_after_task_snapshot() -> None:
            snapshot_crossed.set()
            await asyncio.wait_for(release_stop.wait(), 5)

        svc._on_job = payload
        stop_task: asyncio.Task[None] | None = None
        run_task: asyncio.Task[bool] | None = None
        try:
            with patch.object(
                svc,
                "_join_terminal_operations_for_shutdown",
                side_effect=block_after_task_snapshot,
            ):
                stop_task = asyncio.create_task(svc.stop())
                await asyncio.wait_for(snapshot_crossed.wait(), 2)
                run_task = asyncio.create_task(svc.run_job(job.id))
                svc._running_tasks[job.id] = run_task  # mirrors api_cron_run

                def retire_request(task: asyncio.Task[bool]) -> None:
                    if svc._running_tasks.get(job.id) is task:
                        svc._running_tasks.pop(job.id, None)

                run_task.add_done_callback(retire_request)
                assert await asyncio.wait_for(run_task, 2) is False
                await asyncio.sleep(0)
                assert run_task.done() is True
                assert payloads == []
                self._assert_no_run_state(svc, job.id)
                release_stop.set()
                await asyncio.wait_for(stop_task, 2)
        finally:
            release_stop.set()
            tasks = [task for task in (run_task, stop_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self._assert_no_run_state(svc, job.id)

    @pytest.mark.asyncio
    async def test_stop_still_cancels_and_joins_tracked_pre_stop_run(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="manual", message="go", every_secs=300)
        payload_entered = asyncio.Event()
        payload_cancelled = asyncio.Event()

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            try:
                await asyncio.Future()
            finally:
                payload_cancelled.set()

        svc._on_job = blocked_payload
        run_request = asyncio.create_task(svc.run_job(job.id))
        await asyncio.wait_for(payload_entered.wait(), 2)
        assert job.id in svc._running_tasks

        await asyncio.wait_for(svc.stop(), 2)

        assert await asyncio.wait_for(run_request, 2) is True
        assert payload_cancelled.is_set()
        self._assert_no_run_state(svc, job.id)

    @pytest.mark.asyncio
    async def test_pre_start_manual_run_remains_allowed(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="manual", message="go", every_secs=300)
        payloads: list[str] = []

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        svc._on_job = payload

        assert svc._running is False
        assert svc._stopping is False
        assert await svc.run_job(job.id) is True
        assert payloads == [job.id]
        self._assert_no_run_state(svc, job.id)

    @pytest.mark.asyncio
    async def test_restart_reopens_admission_only_after_start_is_established(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="manual", message="go", every_secs=300)
        payloads: list[str] = []
        rotate_entered = asyncio.Event()
        release_rotate = asyncio.Event()

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        async def blocked_rotate() -> None:
            rotate_entered.set()
            await asyncio.wait_for(release_rotate.wait(), 5)

        svc._on_job = payload
        await svc.stop()
        start_task: asyncio.Task[None] | None = None
        try:
            with (
                patch.object(svc._history, "rotate_all", side_effect=blocked_rotate),
                patch.object(svc, "_arm_timer"),
            ):
                start_task = asyncio.create_task(svc.start())
                await asyncio.wait_for(rotate_entered.wait(), 2)
                assert svc._running is False
                assert svc._stopping is True
                assert await svc.run_job(job.id) is False
                assert payloads == []

                release_rotate.set()
                await asyncio.wait_for(start_task, 2)
                assert svc._running is True
                assert svc._stopping is False
                assert await svc.run_job(job.id) is True
        finally:
            release_rotate.set()
            if start_task is not None and not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)

        assert payloads == [job.id]
        self._assert_no_run_state(svc, job.id)
        await svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("terminal_fails", "pending_fails"),
        [(True, False), (False, True), (True, True)],
        ids=["terminal-fails", "pending-fails", "both-fail"],
    )
    async def test_stop_runs_both_stabilizers_and_preserves_failures(
        self,
        tmp_path: Path,
        terminal_fails: bool,
        pending_fails: bool,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        calls: list[str] = []
        terminal_failure = RuntimeError("terminal stabilization failed")
        pending_failure = RuntimeError("pending stabilization failed")

        async def stabilize_terminal() -> None:
            calls.append("terminal")
            if terminal_fails:
                raise terminal_failure

        async def stabilize_pending() -> None:
            calls.append("pending")
            if pending_fails:
                raise pending_failure

        expected = terminal_failure if terminal_fails else pending_failure
        with (
            patch.object(
                svc,
                "_stabilize_terminal_retries_for_shutdown",
                side_effect=stabilize_terminal,
            ),
            patch.object(
                svc,
                "_stabilize_pending_owed_fires_for_shutdown",
                side_effect=stabilize_pending,
            ),
            pytest.raises(RuntimeError, match=str(expected)) as raised,
        ):
            await svc.stop()

        assert calls == ["terminal", "pending"]
        assert raised.value is expected
        if terminal_fails and pending_fails:
            assert raised.value.__cause__ is pending_failure
        else:
            assert raised.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_stop_does_not_catch_stabilizer_cancellation(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        pending = AsyncMock()

        async def cancel_terminal() -> None:
            raise asyncio.CancelledError

        with (
            patch.object(
                svc,
                "_stabilize_terminal_retries_for_shutdown",
                side_effect=cancel_terminal,
            ),
            patch.object(
                svc,
                "_stabilize_pending_owed_fires_for_shutdown",
                pending,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await svc.stop()

        pending.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_merge_and_preserves_cancelled_claim(
        self, tmp_path: Path
    ) -> None:
        occurrence_id = "100"
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id=occurrence_id,
        )
        svc._jobs = [job]
        svc._save()
        payload_entered = asyncio.Event()
        merge_entered = threading.Event()
        release_merge = threading.Event()
        real_merge = svc._merge_job_result

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            await asyncio.Future()

        def delayed_merge(running_job: CronJob) -> None:
            merge_entered.set()
            assert release_merge.wait(timeout=5)
            real_merge(running_job)

        svc._on_job = blocked_payload
        svc._executing.add(job.id)
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(payload_entered.wait(), 2)

        try:
            with patch.object(svc, "_merge_job_result", side_effect=delayed_merge):
                stop_task = asyncio.create_task(svc.stop())
                assert await asyncio.to_thread(merge_entered.wait, 2)
                assert job.id in svc._executing
                assert await svc.run_job(job.id) is False
                assert stop_task.done() is False
                release_merge.set()
                await asyncio.wait_for(stop_task, 2)
        finally:
            release_merge.set()
            if not run_task.done():
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_owner_takes_over_permanent_owed_result_failure(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        merge_entered = threading.Event()
        release_merge = threading.Event()
        payloads: list[str] = []
        merge_calls = 0

        async def completed_payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def permanently_failed_merge(_running_job: CronJob) -> None:
            nonlocal merge_calls
            merge_calls += 1
            merge_entered.set()
            assert release_merge.wait(timeout=5)
            raise OSError("permanent result failure")

        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        svc._on_job = completed_payload
        svc._executing.add(job.id)
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        try:
            with (
                patch.object(svc, "_merge_job_result", side_effect=permanently_failed_merge),
                patch("kiro_crew.cron.admission_check", return_value=admitted),
                patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
                patch("kiro_crew.cron.sel.sel"),
                patch("kiro_crew.sel.sel"),
            ):
                assert await asyncio.to_thread(merge_entered.wait, 2)
                if terminal == "cancel":
                    terminal_task = asyncio.create_task(svc.cancel(job.id))
                else:
                    terminal_task = asyncio.create_task(
                        svc._force_reap(job.id, elapsed=1900, deadline=1800)
                    )
                await asyncio.sleep(0)
                assert terminal_task.done() is False
                assert svc._terminal_settling.get(job.id) is svc._run_tokens.get(job.id)
                release_merge.set()
                result = await asyncio.wait_for(terminal_task, 2)
                if terminal == "cancel":
                    assert result is True
        finally:
            release_merge.set()
            await asyncio.gather(run_task, return_exceptions=True)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        expected_error = "Cancelled by user" if terminal == "cancel" else "Reaped after"
        assert expected_error in (stored.last_error or "")
        records, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert records[0]["status"] == ("cancelled" if terminal == "cancel" else "timeout")
        assert payloads == [job.id]
        assert merge_calls == 1
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids
        assert all(
            authority_job_id != job.id
            for authority_job_id, _occurrence_id in svc._run_occurrence_timezone_authorities
        )
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic
        assert job.id not in svc._job_jitter

    @pytest.mark.asyncio
    async def test_shutdown_persists_process_only_claim_for_replacement_retry(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._owed_fire_runs[job.id] = occurrence_id
        svc._run_occurrence_ids[job.id] = occurrence_id
        svc._executing.add(job.id)
        payload_entered = asyncio.Event()

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            await asyncio.Future()

        svc._on_job = blocked_payload
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(payload_entered.wait(), 2)

        await svc.stop()

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert stored.last_status is None
        assert stored.last_result is None

        replacement = CronService(base_dir=tmp_path)
        retried: list[str] = []

        async def retry(retry_job: CronJob) -> None:
            retried.append(retry_job.id)

        replacement._on_job = retry
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        with (
            patch("kiro_crew.cron.time.time", return_value=now + 3600),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
        ):
            await replacement._on_timer()
            retry_tasks = list(replacement._running_tasks.values())
            assert retry_tasks
            await asyncio.gather(*retry_tasks)

        settled = CronService(base_dir=tmp_path).get_job(job.id)
        assert settled is not None
        assert settled.owed_occurrence() is None
        assert retried == [job.id]

    @pytest.mark.asyncio
    async def test_shutdown_rejects_process_only_debt_after_bounded_save_failures(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, "100")
        save_calls = 0

        def fail_before_commit() -> None:
            nonlocal save_calls
            save_calls += 1
            raise OSError("before commit")

        with patch.object(svc, "_save", side_effect=fail_before_commit):
            with pytest.raises(RuntimeError, match="could not durably hand off 1 owed"):
                await asyncio.wait_for(svc.stop(), 2)

        assert save_calls == 3
        assert svc._pending_owed_fires == {job.id: "100"}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "final_save_outcome",
        ["success", "commit-then-raise"],
        ids=["eventual-success", "final-commit-ambiguity"],
    )
    async def test_shutdown_accepts_durable_handoff_on_final_attempt(
        self, tmp_path: Path, final_save_outcome: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, "100")
        real_save = svc._save
        save_calls = 0

        def save_on_final_attempt() -> None:
            nonlocal save_calls
            save_calls += 1
            if save_calls < 3:
                raise OSError("before commit")
            real_save()
            if final_save_outcome == "commit-then-raise":
                raise OSError("after commit")

        with patch.object(svc, "_save", side_effect=save_on_final_attempt):
            await asyncio.wait_for(svc.stop(), 2)

        assert save_calls == 3
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "100"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("replacement_authority", "expected_owed"),
        [("deleted", None), ("completed", None), ("newer-debt", "101")],
    )
    async def test_shutdown_defers_to_replacement_authority_after_failed_save(
        self,
        tmp_path: Path,
        replacement_authority: str,
        expected_owed: str | None,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, "100")
        real_persist = svc._persist_pending_owed_fires
        persist_calls = 0

        async def fail_then_replace() -> set[str]:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls > 1:
                return await real_persist()
            with patch.object(svc, "_save", side_effect=OSError("before commit")):
                settled = await real_persist()
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(job.id)
            assert replacement_job is not None
            if replacement_authority == "deleted":
                replacement._jobs = []
            elif replacement_authority == "completed":
                replacement_job.last_run_ts = 100 * 60
            else:
                replacement_job.set_owed_occurrence("101")
            replacement._save()
            return settled

        with patch.object(svc, "_persist_pending_owed_fires", side_effect=fail_then_replace):
            await asyncio.wait_for(svc.stop(), 2)

        assert persist_calls == 2
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        if replacement_authority == "deleted":
            assert stored is None
        else:
            assert stored is not None
            assert stored.owed_occurrence() == expected_owed

    @pytest.mark.asyncio
    async def test_shutdown_without_owed_debt_does_not_enter_publication_path(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        with (
            patch.object(svc, "_persist_pending_owed_fires", new=AsyncMock()) as persist,
            patch.object(svc, "cancel", new=AsyncMock()) as cancel,
            patch.object(svc, "_force_reap", new=AsyncMock()) as reap,
        ):
            await svc.stop()
        persist.assert_not_awaited()
        cancel.assert_not_awaited()
        reap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_never_started_run_does_not_consume_the_owed_fire(self, tmp_path: Path) -> None:
        """The debt reset at run start must not stick when the run never
        started (overlap, pool starvation) — the merge would persist the
        cleared debt and the occurrence would never run."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="3 3 29 2 *"),
        )
        job.owed_fire = True

        async def never_started_cb(j):
            j.last_status = "error"
            j.run_never_started = True

        svc._on_job = never_started_cb
        await svc._execute(job)

        assert job.owed_fire is True

    @pytest.mark.asyncio
    async def test_a_policy_denied_owed_run_drops_the_debt(self, tmp_path: Path) -> None:
        """A fire-time policy denial can persist indefinitely, and an owed job
        is due on every poll — restoring the debt would refire for as long as
        the policy holds. The occurrence is dropped (not run_never_started)."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        )
        job.owed_fire = True

        async def denied_cb(j):
            j.last_status = "error"
            j.fire_time_denied = True

        svc._jobs = [job]
        svc._save()
        claimed = job.owed_occurrence()
        svc._owed_fire_runs[job.id] = claimed
        svc._on_job = denied_cb
        await svc._execute(job)
        await asyncio.to_thread(svc._merge_job_result, job, claimed)

        assert job.owed_fire is False
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_fire is False

    @staticmethod
    async def _start_blocked_scheduled_occurrence(
        svc: CronService, now: float
    ) -> tuple[
        CronJob, asyncio.Task[None], asyncio.Event, list[tuple[str | None, str | None]], str
    ]:
        occurrence_id = str(int(now) // 60)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        entered = asyncio.Event()
        release = asyncio.Event()
        claims: list[tuple[str | None, str | None]] = []

        async def blocked(running_job: CronJob) -> None:
            claims.append(
                (
                    svc._run_occurrence_ids.get(running_job.id),
                    svc._owed_fire_runs.get(running_job.id),
                )
            )
            entered.set()
            await release.wait()

        svc._on_job = blocked
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        with (
            patch("kiro_crew.cron.time.time", return_value=now),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
        ):
            await svc._on_timer()
        await asyncio.wait_for(entered.wait(), timeout=1)
        task = svc._running_tasks[job.id]
        return job, task, release, claims, occurrence_id

    @staticmethod
    def _publish_sibling_occurrence(base_dir: Path, job_id: str, occurrence_id: str) -> None:
        sibling = CronService(base_dir=base_dir)
        with sibling._file_lock():
            sibling._sync()
            sibling_job = next((item for item in sibling._jobs if item.id == job_id), None)
            assert sibling_job is not None
            sibling_job.set_owed_occurrence(occurrence_id)
            sibling._save()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("newer", [False, True], ids=["same", "newer"])
    async def test_scheduled_run_claims_late_published_occurrence(
        self, tmp_path: Path, newer: bool
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job, task, release, claims, occurrence_id = await self._start_blocked_scheduled_occurrence(
            svc, now
        )
        published = str(int(occurrence_id) + int(newer))

        await asyncio.to_thread(self._publish_sibling_occurrence, tmp_path, job.id, published)
        release.set()
        await task

        assert claims == [(occurrence_id, occurrence_id)]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == (published if newer else None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize("newer", [False, True], ids=["same", "newer"])
    async def test_scheduled_terminal_path_claims_late_published_occurrence(
        self, tmp_path: Path, terminal: str, newer: bool
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job, task, _release, claims, occurrence_id = await self._start_blocked_scheduled_occurrence(
            svc, now
        )
        published = str(int(occurrence_id) + int(newer))
        await asyncio.to_thread(self._publish_sibling_occurrence, tmp_path, job.id, published)

        if terminal == "cancel":
            with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
                assert await svc.cancel(job.id) is True
        else:
            with patch("kiro_crew.sel.sel"):
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert claims == [(occurrence_id, occurrence_id)]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == (published if newer else None)

    @staticmethod
    async def _start_blocked_owed_makeup(svc: CronService) -> tuple[CronJob, asyncio.Task[bool]]:
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
            consecutive_failures=2,
        )
        svc._jobs = [job]
        svc._save()
        entered = asyncio.Event()

        async def block_after_consuming_debt(running_job: CronJob) -> None:
            running_job.set_owed_occurrence(None)
            # A concurrent store refresh replaces the service cache with the
            # still-indebted disk object while this execution keeps its live copy.
            await asyncio.to_thread(svc._load)
            entered.set()
            await asyncio.Future()

        svc._execute_with_timeout = block_after_consuming_debt  # type: ignore[method-assign]
        run_task = asyncio.create_task(svc.run_job(job.id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        return job, run_task

    @staticmethod
    async def _start_blocked_ordinary_run_after_sibling_consumes_debt(
        svc: CronService, base_dir: Path
    ) -> tuple[CronJob, asyncio.Task[bool]]:
        """Leave an ordinary started run with stale debt after disk clears it."""
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            consecutive_failures=2,
        )
        await asyncio.to_thread(svc._persist_add_locked, job)
        entered = asyncio.Event()

        def write_sibling_debt(value: bool) -> None:
            sibling = CronService(base_dir=base_dir)
            with sibling._file_lock():
                sibling._sync()
                sibling_job = next((item for item in sibling._jobs if item.id == job.id), None)
                assert sibling_job is not None
                sibling_job.owed_fire = value
                sibling._save()

        async def block_after_sibling_consumes_debt(running_job: CronJob) -> None:
            assert running_job.id in svc._job_start_times
            assert running_job.id in svc._owed_fire_runs
            assert svc._owed_fire_runs[running_job.id] is None

            await asyncio.to_thread(write_sibling_debt, True)
            await asyncio.to_thread(svc._load)
            stale = next(item for item in svc._jobs if item.id == running_job.id)
            assert stale is not running_job
            assert stale.owed_fire is True

            await asyncio.to_thread(write_sibling_debt, False)
            stored = await asyncio.to_thread(
                lambda: CronService(base_dir=base_dir).get_job(running_job.id)
            )
            assert stored is not None
            assert stored.owed_fire is False
            assert stale.owed_fire is True
            entered.set()
            await asyncio.Future()

        svc._execute_with_timeout = block_after_sibling_consumes_debt  # type: ignore[method-assign]
        run_task = asyncio.create_task(svc.run_job(job.id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        return job, run_task

    @pytest.mark.asyncio
    async def test_started_cancel_does_not_resurrect_sibling_consumed_owed_fire(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job, run_task = await self._start_blocked_ordinary_run_after_sibling_consumes_debt(
            svc, tmp_path
        )

        with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
            assert await svc.cancel(job.id) is True
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_fire is False
        assert stored.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_force_reap_does_not_resurrect_sibling_consumed_owed_fire(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job, run_task = await self._start_blocked_ordinary_run_after_sibling_consumes_debt(
            svc, tmp_path
        )

        with patch("kiro_crew.sel.sel"):
            await svc._force_reap(job.id, elapsed=1900, deadline=1800)
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_fire is False
        assert stored.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_started_cancel_preserves_concurrent_sibling_owed_fire(
        self, tmp_path: Path
    ) -> None:
        """A started ordinary run must not consume debt published by another gateway."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            consecutive_failures=2,
        )
        svc._jobs = [job]
        svc._save()
        entered = asyncio.Event()

        async def block_after_sibling_debt(running_job: CronJob) -> None:
            assert running_job.id in svc._job_start_times
            assert running_job.id in svc._owed_fire_runs
            assert svc._owed_fire_runs[running_job.id] is None

            def publish_sibling_debt() -> None:
                sibling = CronService(base_dir=tmp_path)
                sibling_job = sibling.get_job(running_job.id)
                assert sibling_job is not None
                sibling_job.owed_fire = True
                sibling._save()

            await asyncio.to_thread(publish_sibling_debt)
            # Replace the local cache with the indebted disk copy while this
            # ordinary execution remains blocked with its start marker set.
            await asyncio.to_thread(svc._load)
            refreshed = next(j for j in svc._jobs if j.id == running_job.id)
            assert refreshed is not running_job
            assert refreshed.owed_fire is True
            entered.set()
            await asyncio.Future()

        svc._execute_with_timeout = block_after_sibling_debt  # type: ignore[method-assign]
        run_task = asyncio.create_task(svc.run_job(job.id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert job.id in svc._job_start_times

        with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
            assert await svc.cancel(job.id) is True
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_fire is True
        assert stored.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_user_cancel_persists_consumed_owed_fire(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job, run_task = await self._start_blocked_owed_makeup(svc)

        with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
            assert await svc.cancel(job.id) is True
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_fire is False
        assert stored.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_force_reap_persists_consumed_owed_fire(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job, run_task = await self._start_blocked_owed_makeup(svc)

        with patch("kiro_crew.sel.sel"):
            await svc._force_reap(job.id, elapsed=1900, deadline=1800)
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_fire is False
        assert stored.consecutive_failures == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_path_preserves_newer_sibling_occurrence(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job, run_task = await self._start_blocked_owed_makeup(svc)

        def publish_newer() -> None:
            sibling = CronService(base_dir=tmp_path)
            sibling_job = sibling.get_job(job.id)
            assert sibling_job is not None
            sibling_job.set_owed_occurrence("101")
            sibling._save()

        await asyncio.to_thread(publish_newer)
        await asyncio.to_thread(svc._load)
        if terminal == "cancel":
            with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
                assert await svc.cancel(job.id) is True
        else:
            with patch("kiro_crew.sel.sel"):
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)
        assert await run_task is True

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_occurrence() == "101"

    def test_terminal_merge_clears_owed_fire_on_persisted_copy(self, tmp_path: Path) -> None:
        """Cancel/reap must clear debt on the disk copy loaded under the lock,
        not only on the execution object that already consumed it in memory."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()

        svc._merge_terminal_state_locked(
            job.id,
            last_status="error",
            last_error="cancelled",
            last_run_ts=123.0,
            claimed_owed_fire="100",
        )

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_fire is False

    def test_ordinary_completion_preserves_newer_sibling_occurrence(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        running = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        )
        svc._jobs = [running]
        svc._save()

        sibling = CronService(base_dir=tmp_path)
        sibling_job = sibling.get_job(running.id)
        assert sibling_job is not None
        sibling_job.set_owed_occurrence("101")
        sibling._save()

        running.last_status = "ok"
        svc._merge_job_result(running)

        stored = CronService(base_dir=tmp_path).get_job(running.id)
        assert stored is not None
        assert stored.owed_occurrence() == "101"

    def test_owed_completion_clears_only_its_claimed_occurrence(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        running = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [running]
        svc._save()
        running.set_owed_occurrence(None)

        sibling = CronService(base_dir=tmp_path)
        sibling_job = sibling.get_job(running.id)
        assert sibling_job is not None
        sibling_job.set_owed_occurrence("101")
        sibling._save()

        svc._merge_job_result(running, claimed_owed_fire="100")

        stored = CronService(base_dir=tmp_path).get_job(running.id)
        assert stored is not None
        assert stored.owed_occurrence() == "101"

    @pytest.mark.asyncio
    async def test_valid_unchanged_pending_occurrence_is_persisted(self, tmp_path: Path) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, occurrence_id)

        assert await svc._persist_pending_owed_fires() == {job.id}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert svc._pending_owed_fires == {}

    @staticmethod
    def _pending_timezone_service(
        tmp_path: Path,
        *,
        timezone_name: str = "",
        cron_expr: str = "0 6 * * *",
        occurrence_ts: float | None = None,
    ) -> tuple[CronService, CronJob, str, float]:
        timestamp = occurrence_ts or datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(timestamp) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr=cron_expr),
            timezone=timezone_name,
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        return svc, job, occurrence_id, timestamp

    @pytest.mark.asyncio
    async def test_captured_inherited_pending_occurrence_survives_default_change(
        self, tmp_path: Path
    ) -> None:
        svc, job, occurrence_id, _timestamp = self._pending_timezone_service(tmp_path)
        svc._capture_run_occurrence_timezone_authority(
            job,
            occurrence_id,
            ("UTC", 10),
        )
        svc._queue_owed_occurrence(job.id, occurrence_id)

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_captured_pending_authority_survives_timezone_aba_generation(
        self, tmp_path: Path
    ) -> None:
        svc, job, occurrence_id, _timestamp = self._pending_timezone_service(tmp_path)
        svc._capture_run_occurrence_timezone_authority(job, occurrence_id, ("UTC", 10))
        svc._capture_run_occurrence_timezone_authority(
            job,
            occurrence_id,
            ("America/Los_Angeles", 11),
        )
        svc._capture_run_occurrence_timezone_authority(job, occurrence_id, ("UTC", 12))
        svc._queue_owed_occurrence(job.id, occurrence_id)

        with (
            patch("kiro_crew.cron.published_config_timezone", return_value="UTC"),
            patch.object(
                svc,
                "_occurrence_matches_calendar",
                wraps=svc._occurrence_matches_calendar,
            ) as calendar_check,
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        assert svc._run_occurrence_timezone_authorities[(job.id, occurrence_id)] == (
            "UTC",
            10,
        )
        assert calendar_check.call_count == 1
        assert calendar_check.call_args.kwargs["default_timezone"] == "UTC"
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_explicit_pending_timezone_ignores_capture_and_live_defaults(
        self, tmp_path: Path
    ) -> None:
        svc, job, occurrence_id, _timestamp = self._pending_timezone_service(
            tmp_path,
            timezone_name="UTC",
        )
        svc._capture_run_occurrence_timezone_authority(
            job,
            occurrence_id,
            ("America/New_York", 10),
        )
        svc._queue_owed_occurrence(job.id, occurrence_id)

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        assert svc._run_occurrence_timezone_authorities == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_uncaptured_inherited_pending_occurrence_uses_live_default(
        self, tmp_path: Path
    ) -> None:
        svc, job, occurrence_id, _timestamp = self._pending_timezone_service(tmp_path)
        svc._queue_owed_occurrence(job.id, occurrence_id)

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fresh_change", ["schedule", "timezone", "skip-date"])
    async def test_capture_timezone_does_not_freeze_fresh_calendar_fields(
        self, tmp_path: Path, fresh_change: str
    ) -> None:
        svc, job, occurrence_id, _timestamp = self._pending_timezone_service(tmp_path)
        svc._capture_run_occurrence_timezone_authority(job, occurrence_id, ("UTC", 10))
        svc._queue_owed_occurrence(job.id, occurrence_id)

        writer = CronService(base_dir=tmp_path)
        fresh = writer.get_job(job.id)
        assert fresh is not None
        if fresh_change == "schedule":
            fresh.schedule = CronSchedule(kind="cron", cron_expr="1 6 * * *")
        elif fresh_change == "timezone":
            fresh.timezone = "America/Los_Angeles"
        else:
            fresh.skip_dates = ["2026-01-02"]
        writer._save()

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fresh_change", ["disabled", "last-run"])
    async def test_capture_timezone_keeps_fresh_dispatch_and_completion_authority(
        self, tmp_path: Path, fresh_change: str
    ) -> None:
        svc, job, occurrence_id, timestamp = self._pending_timezone_service(tmp_path)
        svc._capture_run_occurrence_timezone_authority(job, occurrence_id, ("UTC", 10))
        svc._queue_owed_occurrence(job.id, occurrence_id)

        writer = CronService(base_dir=tmp_path)
        fresh = writer.get_job(job.id)
        assert fresh is not None
        if fresh_change == "disabled":
            fresh.enabled = False
            fresh.user_paused = True
        else:
            fresh.last_run_ts = timestamp + 30
        writer._save()

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/Los_Angeles",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_pending_newer_sibling_uses_only_its_exact_capture_authority(
        self, tmp_path: Path
    ) -> None:
        newer_ts = datetime(2026, 1, 3, 14, 0, tzinfo=timezone.utc).timestamp()
        svc, job, older_id, _timestamp = self._pending_timezone_service(tmp_path)
        newer_id = str(int(newer_ts) // 60)
        job.set_owed_occurrence(older_id)
        svc._save()
        svc._capture_run_occurrence_timezone_authority(job, older_id, ("UTC", 10))
        svc._capture_run_occurrence_timezone_authority(
            job,
            newer_id,
            ("America/Los_Angeles", 11),
        )
        svc._queue_owed_occurrence(job.id, newer_id)

        with patch(
            "kiro_crew.cron.published_config_timezone",
            return_value="America/New_York",
        ):
            assert await svc._persist_pending_owed_fires() == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == newer_id
        assert svc._run_occurrence_timezone_authorities[(job.id, older_id)] == (
            "UTC",
            10,
        )
        assert svc._run_occurrence_timezone_authorities[(job.id, newer_id)] == (
            "America/Los_Angeles",
            11,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fresh_change",
        ["disabled", "schedule", "timezone", "skip-date", "last-run"],
    )
    async def test_stale_pending_occurrence_is_retired_against_fresh_target(
        self, tmp_path: Path, fresh_change: str
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        old_gateway = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        old_gateway._jobs = [job]
        old_gateway._save()
        old_gateway._queue_owed_occurrence(job.id, occurrence_id)

        writer = CronService(base_dir=tmp_path)
        fresh = writer.get_job(job.id)
        assert fresh is not None
        if fresh_change == "disabled":
            fresh.enabled = False
            fresh.user_paused = True
        elif fresh_change == "schedule":
            fresh.schedule = CronSchedule(kind="cron", cron_expr="1 6 * * *")
        elif fresh_change == "timezone":
            fresh.timezone = "America/Los_Angeles"
        elif fresh_change == "skip-date":
            fresh.skip_dates = ["2026-01-02"]
        else:
            fresh.last_run_ts = now + 20
        writer._save()

        assert await old_gateway._persist_pending_owed_fires() == {job.id}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert old_gateway._pending_owed_fires == {}

    def test_stale_failed_publication_is_not_republished_by_fallback_merge(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        old_gateway = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        old_gateway._jobs = [stored]
        old_gateway._save()
        running = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=occurrence_id,
            run_never_started=True,
            keep_overdue=True,
        )
        old_gateway._queue_owed_occurrence(running.id, occurrence_id)

        writer = CronService(base_dir=tmp_path)
        paused = writer.get_job(running.id)
        assert paused is not None
        paused.enabled = False
        paused.user_paused = True
        writer._save()

        old_gateway._merge_job_result(running)

        stored = CronService(base_dir=tmp_path).get_job(running.id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.owed_occurrence() is None
        assert old_gateway._pending_owed_fires == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("occurrence_id", ["legacy", "future-format"])
    async def test_unknown_pending_identity_is_preserved_safely(
        self, tmp_path: Path, occurrence_id: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            enabled=False,
            user_paused=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, occurrence_id)

        assert await svc._persist_pending_owed_fires() == {job.id}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_pending_drain_preserves_persisted_newer_sibling(self, tmp_path: Path) -> None:
        occurrence_id = "100"
        newer = "101"
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id=newer,
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, occurrence_id)

        assert await svc._persist_pending_owed_fires() == {job.id}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == newer
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_pending_drain_keeps_newer_concurrent_arrival(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()
        svc._queue_owed_occurrence(job.id, "100")
        real_save = svc._save
        save_completed = threading.Event()
        allow_save_return = threading.Event()

        def save_then_pause() -> None:
            real_save()
            save_completed.set()
            assert allow_save_return.wait(timeout=5)

        with patch.object(svc, "_save", side_effect=save_then_pause):
            drain = asyncio.create_task(svc._persist_pending_owed_fires())
            assert await asyncio.to_thread(save_completed.wait, 2)
            svc._queue_owed_occurrence(job.id, "101")
            allow_save_return.set()
            assert await drain == {job.id}

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {job.id: "101"}

    @pytest.mark.parametrize(
        ("captured_offset", "current_offset", "expected_offset"),
        [(-1, 0, 0), (1, 0, 1), (1, None, 1)],
        ids=["current-newer", "captured-newer", "no-current-boundary"],
    )
    def test_pruned_launch_selects_newest_captured_or_current_boundary(
        self,
        tmp_path: Path,
        captured_offset: int,
        current_offset: int | None,
        expected_offset: int,
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        current_id = int(now) // 60
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._run_occurrence_ids[job.id] = str(current_id + captured_offset)
        if current_offset is None:
            job.last_run_ts = now
        selected = svc.occurrence_for_pruned_launch(job, now)
        assert selected == str(current_id + expected_offset)

    def test_pruned_launch_current_boundary_supersedes_legacy_claim(self, tmp_path: Path) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="minutely",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._run_occurrence_ids[job.id] = "legacy"
        assert svc.occurrence_for_pruned_launch(job, now) == str(int(now) // 60)
        job.last_run_ts = now
        assert svc.occurrence_for_pruned_launch(job, now) == "legacy"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [CronStoreBusy("busy"), OSError("disk full")],
        ids=["store-busy", "save-oserror"],
    )
    async def test_publication_failure_retains_retry_intent(
        self, tmp_path: Path, failure: Exception
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()

        with patch.object(svc, "_flush_pending_owed_fires_locked", side_effect=failure):
            assert await svc.persist_owed_occurrence(job.id, "100") is False
        assert svc._pending_owed_fires == {job.id: "100"}

        assert await svc.persist_owed_occurrence(job.id, "100") is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_pending_save_failure_restores_hot_cache_and_retries(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()

        with patch.object(svc, "_save", side_effect=OSError("before commit")):
            assert await svc.persist_owed_occurrence(job.id, "100") is False

        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() is None
        assert svc._pending_owed_fires == {job.id: "100"}
        assert svc._last_digest != b""
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.owed_occurrence() is None

        assert await svc.persist_owed_occurrence(job.id, "100") is True
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_pending_commit_then_raise_reloads_before_unrelated_writer(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [job]
        svc._save()
        real_save = svc._save

        def commit_then_raise() -> None:
            real_save()
            raise OSError("after commit")

        with patch.object(svc, "_save", side_effect=commit_then_raise):
            assert await svc.persist_owed_occurrence(job.id, "100") is True

        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}
        assert svc._last_digest != b""
        committed = CronService(base_dir=tmp_path).get_job(job.id)
        assert committed is not None
        assert committed.owed_occurrence() == "100"

        updated = await svc.update_job_async(job.id, name="renamed")
        assert updated is not None
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.name == "renamed"
        assert persisted.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}

        assert await svc._persist_pending_owed_fires() == set()
        assert svc._pending_owed_fires == {}
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == "100"

    @pytest.mark.parametrize("mode", ["never-started-publish", "claimed-completion-clear"])
    @pytest.mark.parametrize(
        "failure_kind",
        ["handled-unreadable", "before-commit-write-fault", "commit-then-raise"],
    )
    def test_merge_job_result_owed_failure_restores_cache_and_retries(
        self, tmp_path: Path, mode: str, failure_kind: str
    ) -> None:
        prior_owed_fire = "100" if mode == "claimed-completion-clear" else None
        intended_owed_fire = None if prior_owed_fire else "100"
        svc = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=prior_owed_fire is not None,
            owed_fire_id=prior_owed_fire or "",
        )
        svc._jobs = [stored]
        svc._save()
        running = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=mode == "never-started-publish",
            owed_fire_id="100" if mode == "never-started-publish" else "",
            run_never_started=mode == "never-started-publish",
        )
        claimed = "100" if mode == "claimed-completion-clear" else None
        if mode == "never-started-publish":
            svc._queue_owed_occurrence(stored.id, "100")
        real_save = svc._save

        def commit_then_raise() -> None:
            real_save()
            raise OSError("after commit")

        if failure_kind == "handled-unreadable":
            save_effect = CronStoreUnreadable("unreadable")
        elif failure_kind == "before-commit-write-fault":
            save_effect = OSError("disk full")
        else:
            save_effect = commit_then_raise

        with patch.object(svc, "_save", side_effect=save_effect):
            if mode == "never-started-publish":
                if failure_kind == "handled-unreadable":
                    svc._merge_job_result(running, claimed_owed_fire=claimed)
                else:
                    message = "after commit" if failure_kind == "commit-then-raise" else "disk full"
                    with pytest.raises(OSError, match=message):
                        svc._merge_job_result(running, claimed_owed_fire=claimed)
            elif failure_kind == "commit-then-raise":
                svc._merge_job_result(running, claimed_owed_fire=claimed)
            else:
                with pytest.raises(OSError, match="did not clear owed occurrence"):
                    svc._merge_job_result(running, claimed_owed_fire=claimed)

        hot = svc.get_job(stored.id)
        assert hot is not None
        expected_persisted = (
            intended_owed_fire if failure_kind == "commit-then-raise" else prior_owed_fire
        )
        expected_hot = expected_persisted if mode == "claimed-completion-clear" else prior_owed_fire
        assert hot.owed_occurrence() == expected_hot
        if mode == "claimed-completion-clear":
            assert svc._last_digest != b""
        else:
            assert svc._last_digest == b""
        expected_pending = {stored.id: "100"} if mode == "never-started-publish" else {}
        assert svc._pending_owed_fires == expected_pending
        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == expected_persisted

        svc._merge_job_result(running, claimed_owed_fire=claimed)

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == intended_owed_fire
        assert svc._pending_owed_fires == {}

    @pytest.mark.parametrize(
        "failure_kind",
        ["handled-unreadable", "before-commit-write-fault", "commit-then-raise"],
    )
    def test_terminal_owed_clear_failure_restores_cache_and_retries(
        self, tmp_path: Path, failure_kind: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        real_save = svc._save

        def commit_then_raise() -> None:
            real_save()
            raise OSError("after commit")

        if failure_kind == "handled-unreadable":
            save_effect = CronStoreUnreadable("unreadable")
        elif failure_kind == "before-commit-write-fault":
            save_effect = OSError("disk full")
        else:
            save_effect = commit_then_raise

        with patch.object(svc, "_save", side_effect=save_effect):
            if failure_kind == "commit-then-raise":
                svc._merge_terminal_state_locked(
                    job.id,
                    last_status="error",
                    last_error="cancelled",
                    last_run_ts=123.0,
                    claimed_owed_fire="100",
                )
            else:
                with pytest.raises(OSError, match="did not clear owed occurrence"):
                    svc._merge_terminal_state_locked(
                        job.id,
                        last_status="error",
                        last_error="cancelled",
                        last_run_ts=123.0,
                        claimed_owed_fire="100",
                    )

        hot = svc.get_job(job.id)
        assert hot is not None
        expected_after_failure = None if failure_kind == "commit-then-raise" else "100"
        assert hot.owed_occurrence() == expected_after_failure
        assert svc._last_digest != b""
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == expected_after_failure

        svc._merge_terminal_state_locked(
            job.id,
            last_status="error",
            last_error="cancelled",
            last_run_ts=123.0,
            claimed_owed_fire="100",
        )
        persisted = CronService(base_dir=tmp_path).get_job(job.id)
        assert persisted is not None
        assert persisted.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize("failure_kind", ["pre-commit", "commit-then-raise"])
    async def test_terminal_owed_clear_settles_before_success_and_fence_release(
        self, tmp_path: Path, terminal: str, failure_kind: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._job_run_meta[job.id] = (time.time() - 5, "manual")
        svc._job_start_times[job.id] = time.time() - 5
        svc._job_start_monotonic[job.id] = time.monotonic() - 5
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        real_save = svc._save
        save_calls = 0

        def fail_save() -> None:
            nonlocal save_calls
            save_calls += 1
            if failure_kind == "commit-then-raise":
                real_save()
            raise OSError(failure_kind)

        async def settle() -> object:
            if terminal == "cancel":
                with patch(
                    "kiro_crew.cron.cron_script.kill_running_process",
                    return_value=False,
                ):
                    return await svc.cancel(job.id)
            return await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        with (
            patch.object(svc, "_save", side_effect=fail_save),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            if failure_kind == "pre-commit":
                with pytest.raises(OSError, match="did not clear owed occurrence"):
                    await settle()
            else:
                result = await settle()
                if terminal == "cancel":
                    assert result is True

        if failure_kind == "pre-commit":
            assert save_calls == 3
            assert svc._terminal_retryable == {job.id: terminal}
            assert job.id in svc._terminal_settling
            assert job.id in svc._run_tokens
            assert job.id in svc._executing
            assert job.id in svc._job_start_times
            svc._history.append.assert_not_awaited()  # type: ignore[attr-defined]
            audit.log_tool_invocation.assert_not_called()
            stored = CronService(base_dir=tmp_path).get_job(job.id)
            assert stored is not None
            assert stored.owed_occurrence() == "100"

            with (
                patch("kiro_crew.cron.sel.sel", return_value=audit),
                patch("kiro_crew.sel.sel", return_value=audit),
            ):
                result = await settle()
                if terminal == "cancel":
                    assert result is True
        else:
            assert save_calls == 1

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._executing
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_caller_cancellation_waits_through_owed_clear_retry(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        first_failure = threading.Event()
        real_save = svc._save
        calls = 0

        def fail_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_failure.set()
                raise OSError("transient")
            real_save()

        with (
            patch.object(svc, "_save", side_effect=fail_once),
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel"),
        ):
            cancel_task = asyncio.create_task(svc.cancel(job.id))
            assert await asyncio.to_thread(first_failure.wait, 2)
            cancel_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(cancel_task, 2)

        assert calls == 2
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._executing

    @staticmethod
    async def _settle_terminal_for_test(svc: CronService, job_id: str, terminal: str) -> object:
        if terminal == "cancel":
            with patch(
                "kiro_crew.cron.cron_script.kill_running_process",
                return_value=False,
            ):
                return await svc.cancel(job_id)
        return await svc._force_reap(job_id, elapsed=1900, deadline=1800)

    @classmethod
    async def _service_with_retryable_terminal(
        cls, tmp_path: Path, terminal: str
    ) -> tuple[CronService, CronJob, MagicMock, Callable[[], None]]:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._job_run_meta[job.id] = (time.time() - 5, "manual")
        svc._job_start_times[job.id] = time.time() - 5
        svc._job_start_monotonic[job.id] = time.monotonic() - 5
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        real_save = svc._save
        with (
            patch.object(svc, "_save", side_effect=OSError("before commit")),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
            pytest.raises(OSError, match="did not clear owed occurrence"),
        ):
            await cls._settle_terminal_for_test(svc, job.id, terminal)
        assert svc._terminal_retryable == {job.id: terminal}
        return svc, job, audit, real_save

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize(
        "cancel_caller",
        [False, True],
        ids=["caller-live", "caller-cancelled"],
    )
    async def test_stop_waits_for_inflight_terminal_reset_before_returning(
        self,
        tmp_path: Path,
        terminal: str,
        cancel_caller: bool,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        reset_entered = asyncio.Event()
        reset_release = asyncio.Event()

        class BlockingSessions:
            async def reset(self, *_args, **_kwargs) -> None:
                reset_entered.set()
                await asyncio.wait_for(reset_release.wait(), 5)

        svc._sessions = BlockingSessions()  # type: ignore[assignment]

        async def settle() -> None:
            if terminal == "cancel":
                assert await svc.cancel(job.id) is True
            else:
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        terminal_task: asyncio.Task[None] | None = None
        stop_task: asyncio.Task[None] | None = None
        with (
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            terminal_task = asyncio.create_task(settle())
            await asyncio.wait_for(reset_entered.wait(), 2)
            if cancel_caller:
                terminal_task.cancel()
                await asyncio.sleep(0)
                assert terminal_task.done() is False
            assert job.id in svc._terminal_operations

            stop_task = asyncio.create_task(svc.stop())
            await asyncio.sleep(0)
            try:
                assert stop_task.done() is False
                stored = await asyncio.to_thread(
                    lambda: CronService(base_dir=tmp_path).get_job(job.id)
                )
                assert stored is not None
                assert stored.owed_occurrence() == "100"
            finally:
                reset_release.set()

            await asyncio.wait_for(stop_task, 2)
            if cancel_caller:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(terminal_task, 2)
            else:
                await asyncio.wait_for(terminal_task, 2)

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_operations
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._terminal_retryable
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_stabilizes_retry_published_by_joined_terminal_owner(
        self,
        tmp_path: Path,
        terminal: str,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        first_save_entered = asyncio.Event()
        first_save_release = threading.Event()
        loop = asyncio.get_running_loop()
        real_save = svc._save
        failures_remaining = len(_TERMINAL_OWED_CLEAR_RETRY_DELAYS) + 1

        def fail_initial_owner_then_save() -> None:
            nonlocal failures_remaining
            if failures_remaining:
                if failures_remaining == len(_TERMINAL_OWED_CLEAR_RETRY_DELAYS) + 1:
                    loop.call_soon_threadsafe(first_save_entered.set)
                    if not first_save_release.wait(5):
                        raise AssertionError("terminal save release was not signalled")
                failures_remaining -= 1
                raise OSError("initial terminal owner did not commit")
            real_save()

        async def settle() -> None:
            if terminal == "cancel":
                await svc.cancel(job.id)
            else:
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        with (
            patch.object(svc, "_save", side_effect=fail_initial_owner_then_save),
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            terminal_task = asyncio.create_task(settle())
            await asyncio.wait_for(first_save_entered.wait(), 2)
            stop_task = asyncio.create_task(svc.stop())
            await asyncio.sleep(0)
            try:
                assert stop_task.done() is False
            finally:
                first_save_release.set()

            await asyncio.wait_for(stop_task, 3)
            with pytest.raises(OSError):
                await asyncio.wait_for(terminal_task, 2)

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert failures_remaining == 0
        assert job.id not in svc._terminal_operations
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_does_not_self_await_a_terminal_owner(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        current = asyncio.current_task()
        assert current is not None
        svc._terminal_operations["self"] = current

        await asyncio.wait_for(svc.stop(), 1)

        assert svc._terminal_operations == {"self": current}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_settles_retained_terminal_operation(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc, job, audit, _real_save = await self._service_with_retryable_terminal(
            tmp_path, terminal
        )

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            await svc.stop()

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_fails_closed_when_terminal_retry_exhausts(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc, job, audit, _real_save = await self._service_with_retryable_terminal(
            tmp_path, terminal
        )

        with (
            patch.object(svc, "_save", side_effect=OSError("still before commit")),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
            pytest.raises(RuntimeError, match="could not durably settle 1 terminal"),
        ):
            await svc.stop()

        assert svc._terminal_retryable == {job.id: terminal}
        assert job.id in svc._terminal_settling
        assert job.id in svc._run_tokens
        svc._history.append.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_accepts_terminal_commit_then_raise(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc, job, audit, real_save = await self._service_with_retryable_terminal(tmp_path, terminal)

        def commit_then_raise() -> None:
            real_save()
            raise OSError("after commit")

        with (
            patch.object(svc, "_save", side_effect=commit_then_raise),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            await svc.stop()

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_terminal_retry_preserves_newer_replacement_debt(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc, job, audit, _real_save = await self._service_with_retryable_terminal(
            tmp_path, terminal
        )
        replacement = CronService(base_dir=tmp_path)
        target = replacement.get_job(job.id)
        assert target is not None
        target.set_owed_occurrence("101")
        replacement._save()

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            await svc.stop()

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "101"
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_stop_terminal_retry_defers_to_completed_replacement(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc, job, audit, _real_save = await self._service_with_retryable_terminal(
            tmp_path, terminal
        )
        replacement = CronService(base_dir=tmp_path)
        target = replacement.get_job(job.id)
        assert target is not None
        target.set_owed_occurrence(None)
        target.last_run_ts = 100 * 60
        target.last_status = "ok"
        replacement._save()

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            await svc.stop()

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_exact_clear_fails_closed_on_actual_unreadable_store(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        healthy = (tmp_path / "crons.json").read_bytes()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._job_run_meta[job.id] = (time.time() - 5, "manual")
        svc._job_start_times[job.id] = time.time() - 5
        svc._job_start_monotonic[job.id] = time.monotonic() - 5
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        (tmp_path / "crons.json").write_bytes(
            b'{"version":2,"jobs":[{"id":"j1"}],"bad":"\xff\xfe"}'
        )

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
            pytest.raises(OSError, match="store unreadable"),
        ):
            await self._settle_terminal_for_test(svc, job.id, terminal)

        assert svc._load_failed is True
        assert svc._terminal_retryable == {job.id: terminal}
        assert job.id in svc._terminal_settling
        assert job.id in svc._run_tokens
        assert job.id in svc._executing
        svc._history.append.assert_not_awaited()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_not_called()

        (tmp_path / "crons.json").write_bytes(healthy)
        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await self._settle_terminal_for_test(svc, job.id, terminal)
            if terminal == "cancel":
                assert result is True

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize(
        "failure_site",
        ["lock-busy", "lock-oserror", "sync-oserror"],
    )
    async def test_terminal_exact_clear_fails_closed_before_store_authority(
        self, tmp_path: Path, terminal: str, failure_site: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        if failure_site == "sync-oserror":
            failure_patch = patch.object(svc, "_sync", side_effect=OSError("read failed"))
        else:
            failure = (
                CronStoreBusy("busy") if failure_site == "lock-busy" else OSError("lock failed")
            )
            failure_patch = patch.object(svc, "_file_lock", side_effect=failure)

        with (
            failure_patch,
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
            pytest.raises(OSError, match="fresh store authority"),
        ):
            await self._settle_terminal_for_test(svc, job.id, terminal)

        assert svc._terminal_retryable == {job.id: terminal}
        assert job.id in svc._terminal_settling
        assert job.id in svc._executing
        svc._history.append.assert_not_awaited()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_not_called()

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            result = await self._settle_terminal_for_test(svc, job.id, terminal)
            if terminal == "cancel":
                assert result is True

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()

    def test_terminal_missing_target_settles_only_after_authoritative_sync(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        sibling = CronService(base_dir=tmp_path)
        assert sibling.remove_job(job.id, actor="test", source="test") is True

        with patch.object(svc, "_save") as stale_save:
            svc._merge_terminal_state_locked(
                job.id,
                last_status="error",
                last_error="cancelled",
                last_run_ts=123.0,
                claimed_owed_fire="100",
            )

        stale_save.assert_not_called()
        assert svc.get_job(job.id) is None

    @pytest.mark.parametrize(
        "pending_occurrence",
        ["100", "99"],
        ids=["equal", "older"],
    )
    def test_successful_never_started_merge_retires_covered_pending_occurrence(
        self, tmp_path: Path, pending_occurrence: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [stored]
        svc._save()
        running = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=True,
            owed_fire_id="100",
            run_never_started=True,
        )
        svc._queue_owed_occurrence(stored.id, pending_occurrence)

        svc._merge_job_result(running)

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}

    @pytest.mark.parametrize("arrival", ["before-save", "during-save"])
    def test_newer_pending_occurrence_survives_until_a_covering_merge(
        self, tmp_path: Path, arrival: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [stored]
        svc._save()
        first = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=True,
            owed_fire_id="100",
            run_never_started=True,
        )

        if arrival == "before-save":
            svc._queue_owed_occurrence(stored.id, "101")
            svc._merge_job_result(first)
        else:
            svc._queue_owed_occurrence(stored.id, "99")
            real_save = svc._save
            save_completed = threading.Event()
            allow_save_return = threading.Event()
            merge_errors: list[BaseException] = []

            def save_then_pause() -> None:
                real_save()
                save_completed.set()
                assert allow_save_return.wait(timeout=5)

            def merge_first() -> None:
                try:
                    svc._merge_job_result(first)
                except BaseException as exc:
                    merge_errors.append(exc)

            with patch.object(svc, "_save", side_effect=save_then_pause):
                merge_thread = threading.Thread(target=merge_first)
                merge_thread.start()
                assert save_completed.wait(timeout=5)
                # This publication lands after the old occurrence is durable but
                # before the merge's post-save retirement decision.
                svc._queue_owed_occurrence(stored.id, "101")
                allow_save_return.set()
                merge_thread.join(timeout=5)
            assert not merge_thread.is_alive()
            assert merge_errors == []

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        expected_persisted = "101" if arrival == "before-save" else "100"
        expected_pending = {} if arrival == "before-save" else {stored.id: "101"}
        assert persisted.owed_occurrence() == expected_persisted
        assert svc._pending_owed_fires == expected_pending

        covering = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=True,
            owed_fire_id="101",
            run_never_started=True,
        )
        svc._merge_job_result(covering)

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == "101"
        assert svc._pending_owed_fires == {}

    @pytest.mark.parametrize(
        "failure",
        [CronStoreUnreadable("unreadable"), OSError("disk full")],
        ids=["handled-unreadable", "propagated-write-fault"],
    )
    def test_failed_never_started_merge_retains_exact_pending_until_recovery(
        self, tmp_path: Path, failure: Exception
    ) -> None:
        """Handled unreadable stores and propagated write faults differ only
        in caller visibility: neither is durable proof, so both must preserve
        the exact queued occurrence until a later successful merge covers it."""
        svc = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        )
        svc._jobs = [stored]
        svc._save()
        running = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=True,
            owed_fire_id="100",
            run_never_started=True,
        )
        svc._queue_owed_occurrence(stored.id, "99")

        with patch.object(svc, "_save", side_effect=failure):
            if isinstance(failure, CronStoreUnreadable):
                svc._merge_job_result(running)
            else:
                with pytest.raises(OSError, match="disk full"):
                    svc._merge_job_result(running)

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() is None
        assert svc._pending_owed_fires == {stored.id: "99"}

        svc._merge_job_result(running)

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == "100"
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_replacement_consumption_cannot_be_republished_by_old_gateway(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 7, 1, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()) // 60)
        old_gateway = CronService(base_dir=tmp_path)
        stored = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        )
        old_gateway._jobs = [stored]
        old_gateway._save()
        skipped = CronJob(
            id=stored.id,
            name=stored.name,
            message=stored.message,
            schedule=stored.schedule,
            owed_fire=True,
            owed_fire_id=occurrence_id,
            run_never_started=True,
        )
        old_gateway._queue_owed_occurrence(stored.id, occurrence_id)

        await asyncio.to_thread(old_gateway._merge_job_result, skipped)

        assert old_gateway._pending_owed_fires == {}
        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() == occurrence_id

        replacement = CronService(base_dir=tmp_path)
        executions: list[str] = []

        async def execute(job: CronJob) -> None:
            executions.append(job.id)

        replacement._on_job = execute
        with patch("kiro_crew.cron.time.time", return_value=now):
            assert await replacement.run_job(stored.id) is True
        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() is None
        assert executions == [stored.id]

        drained = await old_gateway._persist_pending_owed_fires()
        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        with (
            patch("kiro_crew.cron.time.time", return_value=now),
            patch("kiro_crew.cron.admission_check", return_value=admitted),
        ):
            await replacement._on_timer()
            await asyncio.gather(*list(replacement._running_tasks.values()))

        persisted = CronService(base_dir=tmp_path).get_job(stored.id)
        assert persisted is not None
        assert persisted.owed_occurrence() is None
        assert drained == set()
        assert executions == [stored.id]

    def test_legacy_boolean_and_new_identity_round_trip(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
        )
        svc._jobs = [job]
        svc._save()
        store = tmp_path / "crons.json"
        data = json.loads(store.read_text())
        data["jobs"][0].pop("owed_fire_id")
        store.write_text(json.dumps(data))

        legacy = CronService(base_dir=tmp_path)
        legacy_job = legacy.get_job(job.id)
        assert legacy_job is not None
        assert legacy_job.owed_occurrence() == "legacy"
        legacy._save()
        assert json.loads(store.read_text())["jobs"][0]["owed_fire_id"] == "legacy"

        legacy_job.set_owed_occurrence("123")
        legacy._save()
        current = CronService(base_dir=tmp_path).get_job(job.id)
        assert current is not None
        assert current.owed_occurrence() == "123"

    @pytest.mark.asyncio
    async def test_manual_due_boundary_defers_payload_until_publication_is_confirmed(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
            consecutive_failures=2,
        )
        svc._jobs = [job]
        svc._save()
        payloads: list[str] = []

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        svc._on_job = payload
        with (
            patch("kiro_crew.cron.time.time", return_value=now),
            patch.object(
                svc,
                "_flush_pending_owed_fires_locked",
                side_effect=OSError("before commit"),
            ),
        ):
            assert await svc.run_job(job.id) is True

        assert payloads == []
        assert job.run_never_started is True
        assert job.consecutive_failures == 2
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_manual_due_boundary_commit_then_raise_admits_payload(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        payloads: list[str] = []
        real_save = svc._save
        save_calls = 0

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def commit_then_raise_once() -> None:
            nonlocal save_calls
            save_calls += 1
            real_save()
            if save_calls == 1:
                raise OSError("after commit")

        svc._on_job = payload
        with (
            patch("kiro_crew.cron.time.time", return_value=now),
            patch.object(svc, "_save", side_effect=commit_then_raise_once),
        ):
            assert await svc.run_job(job.id) is True

        assert payloads == [job.id]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authority", ["completed", "newer-debt"])
    async def test_manual_due_boundary_accepts_authoritative_replacement_coverage(
        self, tmp_path: Path, authority: str
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        payloads: list[str] = []

        async def payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def replace_then_fail() -> set[str]:
            replacement = CronService(base_dir=tmp_path)
            target = replacement.get_job(job.id)
            assert target is not None
            if authority == "completed":
                target.last_run_ts = now
            else:
                target.set_owed_occurrence(str(int(occurrence_id) + 1))
            replacement._save()
            raise OSError("old publisher lost authority")

        svc._on_job = payload
        with (
            patch("kiro_crew.cron.time.time", return_value=now),
            patch.object(
                svc,
                "_flush_pending_owed_fires_locked",
                side_effect=replace_then_fail,
            ),
        ):
            assert await svc.run_job(job.id) is True

        assert payloads == [job.id]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        expected = None if authority == "completed" else str(int(occurrence_id) + 1)
        assert stored.owed_occurrence() == expected

    @pytest.mark.asyncio
    async def test_manual_run_spanning_due_boundary_records_one_makeup(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        start = datetime(2026, 1, 2, 13, 59, 30, tzinfo=timezone.utc).timestamp()
        boundary = datetime(2026, 1, 2, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        clock = {"now": start}
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_job: CronJob) -> None:
            entered.set()
            await release.wait()

        svc._on_job = blocked
        with patch("kiro_crew.cron.time.time", side_effect=lambda: clock["now"]):
            run_task = asyncio.create_task(svc.run_job(job.id))
            await asyncio.wait_for(entered.wait(), timeout=1)
            clock["now"] = boundary
            await svc._on_timer()

            stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
            assert stored is not None
            assert stored.owed_occurrence() == str(int(boundary) // 60)
            assert svc._owed_fire_runs[job.id] is None

            release.set()
            assert await run_task is True
            stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
            assert stored is not None
            assert stored.owed_occurrence() == str(int(boundary) // 60)

            calls: list[str] = []

            async def makeup(makeup_job: CronJob) -> None:
                calls.append(makeup_job.id)

            svc._on_job = makeup
            clock["now"] = boundary + 60
            admitted = type("Admission", (), {"admitted": True, "reason": ""})()
            with patch("kiro_crew.cron.admission_check", return_value=admitted):
                await svc._on_timer()
                tasks = list(svc._running_tasks.values())
                await asyncio.gather(*tasks)
                await svc._on_timer()
            assert calls == [job.id]
            stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
            assert stored is not None
            assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    async def test_manual_completion_between_timer_snapshot_and_scan_publishes_makeup(
        self, tmp_path: Path
    ) -> None:
        """A timer snapshot cannot lose the boundary when the manual run wins.

        The worker returns a snapshot while the manual payload still owns the
        job, then pauses before the loop-side manual-boundary scan. The payload
        completes in that gap, reproducing the stale-snapshot race exactly.
        """
        svc = CronService(base_dir=tmp_path)
        start = datetime(2026, 1, 2, 13, 59, 30, tzinfo=timezone.utc).timestamp()
        boundary = datetime(2026, 1, 2, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(boundary) // 60)
        clock = {"now": start}
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        payload_entered = asyncio.Event()
        payload_release = asyncio.Event()
        snapshot_ready = threading.Event()
        scan_release = threading.Event()
        calls: list[str] = []

        async def blocked(running_job: CronJob) -> None:
            calls.append(running_job.id)
            payload_entered.set()
            await payload_release.wait()

        real_scan = svc._tick_scan_locked

        def snapshot_then_pause() -> list[CronJob]:
            snapshot = real_scan()
            snapshot_ready.set()
            assert scan_release.wait(5), "timer scan was not released"
            return snapshot

        svc._on_job = blocked
        timer_task: asyncio.Task[None] | None = None
        with (
            patch("kiro_crew.cron.time.time", side_effect=lambda: clock["now"]),
            patch.object(svc, "_tick_scan_locked", side_effect=snapshot_then_pause),
        ):
            run_task = asyncio.create_task(svc.run_job(job.id))
            await asyncio.wait_for(payload_entered.wait(), timeout=1)
            clock["now"] = boundary
            timer_task = asyncio.create_task(svc._on_timer())
            assert await asyncio.to_thread(snapshot_ready.wait, 2)
            try:
                payload_release.set()
                assert await run_task is True
                assert job.id not in svc._executing
                assert job.id not in svc._job_run_meta
                stored = await asyncio.to_thread(
                    lambda: CronService(base_dir=tmp_path).get_job(job.id)
                )
                assert stored is not None
                assert stored.owed_occurrence() == occurrence_id
            finally:
                scan_release.set()
            await timer_task

        assert calls == [job.id]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_manual_older_claim_preserves_newer_completion_boundary(
        self, tmp_path: Path
    ) -> None:
        """An older make-up claim cannot consume the newer minute it masks."""
        svc = CronService(base_dir=tmp_path)
        start = datetime(2026, 1, 2, 13, 59, 30, tzinfo=timezone.utc).timestamp()
        boundary = datetime(2026, 1, 2, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        newer_occurrence = str(int(boundary) // 60)
        older_occurrence = str(int(newer_occurrence) - 1)
        clock = {"now": start}
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
            strict_schedule=True,
            owed_fire=True,
            owed_fire_id=older_occurrence,
        )
        svc._jobs = [job]
        svc._save()

        async def complete_at_boundary(running_job: CronJob) -> None:
            assert svc._owed_fire_runs[running_job.id] == older_occurrence
            assert svc._run_occurrence_ids[running_job.id] == older_occurrence
            clock["now"] = boundary

        svc._on_job = complete_at_boundary
        with patch("kiro_crew.cron.time.time", side_effect=lambda: clock["now"]):
            assert await svc.run_job(job.id) is True

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == newer_occurrence
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_manual_completion_publication_failure_falls_back_to_result_merge(
        self, tmp_path: Path
    ) -> None:
        """A failed completion-side publish falls back to the locked result save."""
        svc = CronService(base_dir=tmp_path)
        start = datetime(2026, 1, 2, 13, 59, 30, tzinfo=timezone.utc).timestamp()
        boundary = datetime(2026, 1, 2, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(boundary) // 60)
        clock = {"now": start}
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        failed = False
        real_flush = svc._flush_pending_owed_fires_locked

        def fail_first_publication() -> set[str]:
            nonlocal failed
            if svc._pending_owed_fires and not failed:
                failed = True
                raise OSError("disk full")
            return real_flush()

        async def complete_at_boundary(_running_job: CronJob) -> None:
            clock["now"] = boundary

        svc._on_job = complete_at_boundary
        with (
            patch("kiro_crew.cron.time.time", side_effect=lambda: clock["now"]),
            patch.object(
                svc,
                "_flush_pending_owed_fires_locked",
                side_effect=fail_first_publication,
            ),
        ):
            assert await svc.run_job(job.id) is True

        assert failed is True
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

        # No later drain republishes or duplicates the occurrence: the result
        # merge already saved and retired this exact pending identity.
        assert await svc._persist_pending_owed_fires() == set()
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_completion_does_not_republish_an_already_captured_boundary(
        self, tmp_path: Path
    ) -> None:
        """A consumed admission-time publication stays consumed.

        This is the opposite control for the completion guard: a manual run that
        starts inside the due minute already captured that occurrence. If a
        replacement consumes it while the payload runs, completion must not put
        the same occurrence back and replay completed scheduled work.
        """
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(running_job: CronJob) -> None:
            assert running_job.owed_occurrence() == occurrence_id
            entered.set()
            await release.wait()

        def consume_from_replacement() -> None:
            replacement = CronService(base_dir=tmp_path)
            replacement_job = replacement.get_job(job.id)
            assert replacement_job is not None
            assert replacement_job.owed_occurrence() == occurrence_id
            replacement_job.set_owed_occurrence(None)
            replacement._save()

        svc._on_job = blocked
        with patch("kiro_crew.cron.time.time", return_value=now):
            run_task = asyncio.create_task(svc.run_job(job.id))
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.to_thread(consume_from_replacement)
            release.set()
            assert await run_task is True

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert svc._pending_owed_fires == {}

    @pytest.mark.asyncio
    async def test_manual_run_without_due_boundary_records_no_occurrence(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        now = datetime(2026, 1, 2, 13, 58, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
        )
        svc._jobs = [job]
        svc._save()

        async def completed(_job: CronJob) -> None:
            return None

        svc._on_job = completed
        with patch("kiro_crew.cron.time.time", return_value=now):
            assert await svc.run_job(job.id) is True
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    def test_boundary_identity_respects_skip_date_and_same_minute(self, tmp_path: Path) -> None:
        now = datetime(2026, 1, 2, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            timezone="America/Los_Angeles",
            skip_dates=["2026-01-02"],
        )
        assert CronService._cron_occurrence_at(job, now) is None
        job.skip_dates = []
        job.last_run_ts = now + 20
        assert CronService._cron_occurrence_at(job, now) is None
        job.last_run_ts = None
        job.enabled = False
        svc = CronService(base_dir=tmp_path)
        assert svc.occurrence_for_pruned_launch(job, now) is None

    @pytest.mark.asyncio
    async def test_run_job_isolated_keeps_a_pruned_every_job_overdue(self, tmp_path: Path) -> None:
        """The outer 'every'-job drift correction must not re-consume a
        keep_overdue (pruned-install) run: _execute deliberately left
        last_run_ts untouched so the replacement gateway retries at once."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        job.last_run_ts = 1234.5
        svc._jobs = [job]
        svc._save()
        svc._running = True

        async def pruned_execute(j):
            # _record_pruned_launch_skip contract as _execute observes it.
            j.last_status = "error"
            j.run_never_started = True
            j.keep_overdue = True

        with (
            patch.object(svc, "_execute_with_timeout", side_effect=pruned_execute),
            patch.object(svc, "_arm_timer"),
        ):
            await svc._run_job_isolated(job)

        assert job.last_run_ts == 1234.5

    @pytest.mark.asyncio
    async def test_quiesced_pruned_job_survives_a_store_reload(self, tmp_path: Path) -> None:
        """A per-object enabled=False dies at the next _sync (fresh disk copies
        are enabled by design, for the replacement gateway) — the service-level
        pruned-quiesce registry keeps the job out of the due-scan on THIS
        process regardless of reloads."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="watch",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
        svc._jobs = [job]
        svc._save()
        svc.quiesce_pruned("j1")

        # A store reload hands back a FRESH, enabled copy of the job.
        svc._sync()
        reloaded = next(j for j in svc._jobs if j.id == "j1")
        assert reloaded.enabled is True  # on-disk state, by design

        now = time.time()
        due = [
            j
            for j in svc._jobs
            if j.enabled
            and j.id not in svc._executing
            and j.id not in svc._pruned_quiesced
            and svc._is_due(j, now)
        ]
        assert due == []
        # Without the registry the job WOULD be due — proving it is the guard.
        assert svc._is_due(reloaded, now) is True
        # And the wake computation must skip it too, or a quiesced overdue job
        # drives _next_wake_secs to 0 and re-arms in a zero-delay loop.
        assert svc._next_wake_secs() is None

    def test_a_string_false_owed_fire_on_disk_loads_as_false(self, tmp_path: Path) -> None:
        """Strict identity on deserialization: the string "false" is truthy
        and would dispatch the job outside its schedule."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
        )
        svc._jobs = [job]
        svc._save()
        store = tmp_path / "crons.json"
        data = json.loads(store.read_text())
        data["jobs"][0]["owed_fire"] = "false"
        store.write_text(json.dumps(data))

        svc2 = CronService(base_dir=tmp_path)
        loaded = next(j for j in svc2._jobs if j.id == "j1")
        assert loaded.owed_fire is False

    def test_merge_does_not_queue_removal_for_a_never_started_one_shot(
        self, tmp_path: Path
    ) -> None:
        """The retention guards travel with the deferred delete: a never-started
        (pruned-skip) one-shot is retained, not consumed, even when the store is
        unreadable at merge time."""
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="oneshot",
            message="go",
            schedule=CronSchedule(kind="at", at_ts=1.0),
            delete_after_run=True,
        )
        svc._jobs = [job]
        svc._save()
        (tmp_path / "crons.json").write_text("{ not json")
        job.run_never_started = True

        svc._merge_job_result(job)  # must NOT raise

        assert "j1" not in svc._pending_removals

    @pytest.mark.asyncio
    async def test_manual_due_minute_publication_is_not_claimed_by_manual_run(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        occurrence_id = str(int(now) // 60)
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        claims: list[tuple[bool, str | None, str | None]] = []

        async def completed(running_job: CronJob) -> None:
            claims.append(
                (
                    running_job.id in svc._owed_fire_runs,
                    svc._owed_fire_runs.get(running_job.id),
                    running_job.owed_occurrence(),
                )
            )

        svc._on_job = completed
        with patch("kiro_crew.cron.time.time", return_value=now):
            assert await svc.run_job(job.id) is True

        assert claims == [(True, None, occurrence_id)]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id
        assert job.id not in svc._owed_fire_runs

    @pytest.mark.asyncio
    async def test_shared_alias_pre_rename_failure_retries_without_rerun(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        payloads: list[str] = []
        retry_entered = threading.Event()
        release_retry = threading.Event()
        real_save = svc._save
        save_calls = 0

        async def completed_payload(running_job: CronJob) -> None:
            payloads.append(running_job.id)

        def fail_then_settle() -> None:
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                raise OSError("before rename")
            retry_entered.set()
            assert release_retry.wait(5)
            real_save()

        admitted = type("Admission", (), {"admitted": True, "reason": ""})()
        svc._on_job = completed_payload
        svc._executing.add(job.id)
        try:
            with (
                patch.object(svc, "_save", side_effect=fail_then_settle),
                patch("kiro_crew.cron.admission_check", return_value=admitted),
            ):
                run_task = asyncio.create_task(svc._run_job_isolated(job))
                svc._running_tasks[job.id] = run_task
                assert await asyncio.to_thread(retry_entered.wait, 2)

                hot = svc.get_job(job.id)
                assert hot is not None
                assert hot.owed_occurrence() is None
                assert job.owed_occurrence() == "100"
                durable = CronService(base_dir=tmp_path).get_job(job.id)
                assert durable is not None
                assert durable.owed_occurrence() == "100"
                assert svc._last_digest != b""
                assert run_task.done() is False
                assert job.id in svc._executing
                assert svc._running_tasks[job.id] is run_task
                assert payloads == [job.id]

                release_retry.set()
                await asyncio.wait_for(run_task, 2)
        finally:
            release_retry.set()
            for task in list(svc._running_tasks.values()):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        records, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert records[0]["status"] == "success"
        assert payloads == [job.id]
        assert save_calls == 2
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids

        renamed = await svc.update_job_async(job.id, name="renamed")
        assert renamed is not None
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.name == "renamed"
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_shared_alias_save_failure_restores_claim(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"

        with patch.object(svc, "_save", side_effect=OSError("before rename")):
            with pytest.raises(OSError, match="did not clear owed occurrence"):
                if terminal == "cancel":
                    with patch(
                        "kiro_crew.cron.cron_script.kill_running_process",
                        return_value=False,
                    ):
                        await svc.cancel(job.id)
                else:
                    with patch("kiro_crew.sel.sel"):
                        await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        hot = svc.get_job(job.id)
        assert hot is not None
        assert hot.owed_occurrence() == "100"
        assert svc._last_digest != b""
        assert svc._terminal_retryable == {job.id: terminal}
        assert job.id in svc._terminal_settling

        if terminal == "cancel":
            with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
                assert await svc.cancel(job.id) is True
        else:
            with patch("kiro_crew.sel.sel"):
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_operation_called_by_run_does_not_await_its_caller(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        terminal_returned = asyncio.Event()
        wait_called = asyncio.Event()
        wait_orig = svc._cancel_run_task_and_wait
        called_from_run_values: list[bool] = []

        async def observe_wait(job_id: str, called_from_run: bool) -> None:
            called_from_run_values.append(called_from_run)
            wait_called.set()
            await wait_orig(job_id, called_from_run)

        svc._cancel_run_task_and_wait = observe_wait  # type: ignore[method-assign]

        async def terminate_self(running_job: CronJob) -> None:
            if terminal == "cancel":
                assert await svc.cancel(running_job.id) is True
            else:
                await svc._force_reap(running_job.id, elapsed=1900, deadline=1800)
            terminal_returned.set()

        svc._on_job = terminate_self
        svc._executing.add(job.id)
        with (
            patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False),
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            run_task = asyncio.create_task(svc._run_job_isolated(job))
            svc._running_tasks[job.id] = run_task
            await asyncio.wait_for(wait_called.wait(), 2)
            assert called_from_run_values == [True]
            await asyncio.wait_for(run_task, 2)

        assert terminal_returned.is_set()
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.last_status == "error"
        expected_error = "Cancelled by user" if terminal == "cancel" else "Reaped after"
        assert expected_error in (stored.last_error or "")
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._run_tokens
        assert job.id not in svc._result_merge_relinquished_runs
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids
        assert (job.id in svc._cancelled_jobs) is (terminal == "cancel")
        assert (job.id in svc._reaped_jobs) is (terminal == "reap")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_settlement_blocks_timer_redispatch(
        self, tmp_path: Path, terminal: str
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
            owed_fire=True,
            owed_fire_id=str(int(now) // 60),
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = job.owed_occurrence()
        entered = threading.Event()
        release = threading.Event()
        real_merge = svc._merge_terminal_state_locked
        executions: list[str] = []

        def blocked_merge(*args, **kwargs) -> None:
            entered.set()
            assert release.wait(5), "terminal merge was not released"
            real_merge(*args, **kwargs)

        async def execute(running_job: CronJob) -> None:
            executions.append(running_job.id)

        async def settle() -> None:
            if terminal == "cancel":
                with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
                    assert await svc.cancel(job.id) is True
            else:
                with patch("kiro_crew.sel.sel"):
                    await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        svc._on_job = execute
        with patch.object(svc, "_merge_terminal_state_locked", side_effect=blocked_merge):
            terminal_task = asyncio.create_task(settle())
            assert await asyncio.to_thread(entered.wait, 2), "terminal merge never started"
            try:
                admitted = type("Admission", (), {"admitted": True, "reason": ""})()
                with (
                    patch("kiro_crew.cron.time.time", return_value=now + 120),
                    patch("kiro_crew.cron.admission_check", return_value=admitted),
                ):
                    wake_while_settling = svc._next_wake_secs()
                    await svc._on_timer()
                    await asyncio.sleep(0)
                observed = list(executions)
            finally:
                release.set()
                await terminal_task
                pending = list(svc._running_tasks.values())
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        assert wake_while_settling is None
        assert observed == []

    @pytest.mark.asyncio
    async def test_cancelled_pruned_merge_keeps_claim_without_never_started_marker(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
            strict_schedule=True,
        )
        svc._jobs = [job]
        svc._save()

        async def cancelled_pruned(running_job: CronJob) -> None:
            running_job.run_never_started = False
            running_job.keep_overdue = True
            raise asyncio.CancelledError

        with patch.object(svc, "_execute_with_timeout", side_effect=cancelled_pruned):
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == "100"

    @pytest.mark.asyncio
    async def test_shutdown_cancel_queues_process_claim_before_first_lock(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job, run_task, _release, _claims, occurrence_id = (
            await self._start_blocked_scheduled_occurrence(svc, now)
        )
        real_file_lock = svc._file_lock
        lock_calls = 0

        def busy_once():
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 1:
                raise CronStoreBusy("first result lock busy")
            return real_file_lock()

        with patch.object(svc, "_file_lock", side_effect=busy_once):
            await asyncio.wait_for(svc.stop(), 2)

        assert run_task.cancelled()
        assert lock_calls >= 2
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == occurrence_id

    @pytest.mark.asyncio
    async def test_shutdown_cancel_keeps_pending_claim_when_every_lock_fails(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job, run_task, _release, _claims, occurrence_id = (
            await self._start_blocked_scheduled_occurrence(svc, now)
        )

        with (
            patch.object(
                svc,
                "_file_lock",
                side_effect=CronStoreBusy("all result and drain locks busy"),
            ),
            pytest.raises(RuntimeError, match="could not durably hand off 1 owed"),
        ):
            await asyncio.wait_for(svc.stop(), 2)

        assert run_task.cancelled()
        assert svc._pending_owed_fires == {job.id: occurrence_id}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authority", ["completed", "newer-debt"])
    async def test_shutdown_cancel_first_lock_busy_defers_to_replacement_authority(
        self, tmp_path: Path, authority: str
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        job, run_task, _release, _claims, occurrence_id = (
            await self._start_blocked_scheduled_occurrence(svc, now)
        )
        replacement = CronService(base_dir=tmp_path)
        target = replacement.get_job(job.id)
        assert target is not None
        newer = str(int(occurrence_id) + 1)
        if authority == "completed":
            target.last_run_ts = int(occurrence_id) * 60
            target.last_status = "ok"
        else:
            target.set_owed_occurrence(newer)
        replacement._save()
        real_file_lock = svc._file_lock
        lock_calls = 0

        def busy_once():
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 1:
                raise CronStoreBusy("first result lock busy")
            return real_file_lock()

        with patch.object(svc, "_file_lock", side_effect=busy_once):
            await asyncio.wait_for(svc.stop(), 2)

        assert run_task.cancelled()
        assert svc._pending_owed_fires == {}
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() == (None if authority == "completed" else newer)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    @pytest.mark.parametrize("worker_fails", [False, True], ids=["saved", "save-failed"])
    async def test_terminal_cancellation_holds_fence_until_merge_worker_finishes(
        self, tmp_path: Path, terminal: str, worker_fails: bool
    ) -> None:
        now = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc).timestamp()
        svc = CronService(base_dir=tmp_path)
        svc._history.append = AsyncMock()  # type: ignore[method-assign]
        audit = MagicMock()
        job, run_task, _run_release, _claims, occurrence_id = (
            await self._start_blocked_scheduled_occurrence(svc, now)
        )
        await asyncio.to_thread(self._publish_sibling_occurrence, tmp_path, job.id, occurrence_id)
        merge_entered = threading.Event()
        merge_release = threading.Event()
        merge_finished = threading.Event()
        real_merge = svc._merge_terminal_state_locked
        redispatched: list[str] = []

        def blocked_merge(*args, **kwargs) -> None:
            merge_entered.set()
            assert merge_release.wait(5), "terminal merge worker was not released"
            try:
                if worker_fails:
                    with patch.object(svc, "_save", side_effect=OSError("before rename")):
                        real_merge(*args, **kwargs)
                else:
                    real_merge(*args, **kwargs)
            finally:
                merge_finished.set()

        async def unexpected_run(running_job: CronJob) -> None:
            redispatched.append(running_job.id)

        async def settle() -> None:
            with (
                patch("kiro_crew.cron.sel.sel", return_value=audit),
                patch("kiro_crew.sel.sel", return_value=audit),
            ):
                if terminal == "cancel":
                    with patch(
                        "kiro_crew.cron.cron_script.kill_running_process", return_value=False
                    ):
                        assert await svc.cancel(job.id) is True
                else:
                    await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        with patch.object(svc, "_merge_terminal_state_locked", side_effect=blocked_merge):
            terminal_task = asyncio.create_task(settle())
            assert await asyncio.to_thread(merge_entered.wait, 2), "terminal merge never started"
            terminal_task.cancel()
            await asyncio.sleep(0)
            try:
                assert terminal_task.done() is False
                assert job.id in svc._terminal_settling
                assert await svc.run_job(job.id) is False
                svc._on_job = unexpected_run
                admitted = type("Admission", (), {"admitted": True, "reason": ""})()
                with (
                    patch("kiro_crew.cron.time.time", return_value=now + 120),
                    patch("kiro_crew.cron.admission_check", return_value=admitted),
                ):
                    assert svc._next_wake_secs() is None
                    await svc._on_timer()
                    await asyncio.sleep(0)
                assert redispatched == []
            finally:
                merge_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(terminal_task, 2)
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

        assert merge_finished.is_set()
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        if worker_fails:
            svc._history.append.assert_not_awaited()  # type: ignore[attr-defined]
            audit.log_tool_invocation.assert_not_called()
            assert svc._terminal_retryable == {job.id: terminal}
            assert job.id in svc._terminal_settling
            assert stored.owed_occurrence() == occurrence_id
            with (
                patch("kiro_crew.cron.sel.sel", return_value=audit),
                patch("kiro_crew.sel.sel", return_value=audit),
            ):
                if terminal == "cancel":
                    with patch(
                        "kiro_crew.cron.cron_script.kill_running_process",
                        return_value=False,
                    ):
                        assert await svc.cancel(job.id) is True
                else:
                    await svc._force_reap(job.id, elapsed=1900, deadline=1800)
            stored = CronService(base_dir=tmp_path).get_job(job.id)
            assert stored is not None
        svc._history.append.assert_awaited_once()  # type: ignore[attr-defined]
        audit.log_tool_invocation.assert_called_once()
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._terminal_retryable
        assert job.id not in svc._owed_fire_runs
        assert job.id not in svc._run_occurrence_ids
        assert stored.owed_occurrence() is None

        svc._on_job = unexpected_run
        assert await svc.run_job(job.id) is True
        assert redispatched == [job.id]
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_cancellation_before_merge_clears_fence(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"

        if terminal == "cancel":
            with (
                patch(
                    "kiro_crew.cron.cron_script.kill_running_process",
                    side_effect=asyncio.CancelledError,
                ),
                pytest.raises(asyncio.CancelledError),
            ):
                await svc.cancel(job.id)
        else:

            class CancelledSessions:
                async def reset(self, *_args, **_kwargs) -> None:
                    raise asyncio.CancelledError

            svc._sessions = CancelledSessions()  # type: ignore[assignment]
            with pytest.raises(asyncio.CancelledError):
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        assert job.id not in svc._terminal_settling

    @pytest.mark.asyncio
    async def test_cancel_failure_before_cleanup_returns_token_to_live_run(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="script",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
            script="/cron.py:run",
        )
        svc._jobs = [job]
        svc._save()
        payload_entered = asyncio.Event()
        payload_release = asyncio.Event()

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            await payload_release.wait()

        svc._on_job = blocked_payload
        svc._executing.add(job.id)
        svc._job_run_meta[job.id] = (time.time(), "scheduled")
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(payload_entered.wait(), 2)
        run_token = svc._run_tokens[job.id]
        run_meta = svc._job_run_meta[job.id]
        started_at = svc._job_start_times[job.id]
        started_mono = svc._job_start_monotonic[job.id]
        jitter = svc._job_jitter[job.id]

        with (
            patch(
                "kiro_crew.cron.cron_script.kill_running_process",
                side_effect=RuntimeError("kill probe failed before signal"),
            ),
            pytest.raises(RuntimeError, match="before signal"),
        ):
            await svc.cancel(job.id)

        try:
            assert svc._run_tokens[job.id] is run_token
            assert job.id in svc._executing
            assert svc._running_tasks[job.id] is run_task
            assert job.id not in svc._terminal_settling
            assert job.id not in svc._cancelled_jobs
            assert svc._job_run_meta[job.id] == run_meta
            assert svc._job_start_times[job.id] == started_at
            assert svc._job_start_monotonic[job.id] == started_mono
            assert svc._job_jitter[job.id] == jitter
        finally:
            payload_release.set()
            await asyncio.wait_for(run_task, 2)

        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic
        assert job.id not in svc._job_jitter

    @pytest.mark.asyncio
    async def test_cancel_failure_consumes_metadata_when_run_wins_race(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="script",
            message="go",
            schedule=CronSchedule(kind="every", every_secs=60),
            script="/cron.py:run",
        )
        svc._jobs = [job]
        svc._save()
        payload_entered = asyncio.Event()
        payload_release = asyncio.Event()
        kill_entered = threading.Event()
        kill_release = threading.Event()

        async def blocked_payload(_running_job: CronJob) -> None:
            payload_entered.set()
            await payload_release.wait()

        def fail_after_run_completes(_job_id: str) -> bool:
            kill_entered.set()
            assert kill_release.wait(5)
            raise RuntimeError("kill probe failed before signal")

        svc._on_job = blocked_payload
        svc._executing.add(job.id)
        svc._job_run_meta[job.id] = (time.time(), "scheduled")
        run_task = asyncio.create_task(svc._run_job_isolated(job))
        svc._running_tasks[job.id] = run_task
        await asyncio.wait_for(payload_entered.wait(), 2)

        try:
            with patch(
                "kiro_crew.cron.cron_script.kill_running_process",
                side_effect=fail_after_run_completes,
            ):
                cancel_task = asyncio.create_task(svc.cancel(job.id))
                assert await asyncio.to_thread(kill_entered.wait, 2)
                payload_release.set()
                await asyncio.wait_for(run_task, 2)
                assert job.id not in svc._executing
                assert job.id in svc._job_start_times
                kill_release.set()
                with pytest.raises(RuntimeError, match="before signal"):
                    await asyncio.wait_for(cancel_task, 2)
        finally:
            payload_release.set()
            kill_release.set()
            await asyncio.gather(run_task, return_exceptions=True)

        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._run_tokens
        assert job.id not in svc._terminal_settling
        assert job.id not in svc._cancelled_jobs
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic
        assert job.id not in svc._job_jitter

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_caller_cancellation_before_worker_finishes_bookkeeping(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        before_entered = threading.Event()
        before_release = threading.Event()
        events: list[str] = []
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = lambda **_kwargs: events.append("audit")

        async def append_history(_record) -> None:
            events.append("history")

        svc._history.append = AsyncMock(side_effect=append_history)  # type: ignore[method-assign]
        svc._push_refresh = lambda kind: events.append(f"refresh:{kind}")

        def block_kill(*_args, **_kwargs) -> bool:
            before_entered.set()
            assert before_release.wait(5), "cancel pre-worker stage was not released"
            return False

        class BlockingSessions:
            async def reset(self, *_args, **_kwargs) -> None:
                before_entered.set()
                assert await asyncio.to_thread(before_release.wait, 5)

        if terminal == "reap":
            svc._sessions = BlockingSessions()  # type: ignore[assignment]

        async def settle() -> None:
            if terminal == "cancel":
                assert await svc.cancel(job.id) is True
            else:
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        kill_patch = (
            patch("kiro_crew.cron.cron_script.kill_running_process", side_effect=block_kill)
            if terminal == "cancel"
            else contextlib.nullcontext()
        )
        with (
            kill_patch,
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            terminal_task = asyncio.create_task(settle())
            assert await asyncio.to_thread(before_entered.wait, 2)
            terminal_task.cancel()
            await asyncio.sleep(0)
            assert terminal_task.done() is False
            assert job.id in svc._terminal_settling
            before_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(terminal_task, 2)

        assert events[0] == "history"
        assert events[-1] == "audit"
        assert job.id not in svc._terminal_settling
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["cancel", "reap"])
    async def test_terminal_caller_cancellation_during_history_defers_audit_and_cleanup(
        self, tmp_path: Path, terminal: str
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        job = CronJob(
            id="j1",
            name="daily",
            message="go",
            schedule=CronSchedule(kind="cron", cron_expr="0 6 * * *"),
            owed_fire=True,
            owed_fire_id="100",
        )
        svc._jobs = [job]
        svc._save()
        svc._executing.add(job.id)
        svc._owed_fire_runs[job.id] = "100"
        history_entered = asyncio.Event()
        history_release = asyncio.Event()
        events: list[str] = []
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = lambda **_kwargs: events.append("audit")

        async def blocked_history(_record) -> None:
            events.append("history")
            history_entered.set()
            await history_release.wait()

        svc._history.append = AsyncMock(side_effect=blocked_history)  # type: ignore[method-assign]
        svc._push_refresh = lambda kind: events.append(f"refresh:{kind}")

        async def settle() -> None:
            if terminal == "cancel":
                with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
                    assert await svc.cancel(job.id) is True
            else:
                await svc._force_reap(job.id, elapsed=1900, deadline=1800)

        with (
            patch("kiro_crew.cron.sel.sel", return_value=audit),
            patch("kiro_crew.sel.sel", return_value=audit),
        ):
            terminal_task = asyncio.create_task(settle())
            await asyncio.wait_for(history_entered.wait(), 2)
            terminal_task.cancel()
            await asyncio.sleep(0)
            assert terminal_task.done() is False
            assert job.id in svc._terminal_settling
            assert await svc.run_job(job.id) is False
            history_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(terminal_task, 2)

        assert events[0] == "history"
        assert events[-1] == "audit"
        assert events.index("history") < events.index("audit")
        assert job.id not in svc._terminal_settling
        stored = CronService(base_dir=tmp_path).get_job(job.id)
        assert stored is not None
        assert stored.owed_occurrence() is None

    @pytest.mark.parametrize("cleanup", ["isolated", "cancel", "reap"])
    @pytest.mark.parametrize(
        ("successor_claim", "successor_occurrence"),
        [("101", "101"), (None, None)],
        ids=["scheduled", "manual-no-debt"],
    )
    def test_stale_cleanup_cannot_pop_successor_run_ownership(
        self,
        tmp_path: Path,
        cleanup: str,
        successor_claim: str | None,
        successor_occurrence: str | None,
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        old_token = object()
        svc._run_tokens["j1"] = old_token
        svc._reaped_jobs.add("j1")
        svc._cancelled_jobs.add("j1")
        successor_token = svc._mint_run_token("j1")
        svc._result_merge_relinquished_runs["j1"] = successor_token
        assert "j1" not in svc._reaped_jobs
        assert "j1" not in svc._cancelled_jobs
        svc._owed_fire_runs["j1"] = successor_claim
        successor_authority = None
        if successor_occurrence is not None:
            svc._run_occurrence_ids["j1"] = successor_occurrence
            successor_authority = ("UTC", 21)
            svc._run_occurrence_timezone_authorities[("j1", successor_occurrence)] = (
                successor_authority
            )
        terminal_owner = cleanup != "isolated"
        if terminal_owner:
            svc._terminal_settling["j1"] = old_token

        assert svc._retire_run_ownership("j1", old_token, terminal_owner=terminal_owner) is False

        assert svc._run_tokens["j1"] is successor_token
        assert svc._result_merge_relinquished_runs["j1"] is successor_token
        assert "j1" in svc._owed_fire_runs
        assert svc._owed_fire_runs["j1"] == successor_claim
        if successor_occurrence is None:
            assert "j1" not in svc._run_occurrence_ids
        else:
            assert svc._run_occurrence_ids["j1"] == successor_occurrence
            assert (
                svc._run_occurrence_timezone_authorities[("j1", successor_occurrence)]
                == successor_authority
            )
        assert "j1" not in svc._terminal_settling
