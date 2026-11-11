"""A cron run a gateway stop cuts short tells its owner, and ``kirocrew restart`` names it first.

``CronService.stop()`` cancels every run task without a cancel or reap flag, so a run
with no outcome of its own is filed ``cancelled`` with its interruption note and is
not retried: a daily job interrupted by a restart skips that day. So the run starts
the notice the gateway sets (``set_interrupt_alert``), rather than leaving the history
row as the only trace, ``stop()`` waits for it, bounded, before the process exits,
and ``kirocrew restart`` names the runs a restart interrupts before it signals
anything. The planned in-app restarts (the dashboard's
and the Slack one) skip the gateway's ``_shutdown``, so each one
stops the cron service itself (``stop_cron_before_restart``) before its sessions close,
and only past its last refusal: a restart that refuses or defers keeps serving, and
its scheduler and the run in flight with it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from cron_gateway_integration_helpers import _make_gw

from kiro_crew import cli_server
from kiro_crew import cron as cron_mod
from kiro_crew import cron_inflight, platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronJob, CronSchedule, CronService
from kiro_crew.dashboard import chat as dashboard_chat
from kiro_crew.dashboard.handlers import updates
from kiro_crew.slack import events
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until, finished

pytestmark = pytest.mark.usefixtures("ample_host_free_memory")

_FINISHED_NOTE = "Interrupted: the cron service stopped before this run finished"


def _held_run(started: asyncio.Event):
    """A job callback that runs until something cancels it."""

    async def on_job(job: CronJob) -> str | None:
        started.set()
        # Never resolved: only stop()'s cancel ends this run.
        await asyncio.get_running_loop().create_future()
        return None

    return on_job


async def _cut_mid_run(svc: CronService, job_id: str, started: asyncio.Event) -> None:
    """Start a run of *job_id*, then stop the service while its callback runs."""
    assert svc.trigger_run(job_id)
    await await_until(started, what="the run's callback")
    await finished(svc.stop(), what="stop()")


@pytest.mark.asyncio
async def test_a_gateway_stop_tells_the_owner_about_the_run_it_cut(tmp_path, monkeypatch) -> None:
    """Through the gateway's own wiring: the bell and the job's Slack channel both
    say the run was interrupted, and the history row still files it cancelled."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    gw.dashboard_state.last_notification_persist = None
    monkeypatch.setattr("kiro_crew.slack.gateway.data_home", lambda: tmp_path)
    with patch("kiro_crew.slack.gateway.sel"):
        await gw._init_cron(arm=False)
    svc = gw.cron_svc
    assert isinstance(svc, CronService)
    started = asyncio.Event()
    svc._on_job = _held_run(started)
    job = await svc.add_job_async("nightly-sync", "hi", cron_expr="0 3 * * *", channel="C123")

    await _cut_mid_run(svc, job.id, started)

    gw.dashboard_state.notify.assert_called_once()
    kind, title, body = gw.dashboard_state.notify.call_args.args
    assert (kind, title) == ("cron", "Cron: nightly-sync")
    assert body.startswith(f"⏹ {_FINISHED_NOTE}.")
    assert "The job runs again at its next scheduled time" in body
    assert gw.dashboard_state.notify.call_args.kwargs["meta"] == {"job_id": job.id}
    gw.slack.post_message.assert_awaited_once()
    channel, text = gw.slack.post_message.await_args.args
    assert channel == "C123" and "Interrupted" in text and _FINISHED_NOTE in text
    runs, total = await svc._history.get_job_history(job.id)
    assert total == 1 and (runs[0]["status"], runs[0]["error"]) == ("cancelled", _FINISHED_NOTE)
    # Not a failure: nothing is counted and the failure dedup anchor stays put.
    stored = svc.get_job(job.id)
    assert stored is not None
    assert (stored.consecutive_failures, stored.last_failure_hash) == (0, "")


@pytest.mark.asyncio
async def test_a_silent_job_is_not_told() -> None:
    """``silent`` gates every surface, as it does for a failure."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    job = CronJob(id="q1", name="quiet", message="hi", channel="C123", silent=True)

    await gw._alert_cron_interrupted(job, _FINISHED_NOTE)

    gw.dashboard_state.notify.assert_not_called()
    gw.slack.post_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schedule", "tail"),
    [
        (
            CronSchedule(kind="every", every_secs=300),
            " The job runs again at its next scheduled time.",
        ),
        (
            CronSchedule(kind="cron", cron_expr="0 3 * * *"),
            " The job runs again at its next scheduled time, or at once if Kiro Crew is back"
            " within the minute this run was due.",
        ),
        (CronSchedule(kind="at", at_ts=1_700_000_000.0), ""),
    ],
    ids=["every", "cron", "at"],
)
async def test_the_notice_says_when_the_job_runs_again(schedule: CronSchedule, tail: str) -> None:
    """A stopped ``every`` run's ``last_run_ts`` moves to its start, so its job waits for
    the next interval. A stopped cron-expression run keeps the previous run's, so a
    gateway back within the minute the run was due finds that slot due again, and the
    note says so rather than promising no rerun. A one-shot gets no line about later."""
    gw = _make_gw()
    gw.dashboard_state.last_notification_persist = None
    gw._deliver_failure_alert = AsyncMock(return_value=(False, True, False))
    job = CronJob(id="k1", name="nightly", message="hi", channel="C123", schedule=schedule)

    await gw._alert_cron_interrupted(job, _FINISHED_NOTE)

    _, _, body = gw.dashboard_state.notify.call_args.args
    assert body == f"⏹ {_FINISHED_NOTE}.{tail}"
    assert gw._deliver_failure_alert.await_args.kwargs["plain"].endswith(f"{_FINISHED_NOTE}.{tail}")


@pytest.mark.asyncio
@pytest.mark.parametrize("paused_during_run", [False, True], ids=["paused-first", "paused-mid-run"])
async def test_a_paused_job_is_not_promised_a_next_run(
    tmp_path, monkeypatch, paused_during_run: bool
) -> None:
    """A paused recurring job can still be run by hand, but the scheduler skips it,
    so the notice for its cut run says it waits for a resume instead of promising
    its next scheduled time. A pause stored while the run is going counts too."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    gw.dashboard_state.last_notification_persist = None
    monkeypatch.setattr("kiro_crew.slack.gateway.data_home", lambda: tmp_path)
    with patch("kiro_crew.slack.gateway.sel"):
        await gw._init_cron(arm=False)
    svc = gw.cron_svc
    assert isinstance(svc, CronService)
    started = asyncio.Event()
    svc._on_job = _held_run(started)
    job = await svc.add_job_async("nightly-sync", "hi", cron_expr="0 3 * * *", channel="C123")
    if not paused_during_run:
        assert await svc.enable_job_async(job.id, False)

    assert svc.trigger_run(job.id)
    await await_until(started, what="the run's callback")
    if paused_during_run:
        assert await svc.enable_job_async(job.id, False)
    await finished(svc.stop(), what="stop()")

    _, _, body = gw.dashboard_state.notify.call_args.args
    assert body == (
        f"⏹ {_FINISHED_NOTE}. The job is paused, so it does not run again until you"
        " resume it in Schedule."
    )
    stored = svc.get_job(job.id)
    assert stored is not None and not stored.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paused_at_start", [False, True], ids=["paused-elsewhere", "resumed-elsewhere"]
)
async def test_a_pause_or_resume_from_another_process_is_told_right(
    tmp_path, monkeypatch, paused_at_start: bool
) -> None:
    """A pause or resume another process writes to the store during the run (the
    CLI or the cron_pause MCP tool, each with its own CronService) decides the
    notice: the merge's reload replaces the run's own job object, so its stale
    ``enabled`` must not."""
    gw = _make_gw()
    gw.slack.post_message = AsyncMock()
    gw.dashboard_state.last_notification_persist = None
    monkeypatch.setattr("kiro_crew.slack.gateway.data_home", lambda: tmp_path)
    with patch("kiro_crew.slack.gateway.sel"):
        await gw._init_cron(arm=False)
    svc = gw.cron_svc
    assert isinstance(svc, CronService)
    started = asyncio.Event()
    svc._on_job = _held_run(started)
    job = await svc.add_job_async("nightly-sync", "hi", cron_expr="0 3 * * *", channel="C123")
    if paused_at_start:
        assert await svc.enable_job_async(job.id, False)

    assert svc.trigger_run(job.id)
    await await_until(started, what="the run's callback")
    other = await CronService.create(base_dir=tmp_path)
    assert await other.enable_job_async(job.id, paused_at_start)
    await finished(svc.stop(), what="stop()")

    _, _, body = gw.dashboard_state.notify.call_args.args
    if paused_at_start:
        tail = (
            " The job runs again at its next scheduled time, or at once if Kiro Crew is"
            " back within the minute this run was due."
        )
    else:
        tail = " The job is paused, so it does not run again until you resume it in Schedule."
    assert body == f"⏹ {_FINISHED_NOTE}.{tail}"
    stored = svc.get_job(job.id)
    assert stored is not None and stored.enabled is paused_at_start


#: 03:00:00 UTC, a slot of ``0 3 * * *``.
_SLOT = 1_700_017_200.0


@pytest.mark.parametrize(
    ("schedule", "due_in_its_minute"),
    [
        (CronSchedule(kind="every", every_secs=300), False),
        (CronSchedule(kind="cron", cron_expr="0 3 * * *"), True),
    ],
    ids=["every", "cron"],
)
def test_a_stopped_run_is_due_again_in_its_minute_only_on_a_cron_expression(
    schedule: CronSchedule, due_in_its_minute: bool
) -> None:
    """What the notice's line about the next run rests on. ``close_run`` stamps a stopped
    ``every`` run's start as ``last_run_ts``; a stopped cron-expression run keeps the
    previous run's, so a gateway back within that slot's minute finds it due again, and
    once the minute is over nothing runs it before its next slot."""
    job = CronJob(
        id="k1",
        name="nightly",
        message="hi",
        schedule=schedule,
        timezone="UTC",
        created_ts=_SLOT - 2 * 86_400,
        last_run_ts=_SLOT - 86_400 + 20,
    )

    cron_mod.close_run(job, started_at=_SLOT + 1, generation=1, being_cancelled=True)

    assert cron_mod.is_due(job, _SLOT + 40) is due_in_its_minute
    assert cron_mod.is_due(job, _SLOT + 61) is False


@pytest.mark.asyncio
async def test_stop_returns_only_after_the_notices_it_started(tmp_path, monkeypatch) -> None:
    """The gateway exits once ``stop()`` returns, so a notice still on its way then
    reaches no one: ``stop()`` waits on it, for the whole bound, and lets it land.

    The notice is held until ``stop()`` is inside that wait, so it is in flight there
    however the loop orders its steps. A ``stop()`` that skips the wait returns with
    the notice undelivered; one that waits less than ``_INTERRUPT_ALERT_WAIT_SECS``,
    or cancels the notice, shows in what its wait was given and how the notice ended.
    """
    monkeypatch.setattr(cron_mod, "_INTERRUPT_ALERT_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    started = asyncio.Event()
    svc = CronService(base_dir=tmp_path, on_job=_held_run(started))
    release = asyncio.Event()
    notices: list[asyncio.Task[object]] = []
    delivered: list[tuple[str, str]] = []

    async def _notice(job: CronJob, detail: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        notices.append(task)
        await release.wait()
        delivered.append((job.id, detail))

    real_wait = asyncio.wait
    waits: list[tuple[set[object], object, bool]] = []

    async def _spy(fs, *, timeout=None, **kw):
        if notices and notices[0] in fs:
            # stop() is in its wait: note what it waits on, for how long and that
            # the notice is still running, then let the notice land.
            waits.append((set(fs), timeout, notices[0].done()))
            release.set()
        return await real_wait(fs, timeout=timeout, **kw)

    monkeypatch.setattr(asyncio, "wait", _spy)
    svc.set_interrupt_alert(_notice)
    job = await svc.add_job_async("nightly", "hi", cron_expr="0 3 * * *")

    await _cut_mid_run(svc, job.id, started)

    (notice,) = notices
    assert waits == [({notice}, FALLBACK_TIMEOUT_SECS, False)]
    assert notice.done() and not notice.cancelled()
    assert delivered == [(job.id, _FINISHED_NOTE)]
    assert not svc._interrupt_alerts


@pytest.mark.asyncio
async def test_stop_cuts_a_notice_still_running_at_its_bound(tmp_path, monkeypatch) -> None:
    """The wait spends the graceful-shutdown budget, so it is bounded: a notice
    still running at ``_INTERRUPT_ALERT_WAIT_SECS`` is cancelled."""
    monkeypatch.setattr(cron_mod, "_INTERRUPT_ALERT_WAIT_SECS", 0)
    started = asyncio.Event()
    svc = CronService(base_dir=tmp_path, on_job=_held_run(started))
    cut = asyncio.Event()

    async def _stuck(job: CronJob, detail: str) -> None:
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            cut.set()
            raise

    svc.set_interrupt_alert(_stuck)
    job = await svc.add_job_async("nightly", "hi", cron_expr="0 3 * * *")

    await _cut_mid_run(svc, job.id, started)

    await await_until(cut, what="the late notice's cancel")


@pytest.mark.asyncio
async def test_a_run_that_ended_on_its_own_is_not_told(tmp_path) -> None:
    alert = AsyncMock()
    svc = CronService(base_dir=tmp_path, on_job=AsyncMock(return_value="done"))
    svc.set_interrupt_alert(alert)
    job = await svc.add_job_async("nightly", "hi", cron_expr="0 3 * * *")
    assert svc.trigger_run(job.id)
    run = svc._claims[job.id].task
    assert run is not None

    await await_until(run, what="the run")
    await finished(svc.stop(), what="stop()")

    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_the_user_cancelled_is_not_told_again(tmp_path) -> None:
    """``cancel()`` flags its claim and writes its own row; the user asked for it."""
    alert = AsyncMock()
    svc = CronService(base_dir=tmp_path, on_job=AsyncMock(side_effect=asyncio.CancelledError))
    svc.set_interrupt_alert(alert)
    job = CronJob(
        id="c1", name="nightly", message="hi", schedule=CronSchedule(kind="every", every_secs=300)
    )
    svc._jobs = [job]
    claim = svc._claim_run("c1", "scheduled")
    claim.cancelled = True

    with (
        patch.object(svc, "_merge_job_result"),
        patch.object(svc, "_compute_jitter", return_value=0),
        pytest.raises(asyncio.CancelledError),
    ):
        await svc._run_job_isolated(job, claim)

    alert.assert_not_awaited()
    assert not svc._interrupt_alerts


class _PlannedRestart:
    """One planned in-app restart of a gateway with a cron run in flight, and the
    order of what it does."""

    def __init__(self, crons: CronService) -> None:
        self.order: list[str] = []
        self.crons = crons
        self.state = SimpleNamespace(
            _slots={},
            _gateway_restart_in_progress=False,
            push_update_progress=Mock(),
            sessions=SimpleNamespace(close_all=self.close_all),
            crons=crons,
        )

    async def close_all(self, *_args: object, **_kwargs: object) -> None:
        self.order.append("close_all")

    def end(self, label: str) -> Callable[..., None]:
        return lambda *_a, **_k: self.order.append(label)


def _dashboard_seams(restart: _PlannedRestart, monkeypatch) -> None:
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: True)
    monkeypatch.setattr(updates, "flush_breadcrumb_writes", lambda _timeout: None)
    monkeypatch.setattr(updates, "reexec_python_module", restart.end("exec"))


def _dashboard_restart(restart: _PlannedRestart, monkeypatch) -> Awaitable[object]:
    _dashboard_seams(restart, monkeypatch)
    return updates._restart_gateway(restart.state, resolver=lambda: "/python")


def _slack_orchestrator(
    restart: _PlannedRestart, monkeypatch, *, owner: bool = True
) -> SimpleNamespace:
    monkeypatch.setattr(events, "is_owner", lambda _caller: owner)
    monkeypatch.setattr(events, "sel", lambda: MagicMock())
    monkeypatch.setenv("INVOCATION_ID", "restart-test")
    monkeypatch.setattr("kiro_crew.cli.drain_log_queue_before_hard_exit", AsyncMock())
    monkeypatch.setattr(platform_compat, "hard_exit", restart.end("exit"))
    return SimpleNamespace(
        sessions=restart.state.sessions, dashboard_state=restart.state, cron_svc=restart.crons
    )


def _slack_restart(restart: _PlannedRestart, monkeypatch) -> Awaitable[object]:
    return events._handle_restart(_slack_orchestrator(restart, monkeypatch), "U1", "", AsyncMock())


@pytest.mark.parametrize(
    "run, ends_with",
    [
        pytest.param(_dashboard_restart, "exec", id="dashboard"),
        pytest.param(_slack_restart, "exit", id="slack"),
    ],
)
@pytest.mark.asyncio
async def test_a_planned_restart_stops_the_cron_run_it_cuts_and_tells_the_owner(
    run, ends_with, tmp_path, monkeypatch
) -> None:
    """The in-app restarts exec or exit without the gateway's ``_shutdown``, so each
    stops the cron service itself, before it closes the sessions: the run is filed
    cancelled, its in-flight marker is cleared and its owner is told, all before the
    exec or exit. Left running, the run would end with no row, no notice, and a
    marker whose writer an exec keeps alive."""
    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)
    started = asyncio.Event()
    svc = CronService(base_dir=tmp_path, on_job=_held_run(started))
    restart = _PlannedRestart(svc)
    told: list[tuple[str, str]] = []

    async def _notice(job: CronJob, detail: str) -> None:
        restart.order.append("told")
        told.append((job.id, detail))

    svc.set_interrupt_alert(_notice)
    job = await svc.add_job_async("nightly", "hi", cron_expr="0 3 * * *")
    assert svc.trigger_run(job.id)
    await await_until(started, what="the run's callback")
    assert [m.job_id for m in cron_inflight.read_markers(tmp_path)] == [job.id]

    await finished(run(restart, monkeypatch), what="the planned restart")

    assert restart.order == ["told", "close_all", ends_with]
    assert told == [(job.id, _FINISHED_NOTE)]
    runs, total = await svc._history.get_job_history(job.id)
    assert total == 1 and (runs[0]["status"], runs[0]["error"]) == ("cancelled", _FINISHED_NOTE)
    assert cron_inflight.read_markers(tmp_path) == []


async def _dashboard_with_an_invalid_launcher(restart: _PlannedRestart, monkeypatch) -> None:
    _dashboard_seams(restart, monkeypatch)

    def _invalid() -> None:
        raise ValueError("not an executable launcher")

    monkeypatch.setattr(updates, "resolve_restart_launcher", _invalid)
    assert await updates._restart_gateway(restart.state, resolver=lambda: "/python") is False
    restart.state.push_update_progress.assert_called_with(
        "error", "Cannot restart: invalid gateway launcher path"
    )


async def _dashboard_with_its_interpreter_gone(restart: _PlannedRestart, monkeypatch) -> None:
    _dashboard_seams(restart, monkeypatch)
    monkeypatch.setattr(platform_compat, "execv_target_available", lambda _exe: False)
    assert await updates._restart_gateway(restart.state, resolver=lambda: "/python") is False
    restart.state.push_update_progress.assert_called_with(
        "error", "Cannot restart: invalid Python executable path"
    )


async def _slack_from_a_non_owner(restart: _PlannedRestart, monkeypatch) -> None:
    orch = _slack_orchestrator(restart, monkeypatch, owner=False)
    respond = AsyncMock()
    await events._handle_restart(orch, "U2", "", respond)
    (refusal,) = respond.await_args_list
    assert "Only the owner can restart the gateway." in refusal.args[0]


async def _slack_with_no_supervisor(restart: _PlannedRestart, monkeypatch) -> None:
    orch = _slack_orchestrator(restart, monkeypatch)
    monkeypatch.delenv("INVOCATION_ID")
    respond = AsyncMock()
    await events._handle_restart(orch, "U1", "", respond)
    (refusal,) = respond.await_args_list
    assert "Restart requires a process supervisor" in refusal.args[0]


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(_dashboard_with_an_invalid_launcher, id="dashboard-invalid-launcher"),
        pytest.param(_dashboard_with_its_interpreter_gone, id="dashboard-interpreter-gone"),
        pytest.param(_slack_from_a_non_owner, id="slack-non-owner"),
        pytest.param(_slack_with_no_supervisor, id="slack-no-supervisor"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_or_deferred_restart_leaves_the_cron_service_serving(
    refused, tmp_path, monkeypatch
) -> None:
    """Stopping the cron service is safe only past a restart's last refusal. A
    restart that refuses or defers keeps the gateway serving, and a stop ahead of
    that would leave it running no cron job until the next restart, with no one
    told. So the run in flight goes on, the timer stays armed, and no row or
    notice is written."""
    monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)
    started = asyncio.Event()
    svc = CronService(base_dir=tmp_path, on_job=_held_run(started))
    await svc.start()
    try:
        restart = _PlannedRestart(svc)
        told: list[tuple[str, str]] = []

        async def _notice(job: CronJob, detail: str) -> None:
            told.append((job.id, detail))

        svc.set_interrupt_alert(_notice)
        # Due a day after it is made, so the armed timer never fires it here.
        job = await svc.add_job_async("nightly", "hi", every_secs=86_400)
        assert svc.trigger_run(job.id)
        await await_until(started, what="the run's callback")
        run = svc._claims[job.id].task
        assert run is not None

        await finished(refused(restart, monkeypatch), what="the refused restart")

        assert restart.order == []
        assert svc._running is True
        assert svc._timer_task is not None and not svc._timer_task.done()
        assert svc.is_running(job.id) and not run.done()
        assert told == []
        _, total = await svc._history.get_job_history(job.id)
        assert total == 0
    finally:
        await finished(svc.stop(), what="stop()")


@pytest.mark.asyncio
async def test_a_cron_stop_past_its_bound_does_not_hold_the_restart(monkeypatch) -> None:
    """A planned restart is past its point of no return, so a stop still running at
    ``_RESTART_STOP_SECS`` is cancelled, not awaited, and the restart goes on."""
    monkeypatch.setattr(cron_mod, "_RESTART_STOP_SECS", 0)
    wedged: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    await finished(
        cron_mod.stop_cron_before_restart(SimpleNamespace(stop=lambda: wedged)),
        what="the bounded stop",
    )

    assert wedged.cancelled()


def _restart_through_the_service(capsys) -> list[str]:
    """Run ``kirocrew restart`` down its service branch; return what it printed
    before it asked the service manager to restart."""
    printed_before: list[str] = []

    def _restart_service() -> bool:
        printed_before.append(capsys.readouterr().out)
        return True

    with (
        patch("kiro_crew.cli_server.sel"),
        patch(
            "kiro_crew.cli_server.service_controller.restart_service",
            side_effect=_restart_service,
        ),
        patch("kiro_crew.cli_server._print_token_url"),
    ):
        cli_server._restart(None)
    assert len(printed_before) == 1
    return printed_before


@pytest.mark.parametrize(
    ("schedule", "in_its_minute"),
    [({"cron_expr": "0 3 * * *"}, True), ({"every_secs": 86_400}, False)],
    ids=["cron", "every"],
)
def test_restart_names_the_cron_run_it_interrupts_before_signalling(
    capsys, schedule: dict[str, Any], in_its_minute: bool
) -> None:
    """A recurring job keeps its next-run line, and only one on a cron expression is
    also told it runs again at once within the minute that run was due."""
    svc = CronService(base_dir=config_dir())
    job = svc.add_job("daily-report", "hi", **schedule)
    # Started at 22:13:20 UTC; the writer is this process, so the run is live.
    cron_inflight.write_marker(config_dir(), job.id, job.name, started_at=1_700_000_000.0, run="r1")

    (before,) = _restart_through_the_service(capsys)

    line = "A restart interrupts the cron job(s) running now: daily-report (since 22:13 UTC)."
    assert line in before
    assert "An interrupted run is recorded as cancelled, and after the restart:" in before
    assert "daily-report runs again at its next scheduled time" in before
    clause = "at once if the gateway is back within the minute that run was due"
    assert (clause in before) is in_its_minute


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_after_run", [True, False], ids=["consumed", "kept"])
async def test_a_stopped_one_shot_is_consumed_or_left_due(tmp_path, delete_after_run: bool) -> None:
    """What the restart line about a one-shot rests on. A started ``delete_after_run``
    one-shot that ``stop()`` cuts is consumed, so it never runs again. A one-shot that
    keeps its row is left enabled with its time passed, so the next gateway runs it at
    once: the stop path never reaches ``_execute``'s one-shot disable."""
    started = asyncio.Event()
    svc = CronService(base_dir=tmp_path, on_job=_held_run(started))
    job = await svc.add_job_async(
        "launch-reminder", "hi", at_ts=_SLOT, delete_after_run=delete_after_run
    )

    await _cut_mid_run(svc, job.id, started)

    stored = CronService(base_dir=tmp_path).get_job(job.id)
    if delete_after_run:
        assert stored is None
    else:
        assert stored is not None and stored.enabled
        assert cron_mod.is_due(stored, _SLOT + 61) is True


def test_restart_does_not_promise_a_consumed_one_shot_runs_again(capsys) -> None:
    """A ``delete_after_run`` one-shot the stop cuts is consumed, so the line tells the
    operator to add it again rather than promising its next scheduled time."""
    svc = CronService(base_dir=config_dir())
    job = svc.add_job("launch-reminder", "hi", at_ts=_SLOT, delete_after_run=True)
    cron_inflight.write_marker(config_dir(), job.id, job.name, started_at=1_700_000_000.0, run="r1")

    (before,) = _restart_through_the_service(capsys)

    assert "launch-reminder (since 22:13 UTC)" in before
    assert "next scheduled time" not in before
    assert "does not run again" in before


def test_restart_says_a_kept_one_shot_runs_again_at_once(capsys) -> None:
    """A one-shot that keeps its row is left due by the stop, so the line says the
    gateway runs it again at once, not at a next scheduled time it does not have."""
    svc = CronService(base_dir=config_dir())
    job = svc.add_job("launch-reminder", "hi", at_ts=_SLOT)
    cron_inflight.write_marker(config_dir(), job.id, job.name, started_at=1_700_000_000.0, run="r1")

    (before,) = _restart_through_the_service(capsys)

    assert "launch-reminder (since 22:13 UTC)" in before
    assert "next scheduled time" not in before
    assert "at once when the gateway is back" in before


def test_restart_keeps_the_next_run_line_for_a_recurring_job_beside_a_one_shot(capsys) -> None:
    """With a recurring run and a consumed one-shot run both in flight, the recurring
    job keeps its next-run line and the one-shot is named as one that does not run
    again."""
    svc = CronService(base_dir=config_dir())
    daily = svc.add_job("daily-report", "hi", cron_expr="0 3 * * *")
    once = svc.add_job("launch-reminder", "hi", at_ts=_SLOT, delete_after_run=True)
    for job, run in ((daily, "r1"), (once, "r2")):
        cron_inflight.write_marker(
            config_dir(), job.id, job.name, started_at=1_700_000_000.0, run=run
        )

    (before,) = _restart_through_the_service(capsys)

    assert "daily-report (since 22:13 UTC)" in before
    assert "launch-reminder (since 22:13 UTC)" in before
    assert "runs again at its next scheduled time" in before
    assert "each job runs again" not in before
    assert "does not run again" in before


@pytest.mark.parametrize("store", ["no-row", "unreadable", "paused"])
def test_restart_names_a_run_it_cannot_promise_a_next_run_for(capsys, store: str) -> None:
    """A run whose job the store read cannot classify (no row, an unreadable store),
    or whose job is paused, is named and said to be recorded cancelled, with no word
    on whether the job runs again."""
    job_id = "a1b2c3"
    if store == "paused":
        svc = CronService(base_dir=config_dir())
        job_id = svc.add_job("daily-report", "hi", cron_expr="0 3 * * *").id
        svc.enable_job(job_id, enabled=False)
    elif store == "unreadable":
        (config_dir() / "crons.json").write_text("{not json", encoding="utf-8")
    cron_inflight.write_marker(
        config_dir(), job_id, "daily-report", started_at=1_700_000_000.0, run="r1"
    )

    (before,) = _restart_through_the_service(capsys)

    line = "A restart interrupts the cron job(s) running now: daily-report (since 22:13 UTC)."
    assert line in before
    assert "An interrupted run is recorded as cancelled.\n" in before
    assert "runs again" not in before
    assert "after the restart" not in before


def test_restart_names_no_run_whose_writer_is_not_alive_here(capsys) -> None:
    """A marker another PID domain wrote, or one a crashed gateway left, names no run
    this restart can interrupt."""
    path = cron_inflight.marker_path(config_dir(), "d4e5f6", "r2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": "d4e5f6",
                "name": "elsewhere-report",
                "started_at": 1_700_000_000.0,
                "pid": os.getpid(),
                "pid_domain": "another-host:pidns",
                "pid_start": None,
            }
        ),
        encoding="utf-8",
    )
    assert [m.name for m in cron_inflight.read_markers(config_dir())] == ["elsewhere-report"]

    (before,) = _restart_through_the_service(capsys)

    assert "cron job" not in before
