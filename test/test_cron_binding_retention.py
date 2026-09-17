"""Bounds and lifecycle cleanup for cron session binding identities."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronService
from kiro_crew.slack.gateway import (
    _CRON_SESSION_BINDING_STRING_LIMIT,
    _CRON_SESSION_KEY_LIMIT,
    GatewayOrchestrator,
)


def _binding(value: str = "value") -> tuple[str, str, str | None, str]:
    return (value, value, value, value)


def _gateway() -> GatewayOrchestrator:
    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway._cron_session_binding = {}
    gateway._cron_session_binding_overflow_count = 0
    gateway._cron_injecting = {}
    gateway.subagent_mgr = None
    return gateway


@pytest.mark.asyncio
async def test_removing_a_job_evicts_every_binding_for_that_job(tmp_path) -> None:
    gateway = _gateway()
    gateway._cron_binding_loop = asyncio.get_running_loop()
    service = CronService(
        base_dir=tmp_path,
        on_jobs_removed=gateway._cron_jobs_removed,
    )
    job = await asyncio.to_thread(
        service.add_job,
        name="removed",
        message="run",
        every_secs=300,
    )
    gateway._cron_session_binding = {
        f"cron:{job.id}": _binding(),
        f"cron:{job.id}:first": _binding(),
        f"cron:{job.id}:second": _binding(),
        "cron:other": _binding(),
    }
    service.register_active_session_key(job.id, f"cron:{job.id}:first")

    assert await service.remove_job_async(
        job.id,
        actor="test",
        source="test",
    )
    await asyncio.sleep(0)

    assert set(gateway._cron_session_binding) == {"cron:other"}
    # Read the store, not an accessor: this pins that the removal EVICTED the
    # job's keys, where an accessor's None could also mean "the job is claimed".
    assert job.id not in service._active_session_keys


@pytest.mark.parametrize("field", ["key", "cwd", "agent", "crew_alias", "source"])
def test_every_retained_binding_string_is_length_bounded(field: str) -> None:
    gateway = _gateway()
    too_long = "x" * (_CRON_SESSION_BINDING_STRING_LIMIT + 1)
    # The key is derived rather than persisted, so its cap is its own -- using the
    # field cap here would assert a bound that refuses valid keys (see below).
    key = "x" * (_CRON_SESSION_KEY_LIMIT + 1) if field == "key" else "cron:bounded"
    values = list(_binding())
    if field != "key":
        values[{"cwd": 0, "agent": 1, "crew_alias": 2, "source": 3}[field]] = too_long

    admitted = gateway._retain_cron_session_bindings(
        {key: (values[0], values[1], values[2], values[3])}
    )

    assert not admitted
    assert gateway._cron_session_binding == {}
    assert gateway._cron_session_binding_overflow_count == 1


def test_a_maximal_legitimate_sequence_key_is_still_admitted() -> None:
    """A valid max-length agent name must not be refused into a permanent defer.

    The sequence key is ``cron:<job id>:<agent>`` and an agent name is capped at
    the persisted-field limit, so the longest LEGITIMATE key exceeds that limit.
    Capping the key there instead would refuse this admission, and a refusal
    defers the fire -- so the job would stop firing silently and forever.
    """
    gateway = _gateway()
    agent = "a" * _CRON_SESSION_BINDING_STRING_LIMIT
    key = f"cron:{'0' * 8}:{agent}"
    assert len(key) > _CRON_SESSION_BINDING_STRING_LIMIT

    admitted = gateway._retain_cron_session_bindings({key: ("/cwd", agent, None, "project")})

    assert admitted
    assert key in gateway._cron_session_binding
    assert gateway._cron_session_binding_overflow_count == 0


@pytest.mark.asyncio
async def test_a_stateless_jobs_fresh_per_fire_keys_do_not_accumulate() -> None:
    """A non-persistent job mints a NEW key every fire; only the live one is kept.

    ``build_cron_session_context`` returns ``cron:<job id>:<uuid4>`` when
    ``persistent_session`` is false, so this population would otherwise grow once
    per fire forever -- unbounded in TIME rather than in job count, and so not
    covered by the count ceiling or by deletion eviction. The prune is keyed on the
    job id prefix rather than the exact key, which is what retires the prior fire.
    """
    gateway = _gateway()
    keys = [f"cron:job:{run:08x}" for run in range(3)]

    for key in keys:
        await gateway._prune_cron_session_bindings("job", {key})
        assert gateway._retain_cron_session_bindings({key: _binding()})

    assert set(gateway._cron_session_binding) == {keys[-1]}


@pytest.mark.asyncio
async def test_sequence_orphans_are_pruned_without_discarding_pending_bindings() -> None:
    gateway = _gateway()
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.has_pending_work_for = MagicMock(
        side_effect=AssertionError("sync pending probe called")
    )
    gateway.subagent_mgr.has_pending_work_for_async = AsyncMock(
        side_effect=lambda key: key.endswith(":pending")
    )
    gateway._cron_session_binding = {
        "cron:job:current": _binding(),
        "cron:job:orphan": _binding(),
        "cron:job:pending": _binding(),
        "cron:other:orphan": _binding(),
    }

    result = gateway._prune_cron_session_bindings("job", {"cron:job:current"})
    if inspect.isawaitable(result):
        await result

    gateway.subagent_mgr.has_pending_work_for.assert_not_called()
    assert gateway.subagent_mgr.has_pending_work_for_async.await_count == 2
    assert set(gateway._cron_session_binding) == {
        "cron:job:current",
        "cron:job:pending",
        "cron:other:orphan",
    }


def test_both_cron_dispatch_paths_share_the_bounded_retention_helper() -> None:
    source = inspect.getsource(GatewayOrchestrator._init_cron)

    assert source.count("self._retain_cron_session_bindings(") == 2
    assert "self._cron_session_binding[" not in source
    # Both paths must also PRUNE. Retention alone bounds the population by job
    # count; it is the prefix-keyed prune that retires a stateless job's previous
    # fresh-per-fire key, which nothing else reaches (the job still exists, so
    # deletion eviction never runs, and one entry per fire stays under the count
    # ceiling for a long time while growing without end).
    assert source.count("self._prune_cron_session_bindings(") == 2


def test_a_prior_fires_key_is_stale_while_this_run_is_claimed(tmp_path) -> None:
    """The refusing fire runs INSIDE the callback the dispatcher awaits, so the
    claim it took is still stored. A claim-existence test is therefore always
    true at the call sites and answers None for every one of them, leaving the
    prior fire's key registered forever -- and
    ``release_jobs_for_dead_owners`` then keeps treating that dead owner as
    live. What makes a key stale is that THIS run did not mint it.
    """
    service = CronService(base_dir=tmp_path)
    job = service.add_job("stateless", "run", every_secs=300)
    prior_key = f"cron:{job.id}:deadbeef"
    service.register_active_session_key(job.id, prior_key)

    # The run now executing: claimed, and it has minted no key of its own.
    executing = service._claim_run(job.id, "scheduled")
    assert service._claims.get(job.id) is executing

    assert service.stale_session_key(job.id) == prior_key


def test_this_runs_own_key_is_not_reported_stale(tmp_path) -> None:
    """The guard must not hand a run its OWN live key: clearing that would
    unregister a session this run is still using.
    """
    service = CronService(base_dir=tmp_path)
    job = service.add_job("stateless", "run", every_secs=300)
    # Registration attributes the key to whichever run holds the job's claim,
    # so registering here makes this key the executing run's own.
    service._claim_run(job.id, "scheduled")
    service.register_active_session_key(job.id, f"cron:{job.id}:mine")

    assert service.stale_session_key(job.id) is None


def test_failed_removal_save_keeps_active_session_key_and_suppresses_observer(
    tmp_path, monkeypatch
) -> None:
    removed: list[set[str]] = []
    service = CronService(base_dir=tmp_path, on_jobs_removed=removed.append)
    job = service.add_job("removed", "run", every_secs=300)
    active_key = f"cron:{job.id}:deadbeef"
    service.register_active_session_key(job.id, active_key)

    def fail_save() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        service.remove_job(job.id, actor="test", source="test")

    assert (
        service.stale_session_key(job.id) == active_key
    ), "save failure discarded the only exact handle to the live cron session"
    assert removed == [], "observer was notified about a deletion that did not persist"
