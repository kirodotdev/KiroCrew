"""Cancel must reach a manual run from the instant Run accepted it.

``POST /api/crons/{id}/run`` stores the task it creates in ``_running_tasks``
and returns; ``CronService.run_job`` then sits in its first ``await`` -- the
offloaded ``_synced_snapshot``, up to a full store-lock spin. ``cancel()``'s
guard reads only ``_executing``, so a claim that reaches ``_executing`` only
after that await opens a window in which ``POST /api/crons/{id}/cancel``
answers 409 "job is not running" while a second Run answers 409 "job is
already running" about the same job, and the run then executes anyway. The
claim is therefore taken synchronously while ``run_job(job_id)`` is evaluated,
before the route's ``create_task`` has scheduled anything.

The window is only open while the snapshot is held, which no product route
holds on demand, so this harness is the bar: a gate parks ``run_job``'s
refresh in its worker thread and the requests land inside the window.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew import cron as cron_module
from kiro_crew.cron import CronJob, CronService, _RunMarkers
from kiro_crew.cron_inflight import clear_marker as _clear_marker
from kiro_crew.cron_inflight import read_markers
from kiro_crew.dashboard.handlers.cron import api_cron_cancel, api_cron_run
from kiro_crew.resource_status import POSTURE_AMPLE, AdmissionDecision


class _SelRecorder:
    """Stands in for cron.py's module-level ``sel``: records every audit call."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.events.append(kw)

    def log_tool_invocation(self, **kw: Any) -> None:
        self.events.append(kw)


#: Every wait in this module -- a gate reporting that it parked, a run reporting
#: that it started, a task settling -- is bounded by this. It is a failure
#: budget, not a pacing device: nothing here sleeps or polls, so a slow runner
#: only ever moves a wait's end, never its outcome.
_HANDOFF_TIMEOUT = 30.0


async def _settled(task: "asyncio.Future[Any]") -> None:
    """Wait for *task* to end by any outcome, within the handoff timeout."""
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), _HANDOFF_TIMEOUT)


async def _parked(event: threading.Event, message: str) -> None:
    """Wait (off the loop) for a worker-thread gate to report that it parked."""
    assert await asyncio.to_thread(event.wait, _HANDOFF_TIMEOUT), message


class _TrackedTasks(dict):  # type: ignore[type-arg]
    """``_running_tasks`` that reports the pop ``cancel()`` makes of a live wrapper.

    ``cancel()`` pops the tracked task, cancels it and discards ``_executing``
    in one synchronous step and then yields to its persist; the wrapper's
    cancellation is delivered at the next loop step. The event is set inside
    the pop, BEFORE that ``task.cancel()``, so the waiter's wake-up is
    registered ahead of the wrapper's and ``call_soon`` callbacks run in
    registration order: a test awaiting :attr:`live_popped` resumes in the step
    after ``cancel()``'s release, with the wrapper's teardown still queued
    behind it -- no polling, no sleep.
    """

    def __init__(self) -> None:
        super().__init__()
        self.live_popped = asyncio.Event()

    def pop(self, key: str, *default: Any) -> Any:
        task = super().pop(key, *default)
        if task is not None and not task.done():
            self.live_popped.set()
        return task


def _make_state(svc: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = svc
    state.push_refresh = MagicMock()
    return state


def _make_app(state: MagicMock) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/run", api_cron_run)
    app.router.add_post("/api/crons/{job_id}/cancel", api_cron_cancel)
    return app


async def _service(tmp_path: Path, on_job) -> tuple[CronService, CronJob]:
    svc = CronService(base_dir=tmp_path, on_job=on_job)
    svc._sessions = None
    job = await svc.add_job_async("etl job", "do something", every_secs=3600)
    return svc, job


def _persisted(tmp_path: Path, job_id: str) -> CronJob:
    """The job as a fresh process loads it from the store -- disk, not the cache.

    WORKER-THREAD ONLY (the plain constructor loads inline).
    """
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None, f"job {job_id} is not in the store"
    return job


class _SnapshotGate:
    """Let the route's lookup through; park ``run_job``'s refresh until released.

    Runs in the ``asyncio.to_thread`` worker. The route resolves the job through
    ``get_job_async`` first (call 1); ``run_job``'s own refresh is call 2 and is
    held until :attr:`release` is set, which keeps the claim window open for as
    long as the test needs to land requests inside it. ``park_call`` names a
    later refresh instead when the parked run is not the first one.
    """

    def __init__(self, svc: CronService, park_call: int = 2) -> None:
        self._svc = svc
        self._park_call = park_call
        self.calls = 0
        self.parked = threading.Event()
        self.release = threading.Event()

    def __call__(self, include_disabled: bool = True) -> list[CronJob]:
        self.calls += 1
        if self.calls == self._park_call:
            self.parked.set()
            assert self.release.wait(
                timeout=_HANDOFF_TIMEOUT
            ), "the snapshot gate was never released"
        return list(self._svc._jobs)


class _FirstCallGate:
    """Park the first call of a worker-thread function until released; later calls pass.

    Wraps a function ``_run_job_isolated``'s ``finally`` offloads through
    ``asyncio.to_thread`` -- ``cron_inflight.clear_marker`` before its marker
    pops, ``_merge_job_result`` after them -- so that run's finalizer stays
    pending exactly there for as long as the test holds :attr:`release`. The
    same seam parks ``cancel()`` in its offloaded ``_merge_terminal_state_locked``.
    """

    def __init__(self, delegate: Callable[..., Any]) -> None:
        self._delegate = delegate
        self.calls = 0
        self.parked = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            self.parked.set()
            assert self.release.wait(
                timeout=_HANDOFF_TIMEOUT
            ), "the finalizer gate was never released"
        return self._delegate(*args, **kwargs)


class _EachCallGate:
    """Park every call of a worker-thread function until that call is released.

    Same seam as :class:`_FirstCallGate`, for a test that needs two runs'
    finalizers pending in ``cron_inflight.clear_marker`` at once and released in
    an order of its choosing: call ``n`` (1-based) waits on ``release(n)`` and
    reports itself through ``parked(n)``.
    """

    def __init__(self, delegate: Callable[..., Any]) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()
        self._parked: dict[int, threading.Event] = {}
        self._release: dict[int, threading.Event] = {}
        self._open = False
        self.calls = 0

    def parked(self, call: int) -> threading.Event:
        with self._lock:
            return self._parked.setdefault(call, threading.Event())

    def release(self, call: int) -> threading.Event:
        with self._lock:
            return self._release.setdefault(call, threading.Event())

    def release_all(self) -> None:
        """Release every parked call and let any later call straight through."""
        with self._lock:
            self._open = True
            events = list(self._release.values())
        for event in events:
            event.set()

    def __call__(self, *args: Any) -> Any:
        with self._lock:
            self.calls += 1
            call = self.calls
            parked = not self._open
        if parked:
            self.parked(call).set()
            assert self.release(call).wait(
                timeout=_HANDOFF_TIMEOUT
            ), f"finalizer call {call} was never released"
        return self._delegate(*args)


async def _run_inside_the_gap(svc: CronService, app: web.Application, job_id: str) -> web.Response:
    """Drive the real Run route without yielding to the loop.

    Used where a test has just been woken ahead of a queued teardown (see
    :class:`_TrackedTasks`) and must claim the job before that teardown gets
    its turn: the route's fresh-store lookup is an executor hop, and any yield
    here would let the queued teardown run first. The lookup is made await-free;
    everything after it (the guard, the claim, the tracked task) is the route's
    own code.
    """

    async def await_free_lookup(job_id: str) -> CronJob | None:
        return next((j for j in svc._jobs if j.id == job_id), None)

    with patch.object(svc, "get_job_async", await_free_lookup):
        return await api_cron_run(
            make_mocked_request(
                "POST", f"/api/crons/{job_id}/run", match_info={"job_id": job_id}, app=app
            )
        )


class TestCancelInsideTheClaimWindow:
    @pytest.mark.asyncio
    async def test_cancel_reaches_a_manual_run_parked_in_its_store_refresh(
        self, tmp_path: Path
    ) -> None:
        """Inside the window Cancel finds the run; a late claim answers 409 "job is not running"."""
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            return "ran"

        svc, job = await _service(tmp_path, on_job)
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        wrapper: asyncio.Task[bool] | None = None
        with patch.object(svc, "_synced_snapshot", gate):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    wrapper = svc._running_tasks[job.id]
                    await _parked(gate.parked, "run_job never reached its store refresh")

                    # Inside the window Run says the job is already running ...
                    again = await client.post(f"/api/crons/{job.id}/run")
                    assert again.status == 409
                    assert (await again.json())["error"] == "job is already running"

                    # ... so Cancel must be able to reach that same run.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel.json()
                    assert cancel.status == 200, (
                        f"Cancel inside the claim window answered {cancel.status} {body!r} "
                        "while Run answered 409 'job is already running' about the same job"
                    )
                    assert body["ok"] is True
            finally:
                gate.release.set()
                if wrapper is not None:
                    await _settled(wrapper)

        # The pending run was cancelled, not executed, and left no marker behind.
        assert wrapper is not None and wrapper.cancelled()
        assert executed == []
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert not svc._cancelled_jobs._marks
        assert job.last_status == "error"
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["trigger"] == "manual"

    @pytest.mark.asyncio
    async def test_a_refresh_returning_during_cancel_teardown_does_not_dispatch(
        self, tmp_path: Path
    ) -> None:
        """cancel() pops the claim first and cancels the task only after its teardown awaits.

        The snapshot is released from INSIDE that gap -- the process-kill call
        cancel() awaits in the executor -- and held there until the wrapper has
        resumed. A wrapper that re-checks its claim returns without dispatching;
        one that trusts the snapshot starts the run cancel() is reporting cancelled.
        """
        dispatched: list[str] = []
        resumed = threading.Event()

        async def fake_run(job: CronJob) -> None:
            dispatched.append(job.id)
            resumed.set()
            await asyncio.Event().wait()  # runs until cancelled

        def fake_kill(job_id: str) -> bool:
            # Executor thread, inside cancel()'s first await: let the snapshot
            # return now and hold cancel() here until the wrapper has resumed.
            gate.release.set()
            assert resumed.wait(
                timeout=_HANDOFF_TIMEOUT
            ), "the wrapper never resumed past its refresh"
            return False

        svc, job = await _service(tmp_path, None)
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        with (
            patch.object(svc, "_synced_snapshot", gate),
            patch.object(svc, "_run_job_isolated", side_effect=fake_run),
            patch("kiro_crew.cron_script.kill_running_process", fake_kill),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                run = await client.post(f"/api/crons/{job.id}/run")
                assert run.status == 200
                wrapper = svc._running_tasks[job.id]
                wrapper.add_done_callback(lambda _t: resumed.set())
                await _parked(gate.parked, "run_job never reached its store refresh")

                cancel = await client.post(f"/api/crons/{job.id}/cancel")
                assert cancel.status == 200
                assert (await cancel.json())["ok"] is True
            await _settled(wrapper)

        assert (
            dispatched == []
        ), f"the run was dispatched {dispatched!r} while cancel() was tearing it down"
        assert wrapper.done() and not wrapper.cancelled() and wrapper.result() is False
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert not svc._cancelled_jobs._marks
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_a_cancelled_wrappers_late_teardown_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted while cancel() tears down the previous one keeps its claim.

        cancel() pops ``_running_tasks``, cancels the parked wrapper and discards
        ``_executing`` in one synchronous step, then yields to persist. The
        cancelled wrapper's teardown -- the ``except`` around its refresh -- runs
        at the wrapper's next scheduling, so a Run landing between the two passes
        the route's guard and claims the job first. The teardown must see that
        the stored claim is not its own and leave it alone; releasing it anyway
        erases the replacement run's ``_executing`` entry and meta, so that run is
        either dropped by its own claim re-check after the route answered
        "started", or executes with Cancel answering 409 "job is not running".

        The handoff is the pop itself: ``_TrackedTasks`` sets an event inside
        cancel()'s pop, ahead of its ``task.cancel()``, so this coroutine is
        registered to resume before the wrapper is (callbacks run in
        registration order). The replacement is then driven through the real
        route without yielding -- its fresh-store lookup is made await-free
        (``_run_inside_the_gap``) -- so it claims the job before the wrapper's
        teardown gets its turn.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        tracked_tasks = _TrackedTasks()
        svc._running_tasks = tracked_tasks
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        app = _make_app(state)
        with patch.object(svc, "_synced_snapshot", gate):
            try:
                async with TestClient(TestServer(app)) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    first = svc._running_tasks[job.id]
                    await _parked(gate.parked, "run_job never reached its store refresh")

                    cancelling = asyncio.ensure_future(client.post(f"/api/crons/{job.id}/cancel"))
                    await asyncio.wait_for(tracked_tasks.live_popped.wait(), _HANDOFF_TIMEOUT)
                    assert job.id not in svc._executing and job.id not in svc._running_tasks
                    assert (
                        not first.done()
                    ), "the cancelled wrapper tore down before cancel() yielded"

                    # Inside the gap: the replacement Run is accepted and claims the job.
                    replacement = await _run_inside_the_gap(svc, app, job.id)
                    assert replacement.status == 200
                    second = svc._running_tasks[job.id]
                    assert second is not first
                    claim = svc._job_run_meta[job.id]
                    assert claim[1] == "manual"

                    # Now the first wrapper's teardown runs, with a claim that is not its own.
                    await _settled(first)
                    assert first.cancelled()
                    assert svc.is_running(job.id) and svc._job_run_meta.get(job.id) is claim, (
                        "the cancelled wrapper's teardown erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}"
                    )
                    cancel = await cancelling
                    assert cancel.status == 200
                    assert (await cancel.json())["ok"] is True

                    # The replacement run executes, and Cancel reaches it.
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    assert executed == [job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel_again.json()
                    assert (
                        cancel_again.status == 200
                    ), f"Cancel answered {cancel_again.status} {body!r} about the replacement run"
                    assert body["ok"] is True
                    await _settled(second)
            finally:
                gate.release.set()

        assert second.done() and not second.cancelled() and second.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert not svc._cancelled_jobs._marks
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_finalizer_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted while a cancelled run is still finalizing keeps its claim.

        cancel() releases the run it found -- meta, start stamps, ``_executing``,
        ``_running_tasks`` -- and that run's own ``finally`` then spends a full
        executor round trip (``cron_inflight.clear_marker``) before its marker
        pops, so a Run accepted through the real route during that trip claims
        the job first. The prior run's finalizer must leave that claim alone:
        popping it anyway drops the replacement's ``_executing`` entry, run meta
        and tracked task, so its claim re-check returns without dispatching after
        the route answered "started" -- or, past the re-check, it runs with Cancel
        answering 409 and a further Run accepted beside it.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        # Calls 1-3 pass (the first Run's lookup and refresh, the replacement's
        # lookup); call 4, the replacement's refresh, is parked so its claim
        # re-check runs only after the prior run's finalizer has had its turn.
        snapshot = _SnapshotGate(svc, park_call=4)
        clearing = _FirstCallGate(_clear_marker)
        state = _make_state(svc)
        with (
            patch.object(svc, "_synced_snapshot", snapshot),
            patch("kiro_crew.cron_inflight.clear_marker", clearing),
        ):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    prior = svc._running_tasks[job.id]
                    started.clear()

                    # Cancel releases the run and cancels it; its finalizer parks
                    # in the marker clear with its pops still ahead of it.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    await _parked(
                        clearing.parked, "the cancelled run never reached its marker clear"
                    )
                    assert not prior.done()
                    assert job.id not in svc._executing
                    assert job.id not in svc._running_tasks

                    # The replacement is accepted through the real route and claims the job.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await _parked(
                        snapshot.parked, "the replacement run never reached its store refresh"
                    )
                    claim = svc._job_run_meta[job.id]
                    assert claim[1] == "manual"
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior

                    # Now the prior run's finalizer pops, holding a claim that is not its own.
                    clearing.release.set()
                    await _settled(prior)
                    assert prior.cancelled()
                    assert (
                        svc.is_running(job.id)
                        and svc._job_run_meta.get(job.id) is claim
                        and svc._running_tasks.get(job.id) is tracked
                    ), (
                        "the prior run's finalizer erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}, "
                        f"tracked={sorted(svc._running_tasks)!r}"
                    )

                    # The replacement dispatches, and Cancel reaches it.
                    snapshot.release.set()
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    assert executed == [job.id, job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel_again.json()
                    assert (
                        cancel_again.status == 200
                    ), f"Cancel answered {cancel_again.status} {body!r} about the replacement run"
                    assert body["ok"] is True
                    await _settled(tracked)
            finally:
                clearing.release.set()
                snapshot.release.set()

        assert tracked.done() and not tracked.cancelled() and tracked.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic
        assert not svc._cancelled_jobs._marks
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_finalizer_leaves_a_replacement_runs_crash_marker_alone(
        self, tmp_path: Path
    ) -> None:
        """A replacement run's in-flight marker survives the prior run's finalizer.

        ``cron_inflight`` markers are the breaker's only evidence of which job
        was executing when the gateway hard-exits. cancel() releases the run it
        found, and that run's ``finally`` reaches its marker clear only after its
        cancellation has unwound -- for an agent job through the callback's own
        session teardown -- and then spends an executor round trip in the clear
        itself. A Run accepted through the real route in that window dispatches
        and writes ITS marker before the prior clear runs. A clear keyed by job id
        alone unlinks that file: a hard exit during the replacement then leaves no
        marker, the breaker names no job, and the crashing job runs again on
        restart. Keyed by run, the prior run's clear removes only its own file.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        clearing = _FirstCallGate(_clear_marker)
        state = _make_state(svc)
        with patch("kiro_crew.cron_inflight.clear_marker", clearing):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    prior = svc._running_tasks[job.id]
                    started.clear()
                    (prior_marker,) = read_markers(tmp_path)
                    assert prior_marker.job_id == job.id

                    # Cancel releases the run; its finalizer parks in the marker clear.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    await _parked(
                        clearing.parked, "the cancelled run never reached its marker clear"
                    )
                    assert not prior.done()

                    # The replacement is accepted, dispatches, and writes its own marker.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    assert executed == [job.id, job.id]
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior
                    # Two files now: the token in each name is what tells them
                    # apart (their start stamps can be equal on a coarse clock).
                    markers = read_markers(tmp_path)
                    assert len(markers) == 2, [m.path.name for m in markers]
                    (replacement_marker,) = [m for m in markers if m.path != prior_marker.path]
                    assert replacement_marker.job_id == job.id

                    # Now the prior run's clear runs, with the replacement's marker on disk.
                    clearing.release.set()
                    await _settled(prior)
                    assert prior.cancelled()
                    remaining = read_markers(tmp_path)
                    assert [m.path for m in remaining] == [replacement_marker.path], (
                        "the prior run's finalizer erased the replacement run's crash "
                        f"marker: markers={[m.path.name for m in remaining]!r}; a hard exit "
                        "during the replacement would leave the breaker no evidence"
                    )

                    # The replacement's own finalizer clears its own marker.
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel_again.status == 200
                    await _settled(tracked)
            finally:
                clearing.release.set()

        assert read_markers(tmp_path) == []
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert not svc._cancelled_jobs._marks
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_finalizer_consumes_only_its_own_cancel_marker(
        self, tmp_path: Path
    ) -> None:
        """Two cancellations in flight at once each finalize as cancelled.

        cancel() marks the run it releases, and that run's ``finally`` consumes
        the marker only after a full executor round trip (``clear_marker``).
        While the first run's finalizer is still in that trip, a replacement Run
        is accepted through the real route, dispatches, and is cancelled too;
        then the first finalizer resumes. A marker keyed by job id alone is ONE
        marker for both cancellations: the first finalizer consumes it, the
        replacement's finalizer finds none and treats its run as completed --
        merging the job and appending a ``failure`` row (the cancel message as
        its summary) after the ``cancelled`` row cancel() already wrote for it.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        clearing = _EachCallGate(_clear_marker)
        state = _make_state(svc)
        with patch("kiro_crew.cron_inflight.clear_marker", clearing):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    prior = svc._running_tasks[job.id]
                    prior_claim = svc._job_run_meta[job.id]
                    started.clear()

                    # Cancel the first run: its finalizer parks in the marker
                    # clear with its marker read still ahead of it.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    await _parked(
                        clearing.parked(1), "the first run never reached its marker clear"
                    )
                    assert not prior.done()
                    assert svc._cancelled_jobs.has(job.id, prior_claim)

                    # The replacement is accepted, dispatches, and is cancelled too.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    assert executed == [job.id, job.id]
                    later = svc._running_tasks[job.id]
                    assert later is not prior
                    later_claim = svc._job_run_meta[job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel_again.status == 200
                    assert (await cancel_again.json())["ok"] is True
                    await _parked(
                        clearing.parked(2), "the replacement run never reached its marker clear"
                    )
                    assert not later.done()
                    runs, total = await svc._history.get_job_history(job.id)
                    assert total == 2
                    assert [r["status"] for r in runs] == ["cancelled", "cancelled"]

                    # The first finalizer resumes and consumes ITS cancellation;
                    # the replacement's marker has to survive it.
                    clearing.release(1).set()
                    await _settled(prior)
                    assert prior.cancelled()
                    assert svc._cancelled_jobs.has(job.id, later_claim), (
                        "the prior run's finalizer consumed the replacement run's cancel "
                        "marker, so the replacement will finalize as if it had completed"
                    )
                    assert not svc._cancelled_jobs.has(
                        job.id, prior_claim
                    ), "the prior run's finalizer consumed a marker that was not its own"

                    # Then the replacement's finalizer: cancelled, not completed.
                    clearing.release(2).set()
                    await _settled(later)
                    assert later.cancelled()
            finally:
                clearing.release_all()

        assert not svc._cancelled_jobs._marks
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        runs, total = await svc._history.get_job_history(job.id)
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"], (
            "the replacement run appended a row after its cancelled row: "
            f"history={[(r['status'], r['summary']) for r in runs]!r}"
        )
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_the_manual_wrappers_backstop_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted between a run's own release and its wrapper's resume keeps its claim.

        ``_run_job_isolated`` releases the run's claim in its finally and then
        awaits its result merge and history append; the manual wrapper awaiting
        that task resumes only once it is done. A Run accepted in between claims
        the job, and the wrapper's backstop -- there for a finally cut short --
        must not discard that run's ``_executing`` entry or its tracked task.
        The gap is held open by parking the first run's merge in its worker.
        """
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            return "ran"

        svc, job = await _service(tmp_path, on_job)
        # Calls 1-3 are the first Run's lookup and refresh and the replacement's
        # lookup; call 4, the replacement's refresh, is parked so the first
        # wrapper's backstop runs while the replacement holds an undispatched claim.
        gate = _SnapshotGate(svc, park_call=4)
        merging = _FirstCallGate(svc._merge_job_result)
        state = _make_state(svc)
        with (
            patch.object(svc, "_synced_snapshot", gate),
            patch.object(svc, "_merge_job_result", merging),
        ):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    first = svc._running_tasks[job.id]
                    # The first run has released its claim and parks in its merge;
                    # its wrapper resumes only once that merge and the history
                    # append are done.
                    await _parked(merging.parked, "the first run never reached its result merge")
                    assert job.id not in svc._executing and job.id not in svc._running_tasks
                    assert not first.done(), "the wrapper resumed before its run's finally ended"

                    # Inside the gap: the replacement Run is accepted and claims the job.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    second = svc._running_tasks[job.id]
                    claim = svc._job_run_meta[job.id]
                    await _parked(
                        gate.parked, "the replacement run never reached its store refresh"
                    )

                    # Now the first wrapper's backstop runs, holding a claim that is not its own.
                    merging.release.set()
                    await _settled(first)
                    assert first.result() is True
                    assert (
                        svc.is_running(job.id)
                        and svc._job_run_meta.get(job.id) is claim
                        and svc._running_tasks.get(job.id) is second
                    ), (
                        "the first wrapper's backstop erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}, "
                        f"tracked={sorted(svc._running_tasks)!r}"
                    )
                    gate.release.set()
                    await _settled(second)
            finally:
                merging.release.set()
                gate.release.set()

        assert executed == [job.id, job.id]
        assert second.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["success", "success"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_a_replacement_runs_start_does_not_rewrite_the_prior_runs_terminal_record(
        self, tmp_path: Path
    ) -> None:
        """A run's history row and persisted status are its own, not the next run's.

        Every run of a job shares one ``CronJob`` object. A completed run releases
        its claim in its ``finally`` and only then merges its result (an executor
        round trip under the store lock) and appends its history row; a Run
        accepted through the real route in that window dispatches and its
        ``_execute`` resets ``last_status`` and the result-produced flag on the
        shared object. A finalizer that reads the object after the release files
        the completed run as a failure with no summary and persists the
        replacement's blank status as the completed run's. The record is taken
        before the release, so the replacement's start changes nothing about it.
        """
        started = asyncio.Event()
        calls = 0

        async def on_job(job: CronJob) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                job.set_run_result("first run: 3 rows loaded")
                return None  # the first run completes at once
            started.set()
            await asyncio.Event().wait()  # the replacement runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        merging = _FirstCallGate(svc._merge_job_result)
        state = _make_state(svc)
        with patch.object(svc, "_merge_job_result", merging):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    prior = svc._running_tasks[job.id]
                    # The first run completes, releases its claim, and parks in
                    # its merge with its history append still ahead of it.
                    await _parked(merging.parked, "the first run never reached its result merge")
                    assert not prior.done()
                    assert job.id not in svc._executing

                    # The replacement is accepted and starts executing on the
                    # same job object: its _execute has reset the status fields.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    assert calls == 2
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior

                    # Now the first run's merge and history append run.
                    merging.release.set()
                    await _settled(prior)
                    runs, total = await svc._history.get_job_history(job.id)
                    persisted = svc.get_job(job.id)
                    assert persisted is not None
                    assert (
                        total == 1
                        and runs[0]["status"] == "success"
                        and runs[0]["summary"] == "first run: 3 rows loaded"
                        and persisted.last_status == "ok"
                    ), (
                        "the replacement run's start rewrote the prior run's terminal "
                        f"record: history={[(r['status'], r['summary']) for r in runs]!r}, "
                        f"persisted last_status={persisted.last_status!r}"
                    )

                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    await _settled(tracked)
            finally:
                merging.release.set()

        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert sorted(r["status"] for r in runs) == ["cancelled", "success"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_merge_does_not_restore_its_record_over_a_replacement_runs(
        self, tmp_path: Path
    ) -> None:
        """A terminal merge that lands after a later run's leaves the store the later run's.

        The merge in ``_run_job_isolated``'s ``finally`` is an executor round trip
        behind the release, under a store lock other writers contend for, so a
        Run accepted in that window can complete and merge first. The older
        run's record then has to be discarded: applied, its status, error,
        result and failure counter land over the replacement's -- a succeeded
        run persisted as the failure before it, its result gone and the
        auto-pause budget spent -- until some later run merges again.
        """
        calls = 0

        async def on_job(job: CronJob) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                # The command path's failure shape: the run returns normally
                # and reports on the shared job.
                job.last_status = "error"
                job.last_error = "first run: upstream returned 503"
                job.record_failure()
                return None
            job.set_run_result("second run: 3 rows loaded")
            return None

        svc, job = await _service(tmp_path, on_job)
        merging = _FirstCallGate(svc._merge_job_result)
        state = _make_state(svc)
        with patch.object(svc, "_merge_job_result", merging):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    prior = svc._running_tasks[job.id]
                    # The first run fails, releases its claim, and parks in its
                    # merge -- the store lock is still ahead of it.
                    await _parked(merging.parked, "the first run never reached its result merge")
                    assert not prior.done()
                    assert job.id not in svc._executing

                    # The replacement is accepted, succeeds, and its merge
                    # lands while the first run's is still pending.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior
                    await _settled(tracked)
                    assert calls == 2 and merging.calls == 2
                    settled = await asyncio.to_thread(_persisted, tmp_path, job.id)
                    assert settled.last_status == "ok"
                    assert settled.last_result == "second run: 3 rows loaded"

                    # Now the first run's merge lands, after the replacement's.
                    merging.release.set()
                    await _settled(prior)
            finally:
                merging.release.set()

        persisted = await asyncio.to_thread(_persisted, tmp_path, job.id)
        assert (
            persisted.last_status == "ok"
            and persisted.last_error is None
            and persisted.last_result == "second run: 3 rows loaded"
            and persisted.consecutive_failures == 0
        ), (
            "the prior run's late merge restored its stale record over the replacement "
            f"run's: last_status={persisted.last_status!r}, last_error={persisted.last_error!r}, "
            f"last_result={persisted.last_result!r}, "
            f"consecutive_failures={persisted.consecutive_failures}"
        )
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert sorted(r["status"] for r in runs) == ["failure", "success"]

    @pytest.mark.asyncio
    async def test_a_cancels_late_terminal_merge_does_not_restore_its_record_over_a_replacement_runs(
        self, tmp_path: Path
    ) -> None:
        """``cancel()``'s terminal merge sits behind its release too, and is fenced the same way.

        ``cancel()`` pops the tracked task and the ``_executing`` entry, then
        persists its "Cancelled by user" record through an offloaded
        ``_merge_terminal_state_locked``. A Run accepted between the two can
        complete and merge first; the cancel's record then has to be
        discarded, or the replacement's success is persisted as the
        cancellation before it. The reaper's terminal merge is the same helper.
        """
        started = asyncio.Event()
        calls = 0

        async def on_job(job: CronJob) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()  # the first run runs until cancelled
                return None
            job.set_run_result("second run: 3 rows loaded")
            return None

        svc, job = await _service(tmp_path, on_job)
        merging = _FirstCallGate(svc._merge_terminal_state_locked)
        state = _make_state(svc)
        with patch.object(svc, "_merge_terminal_state_locked", merging):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    prior = svc._running_tasks[job.id]
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)

                    # Cancel releases the run, then parks in its terminal merge.
                    cancelling = asyncio.ensure_future(client.post(f"/api/crons/{job.id}/cancel"))
                    await _parked(merging.parked, "cancel() never reached its terminal merge")
                    assert job.id not in svc._executing
                    assert not cancelling.done()

                    # The replacement is accepted, succeeds, and its merge
                    # lands while the cancel's is still pending.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior
                    await _settled(tracked)
                    assert calls == 2
                    settled = await asyncio.to_thread(_persisted, tmp_path, job.id)
                    assert settled.last_status == "ok"
                    assert settled.last_result == "second run: 3 rows loaded"

                    # Now the cancel's terminal merge lands, after the replacement's.
                    merging.release.set()
                    cancel = await cancelling
                    assert cancel.status == 200
                    await _settled(prior)
            finally:
                merging.release.set()

        persisted = await asyncio.to_thread(_persisted, tmp_path, job.id)
        assert (
            persisted.last_status == "ok"
            and persisted.last_error is None
            and persisted.last_result == "second run: 3 rows loaded"
        ), (
            "cancel()'s late terminal merge restored its record over the replacement run's: "
            f"last_status={persisted.last_status!r}, last_error={persisted.last_error!r}, "
            f"last_result={persisted.last_result!r}"
        )
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert sorted(r["status"] for r in runs) == ["cancelled", "success"]

    @pytest.mark.asyncio
    async def test_a_completed_one_shots_stale_merge_still_consumes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed one-shot's merge that lost the race to a replacement's still retires it.

        The generation fence covers the record's FIELDS; the ``delete_after_run``
        consume is owed by the completion whatever landed since. A replacement
        run's cancel writes status fields and retires nothing, so a stale merge
        that skipped the consume would leave the fired at-job enabled on disk --
        past due, so due again on every tick.
        """
        recorder = _SelRecorder()
        monkeypatch.setattr(cron_module, "sel", SimpleNamespace(sel=lambda: recorder))
        started = asyncio.Event()
        calls = 0

        async def on_job(job: CronJob) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                job.set_run_result("reminder sent")
                return None  # the one-shot completes at once
            started.set()
            await asyncio.Event().wait()  # the replacement runs until cancelled
            return None

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        svc._sessions = None
        job = await svc.add_job_async(
            "reminder", "ping", at_ts=time.time() - 5, delete_after_run=True
        )
        merging = _FirstCallGate(svc._merge_job_result)
        bumped: list[set[str]] = []
        real_bump = svc._bump_grant_epochs_for

        def spy_bump(removed_ids: set[str]) -> None:
            bumped.append(set(removed_ids))
            real_bump(removed_ids)

        state = _make_state(svc)
        with (
            patch.object(svc, "_merge_job_result", merging),
            patch.object(svc, "_bump_grant_epochs_for", spy_bump),
        ):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    prior = svc._running_tasks[job.id]
                    # The one-shot completes and parks in its merge: the delete
                    # that retires it is still ahead.
                    await _parked(merging.parked, "the one-shot never reached its result merge")
                    assert job.id not in svc._executing

                    # A replacement Run is accepted (the job is still enabled --
                    # the consume is what retires it), then cancelled: its
                    # terminal record lands first, with a newer generation.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
                    tracked = svc._running_tasks[job.id]
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    await _settled(tracked)
                    assert (await asyncio.to_thread(_persisted, tmp_path, job.id)).last_error == (
                        "Cancelled by user after 0s"
                    )

                    # Now the completed one-shot's stale merge lands.
                    merging.release.set()
                    await _settled(prior)
            finally:
                merging.release.set()

        stored = await asyncio.to_thread(lambda: CronService(base_dir=tmp_path).get_job(job.id))
        removes = [e for e in recorder.events if e.get("operation") == "cron.remove"]
        assert stored is None and len(removes) == 1 and bumped == [{job.id}], (
            "the stale merge of the completed one-shot skipped its consume: "
            f"stored={'enabled=%s' % stored.enabled if stored else None}, "
            f"audited={bool(removes)}, principal retired={bool(bumped)}"
        )
        assert "path=cron_run_complete" in removes[0]["resources"]
        assert f"job_id={job.id}" in removes[0]["resources"]
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert sorted(r["status"] for r in runs) == ["cancelled", "success"]

    @pytest.mark.asyncio
    async def test_cancel_after_the_claim_still_cancels_the_running_job(
        self, tmp_path: Path
    ) -> None:
        """The happy path -- Run, then Cancel once the run is executing -- is unchanged."""
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        state = _make_state(svc)
        async with TestClient(TestServer(_make_app(state))) as client:
            run = await client.post(f"/api/crons/{job.id}/run")
            assert run.status == 200
            wrapper = svc._running_tasks[job.id]
            await asyncio.wait_for(started.wait(), _HANDOFF_TIMEOUT)
            assert svc.is_running(job.id)

            cancel = await client.post(f"/api/crons/{job.id}/cancel")
            assert cancel.status == 200
            assert (await cancel.json())["ok"] is True
            await _settled(wrapper)

        assert executed == [job.id]
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert not svc._cancelled_jobs._marks
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["trigger"] == "manual"

    @pytest.mark.asyncio
    async def test_a_cancel_landing_before_a_scheduled_runs_first_step_records_one_row(
        self, tmp_path: Path
    ) -> None:
        """Cancel between the due-scan's ``create_task`` and that task's first step.

        The due-scan claims the job -- ``_executing``, the run meta, the tracked
        task -- synchronously and returns with the task still unstarted; a Cancel
        whose handler was queued behind the timer step runs next. ``cancel()``
        finds the claim, pops the meta, marks the cancellation for THAT tuple,
        and then awaits the process kill before it reaches ``task.cancel()``:
        that await is the loop iteration in which the unstarted task takes its
        first step. A task that reads its meta from the store there reads None,
        runs on until the cancellation lands, and its finally asks the markers
        about None -- the marker, keyed to the popped tuple, misses, and the run
        is filed as a failure beside the cancelled row ``cancel()`` writes. The
        dispatcher hands the claim to the task instead; a first step that finds
        the claim gone does nothing: no stamps, no marker file, no second row.
        """
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc = CronService(base_dir=tmp_path, on_job=on_job)
        svc._sessions = None
        job = await svc.add_job_async("etl job", "do something", every_secs=60)
        job.last_run_ts = time.time() - 120  # due on the next scan
        state = _make_state(svc)

        first_step: list[float] = []
        cancel_found_a_started_run: list[bool] = []
        real_run = svc._run_job_isolated
        real_cancel = svc.cancel

        async def stepped_run(job: CronJob, *args: Any) -> None:
            first_step.append(time.monotonic())  # this line IS the task's first step
            await real_run(job, *args)

        async def observed_cancel(job_id: str) -> bool:
            cancel_found_a_started_run.append(bool(first_step))
            return await real_cancel(job_id)

        admitted = AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=16.0)
        with (
            patch("kiro_crew.cron.admission_check", return_value=admitted),
            patch.object(svc, "_run_job_isolated", stepped_run),
            patch.object(svc, "cancel", observed_cancel),
        ):
            await svc._on_timer()
            # The scan has claimed the job and created its task; the task has
            # not run, and this coroutine has not yielded since the scan returned.
            assert job.id in svc._executing
            task = svc._running_tasks[job.id]
            claim = svc._job_run_meta[job.id]
            assert claim[1] == "scheduled"
            assert not first_step
            # The Cancel route is await-free up to cancel(), which pops the claim
            # and marks it before its first await -- the process-kill executor
            # hop -- so the task's first step runs inside cancel(), after the pop.
            cancel = await api_cron_cancel(
                make_mocked_request(
                    "POST",
                    f"/api/crons/{job.id}/cancel",
                    match_info={"job_id": job.id},
                    app=_make_app(state),
                )
            )
            assert cancel.status == 200
            assert cancel_found_a_started_run == [
                False
            ], "cancel() did not run before the task's first step"
            assert first_step, "the task never took its first step"
            await _settled(task)

        runs, total = await svc._history.get_job_history(job.id)
        assert [(r["status"], r["trigger"]) for r in runs] == [("cancelled", "scheduled")], (
            "a Cancel that landed before the scheduled run's first step left more than "
            f"its own row: history={[(r['status'], r['summary']) for r in runs]!r}"
        )
        assert executed == [], "the cancelled-before-start run executed"
        assert task.done()
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert not svc._cancelled_jobs._marks, "the run's cancel marker was never consumed"
        # Nothing the release fence would have had to leave behind: the task
        # stamped nothing, so no start stamp or jitter outlives the release. A
        # task handed the claim but not re-checking it stamps all three, and
        # the finally's fence (a claim that is not its own) then keeps them.
        orphaned = {
            name: stamps[job.id]
            for name, stamps in (
                ("_job_start_times", svc._job_start_times),
                ("_job_start_monotonic", svc._job_start_monotonic),
                ("_job_jitter", svc._job_jitter),
            )
            if job.id in stamps
        }
        assert not orphaned, (
            "the cancelled-before-start run stamped state its own finally can no "
            f"longer release: {orphaned!r}"
        )
        assert read_markers(tmp_path) == []
        assert (job.last_error or "").startswith("Cancelled by user")


class TestRunJobClaim:
    @pytest.mark.asyncio
    async def test_claim_is_taken_when_run_job_is_called(self, tmp_path: Path) -> None:
        """``_executing`` and the manual run meta exist before the first await."""
        svc, job = await _service(tmp_path, None)

        async def fake_run(job: CronJob, meta: tuple[float, str] | None = None) -> None:
            assert meta is svc._job_run_meta[job.id]  # the wrapper hands the task its claim

        with patch.object(svc, "_run_job_isolated", side_effect=fake_run):
            before = time.time()
            pending = svc.run_job(job.id)
            assert svc.is_running(job.id)
            started_at, trigger = svc._job_run_meta[job.id]
            assert trigger == "manual"
            assert before <= started_at <= time.time()
            assert await pending is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks

    @pytest.mark.asyncio
    async def test_claim_is_released_when_the_store_has_no_such_job(self, tmp_path: Path) -> None:
        svc, _job = await _service(tmp_path, None)

        pending = svc.run_job("ghost")
        assert svc.is_running("ghost")
        assert await pending is False
        assert "ghost" not in svc._executing
        assert "ghost" not in svc._job_run_meta

    @pytest.mark.asyncio
    async def test_a_job_already_executing_is_refused_and_its_claim_untouched(
        self, tmp_path: Path
    ) -> None:
        svc, job = await _service(tmp_path, None)
        meta = (time.time() - 5, "scheduled")
        svc._executing.add(job.id)
        svc._job_run_meta[job.id] = meta

        assert await svc.run_job(job.id) is False
        assert svc.is_running(job.id)
        assert svc._job_run_meta[job.id] is meta


class TestRunMarkers:
    def test_markers_are_keyed_by_run_identity_not_equality(self) -> None:
        """Two equal meta tuples are two runs: each marker is consumed by its own run only."""
        markers = _RunMarkers()
        first = (1.0, "manual")
        second = (first[0], first[1])  # built at runtime: equal, not the folded constant
        assert first == second and first is not second

        markers.mark("job", first)
        markers.mark("job", first)  # marking the same run twice is one marker
        assert markers.has("job", first)
        assert markers.has("job", first) and not markers.has("job", second)
        assert not markers.has("job", None)

        markers.mark("job", second)
        assert markers.consume("job", first) is True
        assert markers.has("job", second), "consuming one run's marker removed the other's"
        assert markers.consume("job", first) is False
        assert markers.consume("job", second) is True
        assert not markers._marks
        assert markers.consume("job", second) is False
        assert markers.consume("other", None) is False


class TestTerminalMergeGeneration:
    """Both terminal merges are ordered by the run generation the store persists."""

    def test_an_older_completed_records_fields_are_not_applied(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("etl job", "do something", every_secs=3600)
        newer = copy.copy(job)
        newer.run_generation = 2
        newer.last_status = "ok"
        newer.last_result = "second run: 3 rows loaded"
        newer.last_result_ts = 200.5
        svc._merge_job_result(newer)

        older = copy.copy(job)
        older.run_generation = 1
        older.last_status = "error"
        older.last_error = "first run: upstream returned 503"
        older.consecutive_failures = 1
        older.last_result = None
        svc._merge_job_result(older)

        stored = _persisted(tmp_path, job.id)
        assert (
            stored.run_generation,
            stored.last_status,
            stored.last_error,
            stored.last_result,
            stored.consecutive_failures,
        ) == (2, "ok", None, "second run: 3 rows loaded", 0), (
            "an older run's record was applied over the newer run's: "
            f"generation={stored.run_generation}, last_status={stored.last_status!r}"
        )

    def test_a_record_of_the_same_or_a_newer_generation_is_applied(self, tmp_path: Path) -> None:
        """No self-discard: a single run always merges, and so does a re-merge of the same run."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("etl job", "do something", every_secs=3600)
        assert _persisted(tmp_path, job.id).run_generation == 0  # never ran

        first = copy.copy(job)
        first.run_generation = 1
        first.last_status = "error"
        svc._merge_job_result(first)
        assert _persisted(tmp_path, job.id).last_status == "error"

        same = copy.copy(job)
        same.run_generation = 1
        same.last_status = "ok"
        svc._merge_job_result(same)
        assert _persisted(tmp_path, job.id).last_status == "ok"

        svc._merge_terminal_state_locked(
            job.id,
            last_status="error",
            last_error="Cancelled by user after 3s",
            last_run_ts=300.0,
            run_generation=3,
        )
        stored = _persisted(tmp_path, job.id)
        assert (stored.run_generation, stored.last_error) == (3, "Cancelled by user after 3s")

    def test_an_older_terminal_record_is_not_applied(self, tmp_path: Path) -> None:
        """The cancel/reap helper honours the same fence; an unknown id stays a no-op."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("etl job", "do something", every_secs=3600)
        newer = copy.copy(job)
        newer.run_generation = 2
        newer.last_status = "ok"
        newer.last_run_ts = 200.5
        svc._merge_job_result(newer)

        svc._merge_terminal_state_locked(
            job.id,
            last_status="error",
            last_error="Reaped after 1800s (exceeded 1800s deadline)",
            last_run_ts=1999.0,
            run_generation=1,
        )
        stored = _persisted(tmp_path, job.id)
        assert (stored.run_generation, stored.last_status, stored.last_error) == (2, "ok", None), (
            "an older run's terminal record was applied over the newer run's: "
            f"generation={stored.run_generation}, last_status={stored.last_status!r}"
        )
        assert stored.last_run_ts == 200.5

        svc._merge_terminal_state_locked(
            "ghost", last_status="error", last_error="x", last_run_ts=1.0, run_generation=1
        )  # no such job: a no-op, not an error
        assert [j.id for j in CronService(base_dir=tmp_path)._jobs] == [job.id]

    def test_a_stale_record_of_a_completed_one_shot_still_consumes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the field copies are fenced; the one-shot consume is owed by the completion."""
        recorder = _SelRecorder()
        monkeypatch.setattr(cron_module, "sel", SimpleNamespace(sel=lambda: recorder))
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job("reminder", "ping", at_ts=time.time() - 5, delete_after_run=True)
        # A replacement's terminal record landed first (a cancel: status fields
        # only, the one-shot itself untouched).
        svc._merge_terminal_state_locked(
            job.id,
            last_status="error",
            last_error="Cancelled by user after 0s",
            last_run_ts=time.time(),
            run_generation=3,
        )

        completed = copy.copy(job)
        completed.run_generation = 1
        completed.last_status = "ok"
        svc._merge_job_result(completed)

        stored = CronService(base_dir=tmp_path).get_job(job.id)
        removes = [e for e in recorder.events if e.get("operation") == "cron.remove"]
        assert stored is None and len(removes) == 1, (
            "the stale record of the completed one-shot skipped its consume: "
            f"stored={'enabled=%s' % stored.enabled if stored else None}, audited={bool(removes)}"
        )
        assert "path=cron_run_complete" in removes[0]["resources"]

    @pytest.mark.asyncio
    async def test_two_runs_claimed_in_one_clock_tick_still_merge_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows' ``time.time()`` ticks every ~15.6 ms, so two runs can claim with EQUAL
        timestamps; the generation is a counter, not the clock, so the older record is
        still the one that yields."""
        calls = 0

        async def on_job(job: CronJob) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                job.last_status = "error"
                job.last_error = "first run: upstream returned 503"
                job.record_failure()
                return None
            job.set_run_result("second run: 3 rows loaded")
            return None

        svc, job = await _service(tmp_path, on_job)
        records: list[CronJob] = []
        frozen = time.time()
        monkeypatch.setattr(time, "time", lambda: frozen)
        with patch.object(svc, "_merge_job_result", side_effect=records.append):
            assert await svc.run_job(job.id) is True
            assert await svc.run_job(job.id) is True
        monkeypatch.undo()
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert runs[0]["started_at"] == runs[1]["started_at"] == frozen, "premise: one clock tick"
        first, second = records

        # The later run's record lands first, then the earlier run's.
        await asyncio.to_thread(svc._merge_job_result, second)
        await asyncio.to_thread(svc._merge_job_result, first)
        persisted = await asyncio.to_thread(_persisted, tmp_path, job.id)
        assert (
            persisted.last_status,
            persisted.last_error,
            persisted.last_result,
            persisted.consecutive_failures,
        ) == ("ok", None, "second run: 3 rows loaded", 0), (
            "two runs claimed in one clock tick were not ordered: the earlier run's record "
            f"landed over the later run's: last_status={persisted.last_status!r}, "
            f"last_error={persisted.last_error!r}, last_result={persisted.last_result!r}, "
            f"consecutive_failures={persisted.consecutive_failures}"
        )
        assert first.run_generation < second.run_generation == persisted.run_generation
