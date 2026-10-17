"""A one-shot whose fire keeps being refused retries on a floor, not in a loop.

A refused fire leaves a plain ``at`` job enabled past its time, so it runs once
dispatch is possible. Its wake must not stay at ``at_ts``: that time has passed,
so the timer would re-arm at zero delay and re-fire the job on every pass for
as long as the refusal lasts, each pass taking the store lock, merging, saving
and appending a cron-history row.

The tests tick the real ``_on_timer`` by hand and await the claim it dispatched,
then read the delay the timer would arm (``next_wake_secs`` at an explicit
``now``, and ``_effective_delay``). Nothing sleeps and no elapsed time is
measured. The refusal is the real ``_defer_cron_before_dispatch`` called from
the job callback; only ``admission_check`` is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import write_member_home

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import CronJob, CronService
from kiro_crew.cron_service import schedule
from kiro_crew.cron_service.schedule import next_wake_secs
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.slack.gateway_runtime.cron_dispatch import _defer_cron_before_dispatch

_ADMITTED = SimpleNamespace(admitted=True, reason="")
_EMPTY_STEP = "empty agent_sequence step cannot resolve a crew; refusing to dispatch"


class _Gateway:
    """A job callback that refuses to dispatch while ``refusing`` is set."""

    def __init__(self, *, refusing: bool = True) -> None:
        self.refusing = refusing
        self.refused: list[str] = []
        self.ran: list[str] = []

    async def on_job(self, job: CronJob) -> None:
        if self.refusing:
            self.refused.append(job.id)
            _defer_cron_before_dispatch(job, "gateway admission is closed")
            return
        self.ran.append(job.id)


async def _service(tmp_path, gateway: _Gateway) -> CronService:
    """A started service whose timer only fires when the test ticks it."""
    service = CronService(base_dir=tmp_path, on_job=gateway.on_job)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
    return service


async def _tick(service: CronService, job_id: str) -> None:
    """One timer tick, then wait for the run it dispatched, if any."""
    await service._on_timer()
    claim = service._claims.get(job_id)
    if claim is not None and claim.task is not None:
        await asyncio.wait_for(claim.task, timeout=5.0)


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _wake_after_attempt(service: CronService, job: CronJob) -> float | None:
    """The wake the timer arms for ``job``, read at the moment of its last attempt."""
    assert job.last_run_ts is not None, "the refused attempt was not stamped"
    return next_wake_secs(service._jobs, service._claims, job.last_run_ts)


@pytest.mark.asyncio
async def test_a_sustained_refusal_does_not_refire_the_one_shot_in_a_loop(tmp_path):
    gateway = _Gateway()
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        service = await _service(tmp_path, gateway)
        try:
            at_ts = time.time() - 1
            job = await service.add_job_async("remind-once", "msg", at_ts=at_ts)
            await _tick(service, job.id)
            stored = _stored(tmp_path, job.id)
            assert gateway.refused == [job.id]
            assert stored.enabled, "the refused one-shot was parked"
            assert stored.last_run_ts is not None and stored.last_run_ts >= at_ts
            wake = _wake_after_attempt(service, stored)
            armed = service._effective_delay()
        finally:
            await service.stop()
    assert wake == pytest.approx(
        schedule._AT_RETRY_FLOOR_SECS
    ), f"the refused one-shot's wake is {wake}s: the timer re-arms it at zero delay"
    assert armed > 0.0, "the timer would re-arm at zero delay and re-fire the job"


@pytest.mark.asyncio
async def test_a_refused_one_shot_retries_after_the_floor_and_runs_once_the_refusal_clears(
    tmp_path,
):
    gateway = _Gateway()
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        service = await _service(tmp_path, gateway)
        try:
            job = await service.add_job_async("remind-once", "msg", at_ts=time.time() - 1)
            await _tick(service, job.id)
            first = _stored(tmp_path, job.id)
            # The floor passes and the timer ticks again: still refused, and the
            # wake moves to one floor after this attempt too.
            await _tick(service, job.id)
            second = _stored(tmp_path, job.id)
            assert gateway.refused == [job.id, job.id], "the refused one-shot was not retried"
            assert second.enabled
            assert second.last_run_ts is not None and first.last_run_ts is not None
            assert second.last_run_ts >= first.last_run_ts
            assert _wake_after_attempt(service, second) == pytest.approx(
                schedule._AT_RETRY_FLOOR_SECS
            )
            gateway.refusing = False
            await _tick(service, job.id)
        finally:
            await service.stop()
    assert gateway.ran == [job.id], "the one-shot did not run once the refusal cleared"
    assert not _stored(tmp_path, job.id).enabled, "the one-shot was not parked after it ran"


@pytest.mark.asyncio
async def test_an_accepted_one_shot_fires_at_its_time(tmp_path):
    gateway = _Gateway(refusing=False)
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        service = await _service(tmp_path, gateway)
        try:
            at_ts = time.time() - 1
            job = await service.add_job_async("remind-once", "msg", at_ts=at_ts)
            # Unattempted, its wake is its own time: no floor is added.
            assert next_wake_secs(service._jobs, service._claims, at_ts - 7) == pytest.approx(7.0)
            assert next_wake_secs(service._jobs, service._claims, at_ts) == 0.0
            await _tick(service, job.id)
        finally:
            await service.stop()
    assert gateway.ran == [job.id], f"the one-shot ran {len(gateway.ran)} times"
    assert not _stored(tmp_path, job.id).enabled


@pytest.mark.asyncio
async def test_only_the_wake_of_a_retried_one_shot_moves(tmp_path):
    service = CronService(base_dir=tmp_path)
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    now = time.time()
    every = await service.add_job_async("every", "msg", every_secs=600)
    fresh = await service.add_job_async("fresh", "msg", at_ts=now + 120)
    refused = await service.add_job_async("refused", "msg", at_ts=now - 10)
    every.last_run_ts = now - 100
    floor = schedule._AT_RETRY_FLOOR_SECS

    # A recurring job and an unattempted one-shot keep their own times.
    assert next_wake_secs([every], (), now) == pytest.approx(500.0)
    assert next_wake_secs([fresh], (), now) == pytest.approx(120.0)
    # An enabled one-shot attempted at or after its time waits one floor.
    refused.last_run_ts = now - 5
    assert next_wake_secs([refused], (), now) == pytest.approx(floor - 5)
    refused.last_run_ts = refused.schedule.at_ts
    assert next_wake_secs([refused], (), now) == pytest.approx(floor - 10)
    # An attempt before its time (the one-shot was moved later) does not count.
    refused.last_run_ts = now - 11
    assert next_wake_secs([refused], (), now) == 0.0


async def _gateway_cron_callback(monkeypatch, cfg):
    """The gateway's own cron callback (``GatewayOrchestrator._init_cron``), stopped
    before any provider is reached, set up as ``test_cron_sequence_member_alias.py``
    sets it up."""
    from kiro_crew.slack import gateway

    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)
    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("stop before the provider"))
    gw.ctx_builder = MagicMock()
    gw.slack = gw.conv_log = gw.dashboard_state = gw.subagent_mgr = None
    gw._owner_id = "owner"
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._cfg = cfg
    gw.cron_svc = None
    callbacks = []

    async def create(on_job=None, **kwargs):
        callbacks.append(on_job)
        service = MagicMock()
        service.start = AsyncMock()
        return service

    monkeypatch.setattr(gateway.CronService, "create", create)
    monkeypatch.setattr(
        gateway, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
    )
    await gw._init_cron()
    return callbacks[0]


@pytest.mark.asyncio
async def test_a_refusal_a_retry_cannot_clear_parks_a_plain_one_shot(monkeypatch, tmp_path):
    """A plain one-shot whose ``agent_sequence`` holds an empty step is refused by the
    gateway's real cron callback before dispatch, for a reason in its own
    configuration. The next attempt meets the same refusal, so the one-shot is parked
    after the first one instead of retrying every floor for as long as the gateway
    runs."""
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    gateway_on_job = await _gateway_cron_callback(monkeypatch, cfg)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
        service = await _service(tmp_path, SimpleNamespace(on_job=gateway_on_job))
        try:
            job = await service.add_job_async("seq-empty-step", "task", at_ts=time.time() - 1)
            listed = next(j for j in service._jobs if j.id == job.id)
            listed.agent_sequence = ["", "beta"]
            listed.execution_context = captured.to_record()
            await _tick(service, job.id)
            live = next(j for j in service._jobs if j.id == job.id)
        finally:
            await service.stop()
    assert _EMPTY_STEP in (live.last_error or ""), live.last_error
    assert not live.enabled, (
        "a plain one-shot refused at its empty agent_sequence step is still enabled: "
        "it retries every floor for as long as the gateway runs"
    )
    assert not _stored(tmp_path, job.id).enabled, "the parked one-shot was not saved disabled"
