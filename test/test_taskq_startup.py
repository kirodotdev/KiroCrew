"""Task-store startup stays off-loop; requests fail closed until attachment."""

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.taskq import store as store_mod
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable


@pytest.mark.asyncio
async def test_manager_open_is_off_loop_and_pending_spawn_is_refused(monkeypatch):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = TaskStore.open
    observed = []

    def parked_open(store):
        observed.append(TaskStore._on_running_loop_thread())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(store)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    monkeypatch.setattr(TaskStore, "open", parked_open)
    sessions = MagicMock()
    sessions.get_agent_selection.return_value = ("template", "")
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert manager._taskq is None
        for spawn in (manager.spawn, manager.spawn_async):
            result = spawn("before attachment", parent_session_key="web-1")
            info = await result if asyncio.iscoroutine(result) else result
            assert info is not None and info.done
            assert info.error_code == "task_store_unavailable"
        assert manager._running_count == 0
        assert not manager._agents
        release.set()
        await manager.wait_taskq_ready()
        assert manager._taskq is not None
        assert manager._taskq_unavailable is None
        assert manager._taskq.loop_thread_calls == 0
        assert observed == [False]
        record = manager.prepare_spawn("after attachment", parent_session_key="web-1")
        assert record is not None and hasattr(record, "record")
    finally:
        release.set()
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)


@pytest.mark.asyncio
async def test_failed_open_keeps_typed_refusal_after_startup(monkeypatch):
    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(TaskStore, "open", fail_open)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    await manager.wait_taskq_ready()
    info = await manager.spawn_async("not accepted")
    assert manager._taskq is None
    assert info.error_code == "task_store_unavailable"
    assert "locked" in info.error
    await manager.cancel_all()


def test_manager_without_a_loop_opens_synchronously():
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert manager._taskq_init_task is None
    assert manager._taskq is not None
    assert manager._taskq.loop_thread_calls == 0
    manager._taskq.close()


def test_feature_map_covers_all_overload_routes():
    root = Path(__file__).resolve().parents[1]
    feature_map = (root / "docs/feature-map/README.md").read_text(encoding="utf-8")
    for route in (
        "GET /api/tasks",
        "GET /api/tasks/summary",
        "GET /api/tasks/{task_id}",
        "POST /api/tasks/{task_id}",
        "POST /api/tasks/{task_id}/cancel",
        "GET /api/spawn/lanes",
        "GET /api/spawn/{agent_id}/resume",
        "GET /api/sessions/health",
    ):
        assert f"`{route}`" in feature_map
    assert "`answer_input`" in feature_map and "`cancel_wait`" in feature_map


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_dependency_tick_keeps_store_off_loop_and_callbacks_on_loop(
    monkeypatch, live, expired
):
    from kiro_crew.subagent import SubagentInfo
    from kiro_crew.taskq import dependency, model

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    await manager.wait_taskq_ready()
    store = manager._taskq
    assert store is not None
    await manager._admission.ensure_coordinator_async()
    coordinator = manager.dependency_coordinator()
    now = [coordinator.now()]
    started = now[0]
    coordinator._clock = lambda: now[0]
    seen = []
    loop_thread = threading.get_ident()
    coordinator.subscribe(
        on_wake=lambda task_id: seen.append(("wake", task_id, threading.get_ident())),
        on_fail=lambda task_id, reason: seen.append(("fail", task_id, threading.get_ident())),
    )
    monkeypatch.setattr(manager, "_drain_queue", MagicMock())
    resumes = []
    original_resume = SpawnAdmissionCoordinator.request_resume

    def request_resume(admission, info, **kwargs):
        resumes.append(threading.get_ident())
        return original_resume(admission, info, **kwargs)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "request_resume", request_resume)

    def seed():
        store.accept([model.TaskRecord(id="tick-row", kind=model.KIND_SUBAGENT)])
        claimed = store.claim("tick-row")
        assert claimed is not None
        store.transition("tick-row", model.STARTING, generation=claimed.generation)
        if live:
            store.transition("tick-row", model.RUNNING, generation=claimed.generation)
        coordinator.report(
            "tick-row",
            dependency.DependencySignal(
                dependency.KIND_RATE_LIMITED, "provider:test", "test", retry_at=started + 1.0
            ),
            generation=claimed.generation,
        )
        return claimed.generation

    try:
        generation = await store.run(seed)
        if live:
            info = SubagentInfo(id="tick-row", task="resume")
            info._slot_released = True
            info._taskq_generation = generation
            info._resume_event = asyncio.Event()
            manager._agents[info.id] = info
            assert not manager._monitor.taskq_wake_through(info.id, generation + 1)
        now[0] = started + coordinator.wait_deadline_secs + 1 if expired else started + 2.0
        manager._taskq_pump()
        tick_task = manager._taskq_tick_task
        manager._taskq_pump()
        assert manager._taskq_tick_task is tick_task
        await tick_task
        assert seen == [("fail" if expired else "wake", "tick-row", loop_thread)]
        assert resumes == ([loop_thread] if live and not expired else [])
        row = await store.run(store.get, "tick-row")
        assert row.state == (
            model.FAILED if expired else model.WAITING_DEPENDENCY if live else model.QUEUED
        )
        if not expired:
            assert row.generation == generation  # The admission pump owns the next claim.
        assert store.loop_thread_calls == 0
    finally:
        manager._agents.clear()
        timer = getattr(manager, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        await manager.cancel_all()
        await asyncio.to_thread(store.close)


def _accept_through_the_sync_door(manager, agent_id: str) -> str | None:
    """The door that refuses while the store is unavailable, called directly.

    ``prepare_spawn`` returns its ``PreparedSpawn`` BEFORE the store is
    consulted, so it answers the same either way; this is the call whose return
    value ``spawn`` turns into ``task_store_unavailable``.
    """
    return manager._admission.taskq_accept(
        agent_id,
        {"task": "after the retry", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model=None,
        allowed_tools=None,
        approval_mode=None,
    )


@pytest.mark.asyncio
async def test_a_transient_open_failure_is_retried_and_the_store_attaches(monkeypatch):
    """A lock is not a permanent verdict: the next attempt opens and work flows.

    The refusal itself stays immediate and fail-closed
    (``test_failed_open_keeps_typed_refusal_after_startup``); what this pins is
    that it ends without a restart.
    """
    original = TaskStore.open
    opens: list[int] = []

    def fail_once(store):
        opens.append(1)
        if len(opens) == 1:
            raise TaskStoreUnavailable("database is locked")
        return original(store)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "open", fail_once)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        assert manager._taskq is None
        assert manager._admission.taskq_required_but_unavailable()
        assert manager._taskq_reopen_attempts == 1
        refused = await manager.spawn_async("before the retry")
        assert refused.error_code == "task_store_unavailable"
        assert _accept_through_the_sync_door(manager, "refused-row")

        manager._taskq_reopen_at = 0.0
        assert manager._admission.taskq_reopen_if_due() is True
        await manager.wait_taskq_ready()

        assert manager._taskq is not None
        assert manager._taskq_unavailable is None
        assert manager._taskq_reopen_attempts == 0
        assert _accept_through_the_sync_door(manager, "accepted-row") is None
        assert manager._taskq.get("accepted-row") is not None
        assert manager._taskq.get("refused-row") is None
    finally:
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)


@pytest.mark.asyncio
async def test_the_reaper_sweep_is_what_retries_the_store_open(monkeypatch):
    """The wiring, so the retry cannot be orphaned from the loop that drives it."""
    import kiro_crew.subagent as subagent_mod

    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    asked = asyncio.Event()

    def counting_reopen(_admission):
        asked.set()
        return False

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "open", fail_open)
    monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0.01)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_reopen_if_due", counting_reopen)
        manager.start_reaper()
        await asyncio.wait_for(asked.wait(), 10)
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
async def test_the_reopen_backoff_follows_the_shared_recovery_schedule(monkeypatch):
    """The delay is ``RecoveryPolicy``'s equal-jitter step, not a local constant."""
    from types import SimpleNamespace

    from kiro_crew.recovery.policy import RecoveryPolicy

    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "open", fail_open)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        # The boot failure armed attempt 1 and its deadline is in the future, so
        # nothing may be re-armed yet.
        before = manager._taskq_init_task
        assert manager._admission.taskq_reopen_if_due() is False
        assert manager._taskq_init_task is before
        assert manager._taskq_reopen_attempts == 1

        shared = RecoveryPolicy()
        delays = [manager._admission.taskq_arm_reopen() for _ in range(3)]
        for attempt, delay in enumerate(delays, start=2):
            ceiling = min(shared.max_secs, shared.base_secs * 2 ** (attempt - 1))
            assert ceiling / 2.0 <= delay <= ceiling, (attempt, delay)
        assert delays[-1] > delays[0]  # the step grows
        assert manager._taskq_reopen_attempts == 4

        # ...and it is the CONFIG's schedule: a 1s cap cannot be the default's
        # step for attempt 5 (16s).
        tuned = SimpleNamespace(
            agent=SimpleNamespace(recovery_backoff_base_secs=0.5, recovery_backoff_max_secs=1.0)
        )
        delay = manager._admission.taskq_arm_reopen(tuned)
        assert 0.5 <= delay <= 1.0, delay
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
async def test_a_reopen_never_un_attaches_an_open_store(monkeypatch):
    """The attach is one-way: a re-open armed while the refusal stood can land
    after another attempt already attached, and must not drop that store."""
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        store = manager._taskq
        assert store is not None
        monkeypatch.setattr(manager, "_open_taskq", lambda: None)
        await manager._initialize_taskq()
        assert manager._taskq is store
    finally:
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)


@pytest.mark.asyncio
async def test_a_reopen_is_skipped_while_one_is_in_flight_or_the_manager_stops(monkeypatch):
    """Two attempts must never run at once, and a stopping gateway starts none."""

    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "open", fail_open)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        manager._taskq_reopen_at = 0.0

        manager._shutting_down = True
        assert manager._admission.taskq_reopen_if_due() is False
        manager._shutting_down = False

        parked = asyncio.get_running_loop().create_future()

        async def _in_flight():
            await parked

        manager._taskq_init_task = asyncio.create_task(_in_flight())
        assert manager._admission.taskq_reopen_if_due() is False
        assert manager._taskq_init_task is not None
        parked.set_result(None)
        await manager._taskq_init_task

        assert manager._admission.taskq_reopen_if_due() is True
        await manager.wait_taskq_ready()
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
async def test_a_config_that_cannot_be_read_still_arms_the_shared_default_delay(monkeypatch):
    """Recovery never depends on config parsing succeeding."""

    class _BrokenConfig:
        @property
        def agent(self):
            raise RuntimeError("config is unreadable")

    def fail_open(store):
        raise TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(TaskStore, "open", fail_open)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        delay = manager._admission.taskq_arm_reopen(_BrokenConfig())
        assert 2.0 <= delay <= 4.0  # the shared schedule's step for attempt 2
        assert manager._taskq_reopen_attempts == 2
    finally:
        await manager.cancel_all()


@pytest.mark.parametrize(
    "phase, warning",
    [
        ("import_legacy", "taskq legacy import failed"),
        ("reconcile_on_boot", "taskq boot reconcile failed"),
        ("rebuild", "taskq wait rebuild failed"),
    ],
)
def test_a_boot_phase_that_raises_is_logged_and_still_yields_a_working_store(
    tmp_path: Path, monkeypatch, caplog, phase: str, warning: str
) -> None:
    """A store that OPENS accepts work, whatever the three boot phases did.

    Each phase reads rows the previous incarnation left; none of them is what
    makes the store usable, so a raise there is a degraded boot and not a refused
    one -- only ``TaskStore.open`` itself is allowed to be fatal. Every arm is
    exercised because "best-effort by contract" is a claim about the arm, and an
    untested arm is the claim's opposite: a typo there turns a stale row into a
    gateway that will not start.
    """
    import kiro_crew.taskq as taskq_pkg
    from kiro_crew.taskq import model

    def _boom(*a: object, **kw: object) -> None:
        raise RuntimeError("boot phase blew up")

    if phase == "rebuild":
        # A method on the ledger the module constructs, not a module attribute.
        monkeypatch.setattr(taskq_pkg.WaitLedger, "rebuild", _boom, raising=True)
    else:
        monkeypatch.setattr(taskq_pkg, phase, _boom, raising=True)
    with caplog.at_level("WARNING", logger="kiro_crew.taskq"):
        store = taskq_pkg.open_default_store(tmp_path, import_legacy_records=True)
    try:
        assert warning in caplog.text
        # Usable, not merely returned: the accept path is what a caller needs.
        rec = model.TaskRecord(id="boot-1", kind=model.KIND_SUBAGENT, session_key="dash:1")
        assert store.accept([rec]) == ["boot-1"]
        assert store.state_of("boot-1") == model.QUEUED
    finally:
        store.close()


def _seed_wakeable_parent(home: Path) -> None:
    """A row the boot's wait rebuild must wake: ``waiting_children`` whose only
    child is done, of a kind with no recovery adapter.

    The kind is what makes the row reach ``rebuild`` at all: reconcile runs first
    and settles a dead owner's WAITING row of an ADAPTED kind by side-effect
    class, while an adapter-less kind keeps its state and only loses its lease
    (``awaiting_adapter``) -- so this is the shape the wake half sees at boot.
    """
    from kiro_crew.taskq import model
    from kiro_crew.taskq.waits import WaitRecord

    kind = model.KIND_TASKRUNNER_STEP
    store = TaskStore(TaskStore.default_path(home), network_fs=False).open()
    now = store.now()
    store.accept(
        [
            model.TaskRecord(id="parent", kind=kind, session_key="dash:1"),
            model.TaskRecord(id="kid", kind=kind, session_key="dash:1", parent_id="parent"),
        ]
    )
    for tid in ("parent", "kid"):
        gen = store.claim(tid).generation
        store.transition(tid, model.STARTING, generation=gen)
        store.transition(tid, model.RUNNING, generation=gen)
        if tid == "parent":
            store.enter_wait(
                "parent",
                WaitRecord.children(["kid"], since=now).to_dict(),
                generation=gen,
            )
        else:
            store.transition("kid", model.DONE, generation=gen)
    store.close()  # crash: the parent's wake died with the process


def test_open_default_store_names_the_row_a_boot_sweep_could_not_settle(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A refused write inside a boot sweep is named, per row, at WARNING.

    ``open_default_store`` runs each sweep once and keeps only the store, and the
    pump's repeated sweep re-reads wait DEADLINES alone -- so this parent's wake
    is never attempted again and the log line is the only record that it is
    waiting for the next restart. A silent report makes that indistinguishable
    from a clean boot.
    """
    import kiro_crew.taskq as taskq_pkg
    from kiro_crew.taskq import model

    _seed_wakeable_parent(tmp_path)

    def _refuse(self, task_id: str, *a: object, **kw: object) -> None:
        raise TaskStoreUnavailable("task wake failed: disk I/O error")

    monkeypatch.setattr(TaskStore, "wake_wait", _refuse, raising=True)
    with caplog.at_level("WARNING", logger="kiro_crew.taskq"):
        store = taskq_pkg.open_default_store(tmp_path, import_legacy_records=False)
    try:
        assert "taskq boot wait rebuild left 1 row(s) unsettled" in caplog.text
        assert "parent" in caplog.text
        assert "taskq wait rebuild failed" not in caplog.text  # reported, not raised
        assert store.state_of("parent") == model.WAITING_CHILDREN
    finally:
        store.close()


def test_open_default_store_names_the_row_whose_artifact_probe_raised(
    tmp_path: Path, caplog
) -> None:
    """Reconcile's per-row errors reach the log too, and the row is still settled."""
    import kiro_crew.taskq as taskq_pkg
    from kiro_crew.taskq import model

    store = TaskStore(TaskStore.default_path(tmp_path), network_fs=False).open()
    store.accept([model.TaskRecord(id="lost", kind=model.KIND_SUBAGENT, session_key="dash:1")])
    gen = store.claim("lost").generation
    store.transition("lost", model.STARTING, generation=gen)
    store.close()  # crash before the terminal write

    def _probe(rec: object) -> str:
        raise OSError("tombstone directory is unreadable")

    with caplog.at_level("WARNING", logger="kiro_crew.taskq"):
        reopened = taskq_pkg.open_default_store(
            tmp_path, import_legacy_records=False, artifact_probe=_probe
        )
    try:
        assert "taskq boot reconcile left 1 row(s) unsettled" in caplog.text
        assert "lost: artifact probe failed" in caplog.text
        assert "taskq boot reconcile failed" not in caplog.text
        # Settled by class, which is the verdict a probe answering ``None`` gives.
        assert reopened.state_of("lost") == model.UNKNOWN_SIDE_EFFECT
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_a_deferred_pump_claims_nothing_until_the_gateway_releases_it(monkeypatch, caplog):
    """``defer_queue_dispatch`` holds the durable rows a restart left behind.

    The gateway starts the reaper before its memory barrier, and the reaper's
    boot dispatch pumps the queue as soon as the loop yields -- which is the
    barrier's own ``await``. A run admitted there prepares its memory store
    first, so it either fails on ``MemoryStartupUnavailable`` or starts without
    its learned memory. While the hold stands every drain request (the boot
    ``call_later``, the store attach, a dependency wake) returns without a pass
    and the row stays ``queued`` on disk; ``release_queue_dispatch`` is the one
    pass that picks it up. A manager built without the flag pumps at once.
    """
    from kiro_crew.taskq import model

    caplog.set_level(logging.DEBUG, logger="kiro_crew.subagent")
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    dispatched: list[str] = []

    async def record(_admission, params):
        dispatched.append(str(params.get("_preassigned_id") or ""))
        return None

    monkeypatch.setattr(SpawnAdmissionCoordinator, "_dispatch_async_impl", record)
    manager = SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), defer_queue_dispatch=True
    )
    passes = 0
    original_pass = manager._drain_queue_pass

    async def counting_pass():
        nonlocal passes
        passes += 1
        await original_pass()

    monkeypatch.setattr(manager, "_drain_queue_pass", counting_pass)
    try:
        await manager.wait_taskq_ready()
        store = manager._taskq
        assert store is not None and manager._queue_dispatch_held
        # The row a restart left behind, written through the product's own door.
        assert _accept_through_the_sync_door(manager, "survivor") is None
        manager.start_reaper()  # arms the boot dispatch: call_later(0, _drain_queue)
        manager._drain_queue()  # what the store attach and a dependency wake do
        for _ in range(10):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert passes == 0 and dispatched == []
        # The refusals are not silent: exactly one debug line names the hold,
        # so a release that never happens is diagnosable from the log alone.
        held_lines = [r for r in caplog.records if "taskq pump refusing passes" in r.getMessage()]
        assert len(held_lines) == 1
        row = await store.run(store.get, "survivor")
        assert row is not None and row.state == model.QUEUED

        manager.release_queue_dispatch()
        assert not manager._queue_dispatch_held
        drain = manager._drain_task
        assert drain is not None
        await drain
        assert passes == 1
        assert dispatched == ["survivor"]

        manager.release_queue_dispatch()  # idempotent: no second pass
        assert passes == 1
    finally:
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)


@pytest.mark.asyncio
async def test_an_unheld_manager_pumps_a_recovered_row_at_once(monkeypatch):
    """The default keeps every existing caller's behaviour: no hold, no release needed."""
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    dispatched: list[str] = []

    async def record(_admission, params):
        dispatched.append(str(params.get("_preassigned_id") or ""))
        return None

    monkeypatch.setattr(SpawnAdmissionCoordinator, "_dispatch_async_impl", record)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    try:
        await manager.wait_taskq_ready()
        assert not manager._queue_dispatch_held
        assert _accept_through_the_sync_door(manager, "survivor") is None
        manager.start_reaper()
        for _ in range(10):
            await asyncio.sleep(0)
        drain = manager._drain_task
        if drain is not None:
            await drain
        assert dispatched == ["survivor"]
    finally:
        await manager.cancel_all()
        if manager._taskq is not None:
            await asyncio.to_thread(manager._taskq.close)
