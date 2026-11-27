"""A recurring cron job that pauses itself is announced — silent jobs included.

The notice has ONE owner: the store save's rising-edge detector
(``store_sync._save``) announces when a save writes ``auto_paused`` True for
a job whose disk state was unpaused, under the store lock, whichever writer
saved — so it never outruns the disk, never doubles across the flush/merge
race, and a clear or an externally-landed pause never rings. The notifier receives
``(job, streak, reason)`` read at the transition, so a resume-plus-success in
the hand-off window cannot turn the notice into "0 consecutive failures". The
gateway's ``_alert_cron_auto_paused`` rings the bell and runs the one-surface
delivery ladder, deliberately without ``silent`` — a silent job heard none of
the failures that led here, so this notice is the first and only word its
owner gets, and a schedule must not end with no word at all. Spec:
learn-cron-dashboard.md, "A recurring job that pauses itself is announced on
every surface".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The notice runs the shared redaction passes; without the detectors bound the
# whole message collapses to the redaction-unavailable tag and the content
# assertions below assert nothing.
import kiro_crew.security.redaction_structured.compat  # noqa: F401
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule, CronService
from kiro_crew.cron_service.execution import auto_pause_failed_one_shot
from kiro_crew.slack import gateway as gw


def _job(**kw: Any) -> CronJob:
    job = CronJob(
        id="j1",
        name="nightly",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=60),
    )
    for key, value in kw.items():
        setattr(job, key, value)
    return job


def _capture(svc: CronService) -> list[tuple[CronJob, int, str]]:
    announced: list[tuple[CronJob, int, str]] = []

    def _cb(job: CronJob, streak: int, reason: str) -> None:
        announced.append((job, streak, reason))

    svc.set_auto_pause_alert(_cb)
    return announced


# ── the model/store-side announcement ────────────────────────────────────────


def _svc_with_failing_run(tmp_path, *, last_error: str = "boom"):
    svc = CronService(base_dir=tmp_path)
    announced = _capture(svc)
    job = svc.add_job(name="flaky", message="go", every_secs=60)
    job.last_error = last_error
    for _ in range(_AUTO_PAUSE_THRESHOLD):
        job.record_failure()
    return svc, job, announced


def test_the_claim_side_charge_never_announces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``record_failure`` pauses in memory; the notice waits for the save."""
    _svc, job, announced = _svc_with_failing_run(tmp_path)
    assert job.auto_paused is True
    assert announced == []


def test_the_save_announces_the_rising_edge_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The one announce point is the save: the merge that persists the pause
    announces it with the stored streak and error, and every later save sees
    no edge — the flush/merge race cannot double it."""
    svc, job, announced = _svc_with_failing_run(tmp_path, last_error="exit 2")
    svc._merge_job_result(job)
    assert [a[1:] for a in announced] == [(_AUTO_PAUSE_THRESHOLD, "exit 2")]
    svc._merge_job_result(job)  # a second merge/save repeats nothing
    with svc._file_lock():
        svc._save()  # as would any unrelated later save
    assert len(announced) == 1


def test_a_limit_raised_mid_run_announces_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The run hits the OLD limit; the stored (raised) limit clears the pause
    at the merge — no "Paused itself" ever goes out."""
    svc, job, announced = _svc_with_failing_run(tmp_path)
    run = CronJob(**{f: getattr(job, f) for f in ("id", "name", "message", "schedule")})
    run.last_error = "boom"
    for _ in range(_AUTO_PAUSE_THRESHOLD):
        run.record_failure()
    job.auto_paused = False  # the stored copy never paused
    job.consecutive_failures = 0
    job.auto_pause_after_failures = _AUTO_PAUSE_THRESHOLD + 5  # raised during the run
    with svc._file_lock():
        svc._save()
    announced.clear()
    run.run_generation = job.run_generation + 1
    svc._merge_job_result(run)
    assert announced == []
    stored = next(j for j in svc._jobs if j.id == job.id)
    assert stored.auto_paused is False


def test_a_failed_save_announces_nothing_until_one_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The notice never outruns the disk: a save the filesystem refuses
    announces nothing, and the save that eventually lands announces once."""
    svc, job, announced = _svc_with_failing_run(tmp_path)
    import kiro_crew.atomic_write as aw

    real = aw.atomic_write

    def refuse(*a: Any, **k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(aw, "atomic_write", refuse)
    with pytest.raises(OSError):
        svc._merge_job_result(job)
    assert announced == []
    monkeypatch.setattr(aw, "atomic_write", real)
    svc._merge_job_result(job)
    assert len(announced) == 1


def test_a_clear_is_a_falling_edge_and_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    svc, job, announced = _svc_with_failing_run(tmp_path)
    svc._merge_job_result(job)
    assert len(announced) == 1
    job.record_success()
    svc._merge_job_result(job)
    assert len(announced) == 1  # the clear has its own delivery


def test_a_pause_already_on_disk_is_never_re_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Boot, and a reload of another process's write, rebuild the snapshot
    silently: the save that landed the pause announced it."""
    svc, job, announced = _svc_with_failing_run(tmp_path)
    svc._merge_job_result(job)
    assert len(announced) == 1
    svc2 = CronService(base_dir=tmp_path)  # boot over the paused store
    svc2.set_auto_pause_alert(lambda job, streak, reason: announced.append((job, streak, reason)))
    assert len(announced) == 1
    with svc2._file_lock():
        svc2._save()  # an unrelated save in the new process: no edge
    assert len(announced) == 1


def test_the_announcement_snapshots_the_streak_and_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The notifier gets the save's readings; a resume plus a success in the
    hand-off window resets the live job, not the captured values."""
    svc, job, announced = _svc_with_failing_run(tmp_path, last_error="exit 2")
    svc._merge_job_result(job)
    job.record_success()  # the hand-off window
    _, streak, reason = announced[0]
    assert streak == _AUTO_PAUSE_THRESHOLD and reason == "exit 2"


def test_a_stall_pause_over_incidental_failures_claims_no_streak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A pause landed with failures BELOW the limit (the loop-stall breaker's
    shape) must not blame them: the streak is the cause only when it reached
    the stored limit; here the cause rides the written last_error."""
    svc = CronService(base_dir=tmp_path)
    announced = _capture(svc)
    job = svc.add_job(name="stalled", message="go", every_secs=60)
    job.consecutive_failures = 2  # incidental, below the limit
    job.last_error = "paused at start: a prior run stalled the event loop"
    job.enabled, job.auto_paused = False, True  # as the stall breaker lands it
    with svc._file_lock():
        svc._save()
    assert [a[1:] for a in announced] == [
        (0, "paused at start: a prior run stalled the event loop")
    ]


def test_a_malformed_retained_record_cannot_fail_a_committed_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``encode_store`` appends retained malformed records after the valid
    jobs; one sharing a valid id with garbage fields must neither raise after
    the atomic write committed (the caller would roll back a save that
    happened) nor be the record the notice reads — the valid job serializes
    first and wins."""
    svc, job, announced = _svc_with_failing_run(tmp_path)
    svc._skipped_records = (
        *svc._skipped_records,
        {"id": job.id, "auto_paused": True, "consecutive_failures": "invalid"},
    )
    svc._merge_job_result(job)  # must not raise
    assert len(announced) == 1  # the valid record's edge, once
    _, streak, reason = announced[0]
    assert streak == _AUTO_PAUSE_THRESHOLD and reason == "boom"


def test_a_failed_one_shots_pause_is_not_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A one-shot's pause is retention bookkeeping with its own unconditional
    failure alert; the save's edge excludes it."""
    svc = CronService(base_dir=tmp_path)
    announced = _capture(svc)
    job = svc.add_job(name="once", message="go", at_ts=4102444800.0)
    job.delete_after_run = True
    job.last_status = "error"
    auto_pause_failed_one_shot(job, "error")
    assert job.auto_paused is True
    with svc._file_lock():
        svc._save()
    assert announced == []


def test_a_raising_notifier_never_reaches_the_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    svc, job, announced = _svc_with_failing_run(tmp_path)

    def _boom(job: CronJob, streak: int, reason: str) -> None:
        raise RuntimeError("notifier down")

    svc.set_auto_pause_alert(_boom)
    svc._merge_job_result(job)  # must not raise
    stored = next(j for j in svc._jobs if j.id == job.id)
    assert stored.auto_paused is True  # the pause persisted despite the notifier


def test_no_notifier_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    svc, job, announced = _svc_with_failing_run(tmp_path)
    svc.set_auto_pause_alert(None)
    svc._merge_job_result(job)
    stored = next(j for j in svc._jobs if j.id == job.id)
    assert stored.auto_paused is True


# ── the gateway notice ───────────────────────────────────────────────────────


def _orchestrator() -> Any:
    """A GatewayOrchestrator whose only alert surfaces are the bell and Slack."""
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch: Any = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value="111.1")
    orch._open_dm_with_retry = AsyncMock(return_value="D_OWNER")
    orch._deliver_cron_to_channel = AsyncMock(return_value=False)
    orch.dashboard_state = MagicMock()
    orch.dashboard_state.notify = MagicMock()
    orch.dashboard_state.last_notification_persist = None
    return orch


@pytest.mark.asyncio
async def test_the_notice_reaches_a_silent_jobs_owner() -> None:
    """``silent`` suppresses run noise; the end of the schedule is not run noise."""
    orch = _orchestrator()
    job = _job(silent=True)
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_auto_paused(job, _AUTO_PAUSE_THRESHOLD, "boom")
    orch.dashboard_state.notify.assert_called_once()
    orch.slack.post_message.assert_awaited_once()
    mrkdwn = orch.slack.post_message.await_args.args[1]
    assert "Paused itself" in mrkdwn and "resume" in mrkdwn
    assert job.id in mrkdwn  # the resume affordance names the job


@pytest.mark.asyncio
async def test_the_notice_names_the_streak_and_fences_the_reason() -> None:
    orch = _orchestrator()
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_auto_paused(job, 7, "exit 2: `config` missing")
    mrkdwn = orch.slack.post_message.await_args.args[1]
    assert "7 consecutive" in mrkdwn
    # The attacker-shaped reason is fenced so its markup cannot swallow the
    # authored resume affordances.
    assert "```" in mrkdwn and "config" in mrkdwn
    assert mrkdwn.index("cron resume") < mrkdwn.index("```")


@pytest.mark.asyncio
async def test_a_streakless_pause_claims_no_failed_runs() -> None:
    """The loop-stall breaker pauses a healthy job: streak 0 must not render
    as "0 consecutive failed runs" — the cause lives in the reason block."""
    orch = _orchestrator()
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_auto_paused(job, 0, "paused at boot: loop stall")
    mrkdwn = orch.slack.post_message.await_args.args[1]
    assert "0 consecutive" not in mrkdwn
    assert "loop stall" in mrkdwn and "Paused itself" in mrkdwn


@pytest.mark.asyncio
async def test_the_notice_moves_no_dedup_anchor() -> None:
    """Once per transition by construction: the per-run failure dedup stays."""
    orch = _orchestrator()
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_auto_paused(job, _AUTO_PAUSE_THRESHOLD, "boom")
    assert job.last_failure_hash == "" and job.last_failure_at == 0.0


@pytest.mark.asyncio
async def test_a_delivery_failure_never_raises() -> None:
    orch = _orchestrator()
    orch.slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
    orch.dashboard_state.notify = MagicMock(side_effect=RuntimeError("bell down"))
    job = _job()
    with patch.object(gw, "sel", lambda: MagicMock()):
        await orch._alert_cron_auto_paused(job, _AUTO_PAUSE_THRESHOLD, "boom")  # must return


# ── the wiring ───────────────────────────────────────────────────────────────


def _gateway_runtime_source(name: str) -> str:
    import kiro_crew.slack.gateway as mod

    return (Path(mod.__file__).resolve().parent / "gateway_runtime" / name).read_text(
        encoding="utf-8"
    )


def test_the_gateway_registers_the_notifier_and_clears_only_its_own() -> None:
    """Registration lives with the other cron alerts; exit drains in-flight
    notices bounded and clears only its own registration, in a finally, so a
    raising stop cannot leave a closed loop registered and a newer in-process
    orchestrator keeps its notifier."""
    fire = _gateway_runtime_source("cron_fire.py")
    assert "_cron_svc.set_auto_pause_alert(_notify_auto_pause)" in fire
    exit_src = _gateway_runtime_source("exit_path.py")
    assert "_auto_pause_alerts" in exit_src
