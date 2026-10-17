"""The freestyle agent-task spawn path backs off when the HOST declines.

The ``route == "execute"`` block in ``_do_poll`` spawns freestyle tasks
serially. Two things stop the ~400 attempts/min re-dispatch storm:

1. The spawn is SERIAL — ``_spawn_agent_task_serial`` awaits each spawn to
   completion (or the spawn timeout) before the loop advances, so a task whose
   spawn is in flight (running, or queued behind the admission gate) cannot be
   re-selected mid-flight. A queued spawn the memory wait later expires ends via
   the timeout path, which fail-counts the task — it is never silently dropped.
2. A real host DECLINE — the admission memory floor refusing a wait it had no
   durable row to park, which the gate marks with ``error_code`` ==
   ``task_store_unavailable`` and ``build_spawn_impl`` surfaces as a
   ``SpawnError`` with ``declined=True`` — arms a cooldown during which no
   freestyle spawn is attempted, since a declined task is never marked done and
   would otherwise re-attempt on every one-second poll.

The backoff arms ONLY on a flagged decline. A ``SpawnError`` that is NOT a
decline (an empty task/agent, a bad cwd, a governance refusal) is a per-task
problem — it does NOT arm the cooldown and does NOT stop the other due tasks
that poll. An accepted spawn clears the cooldown, so a healthy host whose
spawns succeed is never throttled, and the hourly ``activity_budget`` meter
(recorded only after a successful spawn, see ``hooks.py``) is left untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi.queue_poller import (
    FREESTYLE_DECLINE_BACKOFF_MS,
    SPAWN_TIMEOUT_MS,
    QueuePoller,
)
from kiro_crew.apps.spawn_sdk import SpawnError


def _execute_queue(n_tasks: int) -> dict:
    """A queue of ``n_tasks`` due freestyle tasks that routes to 'execute'."""
    return {
        # planned_until far in the future keeps route_poll on 'execute' (no
        # plan/replan branch stealing the poll).
        "planned_until": "2999-01-01T00:00:00.000Z",
        "tasks": [
            {
                "id": f"fs-{i}",
                "type": "freestyle",
                "execute_after": "1970-01-01T00:00:00.000Z",  # long overdue
                "done": False,
                "action": {"prompt": f"do task {i}"},
            }
            for i in range(n_tasks)
        ],
    }


class _DecliningCallbacks:
    """spawn_agent always raises a FLAGGED decline — the host out of memory.

    ``declined=True`` is the host-pressure signal ``build_spawn_impl`` sets when
    the admission memory floor refuses a wait it cannot park; it is the only
    SpawnError the backoff reacts to.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        raise SpawnError("host declined the spawn (low memory)", declined=True)


class _FailingNonDeclineCallbacks:
    """spawn_agent raises a NON-decline SpawnError every call.

    A fault that no cooldown fixes (an empty agent, a normalised impl error):
    ``declined`` is False, so the backoff must NOT arm and the other due tasks
    must still be attempted.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        raise SpawnError("spawn failed: boom")  # declined defaults to False


class _HealthyThenDone:
    """spawn_agent succeeds; the queue marks the task done so it is not re-due.

    Models a well-behaved host: every spawn lands, so the backoff must never
    engage no matter how many polls run.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        return f"spawn-{self.attempts}"


def _silence_writeback(monkeypatch) -> None:
    import contextlib

    monkeypatch.setattr(qf, "write_queue_atomic", lambda p, d: None)
    monkeypatch.setattr(qf, "queue_mutation", lambda p: contextlib.nullcontext())


@pytest.mark.asyncio
async def test_declined_freestyle_spawns_back_off_instead_of_storming(tmp_path, monkeypatch):
    """Repro: many polls, every spawn declined. On main every poll re-attempts
    (hundreds of attempts); the fix caps attempts to one per backoff window."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    # Far more due freestyle tasks than any budget, re-read each poll (the
    # storm's shape: a declined task is never marked done).
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    # 30 one-second polls inside one backoff window.
    for _ in range(30):
        now[0] += 1_000
        await poller.poll()

    # On main this is 30+ (one per poll, and more per poll before any gate).
    # With the decline backoff only the FIRST poll attempts; the rest are
    # deferred until the cooldown elapses.
    assert cb.attempts == 1, (
        "a declined freestyle spawn must arm a backoff, not re-attempt every "
        f"poll; got {cb.attempts} attempts in one window"
    )


@pytest.mark.asyncio
async def test_backoff_releases_after_the_window(tmp_path, monkeypatch):
    """The backoff is a deferral, not a permanent stop: once the cooldown
    elapses, exactly one more attempt is made (and re-arms the backoff)."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    await poller.poll()  # first attempt arms the backoff
    assert cb.attempts == 1

    for _ in range(5):  # still inside the window: no new attempt
        now[0] += 1_000
        await poller.poll()
    assert cb.attempts == 1

    now[0] += FREESTYLE_DECLINE_BACKOFF_MS + 1  # window elapses
    await poller.poll()
    assert cb.attempts == 2, "one more attempt is allowed once the backoff elapses"


@pytest.mark.asyncio
async def test_healthy_host_is_never_throttled(tmp_path, monkeypatch):
    """A host whose spawns all SUCCEED is never backed off, however many run —
    the fix throttles declines only, not healthy throughput."""
    import asyncio
    import contextlib

    _silence_writeback(monkeypatch)
    cb = _HealthyThenDone()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()

    # One due freestyle task per poll, each spawn succeeding. The poller's
    # serial wait is resolved by notify_agent_done so each poll completes.
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(1))

    async def _resolve_when_waiting(task: asyncio.Future) -> None:
        # Spin until the poll has created its serial future, then resolve it.
        while not task.done() and poller._spawn_future is None:
            await asyncio.sleep(0)
        poller.notify_agent_done()

    for _ in range(20):
        now[0] += 1_000
        poll_task = asyncio.ensure_future(poller.poll())
        try:
            # Bound both waits: a broken handshake fails here, loudly and
            # locally, instead of hanging until the suite timeout kills the
            # worker (tests-are-deterministic).
            await asyncio.wait_for(_resolve_when_waiting(poll_task), timeout=5)
            await asyncio.wait_for(poll_task, timeout=5)
        except BaseException:
            poll_task.cancel()
            with contextlib.suppress(BaseException):
                await poll_task
            raise

    assert cb.attempts == 20, (
        "every successful freestyle spawn must be allowed; a healthy host is "
        f"never throttled, got {cb.attempts}/20"
    )
    assert poller._freestyle_backoff_until == 0, "a success must leave no backoff armed"


@pytest.mark.asyncio
async def test_non_decline_spawn_error_does_not_arm_the_cooldown(tmp_path, monkeypatch):
    """A SpawnError that is NOT a host decline (declined=False) is a per-task
    fault, not host pressure: it must leave the backoff disarmed so the next
    poll is free to try again immediately."""
    _silence_writeback(monkeypatch)
    cb = _FailingNonDeclineCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(1))

    await poller.poll()

    assert (
        poller._freestyle_backoff_until == 0
    ), "a non-decline SpawnError must NOT arm the decline cooldown"


@pytest.mark.asyncio
async def test_decline_arms_the_cooldown(tmp_path, monkeypatch):
    """The counterpart: a FLAGGED decline (declined=True) DOES arm the
    cooldown, so the next poll inside the window is skipped."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(1))

    await poller.poll()

    assert poller._freestyle_backoff_until > now[0], "a flagged host decline must arm the cooldown"


@pytest.mark.asyncio
async def test_non_decline_failure_still_runs_the_other_due_tasks(tmp_path, monkeypatch):
    """A non-decline fault on one task must not stop the remaining due tasks in
    the same poll — the fix only pauses the rest on a real host decline."""
    _silence_writeback(monkeypatch)
    cb = _FailingNonDeclineCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    # Three due freestyle tasks in one poll; every spawn faults (non-decline).
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(3))

    await poller.poll()

    assert cb.attempts == 3, (
        "a non-decline failure must not break out of the loop; every due task "
        f"this poll is still attempted, got {cb.attempts}/3"
    )
    assert poller._freestyle_backoff_until == 0, "no cooldown armed by non-decline faults"


@pytest.mark.asyncio
async def test_queued_spawn_that_expires_is_fail_counted_not_silently_dropped(
    tmp_path, monkeypatch
):
    """A spawn ACCEPTED-but-queued (an id returned, agent never starts) that the
    memory wait later expires must end through the timeout path, which
    fail-counts the task — it is NOT marked done at dispatch and silently lost.

    This pins the fix for the silent-loss defect: the earlier revision marked
    the trigger task done the instant an id came back, so an expired queued
    spawn left a done task with no failure recorded and cleanup deleted it. Now
    the task is only ever marked by the timeout handler, so the fail_count and
    the sticky notify after MAX_FAIL_COUNT still run.
    """
    import contextlib

    # A real in-memory queue so the fail_count stamp persists across the read.
    state = {"queue": _execute_queue(1)}
    monkeypatch.setattr(qf, "read_queue", lambda p: state["queue"])
    monkeypatch.setattr(qf, "write_queue_atomic", lambda p, d: state.update(queue=d))
    monkeypatch.setattr(qf, "queue_mutation", lambda p: contextlib.nullcontext())

    cb = _HealthyThenDone()  # the spawn is accepted (returns an id)
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()

    # The queued spawn never fires notify_agent_done; drive the tick past the
    # spawn timeout so _await_spawn ends by timeout -> _locked_mark_timeout.
    async def _wait_until_awaited(task: asyncio.Future) -> None:
        # Readiness wait: yield until the poll has created its serial future.
        # Bounded by wait_for below so a broken dispatch fails here loudly and
        # locally instead of spinning or hanging to the suite timeout.
        while not task.done() and poller._spawn_future is None:
            await asyncio.sleep(0)

    poll_task = asyncio.ensure_future(poller.poll())
    try:
        await asyncio.wait_for(_wait_until_awaited(poll_task), timeout=5)
        assert poller._spawn_future is not None, "the spawn should be awaited serially, not dropped"
        # Expire the serial wait by firing the timeout tick.
        now[0] += SPAWN_TIMEOUT_MS + 1_000
        poller.tick(now[0])
        await asyncio.wait_for(poll_task, timeout=5)
    except BaseException:
        poll_task.cancel()
        with contextlib.suppress(BaseException):
            await poll_task
        raise

    task0 = state["queue"]["tasks"][0]
    assert task0.get("fail_count") == 1, (
        "an expired queued spawn must be fail-counted by the timeout path, "
        f"not marked done at dispatch; got fail_count={task0.get('fail_count')}, "
        f"done={task0.get('done')}"
    )
    assert cb.attempts == 1, "the queued spawn was awaited serially, dispatched once"


# --- SpawnSDK.run + build_spawn_impl: the typed decline flag the backoff gates on
#
# The backoff arms only on ``SpawnError.declined``. That flag is set in two
# places: ``SpawnSDK.run`` when the injected impl returns an empty id (the host
# returned nothing), and ``build_spawn_impl`` when the admission gate refuses a
# spawn with a memory wait reason it had no row to park. An accepted-but-queued
# spawn is NOT a decline — it comes back as an id. These tests pin that mapping
# directly, without a real SubagentManager.


def _spawn_sdk_with_impl(impl):
    """A SpawnSDK whose host-call seam is a direct async ``impl``."""
    from kiro_crew.apps.spawn_sdk import SpawnSDK

    return SpawnSDK("mochi", impl)


class _FakeInfo:
    """A minimal SubagentManager return record: id, error, error_code.

    ``error_code`` mirrors ``SubagentInfo.error_code`` — the field the admission
    gate stamps on a refusal (``task_store_unavailable`` for the memory-floor
    store refusal). The gate never sets ``queued_reason`` on a refusal, only on
    an accepted queued record, so the decline signal reads off ``error_code``.
    """

    def __init__(self, *, id="", error="", error_code=""):
        self.id = id
        self.error = error
        self.error_code = error_code


class _FakeAgent:
    """A list_agents() entry whose filename proves app ownership (``mochi--``)."""

    def __init__(self, name, filename):
        self.name = name
        self.filename = filename


class _FakeManager:
    """A SubagentManager double whose ``spawn`` returns a preset record."""

    def __init__(self, info):
        self._info = info
        self.spawned = False

    def spawn(self, task, **kwargs):
        self.spawned = True
        return self._info


def _patch_ownership(monkeypatch):
    """Let ``build_spawn_impl`` pass its ``mochi--mochi-bg`` ownership scan."""
    from kiro_crew.apps import spawn_sdk

    monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [_FakeAgent("mochi-bg", "mochi--mochi-bg.json")],
    )
    monkeypatch.setattr(spawn_sdk, "is_internal_agent_spec", lambda a: False)


async def _run_impl(info, monkeypatch):
    """Drive the REAL ``build_spawn_impl`` with a manager returning ``info``."""
    from kiro_crew.apps import spawn_sdk

    _patch_ownership(monkeypatch)
    impl = spawn_sdk.build_spawn_impl(_FakeManager(info))
    return await impl("do work", "mochi-bg", True, "", "mochi")


@pytest.mark.asyncio
async def test_build_spawn_impl_memory_refusal_is_a_flagged_decline(monkeypatch):
    """A done record carrying a memory-floor ``error`` + ``error_code`` ==
    ``task_store_unavailable`` raises SpawnError(declined=True) — the real host
    decline this covers. That is the exact record the gate produces when the
    memory floor refuses a wait it has no durable row to park."""
    from kiro_crew.subagent_manager.admission.types import TASK_STORE_UNAVAILABLE_CODE

    info = _FakeInfo(
        id="spawn-ref-1",
        error="spawn refused: the task store could not record this start's "
        "memory wait (low memory: 0.1 GB available, need 2.0 GB)",
        error_code=TASK_STORE_UNAVAILABLE_CODE,
    )
    with pytest.raises(SpawnError) as ei:
        await _run_impl(info, monkeypatch)
    assert ei.value.declined is True, "a store-refused memory wait is a host-pressure decline"


@pytest.mark.asyncio
async def test_build_spawn_impl_non_memory_error_is_not_a_decline(monkeypatch):
    """A done record whose ``error`` is NOT the memory-floor store refusal
    (governance, bad cwd, agent-not-available) raises SpawnError(declined=False)
    — no backoff, since no cooldown fixes a per-task fault."""
    info = _FakeInfo(error="spawn refused by governance: denied", error_code="agent_not_available")
    with pytest.raises(SpawnError) as ei:
        await _run_impl(info, monkeypatch)
    assert ei.value.declined is False, "a non-memory refusal is a fault, not host pressure"


@pytest.mark.asyncio
async def test_build_spawn_impl_queued_returns_the_id_not_a_decline(monkeypatch):
    """An accepted-but-queued record (an id, no error) is returned as that id —
    it is in flight, never a decline. The poller then awaits it serially."""
    info = _FakeInfo(id="spawn-queued-1", error="")
    assert await _run_impl(info, monkeypatch) == "spawn-queued-1"


@pytest.mark.asyncio
async def test_build_spawn_impl_none_record_is_an_empty_id_decline(monkeypatch):
    """A None record (manager declined outright) → empty id → the SDK raises a
    flagged decline. Pins the empty-id host signal through build_spawn_impl."""
    assert await _run_impl(None, monkeypatch) == ""


@pytest.mark.asyncio
async def test_spawn_sdk_empty_id_raises_a_flagged_decline():
    """An empty spawn id (the host returned nothing) raises
    SpawnError(declined=True) — a real host signal kept as a decline."""
    from kiro_crew.apps.spawn_sdk import SpawnError

    async def _declines(task, agent, silent, model, app):
        return ""  # host returned nothing

    sdk = _spawn_sdk_with_impl(_declines)
    with pytest.raises(SpawnError) as ei:
        await sdk.run("do work", agent="mochi-bg")
    assert ei.value.declined is True, "an empty id is a host-pressure decline"


@pytest.mark.asyncio
async def test_spawn_sdk_impl_fault_is_not_a_decline():
    """A normalised impl exception raises SpawnError with declined=False."""
    from kiro_crew.apps.spawn_sdk import SpawnError

    async def _faults(task, agent, silent, model, app):
        raise RuntimeError("boom")

    sdk = _spawn_sdk_with_impl(_faults)
    with pytest.raises(SpawnError) as ei:
        await sdk.run("do work", agent="mochi-bg")
    assert ei.value.declined is False, "an impl fault is not host pressure — no backoff"


@pytest.mark.asyncio
async def test_spawn_sdk_empty_agent_is_not_a_decline():
    """An empty agent is refused loudly as a non-decline SpawnError."""
    from kiro_crew.apps.spawn_sdk import SpawnError

    async def _never_called(task, agent, silent, model, app):  # pragma: no cover
        raise AssertionError("impl must not be reached for an empty agent")

    sdk = _spawn_sdk_with_impl(_never_called)
    with pytest.raises(SpawnError) as ei:
        await sdk.run("do work", agent="")
    assert ei.value.declined is False, "a refused empty agent is a fault, not a decline"


@pytest.mark.asyncio
async def test_spawn_sdk_success_returns_the_id():
    """A non-empty id is returned unchanged (control: no decline, no raise)."""

    async def _ok(task, agent, silent, model, app):
        return "spawn-123"

    sdk = _spawn_sdk_with_impl(_ok)
    assert await sdk.run("do work", agent="mochi-bg") == "spawn-123"
