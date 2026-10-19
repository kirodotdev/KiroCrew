"""A cron run that its deadline ends tells the user, like any other failed run.

``_execute_with_timeout`` cancels a run that outlives its wake budget, and the
reaper force-reaps one the cancel could not stop. Neither returns to the
gateway's cron callback, whose failure paths are the only ones that alert, so
without the hook a timed-out run reaches the history and the log and nothing
else: no bell, no Slack or channel message. The service hands both arms to the
alert the gateway sets, on the same terms it counts the run.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import _make_gw
from test_cron_reaper import _kill_seam, _live_task, _make_job, _mock_sessions  # noqa: F401

from kiro_crew import cron as cron_mod
from kiro_crew.cron import _JOB_TIMEOUT_SECS, CronJob, CronService, _RunClaim
from kiro_crew.cron_history import CronHistoryStore

pytestmark = pytest.mark.usefixtures("ample_host_free_memory")


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real deadline of a few ticks: the run below is cancelled by ``wait_for`` itself."""
    monkeypatch.setattr(cron_mod, "effective_wake_budget", lambda job: 0.05)
    for allowance in ("_pool_queue_allowance", "_gate_budget_allowance", "_vet_allowance"):
        monkeypatch.setattr(cron_mod, allowance, lambda job: 0)


async def _hang(job: CronJob) -> str | None:
    await asyncio.sleep(3600)
    return None


async def _settle(svc: CronService) -> None:
    """Let every alert the service started finish."""
    if svc._timeout_alerts:
        await asyncio.gather(*svc._timeout_alerts)


@pytest.mark.asyncio
async def test_a_run_its_deadline_ends_is_alerted_once(tmp_path, short_deadline) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_hang)
    alert = AsyncMock()
    svc.set_timeout_alert(alert)
    job = CronJob(id="t1", name="slow", message="hi")

    await svc._execute_with_timeout(job)
    await _settle(svc)

    assert job.last_status == "error"
    alert.assert_awaited_once_with(job, job.last_error)
    assert job.last_error.startswith("Timed out after ")


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["failure_recorded", "run_never_started"])
async def test_a_timeout_that_is_not_counted_is_not_alerted(
    tmp_path, monkeypatch, marker: str
) -> None:
    """The alert follows the count: a run that already recorded its failure was
    reported by the handler that recorded it, and a payload that never started is
    not a failure of the job."""
    svc = CronService(base_dir=tmp_path)
    alert = AsyncMock()
    svc.set_timeout_alert(alert)
    job = CronJob(id="t2", name="slow", message="hi", timeout_secs=1)

    async def _timeout(coro, timeout):
        coro.close()
        setattr(job, marker, True)
        raise asyncio.TimeoutError

    monkeypatch.setattr(cron_mod.asyncio, "wait_for", _timeout)
    await svc._execute_with_timeout(job)
    await _settle(svc)

    assert job.last_status == "error"
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slow_alert_does_not_hold_the_run(tmp_path, short_deadline) -> None:
    """The run settles while its alert is still delivering: a delivery that hangs
    must not keep the run's claim past its deadline and into the reaper's."""
    svc = CronService(base_dir=tmp_path, on_job=_hang)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_alert(job: CronJob, detail: str) -> None:
        started.set()
        await release.wait()

    svc.set_timeout_alert(_slow_alert)
    job = CronJob(id="t3", name="slow", message="hi")

    await asyncio.wait_for(svc._execute_with_timeout(job), timeout=30)
    await asyncio.wait_for(started.wait(), timeout=30)
    assert len(svc._timeout_alerts) == 1

    release.set()
    await _settle(svc)
    assert not svc._timeout_alerts


@pytest.mark.asyncio
async def test_no_alert_set_is_a_quiet_no_op(tmp_path, short_deadline) -> None:
    """The CLI, MCP and tests build a service with no gateway behind it."""
    svc = CronService(base_dir=tmp_path, on_job=_hang)
    job = CronJob(id="t4", name="slow", message="hi")

    await svc._execute_with_timeout(job)

    assert job.last_status == "error"
    assert not svc._timeout_alerts


@pytest.mark.asyncio
async def test_a_failing_alert_is_logged_and_the_run_still_settles(
    tmp_path, short_deadline, caplog
) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_hang)

    def _not_a_coroutine(job: CronJob, detail: str) -> None:
        raise RuntimeError("alert wiring broke")

    svc.set_timeout_alert(_not_a_coroutine)
    job = CronJob(id="t5", name="slow", message="hi")

    with caplog.at_level("WARNING", logger="kiro_crew.cron"):
        await svc._execute_with_timeout(job)

    assert job.last_status == "error"
    assert job.consecutive_failures == 1
    assert "could not start the timeout alert" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started", "never_started", "recorded", "alerted"),
    [
        (True, False, False, True),
        # Claimed but never stamped: nothing ran.
        (False, False, False, False),
        # Stopped at the fire-time gate: the payload never started.
        (True, True, False, False),
        # The wait_for arm (or the callback) already counted this run.
        (True, False, True, False),
    ],
)
async def test_the_reaper_alerts_a_reaped_run_on_the_terms_it_counts_it(
    tmp_path, started: bool, never_started: bool, recorded: bool, alerted: bool
) -> None:
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = _mock_sessions()
    alert = AsyncMock()
    svc.set_timeout_alert(alert)
    job = _make_job("reap1")
    job.run_never_started = never_started
    job.failure_recorded = recorded
    svc._jobs = [job]
    claim = svc._claims["reap1"] = _RunClaim(
        trigger="scheduled",
        claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10,
        task=_live_task(),
        started_monotonic=(time.monotonic() - _JOB_TIMEOUT_SECS - 10) if started else None,
    )

    with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
        await svc._force_reap("reap1", _JOB_TIMEOUT_SECS + 10, claim=claim)
    await _settle(svc)

    if alerted:
        alert.assert_awaited_once()
        alerted_job, detail = alert.await_args.args
        assert alerted_job is job
        assert detail == job.last_error
        assert detail.startswith("Reaped after ")
    else:
        alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_gateway_wires_its_failure_alert_into_the_service() -> None:
    gw = _make_gw()
    svc = MagicMock()
    with (
        patch("kiro_crew.slack.gateway.CronService") as cron_cls,
        patch("kiro_crew.slack.gateway.sel"),
    ):
        cron_cls.create = AsyncMock(return_value=svc)
        await gw._init_cron(arm=False)

    svc.set_timeout_alert.assert_called_once_with(gw._alert_cron_failure)


@pytest.mark.asyncio
async def test_a_timed_out_run_reaches_the_bell_and_the_job_destination(
    tmp_path, short_deadline
) -> None:
    """End to end through the gateway's own alert: the dashboard bell and the
    job's Slack channel both carry the timeout, as they carry any failed run."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    svc = CronService(base_dir=tmp_path, on_job=_hang)
    svc.set_timeout_alert(gw._alert_cron_failure)
    job = CronJob(id="t6", name="nightly-sync", message="hi", channel="C123")

    with patch("kiro_crew.slack.gateway.sel"):
        await svc._execute_with_timeout(job)
        await _settle(svc)

    gw.dashboard_state.notify.assert_called_once()
    kind, title, body = gw.dashboard_state.notify.call_args.args
    assert (kind, title) == ("cron", "Cron: nightly-sync")
    assert "Run failed" in body and job.last_error in body
    gw.slack.post_message.assert_awaited_once()
    channel, text = gw.slack.post_message.await_args.args
    assert channel == "C123" and "Timed out after" in text


@pytest.mark.asyncio
async def test_a_silent_job_times_out_without_an_alert(tmp_path, short_deadline) -> None:
    """``silent`` keeps its meaning on the timeout path: the run still counts."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    svc = CronService(base_dir=tmp_path, on_job=_hang)
    svc.set_timeout_alert(gw._alert_cron_failure)
    job = CronJob(id="t7", name="quiet", message="hi", channel="C123", silent=True)

    with patch("kiro_crew.slack.gateway.sel"):
        await svc._execute_with_timeout(job)
        await _settle(svc)

    assert job.consecutive_failures == 1
    gw.dashboard_state.notify.assert_not_called()
    gw.slack.post_message.assert_not_awaited()
