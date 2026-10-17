"""Tests that ``GET /api/crons`` reports exactly the text a run produced.

``last_result`` carries produced text only; whether the run succeeded is
``last_status``. A run with nothing to say therefore stores ``""``, which is
already falsy, so no value-matching rule is needed to hide it — and none may be
added, because every string is legal user output. ``Report("ok")`` and
``Done("ok")`` store the literal ``"ok"`` as a real result, so a renderer that
dropped that value would erase it and leave the UI with nothing to view.

The two halves are pinned together on purpose: a rule that hides ``""`` and a
rule that hides ``"ok"`` look alike, and only a test asserting the second is
*visible* can tell them apart.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers.cron import api_crons


def _request_with_job(**job_kw) -> MagicMock:
    defaults = dict(
        id="cj1",
        name="cmd-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
        last_status="ok",
    )
    defaults.update(job_kw)
    job = CronJob(**defaults)

    state = MagicMock()
    state.crons.list_jobs.return_value = [job]
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


async def _job_payload(**job_kw) -> dict:
    resp = await api_crons(_request_with_job(**job_kw))
    assert resp.status == 200
    return json.loads(resp.body)["jobs"][0]


class TestProducedResultIsVisible:
    """No value may be filtered out of the result fields."""

    @pytest.mark.asyncio
    async def test_result_that_is_exactly_ok_is_returned(self) -> None:
        """A script's ``Report("ok")`` stores "ok"; the UI must be able to view it.

        This is the case a value-matching filter breaks: "ok" doubles as the
        shortest plausible success message a job can report, so dropping it
        discards real output and disables View Result on a job that has some.
        """
        job = await _job_payload(last_result="ok")
        assert job["last_result"] == "ok"
        assert job["has_result"] is True

    @pytest.mark.asyncio
    async def test_ordinary_result_is_returned(self) -> None:
        job = await _job_payload(last_result="42 widgets")
        assert job["last_result"] == "42 widgets"
        assert job["has_result"] is True

    @pytest.mark.asyncio
    async def test_result_merely_containing_ok_is_returned(self) -> None:
        job = await _job_payload(last_result="all checks ok")
        assert job["last_result"] == "all checks ok"
        assert job["has_result"] is True


class TestSilentRunShowsNothing:
    """A run that produced no text must not offer a result to view."""

    @pytest.mark.asyncio
    async def test_cleared_result_reported_as_none(self) -> None:
        """What a silent success writes: empty, so nothing is offered."""
        job = await _job_payload(last_result="")
        assert job["last_result"] is None
        assert job["has_result"] is False

    @pytest.mark.asyncio
    async def test_never_run_job_reported_as_none(self) -> None:
        job = await _job_payload(last_result=None, last_status="")
        assert job["last_result"] is None
        assert job["has_result"] is False

    @pytest.mark.asyncio
    async def test_error_text_still_reported(self) -> None:
        """A cleared result must leave last_error as the text triage reads."""
        job = await _job_payload(
            last_result="", last_status="error", last_error="command failed (exit_code=1)"
        )
        assert job["last_result"] is None
        assert job["last_error"] == "command failed (exit_code=1)"


class TestProducedResultSurvivesLateFailure:
    """A result produced and delivered must survive a failure after the fact.

    The clears exist so a result-less run stops displaying the PREVIOUS run's
    output. A run that produced a result and then raised during cleanup (script
    ``Done`` delivers, then removal fails) is not that case: erasing there would
    destroy this run's real output and report it as having produced nothing.
    """

    def test_clear_is_a_noop_once_this_run_produced_a_result(self):
        job = CronJob(id="j1", name="produced", message="m")
        job.set_run_result("report")
        assert job.result_produced is True
        job.clear_carried_result()
        assert job.last_result == "report", "a produced result must not be erased"
        assert job.result_produced is True

    def test_clear_drops_a_carried_over_result_when_none_was_produced(self):
        job = CronJob(id="j2", name="carried", message="m")
        job.last_result = "previous run output"
        job.result_produced = False
        job.clear_carried_result()
        assert job.last_result == ""

    def test_clear_does_not_mark_the_run_as_having_produced_a_result(self):
        job = CronJob(id="j3", name="marker", message="m")
        job.result_produced = False
        job.clear_carried_result()
        assert job.result_produced is False, "clearing must not claim production"
