"""A one-shot job can be read back and re-timed, not just created.

`at`/`delay`/`at_time` landed on create only, which left a one-shot as the one
schedule kind a client could create and then neither render nor edit: the list
payload carried no machine-readable fire time, `PATCH` accepted no one-shot
spelling, and the store had no `at_ts` branch to apply if it had.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from body_stream_helpers import attach_body  # noqa: E402

from kiro_crew.cron import CronService  # noqa: E402
from kiro_crew.dashboard.handlers import cron as cron_handlers  # noqa: E402

_FUTURE = 4_000_000_000.0  # comfortably inside the 2100 cap


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)


def _service_with_oneshot(at_ts: float = _FUTURE):
    crons = CronService()
    job = crons.add_job(
        name="remind me",
        message="ship it",
        at_ts=at_ts,
        delete_after_run=True,
    )
    return crons, job


def _request(crons, body=None, job_id=None):
    """Mirror the harness the existing cron handler tests use.

    The list handler reads `has_slot`, `is_running` and `running_since` off the
    state and serializes the result, so a bare MagicMock leaks unserializable
    attributes into the JSON body.
    """
    state = MagicMock()
    state.crons = crons
    state.has_slot.return_value = False
    state.crons_is_running = False
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": job_id} if job_id else {}
    if body is not None:
        attach_body(request, body)
    return request


class TestTheListPayloadCarriesTheFireTime:
    @pytest.mark.asyncio
    async def test_a_one_shot_reports_at_ts(self):
        crons, job = _service_with_oneshot()
        resp = await cron_handlers.api_crons(_request(crons))
        row = next(r for r in _rows(resp) if r["id"] == job.id)
        assert row["at_ts"] == _FUTURE
        # And the recurring fields stay None, so a client can branch on the kind.
        assert row["cron_expr"] is None
        assert row["every_secs"] is None

    @pytest.mark.asyncio
    async def test_a_recurring_job_reports_no_at_ts(self):
        crons = CronService()
        crons.add_job(name="hourly", message="x", every_secs=3600)
        resp = await cron_handlers.api_crons(_request(crons))
        assert _rows(resp)[0]["at_ts"] is None
        assert _rows(resp)[0]["every_secs"] == 3600

    @pytest.mark.asyncio
    async def test_delete_after_run_is_reported_independently(self):
        """The flag is its own field, so it is reported rather than inferred."""
        crons, _ = _service_with_oneshot()
        resp = await cron_handlers.api_crons(_request(crons))
        assert "delete_after_run" in _rows(resp)[0]


class TestRetimingAOneShot:
    @pytest.mark.asyncio
    async def test_patch_accepts_an_absolute_fire_time(self):
        crons, job = _service_with_oneshot()
        later = _FUTURE + 3600
        resp = await cron_handlers.api_cron_update(_request(crons, {"at": later}, job.id))
        assert resp.status == 200
        stored = crons.get_job(job.id)
        assert stored.schedule.kind == "at"
        assert stored.schedule.at_ts == later

    @pytest.mark.asyncio
    async def test_patch_accepts_a_relative_delay(self):
        crons, job = _service_with_oneshot()
        before = time.time()
        resp = await cron_handlers.api_cron_update(_request(crons, {"delay": 600}, job.id))
        assert resp.status == 200
        at_ts = crons.get_job(job.id).schedule.at_ts
        assert before + 590 <= at_ts <= time.time() + 610

    @pytest.mark.asyncio
    async def test_an_unparseable_time_is_refused_not_stored(self):
        crons, job = _service_with_oneshot()
        resp = await cron_handlers.api_cron_update(
            _request(crons, {"at_time": "whenever i feel like it"}, job.id)
        )
        assert resp.status == 400
        assert crons.get_job(job.id).schedule.at_ts == _FUTURE

    @pytest.mark.asyncio
    async def test_a_recurring_job_is_untouched_by_an_unrelated_patch(self):
        crons = CronService()
        job = crons.add_job(name="hourly", message="x", every_secs=3600)
        resp = await cron_handlers.api_cron_update(_request(crons, {"name": "renamed"}, job.id))
        assert resp.status == 200
        assert crons.get_job(job.id).schedule.kind == "every"


class TestTheStoreOwnsTheContract:
    def test_at_ts_and_a_recurring_spelling_together_are_refused(self):
        """A schedule has one kind, so taking both would silently drop one."""
        crons, job = _service_with_oneshot()
        with pytest.raises(ValueError, match="at_ts with cron_expr or every_secs"):
            crons.update_job(job.id, at_ts=_FUTURE, every_secs=3600)
        assert crons.get_job(job.id).schedule.at_ts == _FUTURE

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, 4_102_444_801])
    def test_an_unrenderable_fire_time_is_refused(self, bad):
        """Bounded at the persistence owner, not only at the schema.

        A value past what `datetime.fromtimestamp` can render is stored fine by a
        caller that skips the schema and then raises while the job list is
        serialized, which fails `GET /api/crons` for EVERY job until that record is
        deleted by id.
        """
        crons, job = _service_with_oneshot()
        with pytest.raises(ValueError):
            crons.update_job(job.id, at_ts=bad)
        assert crons.get_job(job.id).schedule.at_ts == _FUTURE

    def test_a_non_numeric_fire_time_is_refused(self):
        crons, job = _service_with_oneshot()
        with pytest.raises(ValueError, match="Invalid at_ts"):
            crons.update_job(job.id, at_ts="soon")

    def test_re_timing_leaves_delete_after_run_alone(self):
        """The flag is a separate field, so re-timing never arms or disarms it."""
        crons, job = _service_with_oneshot()
        before = bool(getattr(crons.get_job(job.id), "delete_after_run", False))
        crons.update_job(job.id, at_ts=_FUTURE + 60)
        assert bool(getattr(crons.get_job(job.id), "delete_after_run", False)) is before


class TestConvertingAOneShotToRecurring:
    """The flag that consumes a one-shot must not survive the conversion.

    Every dashboard and composer-shortcut one-shot is created with
    `delete_after_run=True`, so a job converted to a recurring schedule with the
    flag still set ran ONCE and then deleted itself — a recurring schedule the user
    configured and never saw again.
    """

    def test_converting_to_an_interval_disarms_the_deletion(self):
        crons, job = _service_with_oneshot()
        assert crons.get_job(job.id).delete_after_run is True
        crons.update_job(job.id, every_secs=3600)
        stored = crons.get_job(job.id)
        assert stored.schedule.kind == "every"
        assert stored.delete_after_run is False

    def test_converting_to_a_cron_disarms_the_deletion(self):
        crons, job = _service_with_oneshot()
        crons.update_job(job.id, cron_expr="0 9 * * *")
        stored = crons.get_job(job.id)
        assert stored.schedule.kind == "cron"
        assert stored.delete_after_run is False

    def test_a_same_kind_retime_leaves_the_flag_alone(self):
        """A one-shot moved an hour later is still a one-shot."""
        crons, job = _service_with_oneshot()
        crons.update_job(job.id, at_ts=_FUTURE + 3600)
        stored = crons.get_job(job.id)
        assert stored.schedule.kind == "at"
        assert stored.delete_after_run is True

    def test_a_recurring_job_without_the_flag_is_untouched(self):
        crons = CronService()
        job = crons.add_job(name="hourly", message="x", every_secs=3600)
        crons.update_job(job.id, cron_expr="0 9 * * *")
        assert crons.get_job(job.id).delete_after_run is False


def _rows(resp):
    import json

    return json.loads(resp.body.decode())["jobs"]
