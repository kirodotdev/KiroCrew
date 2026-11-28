"""A scheduled agent turn waits while the host's own sign-in has expired.

An edition whose tools sign in with the host operator's credential says so through
``IdentityProvider.credential_freshness``. The cron callback asks before a
scheduled agent turn starts; an expired answer leaves the run unstarted (retained,
not counted) and tells the owner the edition's remedy, once per wait: a one-shot
retried on every timer pass is told once and writes one history row, a recurring
message job is told and writes a row at each held slot a day apart, and a Bot's
tick writes no row. The public default answers ``None``, so every run starts as
before.

The callback is the real one ``_init_cron`` builds; only the turn itself (session
acquire, stream) is a stand-in, and the cron service is a stand-in except where a
test drives the real one through its timer passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import threading
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import _make_command_job, _make_gw_for_llm, _make_llm_job

from kiro_crew import preflight
from kiro_crew.config import KiroCrewConfig
from kiro_crew.cron_service.execution import effective_wake_budget
from kiro_crew.cron_service.model import CronSchedule
from kiro_crew.executors import cron_gate_budget
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultIdentityProvider
from kiro_crew.platform.interfaces import CredentialFreshness
from kiro_crew.preflight import expired_host_credential
from kiro_crew.slack.gateway_runtime.cron_verdict import _FAILURE_REMINDER_SECS
from kiro_crew.testing.wait import default_timeout

_REMEDY = "Your sign-in on this machine has expired, so this run was not started. Sign in again."
_EXPIRED = CredentialFreshness(expired=True, remedy=_REMEDY)
#: 2100-01-01: a one-shot the test fires by hand, never by the clock.
_FAR_FUTURE_TS = 4102444800.0
_DAY_SECS = 86400


class _Identity(DefaultIdentityProvider):
    def __init__(self, reading: CredentialFreshness | None) -> None:
        self.reading = reading
        self.asked = 0
        self.job = None
        self.retained_while_asked: list[bool] = []

    def credential_freshness(self) -> CredentialFreshness | None:
        self.asked += 1
        if self.job is not None:
            self.retained_while_asked.append(self.job.run_never_started)
        return self.reading


class _BrokenIdentity(DefaultIdentityProvider):
    def credential_freshness(self) -> CredentialFreshness | None:
        raise RuntimeError("probe failed")


class _WedgedIdentity(DefaultIdentityProvider):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def credential_freshness(self) -> CredentialFreshness | None:
        self.entered.set()
        self.release.wait(default_timeout())
        self.finished.set()
        return _EXPIRED


@pytest.fixture
def install_identity() -> Iterator:
    def _install(identity: object) -> None:
        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, identity=identity))

    yield _install
    reset_context()


@contextlib.asynccontextmanager
async def _gateway_callback(gw, *, trigger: str = "scheduled") -> AsyncIterator:
    """The gateway's cron callback, with the turn itself stood in, as *trigger*."""
    captured = {}
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    stream = AsyncMock(return_value="PONG")
    with (
        patch("kiro_crew.slack.gateway.CronService") as cron_cls,
        patch("kiro_crew.slack.gateway.run_in_embed_pool", AsyncMock(return_value=("p", None))),
        patch("kiro_crew.slack.gateway.stream_and_collect", stream),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as session_ctx,
        patch("kiro_crew.cron_service.agent_presence.cron_agent_missing", return_value=False),
        patch("kiro_crew.slack.gateway.run_command_sandboxed") as run_command,
    ):
        session_ctx.side_effect = lambda job, *_a, **_kw: (f"cron:{job.id}", job.message)
        run_command.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        def _create(on_job=None, **_kw):
            captured["on_job"] = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            svc.run_trigger = MagicMock(return_value=trigger)
            return svc

        cron_cls.create = AsyncMock(side_effect=_create)
        await gw._init_cron()
        yield captured["on_job"], stream, run_command


async def _fire(gw, job, *, trigger: str = "scheduled"):
    """Fire *job* once through the gateway's cron callback, as *trigger*."""
    async with _gateway_callback(gw, trigger=trigger) as (on_job, stream, run_command):
        result = await on_job(job)
    return result, stream, run_command


def _told(gw) -> int:
    return sum(_REMEDY in str(call.args[2]) for call in gw.dashboard_state.notify.call_args_list)


@contextlib.contextmanager
def _bot_tick(kind: str) -> Iterator[AsyncMock]:
    """Make the callback read the job as a Bot's *kind* tick; yields the tick's fire."""
    from kiro_crew.dashboard import goal_reminder, stale_wake

    module, is_job = {
        "goal check-in": (goal_reminder, "is_reminder_job"),
        "stale-wake": (stale_wake, "is_stale_wake_job"),
    }[kind]
    fire = AsyncMock(return_value="checked in")
    with (
        patch.object(module, is_job, return_value=True),
        patch.object(module, "fire", fire),
        patch("kiro_crew.slack.gateway.vet_job_at_fire_time", return_value=None),
    ):
        yield fire


@pytest.mark.asyncio
async def test_the_public_default_has_no_host_sign_in_to_wait_for() -> None:
    assert DefaultIdentityProvider().credential_freshness() is None
    assert await expired_host_credential() is None


@pytest.mark.asyncio
async def test_a_probe_that_does_not_answer_in_time_holds_nothing(
    install_identity, monkeypatch
) -> None:
    identity = _WedgedIdentity()
    install_identity(identity)
    # Only the run's budget, shorter than the probe's own bound, can end the wait.
    monkeypatch.setattr(preflight, "_CREDENTIAL_FRESHNESS_TIMEOUT_SECS", default_timeout())
    asking = asyncio.ensure_future(expired_host_credential(0.01))
    # Its first step hands the probe to a thread; hold the loop right after it until
    # the thread has started it, so the bound cannot cancel it before it runs.
    asyncio.get_running_loop().call_soon(identity.entered.wait, default_timeout())
    try:
        assert await asking is None
        # The bound gave up on the probe; it did not stop it.
        assert identity.entered.is_set()
        assert not identity.finished.is_set()
    finally:
        identity.release.set()
    # Its thread is left to finish on its own.
    assert identity.finished.wait(default_timeout())


@pytest.mark.asyncio
async def test_an_expired_sign_in_keeps_a_scheduled_agent_turn_waiting(install_identity) -> None:
    identity = _Identity(_EXPIRED)
    install_identity(identity)
    gw, job = _make_gw_for_llm(), _make_llm_job()
    identity.job = job

    result, stream, _ = await _fire(gw, job)

    assert result is None
    gw.sessions.get_or_create.assert_not_awaited()
    stream.assert_not_awaited()
    assert identity.asked == 1
    # Retained while the probe was awaited, so a cancel there keeps a one-shot.
    assert identity.retained_while_asked == [True]
    assert (job.last_status, job.last_error) == ("error", _REMEDY)
    # Retained and not counted: nothing ran, and it runs once the person signs in.
    assert job.run_never_started is True
    assert job.fire_time_denied is False
    assert job.consecutive_failures == 0
    # The pass that told the owner keeps its history row.
    assert job.quiet_run is False
    assert any(
        "Not run:" in str(call.args[2]) and _REMEDY in str(call.args[2])
        for call in gw.dashboard_state.notify.call_args_list
    )


@pytest.mark.asyncio
async def test_a_fresh_sign_in_starts_the_turn(install_identity) -> None:
    identity = _Identity(CredentialFreshness(expired=False, remedy=_REMEDY))
    install_identity(identity)
    gw, job = _make_gw_for_llm(), _make_llm_job()
    identity.job = job

    _, stream, _ = await _fire(gw, job)

    stream.assert_awaited()
    assert identity.retained_while_asked == [True]
    assert job.run_never_started is False
    assert _told(gw) == 0


@pytest.mark.asyncio
async def test_a_probe_that_fails_holds_nothing(install_identity) -> None:
    install_identity(_BrokenIdentity())
    gw, job = _make_gw_for_llm(), _make_llm_job()

    _, stream, _ = await _fire(gw, job)

    stream.assert_awaited()


@pytest.mark.asyncio
async def test_a_manual_run_starts_whatever_the_sign_in_says(install_identity) -> None:
    identity = _Identity(_EXPIRED)
    install_identity(identity)
    gw, job = _make_gw_for_llm(), _make_llm_job()

    _, stream, _ = await _fire(gw, job, trigger="manual")

    stream.assert_awaited()
    assert identity.asked == 0


@pytest.mark.asyncio
async def test_a_command_job_does_not_wait(install_identity) -> None:
    identity = _Identity(_EXPIRED)
    install_identity(identity)
    gw, job = _make_gw_for_llm(), _make_command_job()

    _, _, run_command = await _fire(gw, job)

    run_command.assert_called()
    assert identity.asked == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["goal check-in", "stale-wake"])
async def test_a_bots_tick_waits_too_and_tells_its_owner(install_identity, kind) -> None:
    install_identity(_Identity(_EXPIRED))
    gw, job = _make_gw_for_llm(), _make_llm_job(silent=True)

    with _bot_tick(kind) as fire:
        result, _, _ = await _fire(gw, job)

    assert result is None
    fire.assert_not_awaited()
    assert job.run_never_started is True
    # A Bot's ticks are silent jobs, and a tick that waits woke no one: no history
    # row, but its owner is told how to sign in again.
    assert job.quiet_run is True
    assert _told(gw) == 1


@pytest.mark.asyncio
async def test_a_one_shot_retried_through_the_night_is_told_once(install_identity) -> None:
    install_identity(_Identity(_EXPIRED))
    gw = _make_gw_for_llm()
    job = _make_llm_job(
        schedule=CronSchedule(kind="at", at_ts=_FAR_FUTURE_TS), delete_after_run=True
    )

    await _fire(gw, job)
    assert (_told(gw), job.quiet_run) == (1, False)
    # Retried on every timer pass: each retry is inside the window of the one before,
    # so hours of them are one wait, told once, with one history row.
    for _ in range(3):
        job.last_failure_at -= _FAILURE_REMINDER_SECS - 60
        await _fire(gw, job)
        assert (_told(gw), job.quiet_run) == (1, True)
    # A run that succeeded ends the wait, so the next one is told again.
    job.record_success()
    await _fire(gw, job)
    assert (_told(gw), job.quiet_run) == (2, False)


@pytest.mark.asyncio
async def test_a_recurring_job_is_told_and_recorded_at_each_held_slot(install_identity) -> None:
    install_identity(_Identity(_EXPIRED))
    gw = _make_gw_for_llm()
    # A threshold the owner raised must not hide a run that never starts.
    job = _make_llm_job(
        schedule=CronSchedule(kind="every", every_secs=_DAY_SECS), alert_after_failures=3
    )

    await _fire(gw, job)
    assert (_told(gw), job.quiet_run) == (1, False)
    # The next night: the person may have signed in since and seen it expire again,
    # and nothing ran in between to say so, so this slot is told again.
    job.last_failure_at -= _DAY_SECS
    await _fire(gw, job)
    assert (_told(gw), job.quiet_run) == (2, False)


async def _timer_pass(svc, job_id: str) -> None:
    job = svc.get_job(job_id)
    await svc._run_job_isolated(job, svc._claim_run(job_id, "scheduled"))


@pytest.mark.asyncio
async def test_a_waiting_one_shot_writes_one_history_row_over_many_passes(
    install_identity, tmp_path
) -> None:
    from kiro_crew.cron import CronService

    install_identity(_Identity(_EXPIRED))
    gw = _make_gw_for_llm()
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("nightly report", "Reply PONG", at_ts=_FAR_FUTURE_TS, delete_after_run=True)

    async with _gateway_callback(gw) as (on_job, stream, _):
        svc._on_job = on_job
        gw.cron_svc = svc
        for _ in range(3):
            await _timer_pass(svc, job.id)

    stream.assert_not_awaited()
    rows, total = await svc._history.get_job_history(job.id)
    assert total == 1
    assert rows[0]["status"] == "failure"
    assert _REMEDY in rows[0]["summary"]
    kept = svc.get_job(job.id)
    assert kept is not None and kept.enabled
    assert kept.consecutive_failures == 0
    assert _told(gw) == 1


@pytest.mark.asyncio
async def test_a_recurring_job_held_two_nights_running_is_told_and_recorded_twice(
    install_identity, tmp_path
) -> None:
    from kiro_crew.cron import CronService

    install_identity(_Identity(_EXPIRED))
    gw = _make_gw_for_llm()
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("nightly report", "Reply PONG", every_secs=_DAY_SECS, strict_schedule=True)

    async with _gateway_callback(gw) as (on_job, stream, _):
        svc._on_job = on_job
        gw.cron_svc = svc
        await _timer_pass(svc, job.id)
        # A day later, with nothing run in between (no catch-up runs a held slot).
        svc.get_job(job.id).last_failure_at -= _DAY_SECS
        await _timer_pass(svc, job.id)

    stream.assert_not_awaited()
    rows, total = await svc._history.get_job_history(job.id)
    assert total == 2
    assert all(row["status"] == "failure" and _REMEDY in row["summary"] for row in rows)
    kept = svc.get_job(job.id)
    assert kept.enabled and kept.consecutive_failures == 0
    assert _told(gw) == 2


@pytest.mark.asyncio
async def test_a_short_job_bounds_the_probe_by_its_own_budget(install_identity) -> None:
    install_identity(_Identity(None))
    gw = _make_gw_for_llm()
    job = _make_llm_job(timeout_secs=4)  # flake-ok: only read for the bound; nothing waits on it
    probe = AsyncMock(return_value=None)

    with patch.object(preflight, "expired_host_credential", probe):
        _, stream, _ = await _fire(gw, job)

    stream.assert_awaited()
    # Bounded as the fire-time gate is, so the probe's own bound ends inside the
    # wake deadline and the run starts, instead of the deadline ending the run.
    (budget,) = probe.await_args.args
    wake = effective_wake_budget(job)
    assert budget == cron_gate_budget(wake)
    assert budget < min(wake, preflight._CREDENTIAL_FRESHNESS_TIMEOUT_SECS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "every_secs", "told"),
    # A tick every ten minutes is held again inside the window: one wait, told once.
    # A daily check-in held the next day is told again.
    [("stale-wake", 600, 1), ("goal check-in", _DAY_SECS, 2)],
)
async def test_a_waiting_bot_tick_writes_no_history_row(
    install_identity, tmp_path, kind, every_secs, told
) -> None:
    from kiro_crew.cron import CronService

    install_identity(_Identity(_EXPIRED))
    gw = _make_gw_for_llm()
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("Bot tick", "wake", every_secs=every_secs, silent=True, strict_schedule=True)

    async with _gateway_callback(gw) as (on_job, _, _):
        svc._on_job = on_job
        gw.cron_svc = svc
        with _bot_tick(kind) as fire:
            await _timer_pass(svc, job.id)
            svc.get_job(job.id).last_failure_at -= every_secs
            await _timer_pass(svc, job.id)

    fire.assert_not_awaited()
    assert (await svc._history.get_job_history(job.id))[1] == 0
    assert _told(gw) == told
