"""Model provenance is persisted ONCE, at the crash-safe pre-spawn point.

``SubagentInfo.requested_model`` / ``resolved_model`` are written to disk
before the ``subagent_spawn`` event fires, so a gateway restart in the window
between the event and any later state write cannot lose them — orphan recovery
rebuilds the record from disk (GPT review on #3582). The later ``session_id``
state write in ``_run`` used to re-write the same two fields; that second write
was pure redundant I/O on the spawn hot path and was dropped (#5394). These
tests pin both halves: exactly one provenance write, ordered before the spawn
event, and a session_id write that no longer carries the provenance fields.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# ``SubagentManager.spawn`` refuses while the host looks short of memory, which
# is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _mock_sessions(served_model: str) -> MagicMock:
    """A mock SessionManager whose provider serves *served_model* and streams
    nothing (zero turns) — enough to drive ``_run_inner`` end to end."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    # These provider accessors are synchronous in production.  Leaving them
    # as auto-created AsyncMock children returns un-awaited coroutines from the
    # context-budget and usage probes, making this otherwise deterministic
    # test file emit RuntimeWarnings under xdist.
    provider.context_used_tokens = lambda: 0
    provider.context_window_tokens = lambda: 0
    provider.client = None
    # Public accessor read by _resolved_model_of at spawn time. Plain string
    # attribute: an auto-created AsyncMock child would stringify to a mock repr
    # and masquerade as a served model id.
    provider.served_model = served_model

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


@pytest.mark.asyncio
async def test_provenance_written_once_before_the_spawn_event() -> None:
    """One write carries requested_model/resolved_model, and it lands BEFORE
    the ``subagent_spawn`` event — the crash-safe ordering orphan recovery
    depends on. The later session_id write must NOT re-write those fields."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    # Per-spawn pin: becomes the requested side of the downgrade comparison.
    info = SubagentInfo(id="prov01", task="provenance task", model="model-req")
    manager._agents[info.id] = info

    # Ordered trace of every update_state call and every fired event, so the
    # pre-spawn ordering is asserted on one timeline. update_state runs via
    # asyncio.to_thread for the provenance write, but that thread is awaited
    # before the event fires, so the trace order is deterministic.
    trace: list[tuple[str, dict[str, Any]]] = []

    def _spy_update(agent_id: str, **kwargs: Any) -> bool:
        trace.append(("update_state", dict(kwargs)))
        return True

    orig_fire = manager._fire_event

    async def _spy_fire(kind: str, *args: Any, **kwargs: Any) -> None:
        trace.append(("event", {"kind": kind}))
        await orig_fire(kind, *args, **kwargs)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_spy_update),
        patch.object(manager, "_fire_event", _spy_fire),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    writes = [kw for tag, kw in trace if tag == "update_state"]
    prov_writes = [kw for kw in writes if "requested_model" in kw or "resolved_model" in kw]
    # Exactly one provenance write on this path (the empty stream never reaches
    # the CC first-chunk refinement, which only fills a still-empty value).
    assert len(prov_writes) == 1, f"expected one provenance write, got {prov_writes}"
    assert prov_writes[0]["requested_model"] == "model-req"
    assert prov_writes[0]["resolved_model"] == "model-served"

    # The session_id bookkeeping write no longer re-writes provenance (#5394).
    sid_writes = [kw for kw in writes if "session_id" in kw]
    assert sid_writes, "expected the session_id state write to still happen"
    for kw in sid_writes:
        assert (
            "requested_model" not in kw and "resolved_model" not in kw
        ), f"session_id write re-persists provenance: {kw}"

    # Crash-safe ordering: the provenance write precedes the spawn event.
    prov_idx = next(
        i for i, (tag, kw) in enumerate(trace) if tag == "update_state" and "requested_model" in kw
    )
    spawn_idx = next(
        i
        for i, (tag, kw) in enumerate(trace)
        if tag == "event" and kw.get("kind") == "subagent_spawn"
    )
    assert prov_idx < spawn_idx, "provenance must persist before subagent_spawn"


@pytest.mark.asyncio
async def test_provenance_write_retries_once_on_transient_failure() -> None:
    """The pre-spawn write is the SINGLE owner of the provenance fields, so a
    transient failure gets its second chance from that write's own bounded
    retry -- not from a second writer downstream (the dropped session_id
    re-write). The retry must still land before the spawn event, and a
    persistence failure must never block the spawn."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="prov02", task="provenance retry task", model="model-req")
    manager._agents[info.id] = info

    trace: list[tuple[str, dict[str, Any]]] = []
    provenance_attempts = {"n": 0}

    def _flaky_update(agent_id: str, **kwargs: Any) -> bool:
        if "requested_model" in kwargs:
            provenance_attempts["n"] += 1
            if provenance_attempts["n"] == 1:
                raise OSError("transient fs hiccup")
        trace.append(("update_state", dict(kwargs)))
        return True

    orig_fire = manager._fire_event

    async def _spy_fire(kind: str, *args: Any, **kwargs: Any) -> None:
        trace.append(("event", {"kind": kind}))
        await orig_fire(kind, *args, **kwargs)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_flaky_update),
        patch.object(manager, "_fire_event", _spy_fire),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    # The failure was retried exactly once and the retry landed the write.
    assert provenance_attempts["n"] == 2
    landed = [
        (i, kw)
        for i, (tag, kw) in enumerate(trace)
        if tag == "update_state" and "requested_model" in kw
    ]
    assert len(landed) == 1, f"expected the retry to land one write, got {landed}"
    assert landed[0][1]["requested_model"] == "model-req"
    spawn_idx = next(
        i
        for i, (tag, kw) in enumerate(trace)
        if tag == "event" and kw.get("kind") == "subagent_spawn"
    )
    assert landed[0][0] < spawn_idx, "retried write must still precede subagent_spawn"
    # The spawn itself completed despite the transient failure.
    assert info.error == ""


@pytest.mark.asyncio
async def test_provenance_write_retries_on_silently_skipped_merge() -> None:
    """``update_state`` SKIPS the merge (returns False) when the current state
    cannot be read, without raising. The retry loop must treat that reported
    skip as a failure -- only a reported successful write ends the loop
    (GPT review round 2 on #5824: a silent no-op must not pass for success)."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="prov03", task="provenance skip task", model="model-req")
    manager._agents[info.id] = info

    provenance_attempts = {"n": 0}
    landed: list[dict[str, Any]] = []

    def _skippy_update(agent_id: str, **kwargs: Any) -> bool:
        if "requested_model" in kwargs:
            provenance_attempts["n"] += 1
            if provenance_attempts["n"] == 1:
                return False  # the silent skip: no exception, nothing written
            landed.append(dict(kwargs))
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_skippy_update),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    assert provenance_attempts["n"] == 2, "a reported skip must trigger the retry"
    assert len(landed) == 1 and landed[0]["requested_model"] == "model-req"
    assert info.error == ""


def _mock_sessions_with_tool_event(served_model: str, event: Any) -> MagicMock:
    """Like ``_mock_sessions`` but the stream yields one event before ending —
    enough to drive the per-turn EVENT_PERMISSION_REQUEST branch in
    ``_run_inner`` (the diagnostics ``update_state`` write at issue in #6288)."""
    sessions = _mock_sessions(served_model=served_model)
    provider, _, _ = sessions.get_or_create.return_value

    async def _one_event_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield event

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _one_event_stream())
    return sessions


async def _event_loop_checkpoint() -> None:
    """Yield until callbacks already queued on this loop have run.

    This is a deterministic scheduling barrier, not a clock-based sleep.  The
    cancellation tests use it after ``Task.cancel()`` so the cancellation arm
    can reach its next await before assertions inspect the task and latch.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    reached = loop.create_future()
    loop.call_soon(reached.set_result, None)
    await reached


@pytest.mark.asyncio
async def test_per_turn_diagnostics_write_is_drained_on_cancellation() -> None:
    """Cancelling ``await asyncio.to_thread(...)`` detaches the worker thread,
    and ``update_state`` is an unlocked read-merge-replace — so a stale
    detached worker could overwrite newer state written by a cancel-respawn
    recovery run (recovery waits for the old asyncio TASK, not the worker).
    The fix drains the worker before letting cancellation complete: the
    cancelled task must NOT finish while the diagnostics write is in flight,
    which orders any recovery write strictly after the worker's write and
    closes the race (GPT + Opus review round 1 on #6306; same worker-drain
    posture as autonudge's persistence path, #425)."""
    import asyncio

    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="grep",
        tool_kind="read",
        request_id="req-1",
    )
    sessions = _mock_sessions_with_tool_event("model-served", event)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="turnw04", task="per-turn cancel task", model="model-req")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await release.wait()
        landed.append(dict(kwargs))
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_gated_to_thread),
    ):
        task = asyncio.ensure_future(manager._run_inner(info, f"subagent:{info.id}"))
        try:
            await entered.wait()

            task.cancel()
            # The drain must hold the cancelled task open while the worker is
            # still writing — a task that completes here is the detached-worker
            # race (pre-fix behaviour).
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _run_inner completed while the diagnostics write was "
                "in flight — the detached worker can now overwrite newer "
                "recovery state (#6306 review)"
            )
            # While draining, the latch _run's recovery gate reads must be up
            # (3.10 wait_for double-cancel can deliver that gate mid-drain).
            assert info._diag_drain_active is True, (
                "drain did not raise the _diag_drain_active latch — on 3.10 a "
                "second outer cancel can schedule recovery mid-drain (#6306 "
                "review round 4)"
            )
            # A SECOND cancel while draining (reachable: wait_for deadline
            # cancels the run, then shutdown's cancel_all delivers another)
            # must not detach the worker either — this is what distinguishes
            # the drain loop from a single re-await (#6306 review round 2).
            task.cancel()
            await _event_loop_checkpoint()
            assert not task.done(), (
                "a second cancel during the drain detached the worker — the "
                "drain must keep waiting to its deadline through repeated "
                "cancels (#6306 review round 2)"
            )
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The write itself still landed (drained, not abandoned)...
    assert landed and landed[0]["turns"] == 1
    # ...and the latch is down again: after a completed drain there is no live
    # worker, so recovery is safe and must not stay suppressed.
    assert info._diag_drain_active is False, "latch leaked past the drain"


@pytest.mark.asyncio
async def test_no_recovery_scheduled_while_diagnostics_worker_is_live() -> None:
    """Integration seam from GPT review round 4: drive ``_run`` (the wait_for
    wrapper that classifies cancellation and schedules recovery), cancel it
    twice while the diagnostics worker is gated, and prove recovery is never
    scheduled while the worker is still live. On 3.11+ the second cancel
    routes to the child task so the gate runs only post-drain; on 3.10 the
    second cancel can deliver the gate mid-drain, where the
    ``_diag_drain_active`` latch suppresses it — both paths must satisfy the
    same invariant asserted here."""
    import asyncio

    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="grep",
        tool_kind="read",
        request_id="req-1",
    )
    sessions = _mock_sessions_with_tool_event("model-served", event)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="turnw07", task="per-turn run-cancel task", model="model-req")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    worker_live = True
    recovery_calls: list[Any] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        nonlocal worker_live
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await release.wait()
        worker_live = False
        return True

    def _spy_recovery(info_: Any) -> None:
        # The invariant: recovery must never be scheduled while the worker
        # is still inside update_state.
        assert not worker_live, (
            "cancel-respawn recovery scheduled while the diagnostics worker "
            "was still writing — stale-overwrite race re-opened (#6306 "
            "review round 4)"
        )
        recovery_calls.append(info_)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_gated_to_thread),
        patch.object(manager, "_schedule_cancel_recovery", side_effect=_spy_recovery),
        patch.object(manager, "_write_tombstone"),
    ):
        task = asyncio.ensure_future(manager._run(info))
        try:
            await entered.wait()

            task.cancel()
            await _event_loop_checkpoint()
            task.cancel()  # the 3.10 _cancel_and_wait interruption shape
            await _event_loop_checkpoint()
            assert not recovery_calls, "recovery was scheduled before the diagnostics drain"
        finally:
            release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert worker_live is False
    # _spy_recovery's own assertion is the load-bearing check; whether
    # recovery ran at all afterwards is version-dependent and not pinned.


@pytest.mark.asyncio
async def test_recovery_gate_respects_live_drain_latch() -> None:
    """Direct gate check (kills the condition mutant): an UNEXPECTED
    cancellation with ``_diag_drain_active`` raised must NOT schedule
    cancel-respawn recovery — a fresh recovery writer would race the live
    worker. With the latch down, the same cancellation must recover
    (control, so the test cannot pass by recovery being broken outright)."""
    import asyncio

    async def _cancelled_inner(info_: Any, session_key: str) -> None:
        raise asyncio.CancelledError()

    for latch, expect_recovery in ((True, False), (False, True)):
        sessions = _mock_sessions(served_model="model-served")
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            is_yolo=lambda: True,
        )
        info = SubagentInfo(id=f"turnw08-{latch}", task="gate task", model="model-req")
        manager._agents[info.id] = info
        info._diag_drain_active = latch
        recovery_calls: list[Any] = []

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state", return_value=True),
            patch.object(manager, "_run_inner", side_effect=_cancelled_inner),
            patch.object(
                manager,
                "_schedule_cancel_recovery",
                side_effect=lambda i: recovery_calls.append(i),
            ),
            patch.object(manager, "_write_tombstone"),
        ):
            await manager._run(info)

        assert bool(recovery_calls) is expect_recovery, (
            f"latch={latch}: expected recovery_scheduled={expect_recovery}, "
            f"got {bool(recovery_calls)} — the recovery gate does not respect "
            "_diag_drain_active (#6306 review round 4)"
        )


@pytest.mark.asyncio
async def test_per_turn_diagnostics_drain_is_bounded() -> None:
    """The drain must NOT hold cancellation open forever: cancel_all() gathers
    run tasks with no timeout, so a worker wedged in fsync (the very slow-FS
    premise of #6288) would otherwise hold gateway shutdown indefinitely. On
    deadline expiry the worker is abandoned with a warning and cancellation
    completes (#6306 review round 2; same posture as _REPORT_DRAIN_TIMEOUT)."""
    import asyncio

    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="grep",
        tool_kind="read",
        request_id="req-1",
    )
    sessions = _mock_sessions_with_tool_event("model-served", event)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="turnw05", task="per-turn wedge task", model="model-req")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _wedged_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        try:
            await release.wait()
        finally:
            finished.set()
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_wedged_to_thread),
        patch("kiro_crew.subagent._DIAG_DRAIN_TIMEOUT", 0.0),
    ):
        task = asyncio.ensure_future(manager._run_inner(info, f"subagent:{info.id}"))
        try:
            await entered.wait()

            task.cancel()
            # A zero test deadline deterministically exercises expiry without
            # relying on wall-clock scheduling or a deliberately slow test.
            with pytest.raises(asyncio.CancelledError):
                await task
            # Expiry leaves a live stale writer behind, so the one-shot
            # cancel-respawn recovery must be consumed: a fresh recovery run's
            # PID/session writes could otherwise be rolled back by the zombie
            # worker's read-merge-replace (GPT server review round 3).
            assert info._cancel_retry_used is True, (
                "drain expiry did not suppress cancel-respawn recovery — a "
                "recovery run can now race the abandoned worker"
            )
        finally:
            release.set()
        # Do not leave an executor-shaped task pending at loop teardown.
        await finished.wait()
        await _event_loop_checkpoint()


@pytest.mark.asyncio
async def test_abandoned_diagnostics_worker_exception_is_retrieved() -> None:
    """A worker abandoned at drain expiry may still raise later; the expiry
    branch's done-callback must retrieve that exception so it never surfaces
    through the loop's 'Task exception was never retrieved' handler (Opus
    review round 3 on #6306: CPython's shield removes its retrieving callback
    exactly when the outer await is cancelled while the inner is pending —
    the expiry shape). Deleting the add_done_callback line fails this test."""
    import asyncio

    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="grep",
        tool_kind="read",
        request_id="req-1",
    )
    sessions = _mock_sessions_with_tool_event("model-served", event)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="turnw06", task="per-turn zombie-raise task", model="model-req")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _wedged_raising_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await release.wait()
        finished.set()
        raise OSError("disk came back angry")

    unretrieved: list[Any] = []
    loop = asyncio.get_running_loop()
    prev_handler = loop.get_exception_handler()

    def _capture(loop_: Any, context: dict) -> None:
        unretrieved.append(context)

    loop.set_exception_handler(_capture)
    try:
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state", return_value=True),
            patch(
                "kiro_crew.subagent.asyncio.to_thread",
                side_effect=_wedged_raising_to_thread,
            ),
            patch("kiro_crew.subagent._DIAG_DRAIN_TIMEOUT", 0.0),
        ):
            task = asyncio.ensure_future(manager._run_inner(info, f"subagent:{info.id}"))
            try:
                await entered.wait()

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            await finished.wait()
            await _event_loop_checkpoint()
            # Let the abandoned task get garbage-collected: the
            # never-retrieved handler fires from the task's __del__, so drop
            # the outer task reference and force collection.
            import gc

            task = None  # type: ignore[assignment]
            gc.collect()
            await _event_loop_checkpoint()
        assert not unretrieved, (
            "the abandoned diagnostics worker's exception was never retrieved "
            f"— missing expiry done-callback (#6306 review round 3): {unretrieved}"
        )
    finally:
        loop.set_exception_handler(prev_handler)


def test_update_state_reports_write_vs_skip(tmp_path: object) -> None:
    """The return contract the retry depends on: True when the merge was
    written, False when it was skipped because state.json is unreadable."""
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        read_state,
        update_state,
    )

    create_agent_folder("prov-rc1", task="task")
    assert update_state("prov-rc1", requested_model="model-req") is True
    state = read_state("prov-rc1")
    assert state is not None and state["requested_model"] == "model-req"
    # No folder / no state.json: the merge is skipped and reported as such.
    assert update_state("prov-rc-missing", requested_model="model-req") is False


@pytest.mark.asyncio
async def test_unpinned_spawn_records_requested_model_auto() -> None:
    """An unpinned spawn (no per-spawn model, no role-model pin) records
    ``requested_model="auto"`` rather than ``""`` so the frontend can show a
    neutral chip instead of hiding the model column entirely (#5869).
    ``isModelDowngrade("auto", <any>)`` is already guarded to return False, so
    this never triggers a false amber warning."""
    sessions = _mock_sessions(served_model="claude-opus-4.8")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    # No per-spawn model pin; simulate no role-model config pin either.
    info = SubagentInfo(id="prov-auto01", task="unpinned task", model="")
    manager._agents[info.id] = info

    provenance: list[dict[str, Any]] = []

    def _spy_update(agent_id: str, **kwargs: Any) -> bool:
        if "requested_model" in kwargs:
            provenance.append(dict(kwargs))
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_spy_update),
        # Patch _subagent_default_model to return "" (no role pin configured);
        # with eff_model="" the assignment simplifies to "auto" directly.
        patch("kiro_crew.subagent._subagent_default_model", return_value=""),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    assert provenance, "provenance write must still happen for an unpinned spawn"
    assert (
        provenance[0]["requested_model"] == "auto"
    ), f"unpinned spawn must record requested_model='auto', got {provenance[0]['requested_model']!r}"
