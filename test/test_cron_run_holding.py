"""The idle cleanup after a cron parent's last sub-agent tells the run's own holding by its task.

A message cron run's task is in ``_cron_runs_holding`` from before its first acquire
to its end (``cron_fire._cron_callback``). When the last completion's cleanup finds
the session busy, it reads the permit's holder: a cron run's own task leaves the
session and the key to that run's finalizer, and any other turn keeps the session
while the finished run's key is retired. A key the job's live run registered is kept
for that run, unless the run's engine marked it finished as it released the session
(``CronService.finish_session_key``): that key is owed only to this cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_spawn_sub_agents_settle_at_release import _cron_route_gateway, _registered
from test_subagent_injection_live_turn import _HANG_CEILING_S


@pytest.mark.asyncio
async def test_a_runs_key_survives_a_cleanup_between_its_acquire_and_its_first_dispatch() -> None:
    """A sequence step (or a persistent job's next fire) re-acquires its stable key
    and holds the permit, and is still awaiting something before it dispatches (its
    template rebind, a contended allocation lock). The last completion of an earlier
    holding is acknowledged with no claim and runs the cleanup in that window. The
    permit's holder is the run's own task, so the cleanup leaves the key the run
    registered: a reap or cancel of that run still ends the live step's session."""
    key = "cron:j1:alpha"  # a durable sequence step key
    sessions, gateway, info = _cron_route_gateway(key)
    svc = _registered(gateway, key)  # the step registers its key before it acquires
    acquired, go = asyncio.Event(), asyncio.Event()

    async def step() -> None:
        run_task = asyncio.current_task()
        gateway._cron_runs_holding.add(run_task)  # as _cron_callback enters the run
        try:
            await sessions.get_or_create(key)  # the permit is the step's from here
            acquired.set()
            await go.wait()  # e.g. _rebind_prepared_execution's to_thread
            sessions.release(key)
        finally:
            gateway._cron_runs_holding.discard(run_task)

    task = asyncio.create_task(step())
    await asyncio.wait_for(acquired.wait(), _HANG_CEILING_S)
    await gateway._reset_cron_parent_when_idle(info, key)
    reap_targets = svc._run_session_keys("j1", None) or ["cron:j1"]  # _fence_run_keys
    go.set()
    await asyncio.wait_for(task, _HANG_CEILING_S)

    assert key in reap_targets, (
        f"the live step's key was retired; a reap or cancel would end {reap_targets!r} "
        "and never this step's session"
    )
    assert key in sessions._sessions, "the cleanup reset the session under the run"


# ── a newer run of the job owns the key now ─────────────────────────────────


def _live_run(svc: Any, previous: Any = None) -> Any:
    """End ``previous`` run's claim, if any, and claim the job for a new run."""
    if previous is not None:
        svc._claims.pop("j1", None)
    return svc._claim_run("j1", "scheduled")


@pytest.mark.asyncio
async def test_a_newer_run_waiting_for_the_permit_keeps_the_key_it_registered() -> None:
    """R1's last completion is cleaned up after R1 ended. R2 of the same job has
    registered the same durable sequence-step key and waits for the permit, which a
    tab turn holds. The cleanup leaves the session to the tab turn, and the key stays
    registered to R2: a reap or cancel of R2 still ends this step's session."""
    from kiro_crew.cron import CronService

    key = "cron:j1:alpha"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    r1 = _live_run(svc)
    svc.register_active_session_key("j1", key)
    r2 = _live_run(svc, previous=r1)
    svc.register_active_session_key("j1", key)  # R2's step, before its acquire
    await sessions.get_or_create(key)  # the tab turn holds the permit
    acquired = asyncio.Event()

    async def r2_step() -> None:
        run_task = asyncio.current_task()
        gateway._cron_runs_holding.add(run_task)
        try:
            await sessions.get_or_create(key)  # waits behind the tab turn
            acquired.set()
            sessions.release(key)
        finally:
            gateway._cron_runs_holding.discard(run_task)

    step = asyncio.create_task(r2_step())
    await asyncio.sleep(0)
    await gateway._reset_cron_parent_when_idle(info, key)  # R1's deferred cleanup

    assert key in svc._run_session_keys("j1", r2), "R1's cleanup retired R2's key"
    sessions.release(key)  # the tab turn ends
    await asyncio.wait_for(acquired.wait(), _HANG_CEILING_S)
    await asyncio.wait_for(step, _HANG_CEILING_S)


@pytest.mark.asyncio
async def test_a_newer_runs_key_survives_a_cleanup_that_resets_the_idle_session() -> None:
    """The same, with nothing holding the session: the cleanup resets it, and R2's
    registration of the key still survives, for R2's own finalizer to retire."""
    from kiro_crew.cron import CronService

    key = "cron:j1:alpha"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    r1 = _live_run(svc)
    svc.register_active_session_key("j1", key)
    await sessions.get_or_create(key)
    sessions.release(key)  # R1's turn ended; nothing holds the session
    r2 = _live_run(svc, previous=r1)
    svc.register_active_session_key("j1", key)  # R2, before its acquire

    await gateway._reset_cron_parent_when_idle(info, key)

    assert key not in sessions._sessions, "the idle session was not reset"
    assert key in svc._run_session_keys("j1", r2), "R1's cleanup retired R2's key"
    svc.clear_active_session_key("j1", key)  # R2's finalizer
    assert key not in svc.active_session_keys()


@pytest.mark.asyncio
async def test_a_finished_runs_key_is_retired_when_no_newer_run_owns_it() -> None:
    """R1 registered the key under its claim and ended; no run of the job is live.
    The cleanup retires the key."""
    from kiro_crew.cron import CronService

    key = "cron:j1:alpha"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    _live_run(svc)
    svc.register_active_session_key("j1", key)
    svc._claims.pop("j1")  # R1 ended

    await gateway._reset_cron_parent_when_idle(info, key)

    assert key not in svc.active_session_keys()


@pytest.mark.asyncio
async def test_a_stateless_runs_key_is_retired_while_the_next_run_keeps_its_own() -> None:
    """A stateless job: R1's key is its own. R1's cleanup runs while R2 runs under
    its own key: R1's key is retired and R2's stays registered to R2."""
    from kiro_crew.cron import CronService

    r1_key, r2_key = "cron:j1:r1", "cron:j1:r2"
    sessions, gateway, info = _cron_route_gateway(r1_key)
    svc = gateway.cron_svc = CronService()
    r1 = _live_run(svc)
    svc.register_active_session_key("j1", r1_key)
    r2 = _live_run(svc, previous=r1)
    svc.register_active_session_key("j1", r2_key)

    await gateway._reset_cron_parent_when_idle(info, r1_key)

    assert r1_key not in svc.active_session_keys(), "the finished run's key stayed"
    assert svc._run_session_keys("j1", r2) == [r2_key]


# ── a live run's own key that its turn has finished with ─────────────────────


@pytest.mark.asyncio
async def test_a_live_runs_own_deferred_step_key_is_retired_by_its_last_cleanup() -> None:
    """Step alpha of run R deferred its reset for children that outlived it, and R is
    on step beta, so its claim is live. Alpha's engine marked the key finished as it
    released the session (``finish_session_key``), so alpha's last child's cleanup
    resets the idle session and retires the key: nothing else would."""
    from kiro_crew.cron import CronService

    key = "cron:j1:alpha"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    run = svc._claim_run("j1", "scheduled")
    svc.register_active_session_key("j1", key)  # step alpha, before its acquire
    await sessions.get_or_create(key)
    svc.finish_session_key("j1", key)  # alpha's finally, before its release
    sessions.release(key)  # alpha's turn ended; its finally deferred the reset
    svc.register_active_session_key("j1", "cron:j1:beta")  # R is on step beta now

    await gateway._reset_cron_parent_when_idle(info, key)  # alpha's last child

    assert key not in sessions._sessions, "the idle step session was not reset"
    still = key in svc._run_session_keys("j1", run)
    svc.clear_active_session_key("j1", "cron:j1:beta")  # beta's finalizer
    svc._claims.pop("j1")  # R ends
    assert not still and key not in svc.active_session_keys(), (
        "the finished step's key stayed registered to its live run and outlived the run: "
        f"active_session_keys()={sorted(svc.active_session_keys())!r}"
    )


@pytest.mark.asyncio
async def test_a_stateless_runs_key_is_retired_when_its_last_cleanup_beats_the_claim_release() -> (
    None
):
    """A stateless run's ``finally`` marked its key finished and deferred the reset;
    its last child's cleanup runs before ``_run_job_isolated`` releases the claim.
    The ephemeral key, which no run registers again, is retired there."""
    from kiro_crew.cron import CronService

    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    svc._claim_run("j1", "scheduled")
    svc.register_active_session_key("j1", key)
    await sessions.get_or_create(key)
    svc.finish_session_key("j1", key)
    sessions.release(key)  # the run's finally deferred the reset

    await gateway._reset_cron_parent_when_idle(info, key)  # inside the claim-release window
    svc._claims.pop("j1")  # the claim is released

    assert key not in svc.active_session_keys(), (
        "a finished stateless run's ephemeral key stays registered for the life of the "
        "process, so job ownership reads that run as live"
    )


@pytest.mark.asyncio
async def test_a_key_registered_again_after_it_was_finished_is_kept_for_the_new_run() -> None:
    """R1 finished its turn on a durable key and deferred the reset; R2 of the same
    job registered the same key before its acquire. The registration takes the
    finished mark off, so R1's last cleanup leaves the key to R2."""
    from kiro_crew.cron import CronService

    key = "cron:j1:alpha"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = gateway.cron_svc = CronService()
    r1 = _live_run(svc)
    svc.register_active_session_key("j1", key)
    await sessions.get_or_create(key)
    svc.finish_session_key("j1", key)
    sessions.release(key)
    r2 = _live_run(svc, previous=r1)
    svc.register_active_session_key("j1", key)  # R2, before its acquire

    await gateway._reset_cron_parent_when_idle(info, key)  # R1's deferred cleanup

    assert key in svc._run_session_keys("j1", r2), "R1's cleanup retired R2's key"
    assert ("j1", key) not in svc._finished_session_keys


def test_a_finished_mark_is_dropped_with_the_key_and_ignored_for_an_unknown_key() -> None:
    """``finish_session_key`` marks only a registered key; clearing the key drops the
    mark, so a later registration of the same key starts unfinished."""
    from kiro_crew.cron import CronService

    svc = CronService()
    svc.finish_session_key("j1", "cron:j1:never")
    assert svc._finished_session_keys == set()
    svc._claim_run("j1", "scheduled")
    svc.register_active_session_key("j1", "cron:j1:r1")
    svc.finish_session_key("j1", "cron:j1:r1")
    assert svc._finished_session_keys == {("j1", "cron:j1:r1")}
    svc.clear_active_session_key("j1", "cron:j1:r1")
    assert svc._finished_session_keys == set()
