"""Regression tests for the cron_add orphaned-job bug (audit E, mcp_cron.py).

Bug: timezone / skip_dates were validated AFTER ``svc.add_job()`` — which
persists the job immediately (``self._save()``). An invalid value therefore
returned an error to the caller while leaving the job on disk, and a retried
cron_add would then DUPLICATE it. The fix moves calendar-validity validation
INTO ``add_job`` (the persistence owner), which checks and folds
timezone/skip_dates into the job before its single ``_save()`` — so no caller
can strand an invalid or half-populated job, and every create path is covered
by one check. ``mcp_cron`` keeps a thin pre-check solely to return a redacted,
user-facing error message.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.cron import CronService
from kiro_crew.mcp_cron import _call_tool_inner


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


def _jobs(tmp_path):
    return CronService(base_dir=tmp_path).list_jobs()


class TestCronAddInvalidTimezoneNoOrphan:
    def test_invalid_timezone_returns_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"tz-bad-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "hi", "every": 120, "timezone": "Not/AZone"},
        )
        assert result.startswith("Error"), result
        assert "invalid timezone" in result

    def test_invalid_timezone_persists_no_job(self, monkeypatch, tmp_path):
        """The failure scenario from the audit: no orphaned job left behind."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"tz-orphan-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": name, "message": "hi", "every": 120, "timezone": "Not/AZone"},
        )
        # Pre-fix this list contained the persisted-but-errored job.
        assert [j for j in _jobs(tmp_path) if j.name == name] == []

    def test_invalid_timezone_retry_does_not_duplicate(self, monkeypatch, tmp_path):
        """A retried cron_add after an invalid-tz error must not accumulate jobs."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"tz-retry-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            _call_tool_inner(
                "cron_add",
                {"name": name, "message": "hi", "every": 120, "timezone": "Not/AZone"},
            )
        assert [j for j in _jobs(tmp_path) if j.name == name] == []


class TestCronAddInvalidSkipDatesNoOrphan:
    def test_invalid_skip_date_returns_error_and_no_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"skip-bad-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {
                "name": name,
                "message": "hi",
                "cron_expr": "0 9 * * *",
                "skip_dates": ["2026-13-99"],
            },
        )
        assert result.startswith("Error"), result
        assert "invalid skip_date" in result
        assert [j for j in _jobs(tmp_path) if j.name == name] == []


class TestCronAddValidTimezoneStillWorks:
    def test_valid_timezone_persists_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"tz-ok-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {
                "name": name,
                "message": "hi",
                "cron_expr": "0 9 * * *",
                "timezone": "America/New_York",
                "skip_dates": ["2026-12-25"],
            },
        )
        assert "Added job" in result, result
        matching = [j for j in _jobs(tmp_path) if j.name == name]
        assert len(matching) == 1
        assert matching[0].timezone == "America/New_York"
        assert matching[0].skip_dates == ["2026-12-25"]


class TestCronUpdateValidation:
    """The sibling write path (cron_update) must reject the same invalid
    timezone / calendar-invalid skip_dates the cron_add path does -- the fix
    class was re-open here before validation was added.
    """

    def _make_job(self, tmp_path):
        name = f"upd-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "hi", "cron_expr": "0 9 * * *"},
        )
        assert "Added job" in result, result
        jid = result.split("Added job:", 1)[1].split("(", 1)[0].strip()
        return name, jid

    def test_update_invalid_skip_date_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        _name, jid = self._make_job(tmp_path)
        result = _call_tool_inner(
            "cron_update",
            {"job_id": jid, "skip_dates": ["2026-02-30"]},
        )
        assert result.startswith("Error"), result
        assert "invalid skip_date" in result
        # The bad value must not have persisted.
        job = [j for j in _jobs(tmp_path) if j.id == jid][0]
        assert job.skip_dates == []

    def test_update_invalid_timezone_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        _name, jid = self._make_job(tmp_path)
        result = _call_tool_inner(
            "cron_update",
            {"job_id": jid, "timezone": "Not/AZone"},
        )
        assert result.startswith("Error"), result
        assert "invalid timezone" in result

    def test_update_valid_skip_dates_persist(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        _name, jid = self._make_job(tmp_path)
        result = _call_tool_inner(
            "cron_update",
            {"job_id": jid, "skip_dates": ["2026-12-25"], "timezone": "America/New_York"},
        )
        assert "Updated job" in result, result
        job = [j for j in _jobs(tmp_path) if j.id == jid][0]
        assert job.skip_dates == ["2026-12-25"]
        assert job.timezone == "America/New_York"


class TestCronServiceUpdateJobValidation:
    """Validation lives at the persistence owner so non-MCP callers
    (dashboard, CLI) are covered too."""

    def test_service_update_rejects_calendar_invalid_skip_date(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="svc", message="hi", cron_expr="0 9 * * *")
        import pytest

        with pytest.raises(ValueError, match="skip_date"):
            svc.update_job(job.id, skip_dates=["2026-02-30"])
        # unchanged on disk
        assert CronService(base_dir=tmp_path).list_jobs()[0].skip_dates == []

    def test_service_update_rejects_non_padded_skip_date(self, tmp_path):
        # "2026-1-1" parses via strptime but renders back as "2026-01-01",
        # so fire-time skip matching would silently never match. It must be
        # rejected at the persistence owner rather than stranded on disk.
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="svc", message="hi", cron_expr="0 9 * * *")
        import pytest

        with pytest.raises(ValueError, match="skip_date"):
            svc.update_job(job.id, skip_dates=["2026-1-1"])
        assert CronService(base_dir=tmp_path).list_jobs()[0].skip_dates == []

    def test_service_update_rejects_invalid_timezone(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="svc", message="hi", cron_expr="0 9 * * *")
        import pytest

        with pytest.raises(ValueError, match="timezone"):
            svc.update_job(job.id, timezone="Not/AZone")


class TestCronServiceAddJobValidation:
    """The create path is now owned by ``add_job`` too: it validates
    timezone/skip_dates BEFORE its single ``_save()`` and folds them into the
    job, so ANY caller (MCP, apps SDK, dashboard, CLI) is covered by one check
    and no half-populated or invalid job can be stranded on disk."""

    def test_add_rejects_invalid_timezone_and_persists_nothing(self, tmp_path):
        import pytest

        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="timezone"):
            svc.add_job(name="svc", message="hi", cron_expr="0 9 * * *", timezone="Not/AZone")
        # No orphaned job persisted by the pre-validation failure.
        assert CronService(base_dir=tmp_path).list_jobs() == []

    def test_add_rejects_calendar_invalid_skip_date_and_persists_nothing(self, tmp_path):
        import pytest

        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="skip_date"):
            svc.add_job(
                name="svc", message="hi", cron_expr="0 9 * * *", skip_dates=["2026-02-30"]
            )
        assert CronService(base_dir=tmp_path).list_jobs() == []

    def test_add_rejects_non_padded_skip_date_and_persists_nothing(self, tmp_path):
        # "2026-1-1" is accepted by strptime but never matches at fire time
        # (compared against a zero-padded rendering). add_job must reject it
        # so a silently-never-firing skip is not baked into persisted data.
        import pytest

        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="skip_date"):
            svc.add_job(
                name="svc", message="hi", cron_expr="0 9 * * *", skip_dates=["2026-1-1"]
            )
        assert CronService(base_dir=tmp_path).list_jobs() == []

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            name="svc",
            message="hi",
            cron_expr="0 9 * * *",
            timezone="America/New_York",
            skip_dates=["2026-12-25"],
        )
        # Reload from a fresh service to prove they landed in the FIRST persist.
        reloaded = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.id == job.id][0]
        assert reloaded.timezone == "America/New_York"
        assert reloaded.skip_dates == ["2026-12-25"]
