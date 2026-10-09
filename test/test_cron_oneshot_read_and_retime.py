"""One-shot cron jobs round-trip through the dashboard like recurring jobs."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from body_stream_helpers import attach_body  # noqa: E402

from kiro_crew.cron import CronService  # noqa: E402
from kiro_crew.cron_service.schedule import parse_time_string  # noqa: E402
from kiro_crew.dashboard.handlers import cron as cron_handlers  # noqa: E402

_FUTURE = 4_000_000_000.0


@pytest.fixture(autouse=True)
def _isolate_store(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)


def _state(crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    state.has_slot.return_value = False
    return state


def _request(crons: CronService, body=None, job_id: str = "") -> MagicMock:
    request = MagicMock()
    request.app = {"state": _state(crons)}
    request.match_info = {"job_id": job_id} if job_id else {}
    if body is not None:
        attach_body(request, body)
    return request


def _one_shot(crons: CronService, at: float = _FUTURE):
    return crons.add_job(name="remind me", message="ship it", at_ts=at, delete_after_run=True)


def _body(response):
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_list_reports_machine_readable_one_shot_fields():
    crons = CronService()
    job = _one_shot(crons)

    response = await cron_handlers.api_crons(_request(crons))
    row = next(item for item in _body(response)["jobs"] if item["id"] == job.id)

    assert row["at_ts"] == _FUTURE
    assert "delete_after_run" not in row
    assert row["cron_expr"] is None
    assert row["every_secs"] is None


@pytest.mark.asyncio
async def test_patch_retimes_a_one_shot():
    crons = CronService()
    job = _one_shot(crons)

    response = await cron_handlers.api_cron_update(_request(crons, {"at": _FUTURE + 3600}, job.id))

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored is not None
    assert stored.schedule.kind == "at"
    assert stored.schedule.at_ts == _FUTURE + 3600
    assert stored.delete_after_run is True


@pytest.mark.asyncio
async def test_patch_refuses_a_past_retime_without_mutation():
    crons = CronService()
    job = _one_shot(crons)

    response = await cron_handlers.api_cron_update(_request(crons, {"at": time.time() - 1}, job.id))

    assert response.status == 400
    assert crons.get_job(job.id).schedule.at_ts == _FUTURE


@pytest.mark.asyncio
async def test_patch_retimes_a_persistent_one_shot_without_arming_deletion():
    crons = CronService()
    job = crons.add_job(
        name="persistent one-shot",
        message="ship it",
        at_ts=_FUTURE,
        delete_after_run=False,
    )

    response = await cron_handlers.api_cron_update(_request(crons, {"at": _FUTURE + 3600}, job.id))

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored is not None
    assert stored.schedule.at_ts == _FUTURE + 3600
    assert stored.delete_after_run is False


def test_converting_one_shot_to_recurring_clears_delete_after_run():
    crons = CronService()
    interval = _one_shot(crons)
    cron = _one_shot(crons)

    crons.update_job(interval.id, every_secs=3600)
    crons.update_job(cron.id, cron_expr="0 9 * * *")

    assert crons.get_job(interval.id).delete_after_run is False
    assert crons.get_job(cron.id).delete_after_run is False


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, 4_102_444_801])
def test_store_refuses_unrenderable_one_shot_retime(bad):
    crons = CronService()
    job = _one_shot(crons)

    with pytest.raises(ValueError):
        crons.update_job(job.id, at_ts=bad)

    assert crons.get_job(job.id).schedule.at_ts == _FUTURE


def test_update_refuses_zero_timestamp_without_converting_recurring_job():
    crons = CronService()
    job = crons.add_job(name="hourly", message="x", every_secs=3600)

    with pytest.raises(ValueError, match="at_ts out of range"):
        crons.update_job(job.id, at_ts=0)

    stored = crons.get_job(job.id)
    assert stored is not None
    assert stored.schedule.kind == "every"
    assert stored.schedule.every_secs == 3600
    assert stored.delete_after_run is False


def test_same_kind_recurring_edit_preserves_delete_after_run():
    crons = CronService()
    job = crons.add_job(name="odd", message="x", cron_expr="0 8 * * *")
    job.delete_after_run = True
    crons._save()

    crons.update_job(job.id, cron_expr="0 9 * * *")

    assert crons.get_job(job.id).delete_after_run is True


def test_converting_recurring_to_one_shot_arms_delete_after_run():
    crons = CronService()
    job = crons.add_job(name="hourly", message="x", every_secs=3600)

    crons.update_job(job.id, at_ts=_FUTURE)

    stored = crons.get_job(job.id)
    assert stored.schedule.kind == "at"
    assert stored.delete_after_run is True


# ── A retimed `at_time` means the same instant PATCH-side as create-side ──

# Fixed wall clock, so each zone's epoch is deterministic. No DST transition in
# either zone at this instant, so the parser resolves it unambiguously.
_WALL_CLOCK = "2099-06-01 17:00"
_JOB_TZ = "Asia/Tokyo"
_GATEWAY_TZ = "America/Los_Angeles"


@pytest.fixture
def _gateway_tz(monkeypatch):
    """Pin the CONFIGURED zone away from the job's, so a fallback bug is visible.

    Without this the gateway's own zone is whatever the test host uses, and a
    resolver that ignored both the request and the stored timezone could still
    land on the expected epoch by coincidence.
    """
    monkeypatch.setattr("kiro_crew.cron.get_local_tz", lambda: (_GATEWAY_TZ, ZoneInfo(_GATEWAY_TZ)))


def _instant_in(tz_name: str) -> float:
    """The epoch ``_WALL_CLOCK`` names in *tz_name*, via the shared parser."""
    parsed = parse_time_string(_WALL_CLOCK, tz_name)
    assert isinstance(parsed, float), parsed
    return parsed


@pytest.mark.asyncio
async def test_patch_at_time_reads_the_request_timezone(_gateway_tz):
    crons = CronService()
    job = _one_shot(crons)

    response = await cron_handlers.api_cron_update(
        _request(crons, {"at_time": _WALL_CLOCK, "timezone": _JOB_TZ}, job.id)
    )

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored.schedule.at_ts == _instant_in(_JOB_TZ)
    assert stored.schedule.at_ts != _instant_in(_GATEWAY_TZ)
    assert stored.timezone == _JOB_TZ


@pytest.mark.asyncio
async def test_patch_at_time_falls_back_to_the_stored_job_timezone(_gateway_tz):
    crons = CronService()
    job = crons.add_job(
        name="tokyo reminder",
        message="ship it",
        at_ts=_FUTURE,
        delete_after_run=True,
        timezone=_JOB_TZ,
    )

    response = await cron_handlers.api_cron_update(
        _request(crons, {"at_time": _WALL_CLOCK}, job.id)
    )

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored.schedule.at_ts == _instant_in(_JOB_TZ)
    assert stored.schedule.at_ts != _instant_in(_GATEWAY_TZ)
    # The request named no zone, so the stored one is untouched.
    assert stored.timezone == _JOB_TZ


@pytest.mark.asyncio
async def test_patch_clearing_the_timezone_resolves_at_time_in_the_configured_zone(_gateway_tz):
    crons = CronService()
    job = crons.add_job(
        name="tokyo reminder",
        message="ship it",
        at_ts=_FUTURE,
        delete_after_run=True,
        timezone=_JOB_TZ,
    )

    response = await cron_handlers.api_cron_update(
        _request(crons, {"at_time": _WALL_CLOCK, "timezone": ""}, job.id)
    )

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored.timezone == ""
    assert stored.schedule.at_ts == _instant_in(_GATEWAY_TZ)


@pytest.mark.asyncio
async def test_patch_refuses_an_invalid_timezone_before_resolving_at_time():
    crons = CronService()
    job = _one_shot(crons)

    response = await cron_handlers.api_cron_update(
        _request(crons, {"at_time": "not a time at all", "timezone": "Not/AZone"}, job.id)
    )

    assert response.status == 400
    # The zone is refused as a zone, not as an at_time parse failure.
    assert "invalid timezone" in _body(response)["error"]
    assert crons.get_job(job.id).schedule.at_ts == _FUTURE
    assert crons.get_job(job.id).timezone == ""


@pytest.mark.asyncio
async def test_patch_absolute_at_ignores_every_timezone(_gateway_tz):
    """`at` is already an instant, so neither zone may shift it."""
    crons = CronService()
    job = crons.add_job(
        name="tokyo reminder",
        message="ship it",
        at_ts=_FUTURE,
        delete_after_run=True,
        timezone=_JOB_TZ,
    )

    response = await cron_handlers.api_cron_update(_request(crons, {"at": _FUTURE + 3600}, job.id))

    assert response.status == 200
    assert crons.get_job(job.id).schedule.at_ts == _FUTURE + 3600


@pytest.mark.asyncio
async def test_patch_recurring_with_a_timezone_sets_no_one_shot(_gateway_tz):
    """The opposite mode: a recurring retime still stores no `at_ts`."""
    crons = CronService()
    job = crons.add_job(name="daily", message="x", cron_expr="0 8 * * *")

    response = await cron_handlers.api_cron_update(
        _request(crons, {"cron": "0 9 * * *", "timezone": _JOB_TZ}, job.id)
    )

    assert response.status == 200
    stored = crons.get_job(job.id)
    assert stored.schedule.kind == "cron"
    assert stored.schedule.cron_expr == "0 9 * * *"
    assert stored.schedule.at_ts is None
    assert stored.timezone == _JOB_TZ
