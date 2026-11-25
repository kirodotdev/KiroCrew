"""``GET /api/crons`` builds its rows off the event loop.

The Schedule page reads the whole job list, and each job's free-text fields
(message, last result, last error, script, command, ...) go through the
exfiltration-URL pass and the credential pass. For a few hundred jobs with long
results that took hundreds of milliseconds, all of it on the gateway loop.
Pinned here: the rows are built and encoded in a worker thread from copies of
the jobs, the loop-owned reads (run claims, the slot registry) stay on the
loop, and the payload is redacted exactly as before. The list cuts a long last
result or error to a redacted preview; ``GET /api/crons/{id}`` carries it whole.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from redaction_structured_cases import github_classic

from kiro_crew import redaction_seams
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard import chat_utils_display
from kiro_crew.dashboard.chat_utils_display import _clear_display_redaction_cache
from kiro_crew.dashboard.handlers.cron import api_cron_get, api_crons
from kiro_crew.dashboard.handlers.cron_board import _LIST_TEXT_PREVIEW, _job_snapshot
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.redaction_switch import owner_view

# A GitHub token with a valid checksum: redacted wherever it appears.
_TOKEN = github_classic(seed=161)
SECRET = f"deploy key {_TOKEN} here"


def _truth(text: str) -> str:
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def _request(jobs: list[CronJob]) -> MagicMock:
    state = MagicMock()
    state.crons.list_jobs.return_value = jobs
    state.crons.list_jobs_async = AsyncMock(return_value=jobs)
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


def _job(i: int, **kw) -> CronJob:
    fields = dict(
        id=f"j{i}",
        name=f"job {i}",
        message=f"do the thing {i}",
        schedule=CronSchedule(kind="every", every_secs=60),
        last_result=f"{SECRET} run {i}",
        last_status="ok",
    )
    fields.update(kw)
    return CronJob(**fields)


class TestApiCrons:
    @pytest.mark.asyncio
    async def test_rows_are_built_off_the_loop(self, monkeypatch):
        """The row build runs in a worker thread; the loop-owned reads stay on the loop."""
        import kiro_crew.cron as cron_mod

        loop_thread = threading.get_ident()
        seen: dict[str, set[int]] = {"format": set(), "running": set(), "slot": set()}
        real_format = cron_mod.format_schedule

        def recording_format(*a, **kw):
            seen["format"].add(threading.get_ident())
            return real_format(*a, **kw)

        monkeypatch.setattr(cron_mod, "format_schedule", recording_format)
        request = _request([_job(1), _job(2)])
        state = request.app["state"]
        state.crons.is_running.side_effect = lambda _id: seen["running"].add(threading.get_ident())
        state.has_slot.side_effect = lambda _n: seen["slot"].add(threading.get_ident())

        resp = await api_crons(request)

        assert resp.status == 200
        assert seen["format"] and loop_thread not in seen["format"]
        assert seen["running"] == {loop_thread}
        assert seen["slot"] == {loop_thread}

    @pytest.mark.asyncio
    async def test_payload_is_redacted_and_shaped_as_before(self):
        request = _request([_job(1)])
        state = request.app["state"]
        state.crons.is_running.return_value = True
        state.crons.running_since.return_value = 123.0
        state.has_slot.return_value = True

        resp = await api_crons(request)
        body = json.loads(resp.body)

        assert resp.content_type == "application/json"
        row = body["jobs"][0]
        assert _TOKEN not in row["last_result"]
        assert row["last_result"] == _truth(f"{SECRET} run 1")
        assert row["is_running"] is True
        assert row["running_since"] == 123.0
        assert row["has_slot"] is True
        assert "server_tz" in body

    @pytest.mark.asyncio
    async def test_owner_view_scope_reaches_the_worker(self):
        """The worker inherits the request's context, so a scope the caller opened
        still decides the answer there."""
        jobs = [_job(1)]
        with owner_view():
            body = json.loads((await api_crons(_request(jobs))).body)
            expected = _truth(f"{SECRET} run 1")
        assert body["jobs"][0]["last_result"] == expected


class TestResultPreview:
    @pytest.mark.asyncio
    async def test_the_list_cuts_a_long_result_after_redacting_it(self, monkeypatch):
        """A credential the cut lands in shows as the redaction tag, never as its head,
        and the one-job read masks it too, with the owner's redaction setting off."""
        monkeypatch.setattr(redaction_seams, "_redaction_off", True)
        _clear_display_redaction_cache()
        head = "x" * (_LIST_TEXT_PREVIEW - 11) + " "
        result = f"{head}{_TOKEN} tail\n" + "y" * 20_000
        request = _request([_job(1, last_result=result)])
        body = json.loads((await api_crons(request)).body)
        preview = body["jobs"][0]["last_result"]
        assert preview == _truth(result)[:_LIST_TEXT_PREVIEW]
        assert _TOKEN[:8] not in preview
        request.match_info = {"job_id": "j1"}
        whole = json.loads((await api_cron_get(request)).body)["last_result"]
        assert whole == _truth(result)
        assert _TOKEN not in whole

    @pytest.mark.asyncio
    async def test_a_list_re_read_redacts_no_unchanged_result_again(self, monkeypatch):
        """What makes a re-read fast: the display redaction's cache answers it, with
        the owner's redaction setting off (the shipped default) too."""
        monkeypatch.setattr(redaction_seams, "_redaction_off", True)
        _clear_display_redaction_cache()
        seen: list[int] = []
        real = chat_utils_display.display_battery

        def recording(text, **kw):
            seen.append(len(text))
            return real(text, **kw)

        monkeypatch.setattr(chat_utils_display, "display_battery", recording)
        result = "".join(f"step {i}: checked 14 dashboards, 0 alerts\n" for i in range(500))
        request = _request([_job(1, last_result=result)])
        first = json.loads((await api_crons(request)).body)["jobs"][0]["last_result"]
        assert first == result[:_LIST_TEXT_PREVIEW]
        assert len(result) in seen
        seen.clear()
        await api_crons(request)
        assert len(result) not in seen

    @pytest.mark.asyncio
    async def test_the_one_job_read_carries_the_whole_result(self):
        result = "line of output\n" * 2_000
        request = _request([_job(1, last_result=result), _job(2)])
        request.match_info = {"job_id": "j1"}
        row = json.loads((await api_cron_get(request)).body)
        assert row["id"] == "j1"
        assert row["last_result"] == _truth(result)

    @pytest.mark.asyncio
    async def test_the_one_job_read_of_an_unknown_job_is_a_404(self):
        request = _request([_job(1)])
        request.match_info = {"job_id": "gone"}
        resp = await api_cron_get(request)
        assert resp.status == 404
        assert json.loads(resp.body)["code"] == "job_not_found"


class TestJobSnapshot:
    def test_containers_and_schedule_are_copied(self):
        job = _job(
            1,
            skip_dates=["2026-01-01"],
            agent_sequence=["a"],
            secret_env={"K": "v"},
            secret_env_pending={"P": "w"},
        )
        snap = _job_snapshot(job)
        job.skip_dates.append("2026-01-02")
        job.agent_sequence.append("b")
        job.secret_env["K2"] = "x"
        job.secret_env_pending.clear()
        job.schedule.every_secs = 5
        job.last_result = "changed"
        assert snap.skip_dates == ["2026-01-01"]
        assert snap.agent_sequence == ["a"]
        assert snap.secret_env == {"K": "v"}
        assert snap.secret_env_pending == {"P": "w"}
        assert snap.schedule.every_secs == 60
        assert snap.last_result == f"{SECRET} run 1"
