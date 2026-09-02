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
            assert info._state_drain_active is True, (
                "drain did not raise the _state_drain_active latch — on 3.10 a "
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
    assert info._state_drain_active is False, "latch leaked past the drain"


@pytest.mark.asyncio
async def test_no_recovery_scheduled_while_diagnostics_worker_is_live() -> None:
    """Integration seam from GPT review round 4: drive ``_run`` (the wait_for
    wrapper that classifies cancellation and schedules recovery), cancel it
    twice while the diagnostics worker is gated, and prove recovery is never
    scheduled while the worker is still live. On 3.11+ the second cancel
    routes to the child task so the gate runs only post-drain; on 3.10 the
    second cancel can deliver the gate mid-drain, where the
    ``_state_drain_active`` latch suppresses it — both paths must satisfy the
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
    cancellation with ``_state_drain_active`` raised must NOT schedule
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
        info._state_drain_active = latch
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
            "_state_drain_active (#6306 review round 4)"
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
        patch("kiro_crew.subagent._STATE_DRAIN_TIMEOUT", 0.0),
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
            patch("kiro_crew.subagent._STATE_DRAIN_TIMEOUT", 0.0),
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


@pytest.mark.asyncio
async def test_provenance_write_is_drained_on_cancellation() -> None:
    """#6308 sibling A: cancelling a run while the PRE-SPAWN provenance write is
    in flight must hold cancellation open until that worker finishes.

    The site used to be a bare ``await asyncio.to_thread(...)``: the cancel
    detached the worker, the run finalized immediately, and the zombie's
    WHOLE-FILE rewrite could then roll back whatever landed after its read --
    including the ``pid`` / ``session_id`` a cancel-respawn recovery run writes
    on the loop, without which the reaper can no longer reach the child. Same
    contract as the per-turn diagnostics write (#6306), now shared by every
    off-loop state writer through ``_write_state_off_loop``.
    """
    import asyncio

    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="provdr1", task="provenance cancel task", model="model-req")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        # Only the provenance write carries requested_model; every other
        # off-loop call in the spawn path goes straight through.
        if "requested_model" not in kwargs:
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
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _run_inner completed while the pre-spawn provenance "
                "write was in flight -- the detached worker can still roll back a "
                "recovery run's pid/session_id (#6308)"
            )
            # The same latch the per-turn drain raises, for the same reason: on
            # 3.10 a second outer cancel can deliver _run's recovery gate
            # mid-drain, and recovery must not be scheduled while a worker lives.
            assert (
                info._state_drain_active is True
            ), "the provenance drain did not raise the recovery-gate latch"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The write itself still landed (drained, not abandoned)...
    assert landed and landed[0]["requested_model"] == "model-req"
    assert info._state_drain_active is False, "latch leaked past the drain"
    # ...and the cancellation ended the two-attempt retry loop rather than
    # adding a SECOND writer for the same fields (the composition ruling this
    # site needed: drain per attempt, cancel ends the loop).
    assert len(landed) == 1, f"retry loop started another attempt after the cancel: {landed}"


@pytest.mark.asyncio
async def test_cc_refinement_write_is_drained_on_cancellation() -> None:
    """#6308 sibling B: same contract for the CC-path model refinement write.

    The refinement fires on the first text chunk, when a raw/CC provider first
    reveals its served model -- mid-turn, so a cancellation is more likely to
    find it in flight than the pre-spawn write.
    """
    import asyncio

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    # Spawn-time resolve sees nothing; the provider reveals its served model
    # only as the first chunk streams, which is what arms the refinement.
    sessions = _mock_sessions(served_model="")
    provider = sessions.get_or_create.return_value[0]

    async def _late_model_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        provider.served_model = "model-late"
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello")

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _late_model_stream())

    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="ccdrn1", task="cc refinement cancel task")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        # The refinement is the only write carrying resolved_model WITHOUT the
        # provenance write's requested_model companion.
        if "resolved_model" not in kwargs or "requested_model" in kwargs:
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
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _run_inner completed while the CC-path refinement "
                "write was in flight -- the detached worker can still roll back a "
                "recovery run's pid/session_id (#6308)"
            )
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed and landed[0]["resolved_model"] == "model-late"
    assert info._state_drain_active is False, "latch leaked past the drain"


@pytest.mark.asyncio
async def test_an_abandoned_state_writer_holds_the_conversation() -> None:
    """#6298 review (GPT round 1): the drain is BOUNDED, so on a wedged FS a
    worker outlives its run -- and its stale whole-file rewrite would roll back
    the ``keep`` a promote / release writes on the loop in the meantime.

    Unbounding the drain is not the answer: ``cancel_all()`` gathers run tasks
    with no timeout, so it would hold gateway shutdown open forever (the posture
    ``_REPORT_DRAIN_TIMEOUT`` already sets). Neither is keeping the run
    non-terminal, which would stall its completion event for as long as the FS
    stays wedged. Instead the manager records the abandoned writer and
    ``_conversation_busy`` reports the conversation as held until it settles,
    which defers both retention writes past the zombie. This pins the hold, that
    it survives ``_agents`` eviction (GPT review round 2), and its release.
    """
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
    info = SubagentInfo(id="aband01", task="abandoned writer task", model="model-req")
    manager._agents[info.id] = info
    conv_key = f"subagent:{info.id}"

    wedged = asyncio.Event()
    entered = asyncio.Event()

    async def _wedged_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await wedged.wait()  # never released before the drain deadline
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_wedged_to_thread),
        # Expire the drain immediately: this test is about what happens AFTER
        # expiry, not about the bound itself (pinned separately).
        patch("kiro_crew.subagent._STATE_DRAIN_TIMEOUT", 0.0),
    ):
        task = asyncio.ensure_future(manager._run_inner(info, conv_key))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The run is finished, but a worker is still live.
        info.done = True
        assert info.id in manager._abandoned_state_writers, (
            "drain expiry did not record the abandoned state writer -- "
            "promote/release would run while the zombie is live (#6298)"
        )
        held = manager._conversation_busy(conv_key)
        assert held is not None and held.id == info.id, (
            "a finished run with a live abandoned writer must still hold its "
            "conversation, so the keep write is deferred past the zombie"
        )
        # GPT review round 2: the hold must not depend on the run staying in
        # `_agents`. `evict_completed_agents` prunes completed runs, and an
        # eviction that released the hold would let a continuation write `keep`
        # for the zombie to erase.
        manager._agents.clear()
        evicted = manager._conversation_busy(conv_key)
        assert evicted is not None and evicted.id == info.id, (
            "evicting the completed run released the hold -- the abandoned-writer "
            "record must live on the manager, not on an _agents-resident flag"
        )
        assert evicted._state_writer_abandoned is True
        # Both retention paths therefore refuse rather than being silently undone.
        ok, detail = manager.release_conversation(info.id)
        assert ok is False and "conversation_busy" in detail, detail
        assert (
            "settling a state write" in detail
        ), f"refusal must not promise a completion event that already fired: {detail}"

        # The zombie lands: the hold releases and the conversation is usable again.
        wedged.set()
        for _ in range(200):
            if info.id not in manager._abandoned_state_writers:
                break
            await asyncio.sleep(0.01)
    assert info.id not in manager._abandoned_state_writers, (
        "the hold outlived the worker -- a conversation would stay permanently "
        "un-promotable and un-releasable"
    )
    assert manager._conversation_busy(conv_key) is None


def test_no_bare_to_thread_update_state_outside_the_drained_helper() -> None:
    """Build gate: the drain invariant must not be convention-only.

    "Every off-loop ``state.json`` writer is drained on cancellation" holds only
    while writers call ``_write_state_off_loop`` instead of a bare
    ``asyncio.to_thread(update_state, ...)``. That is precisely the divergence
    this change had to repair: three structurally identical sites, one given a
    drain by #6306 and two left bare, 300 lines apart in one file and identical
    at the call. A convention cannot catch that; a gate can (Design Review on
    #6298/#6308, matching the repo's other static gates -- see
    ``test_no_blocking_call_on_loop.py``).

    Deterministic and false-positive-free: an off-loop ``update_state`` has no
    legitimate undrained form, so exactly one call site is allowed -- the
    helper's own.
    """
    import ast
    from pathlib import Path

    allowed = "_write_state_off_loop_impl"
    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    assert src.is_dir(), src

    class _Scan(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.hits: list[tuple[str, int]] = []

        def _enter(self, node: Any) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _enter
        visit_AsyncFunctionDef = _enter

        def visit_Lambda(self, node: ast.Lambda) -> None:
            # A nested callable is a separate frame, so it must not inherit the
            # helper's exemption (same scope rule as
            # test_no_blocking_call_on_loop.py).
            self.stack.append("<lambda>")
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "update_state"
                and (not self.stack or self.stack[-1] != allowed)
            ):
                self.hits.append((self.stack[-1] if self.stack else "<module>", node.lineno))
            self.generic_visit(node)

    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable source
            continue
        # Cheap pre-filter: only a handful of modules mention either name, and
        # `ast.parse` over the whole package is by far the expensive part. A
        # substring miss cannot hide a call, because the pattern this gate looks
        # for spells both names literally.
        if "to_thread" not in text or "update_state" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - unparseable source
            continue
        scan = _Scan()
        scan.visit(tree)
        rel = path.relative_to(src.parent.parent)
        offenders += [f"{rel}:{line} (in {func})" for func, line in scan.hits]

    assert not offenders, (
        "bare `asyncio.to_thread(update_state, ...)` outside "
        f"`{allowed}`: " + ", ".join(offenders) + ". An off-loop state write must go "
        "through the helper, which drains the worker on cancellation -- an "
        "undrained worker outlives its run and its stale whole-file rewrite rolls "
        "back every field written after its read (#6298, #6308)."
    )


@pytest.mark.asyncio
async def test_the_conversation_hold_covers_the_whole_drain_not_only_expiry() -> None:
    """#6298 review (GPT round 3): the hold must start at the CANCELLATION, not
    at the drain deadline.

    On Python 3.10 a second outer cancel can interrupt ``wait_for``'s
    ``_cancel_and_wait`` and deliver ``_run``'s finalization while the drain is
    still in flight -- so a run can go ``done`` with a live writer and WITHOUT
    ever reaching the expiry branch. A hold armed only on expiry leaves that whole
    window open, and a continuation reaching a released gate writes ``keep`` for
    the live writer to erase. Here the drain keeps its real bound (never expires),
    so the hold under test can only come from the cancellation itself.
    """
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
    info = SubagentInfo(id="middrn1", task="mid-drain hold task", model="model-req")
    manager._agents[info.id] = info
    conv_key = f"subagent:{info.id}"

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "turns" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await release.wait()
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_gated_to_thread),
    ):
        task = asyncio.ensure_future(manager._run_inner(info, conv_key))
        try:
            await entered.wait()
            task.cancel()
            await _event_loop_checkpoint()
            # Mid-drain: the deadline has NOT passed, so nothing has been
            # abandoned -- yet the writer is live and the conversation must
            # already be held, because a 3.10 double-cancel could finalize the
            # run right here.
            assert info._state_drain_active is True, "not draining -- test setup wrong"
            assert info.id in manager._abandoned_state_writers, (
                "the conversation is unheld while a writer is live mid-drain -- a "
                "3.10 double-cancel finalization would let a continuation write "
                "`keep` for that writer to erase (#6298)"
            )
            # Simulate exactly that: the run finalizes while the drain is live.
            info.done = True
            held = manager._conversation_busy(conv_key)
            assert held is not None and held._state_writer_abandoned is True, (
                "a finished run whose writer is still in-drain must keep holding "
                "its conversation"
            )
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Drained normally (never expired), so the one-shot recovery is intact.
        assert info._cancel_retry_used is False, "a completed drain must not burn the retry"
        for _ in range(200):
            if info.id not in manager._abandoned_state_writers:
                break
            await asyncio.sleep(0.01)
    assert info.id not in manager._abandoned_state_writers, "hold outlived the worker"
    assert manager._conversation_busy(conv_key) is None


async def _await_off_loop_gate(entered: Any, what: str) -> None:
    """Wait for an off-loop write to reach its gate, BOUNDED.

    The gate only fires when the write goes through ``asyncio.to_thread``, so an
    unbounded ``await entered.wait()`` turns a regression (the write moved back
    onto the loop) into a 120s pytest-timeout with no diagnosis -- three of those
    is six minutes of a shared runner for one mistake. Five seconds is orders of
    magnitude above the real gate latency (the write is a patched no-op reached
    within one scheduling pass) and still fails in the same breath as the cause.
    """
    import asyncio

    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
    except (asyncio.TimeoutError, TimeoutError):  # pragma: no cover - regression path
        raise AssertionError(
            f"the {what} write never reached asyncio.to_thread, so it ran ON the "
            "event loop -- update_state ends in a synchronous fsync and must go "
            "through _write_state_off_loop (#6288, #7302)"
        ) from None


@pytest.mark.asyncio
async def test_pid_and_session_records_are_written_off_loop() -> None:
    """#7302: the PID record and the session record must not fsync on the loop.

    Both used to call ``update_state`` directly from ``_run_inner``, an ``async
    def`` body, so the read-merge-rewrite plus its fsync ran on the gateway's
    only loop -- the ``no-blocking-call-on-event-loop`` class #6288 names, three
    lines from a provenance write that #7467 had already moved off it. Real
    ``asyncio.to_thread`` is used here (not a passthrough double) so the thread
    identity in the assertion is the production one.
    """
    import threading

    sessions = _mock_sessions(served_model="model-served")
    # The default mock reports no pid, which skips the write under test.
    sessions.get_pid = MagicMock(return_value=4242)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="offloop1", task="off-loop record task")
    manager._agents[info.id] = info

    loop_thread = threading.current_thread()
    seen: list[tuple[str, bool]] = []

    def _spy(_agent_id: str, **fields: Any) -> bool:
        if "session_id" in fields:
            label = "session record"
        elif "pid" in fields:
            label = "PID record"
        elif "requested_model" in fields:
            label = "provenance"
        else:
            label = "other"
        seen.append((label, threading.current_thread() is loop_thread))
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_spy),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    labels = [label for label, _ in seen]
    assert labels.count("PID record") == 1, f"expected one PID record write, got {labels}"
    assert labels.count("session record") == 1, f"expected one session record write, got {labels}"
    on_loop = sorted({label for label, was_on_loop in seen if was_on_loop})
    assert not on_loop, (
        "state.json write(s) ran on the event loop: "
        + ", ".join(on_loop)
        + ". update_state ends in a synchronous fsync, and the reaper, every chat "
        "turn and the heartbeat share this loop (#6288, #7302)."
    )
    # Off-loop is also what makes the write take update_state's per-agent lock,
    # which on-loop callers skip (#7280) -- so this is the interleave fix too.
    assert info._pid == 4242


@pytest.mark.asyncio
async def test_pid_record_write_is_drained_on_cancellation() -> None:
    """#7302: the newly off-loop PID record inherits #7467's drain contract.

    Moving a writer off the loop is what creates the detached-worker hazard in
    the first place: cancelling a ``to_thread`` await abandons the worker, and
    ``update_state`` rewrites the WHOLE file from the snapshot it already read.
    Going through ``_write_state_off_loop`` rather than a bare ``to_thread`` is
    what holds cancellation open until the worker lands.
    """
    import asyncio

    sessions = _mock_sessions(served_model="model-served")
    sessions.get_pid = MagicMock(return_value=4242)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="piddr1", task="pid cancel task")
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        # Only the PID record carries `pid` without `session_id`; the provenance
        # write ahead of it and every other off-loop call go straight through.
        if "pid" not in kwargs or "session_id" in kwargs:
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
            await _await_off_loop_gate(entered, "PID record")
            task.cancel()
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _run_inner completed while the PID record write was in "
                "flight -- the detached worker's whole-file rewrite can still roll "
                "back a recovery run's state (#7302)"
            )
            assert (
                info._state_drain_active is True
            ), "the PID record drain did not raise the recovery-gate latch"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed and landed[0]["pid"] == 4242
    assert info._state_drain_active is False, "latch leaked past the drain"


@pytest.mark.asyncio
async def test_session_record_write_is_drained_on_cancellation() -> None:
    """#7302: same contract for the session record, which also carries ``keep``.

    ``keep`` is the field the two remaining on-loop writers (promote / release)
    contend for, so an abandoned worker here is the #6298 rollback shape --
    which is why this site needs the drain and not merely a ``to_thread``.
    """
    import asyncio

    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="sessdr1", task="session record cancel task", keep=True)
    manager._agents[info.id] = info

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "session_id" not in kwargs:
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
            await _await_off_loop_gate(entered, "session record")
            task.cancel()
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _run_inner completed while the session record write was "
                "in flight -- its detached worker can roll back the `keep` that "
                "promote / release write on the loop (#6298, #7302)"
            )
            assert (
                info._state_drain_active is True
            ), "the session record drain did not raise the recovery-gate latch"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed and landed[0]["keep"] is True
    assert info._state_drain_active is False, "latch leaked past the drain"


@pytest.mark.asyncio
async def test_shared_session_pid_write_is_drained_on_cancellation() -> None:
    """#7302: the session-sharing PID record is the third moved site.

    ``_create_shared_session`` runs on the loop from the spawn path, and its
    ``update_state`` was the one of the three with no ``except Exception``
    around it -- so the swap had to preserve a propagating failure while adding
    the drain. Unlike the other two this site is reached only when
    ``agent.session_sharing`` puts the child on the parent's runtime.
    """
    import asyncio

    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(
        id="sharedpid1", task="shared session task", parent_session_key="dashboard:1"
    )
    manager._agents[info.id] = info

    runtime = MagicMock()
    runtime.pid = 9191
    runtime.create_session = AsyncMock(return_value=MagicMock(session_id="sid-shared"))

    entered = asyncio.Event()
    release = asyncio.Event()
    landed: list[dict[str, Any]] = []

    async def _gated_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "pid" not in kwargs:
            return func(*args, **kwargs)
        entered.set()
        await release.wait()
        landed.append(dict(kwargs))
        return True

    with (
        patch("kiro_crew.subagent.SubagentManager._get_parent_runtime", return_value=runtime),
        patch("kiro_crew.subagent.AcpSessionProvider", MagicMock()),
        patch("kiro_crew.subagent.update_state", return_value=True),
        patch("kiro_crew.subagent.asyncio.to_thread", side_effect=_gated_to_thread),
    ):
        task = asyncio.ensure_future(
            manager._create_shared_session(info, f"subagent:{info.id}", "")
        )
        try:
            await _await_off_loop_gate(entered, "shared-session PID record")
            task.cancel()
            await _event_loop_checkpoint()
            assert not task.done(), (
                "cancelled _create_shared_session completed while its PID record "
                "write was in flight -- a lost pid is an orphan the reaper can no "
                "longer reach (#7302)"
            )
            assert (
                info._state_drain_active is True
            ), "the shared-session PID drain did not raise the recovery-gate latch"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed and landed[0]["pid"] == 9191
    assert info._state_drain_active is False, "latch leaked past the drain"


def test_no_on_loop_update_state_inside_a_coroutine() -> None:
    """Build gate: a coroutine body must not call ``update_state`` directly.

    Sibling of ``test_no_bare_to_thread_update_state_outside_the_drained_helper``
    for the other half of the same divergence. That gate pins HOW an off-loop
    write is performed; this one pins WHERE a write may happen at all. An
    ``async def`` body runs on the gateway's only event loop, so a direct call
    there is an fsync on the loop by construction (#6288) -- the exact residue
    #7302 records, and the thing no reviewer caught across five triage passes
    because the offending line reads identically to a legitimate one two
    functions away.

    Scope-aware, so it is deterministic rather than a substring heuristic: only
    the INNERMOST enclosing frame counts, which is what makes a synchronous
    helper nested inside a coroutine (the shape a worker thread runs) not an
    offender. The two retention writers -- ``_promote_conversation_impl`` and
    ``release_conversation_impl`` -- are synchronous ``def``s, so they are
    outside this gate's reach by that same rule; moving them is the rest of
    #7302 and needs its own change (their other work, the ``SessionMap``
    mutation, is required to stay on the loop).
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    assert src.is_dir(), src

    class _Scan(ast.NodeVisitor):
        def __init__(self) -> None:
            # (name, is_coroutine) per enclosing frame.
            self.stack: list[tuple[str, bool]] = []
            self.hits: list[tuple[str, int]] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append((node.name, False))
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.stack.append((node.name, True))
            self.generic_visit(node)
            self.stack.pop()

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.stack.append(("<lambda>", False))
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == "update_state" and self.stack and self.stack[-1][1]:
                self.hits.append((self.stack[-1][0], node.lineno))
            self.generic_visit(node)

    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable source
            continue
        # Cheap pre-filter; a substring miss cannot hide a call, because the
        # pattern spells the name literally.
        if "update_state" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - unparseable source
            continue
        scan = _Scan()
        scan.visit(tree)
        rel = path.relative_to(src.parent.parent)
        offenders += [f"{rel}:{line} (in {func})" for func, line in scan.hits]

    assert not offenders, (
        "`update_state(...)` called directly from a coroutine body: "
        + ", ".join(offenders)
        + ". A coroutine runs on the gateway's only event loop, and update_state "
        "ends in a synchronous fsync -- route it through "
        "`_write_state_off_loop`, which also drains the worker on cancellation "
        "(#6288, #7302)."
    )
