"""The subagent DEPENDENCY park's ``running`` mark against a row still ``admitted``.

The dependency path does not reach ``ensure_running_marked``: its wait write is
inline, so its mark travels into the same database phase
(``RunEventCoordinator._dependency_report_db``). That made it the one mark that
did NOT get the missed-``starting`` replay, and a lost start write there produced
three consecutive ``rejected_transition`` events -- the ``running`` mark, then the
wait, then the coordinator's ``retry_wait`` park -- leaving a LIVE run parked on a
dependency backoff behind a row the next boot requeues blind.

The mark is also UNCONDITIONAL, which is the second half: the in-memory
``_taskq_running_marked`` flag is published ahead of a POSTED write whose boolean
is discarded, so any durable decision that reads the flag can be wrong in exactly
the direction that loses the run's result. Those pins drive the real
``_dependency_verdict`` with the production off-loop hand-off.

Fake clock, a real SQLite store in ``tmp_path``. No kiro-cli: the subjects are one
static database phase, the coroutine that hands work to it, and the store under
both.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin
from kiro_crew.subagent_manager.run import RunEventCoordinator
from kiro_crew.taskq import model as m
from kiro_crew.taskq.dependency import (
    KIND_RATE_LIMITED,
    PARK_STATE,
    WAIT_STATE,
    DependencyCoordinator,
    DependencySignal,
)
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock)


def _signal() -> DependencySignal:
    return DependencySignal(
        kind=KIND_RATE_LIMITED,
        dependency_scope="provider:kiro",
        source="acp",
        detail="429 before the first stream event",
    )


def _admitted_row(store: TaskStore, task_id: str = "sa-1") -> int:
    """A claimed subagent row whose ``starting`` write never landed."""
    store.accept_one(m.TaskRecord(id=task_id, kind=m.KIND_SUBAGENT, session_key="dash:1"))
    claimed = store.claim(task_id, owner="dead-incarnation")
    assert claimed is not None and store.state_of(task_id) == m.ADMITTED
    return claimed.generation


def _kinds(store: TaskStore, task_id: str) -> list[str]:
    return [e.kind for e in store.events(task_id)]


def _moves(store: TaskStore, task_id: str) -> list[tuple[str, str]]:
    return [(e.data["from"], e.data["to"]) for e in store.events(task_id) if e.kind == "transition"]


def test_a_dependency_park_on_an_admitted_row_replays_the_missed_start(
    store: TaskStore, clock: Clock
) -> None:
    """The whole park lands: mark, wait record, no refusal anywhere.

    ``admitted -> running`` is not an edge, so a bare ``transition`` here refused
    the mark, ``report`` then refused the wait (a wait is reachable only FROM
    ``running``) and refused the ``retry_wait`` park too (``admitted ->
    retry_wait`` is not an edge either), so even the coordinator's fallback had
    nowhere to put the row.
    """
    generation = _admitted_row(store)
    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)

    verdict = RunEventCoordinator._dependency_report_db(
        coordinator, store, "sa-1", _signal(), generation
    )

    assert verdict.outcome == "wait" and verdict.state == WAIT_STATE
    row = store.get("sa-1")
    assert row is not None and row.state == m.WAITING_DEPENDENCY
    assert row.wait is not None and row.wait["dependency_scope"] == "provider:kiro"
    assert "rejected_transition" not in _kinds(store, "sa-1")
    assert _moves(store, "sa-1") == [
        (m.ADMITTED, m.STARTING),
        (m.STARTING, m.RUNNING),
        (m.RUNNING, m.WAITING_DEPENDENCY),
    ]
    assert "sa-1" in coordinator.waiters("provider:kiro")


def test_the_parked_row_is_no_longer_one_the_next_boot_requeues_blind(
    store: TaskStore, clock: Clock
) -> None:
    """The consequence the mark exists to prevent.

    ``admitted`` is the store's word that no executor ever held the row, so the
    boot reconciler requeues it WITHOUT asking the side-effect class. A live run
    parked on a 15-minute dependency backoff behind such a row is re-dispatched
    and its side effect runs twice. Reached ``waiting_dependency`` instead, the
    same reconciler asks the class -- ``unknown`` for a subagent -- and refuses
    to replay it.
    """
    generation = _admitted_row(store)
    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)
    RunEventCoordinator._dependency_report_db(coordinator, store, "sa-1", _signal(), generation)

    report = reconcile_on_boot(store)

    assert report.requeued == 0, "a live parked run was handed back to the dispatcher"
    assert report.unknown_side_effect == 1
    assert store.state_of("sa-1") == m.UNKNOWN_SIDE_EFFECT


def test_the_park_still_falls_back_when_the_row_is_no_longer_the_runs_to_move(
    store: TaskStore, clock: Clock
) -> None:
    """The replay is generation-fenced, so it never resurrects someone else's row.

    A cancel between the claim and the park bumps the generation; the mark is
    refused as ``stale_result`` (not a widened edge), the wait is refused because
    the row is terminal, and the park is refused for the same reason. The row
    keeps the operator's outcome.
    """
    generation = _admitted_row(store)
    assert store.cancel("sa-1", reason="user_stop") == m.ADMITTED
    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)

    RunEventCoordinator._dependency_report_db(coordinator, store, "sa-1", _signal(), generation)

    assert store.state_of("sa-1") == m.CANCELLED
    assert "stale_result" in _kinds(store, "sa-1")


def test_the_dependency_mark_and_the_stream_mark_share_one_store_entry_point(
    store: TaskStore, clock: Clock
) -> None:
    """Neither path can gain the replay without the other.

    The defect was a SECOND spelling of the same write: ``taskq_advance`` replayed
    the missed step and ``_dependency_report_db`` called ``transition`` directly.
    Both now go through ``TaskStore.advance``, so the guarantee is the store's.
    """
    generation = _admitted_row(store)
    seen: list[tuple[str, str]] = []
    real = store.advance

    def _spy(task_id: str, new_state: str, **kw: object) -> bool:
        seen.append((task_id, new_state))
        return real(task_id, new_state, **kw)

    store.advance = _spy  # type: ignore[method-assign]
    try:
        RunEventCoordinator._dependency_report_db(
            coordinator := DependencyCoordinator(store, clock=clock, wake_per_tick=1),
            store,
            "sa-1",
            _signal(),
            generation,
        )
        assert coordinator is not None
    finally:
        store.advance = real  # type: ignore[method-assign]
    assert seen == [("sa-1", m.RUNNING)]


def _mark_bridge(store: TaskStore) -> object:
    """``taskq_mark`` over one store, on the caller's own thread.

    ``pump_off_loop = False`` is the suite-wide default, so ``_post_store_write``
    runs the phase inline and the mark's own answer is observable here -- which it
    is not in the gateway, where the write is posted and its refusal reaches
    nobody.
    """

    class _Bridge:
        pump_off_loop = False
        taskq_store = staticmethod(lambda: store)
        taskq_mark = _TaskqBridgeMixin.taskq_mark
        taskq_advance = _TaskqBridgeMixin.taskq_advance
        _post_store_write = _TaskqBridgeMixin._post_store_write

    return _Bridge()


def test_the_stream_mark_replays_the_missed_start_from_an_admitted_row(
    store: TaskStore, clock: Clock
) -> None:
    """``taskq_mark`` is the OTHER half, and the replay is what it buys.

    A pin that only spies the store entry point is satisfied by a mark that calls
    ``transition``: from ``admitted`` that asks for an edge the table forbids, so
    it answers False and the row stays in the one state a boot reconciler requeues
    without asking the side-effect class. The row's state after the mark is
    therefore the subject -- never the call it made.
    """
    generation = _admitted_row(store)

    _mark_bridge(store).taskq_mark(  # type: ignore[attr-defined]
        SimpleNamespace(id="sa-1", _taskq_generation=generation), m.RUNNING
    )

    assert store.state_of("sa-1") == m.RUNNING
    assert _kinds(store, "sa-1").count("rejected_transition") == 0
    assert [
        (ev.data.get("from"), ev.data.get("to"))
        for ev in store.events("sa-1")
        if ev.kind == "transition"
    ] == [(m.ADMITTED, m.STARTING), (m.STARTING, m.RUNNING)]


def test_the_stream_mark_never_resurrects_a_row_under_a_stale_generation(
    store: TaskStore, clock: Clock
) -> None:
    """The replay is fenced, so it cannot be a way back into a cancelled row.

    Without this the pin above is satisfied by a replay that ignores the
    generation, which would walk a row a newer claim owns back into ``running``.
    """
    generation = _admitted_row(store)
    assert store.cancel("sa-1")

    _mark_bridge(store).taskq_mark(  # type: ignore[attr-defined]
        SimpleNamespace(id="sa-1", _taskq_generation=generation), m.RUNNING
    )

    assert store.state_of("sa-1") == m.CANCELLED


def test_the_unconditional_mark_writes_nothing_on_a_row_already_running(
    store: TaskStore, clock: Clock
) -> None:
    """The mark costs no event on the row it finds already ``running`` -- which is
    every park after the first for one run.

    ``TaskStore.advance`` answers True from the row's own state without a write,
    so "always mark" is free. A bare ``transition`` cannot deliver that: the
    table has no self-edge, so ``running -> running`` is a
    ``rejected_transition`` event on every park, and a park that reads a
    per-process flag to dodge that event is exactly what loses the run. The row
    is live, so the wait is a wait -- never ``PARK_STATE``, which holds no
    runtime.
    """
    generation = _admitted_row(store)
    assert store.advance("sa-1", m.RUNNING, generation=generation)
    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)

    verdict = RunEventCoordinator._dependency_report_db(
        coordinator, store, "sa-1", _signal(), generation
    )

    assert verdict.state == WAIT_STATE != PARK_STATE
    assert store.state_of("sa-1") == m.WAITING_DEPENDENCY
    assert _kinds(store, "sa-1").count("rejected_transition") == 0
    assert _moves(store, "sa-1") == [
        (m.ADMITTED, m.STARTING),
        (m.STARTING, m.RUNNING),
        (m.RUNNING, m.WAITING_DEPENDENCY),
    ]


# ── the mark is the ROW's decision, never the process's flag ─────────────────


class _Admission:
    """The bridge entries both marks reach, over one store, POSTED like production.

    ``pump_off_loop = True`` is the gateway's shape and not the suite default, so
    the mark really is handed to the writer thread and its refusal really does
    reach nobody -- the two facts the flag-gated park was wrong about.
    """

    pump_off_loop = True

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self._manager = SimpleNamespace(_report_tasks=set())

    def taskq_store(self) -> TaskStore:
        return self._store

    taskq_mark = _TaskqBridgeMixin.taskq_mark
    taskq_advance = _TaskqBridgeMixin.taskq_advance
    _post_store_write = _TaskqBridgeMixin._post_store_write
    track_store_task = _TaskqBridgeMixin.track_store_task


def _run_events(store: TaskStore, generation: int) -> tuple[RunEventCoordinator, SimpleNamespace]:
    """A real ``RunEventCoordinator`` over one store, plus the run record it acts on."""
    events = RunEventCoordinator(SimpleNamespace(_admission=_Admission(store)))
    info = SimpleNamespace(id="sa-1", _taskq_generation=generation, _taskq_running_marked=False)
    return events, info


async def _drain_posted_writes(events: RunEventCoordinator) -> None:
    """Await the posted store writes the bridge tracked -- a SIGNAL, never a sleep."""
    tasks = tuple(events._manager._admission._manager._report_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@contextlib.contextmanager
def _advance_outage(store: TaskStore) -> Iterator[None]:
    """The transient database failure a best-effort mark is swallowed on."""
    real = store.advance

    def _locked(task_id: str, new_state: str, **kw: object) -> bool:
        raise TaskStoreUnavailable("database is locked")

    store.advance = _locked  # type: ignore[method-assign]
    try:
        yield
    finally:
        store.advance = real  # type: ignore[method-assign]


async def _park_behind_a_lost_running_mark(
    store: TaskStore, clock: Clock
) -> tuple[SimpleNamespace, object]:
    """A live run whose posted ``running`` mark was lost, then parked on a scope.

    The row is ``starting`` (the spawn gate's mark landed, the run's own did not)
    while ``_taskq_running_marked`` already says a mark went out: the flag leads
    the write, so this is the state any flag-gated durable decision reads. Every
    setup write goes through ``store.run`` for the same reason production does --
    a synchronous store call from a coroutine is what ``loop_thread_calls``
    counts, and these tests assert that counter.
    """
    generation = await store.run(_admitted_row, store)
    assert await store.run(store.advance, "sa-1", m.STARTING, generation=generation)
    events, info = _run_events(store, generation)

    with _advance_outage(store):
        events.ensure_running_marked(info)
        await _drain_posted_writes(events)
    assert info._taskq_running_marked is True
    assert await store.run(store.state_of, "sa-1") == m.STARTING, "the mark must be LOST here"

    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)
    verdict = await events._dependency_verdict(coordinator, info, _signal())
    return info, verdict


@pytest.mark.asyncio
async def test_a_park_behind_a_lost_mark_still_reaches_the_live_runs_wait_state(
    store: TaskStore, clock: Clock
) -> None:
    """The park is chosen from the ROW, so a lost mark cannot turn it into a park.

    Gated on ``_taskq_running_marked``, this park skipped the mark on a row still
    ``starting``, ``report`` refused the wait (reachable only FROM ``running``)
    and the coordinator parked the row ``retry_wait`` -- a claimable state, on a
    LIVE resident run, that no boot reconcile examines (``model.ACTIVE``).
    """
    _info, verdict = await _park_behind_a_lost_running_mark(store, clock)

    state = await store.run(store.state_of, "sa-1")
    kinds = await store.run(_kinds, store, "sa-1")
    moves = await store.run(_moves, store, "sa-1")

    assert verdict.outcome == "wait" and verdict.state == WAIT_STATE
    assert state == m.WAITING_DEPENDENCY != PARK_STATE
    assert state not in m.CLAIMABLE and state in m.ACTIVE
    assert "rejected_transition" not in kinds
    assert moves == [
        (m.ADMITTED, m.STARTING),
        (m.STARTING, m.RUNNING),
        (m.RUNNING, m.WAITING_DEPENDENCY),
    ]
    assert store.loop_thread_calls == 0, "the park's database phase ran on the event loop"


@pytest.mark.asyncio
async def test_a_park_behind_a_lost_mark_leaves_the_wake_and_the_result_writable(
    store: TaskStore, clock: Clock
) -> None:
    """What the flag-gated park actually cost: the run's own result.

    ``retry_wait`` has an edge to neither ``running`` nor ``done`` and is not a
    wait, so from there the scope's wake, the resume and the terminal write are
    all refused -- the work completes and nothing can record it. Both writes the
    finished run owes must therefore be open from the state the park left.
    """
    info, _verdict = await _park_behind_a_lost_running_mark(store, clock)
    generation = info._taskq_generation

    woken = await store.run(
        store.wake_wait, "sa-1", reason="scope recovered", generation=generation, to=m.RUNNING
    )

    assert woken == generation + 1, "the scope's wake was refused on a live run"
    assert await store.run(
        store.finish, "sa-1", m.DONE, generation=woken
    ), "the run's result was refused"
    assert await store.run(store.state_of, "sa-1") == m.DONE
    assert store.loop_thread_calls == 0, "a store call on the event loop"


@pytest.mark.asyncio
async def test_the_park_claims_the_flag_so_a_later_stream_mark_stays_out(
    store: TaskStore, clock: Clock
) -> None:
    """The flag has one job left, and dropping it is the other way to lose the wait.

    A throttle that lands before the run's FIRST frame parks a row whose stream
    mark has not been issued at all, so the flag is this park's to claim.
    ``TaskStore.advance`` will happily walk ``waiting_dependency -> running`` --
    the table allows it, a wait leaves only through ``running`` -- so a stream mark
    arriving afterwards on an unclaimed flag clears ``wait_json`` with no ``wake``
    event and no generation bump: the scope still lists the waiter and nothing on
    the row says what it is waiting for.
    """
    generation = await store.run(_admitted_row, store)
    assert await store.run(store.advance, "sa-1", m.STARTING, generation=generation)
    events, info = _run_events(store, generation)
    assert info._taskq_running_marked is False, "the first frame has not been seen yet"
    coordinator = DependencyCoordinator(store, clock=clock, wake_per_tick=1)

    verdict = await events._dependency_verdict(coordinator, info, _signal())
    events.ensure_running_marked(info)
    await _drain_posted_writes(events)

    assert verdict.outcome == "wait"
    assert info._taskq_running_marked is True
    row = await store.run(store.get, "sa-1")
    assert row.state == m.WAITING_DEPENDENCY
    assert row.wait is not None and row.wait["dependency_scope"] == "provider:kiro"
    assert "sa-1" in coordinator.waiters("provider:kiro")
    assert store.loop_thread_calls == 0, "a store call on the event loop"


# ── the typed error every store call reports a database failure as ───────────


def _delete_mode_store(tmp_path: Path, clock: Clock) -> TaskStore:
    """A store in the journal mode a data home on a network filesystem gets.

    ``journal_mode=delete`` is what ``agent.task_store_journal_mode=delete``
    selects and what the store AUTO-DETECTS over NFS/SMB, and it is the mode in
    which a competing writer blocks readers -- so a bare ``SELECT`` fails there
    exactly as a transaction does. WAL, the local default, does not block
    readers, which is why no other test in this suite reaches this.
    """
    return TaskStore(
        tmp_path / "tasks.db", clock=clock, network_fs=True, busy_timeout_secs=0.05
    ).open()


def test_a_locked_read_is_the_stores_typed_error_and_not_a_raw_sqlite_one(
    tmp_path: Path, clock: Clock
) -> None:
    """Every public method of the store reports a database failure as ONE type.

    ``advance`` reads the row's state before it writes, and on
    ``journal_mode=delete`` that read raises. Unwrapped it was a
    ``sqlite3.OperationalError``, which no caller in the tree catches: the mark's
    hand-off (``_post_store_write``) and its two callers all catch
    ``TaskStoreUnavailable``, so the raw error escaped the spawn path entirely.
    """
    store = _delete_mode_store(tmp_path, clock)
    try:
        generation = _admitted_row(store)
        assert store.journal_mode == "delete"
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        rival.execute("BEGIN EXCLUSIVE")
        rival.execute("UPDATE tasks SET updated_at=1 WHERE id='sa-1'")
        try:
            for read in (
                lambda: store.state_of("sa-1"),
                lambda: store.get("sa-1"),
                lambda: store.events("sa-1"),
                lambda: store.count_pending(m.KIND_SUBAGENT),
                lambda: store.active_rows(),
                lambda: store.advance("sa-1", m.RUNNING, generation=generation),
            ):
                with pytest.raises(TaskStoreUnavailable):
                    read()
        finally:
            rival.execute("ROLLBACK")
            rival.close()
    finally:
        store.close()


def test_a_locked_mark_is_swallowed_at_debug_and_never_raises_at_the_spawn_path(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """``taskq_mark`` is best-effort BY CONTRACT: nothing awaits it, so it must
    not raise into the gate's spawn path or the run loop. The inline branch
    (``pump_off_loop=False``, the suite-wide default) catches exactly
    ``TaskStoreUnavailable``, which is what makes the store-boundary wrap the
    fix and a per-call-site ``except sqlite3.Error`` the wrong layer."""
    store = _delete_mode_store(tmp_path, clock)
    try:
        generation = _admitted_row(store)

        class _Bridge:
            pump_off_loop = False
            taskq_store = staticmethod(lambda: store)
            taskq_advance = _TaskqBridgeMixin.taskq_advance
            _post_store_write = _TaskqBridgeMixin._post_store_write
            taskq_mark = _TaskqBridgeMixin.taskq_mark

        info = SimpleNamespace(id="sa-1", _taskq_generation=generation)
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        rival.execute("BEGIN EXCLUSIVE")
        rival.execute("UPDATE tasks SET updated_at=1 WHERE id='sa-1'")
        try:
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.subagent_manager.admission"):
                _Bridge().taskq_mark(info, m.RUNNING)  # must not raise
        finally:
            rival.execute("ROLLBACK")
            rival.close()
        levels = {r.levelno for r in caplog.records if "sa-1 -> running" in r.getMessage()}
        assert levels == {logging.DEBUG}, "a best-effort mark was reported as a real failure"
    finally:
        store.close()


# ── the terminal write's discarded result ────────────────────────────────────


def test_a_refused_terminal_write_is_said_out_loud_with_the_rows_real_state(
    store: TaskStore, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """``retry_wait -> done`` is closed ON PURPOSE, so that a wait the store
    could not record is a loud failure rather than a row that quietly re-runs
    finished work. Loud only holds if someone reads the boolean: discarded, the
    deliberate refusal became the quietest outcome there is."""
    generation = _admitted_row(store)
    assert store.advance("sa-1", m.RUNNING, generation=generation)
    assert store.transition("sa-1", PARK_STATE, generation=generation)
    assert not store.finish("sa-1", m.DONE, generation=generation)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"):
        _TaskqBridgeMixin.taskq_report_refused_settle(store, "sa-1", m.DONE, False)

    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert said and "sa-1" in said[0] and PARK_STATE in said[0]
    # A committed write says nothing: the log is the exception, not the norm.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"):
        _TaskqBridgeMixin.taskq_report_refused_settle(store, "sa-1", m.DONE, True)
    assert caplog.records == []


def _settle_bridge(store: TaskStore) -> object:
    """``taskq_settle`` and the report it owes, over one store and nothing else.

    The propagation entries are stubbed because they make store writes of their
    own; the subject is the terminal write's boolean and the call it feeds.
    """

    class _Bridge:
        pump_off_loop = False
        taskq_store = staticmethod(lambda: store)
        taskq_settle = _TaskqBridgeMixin.taskq_settle
        taskq_report_refused_settle = staticmethod(_TaskqBridgeMixin.taskq_report_refused_settle)
        # The REAL predicate, not a stub: it is what decides whether the refusal
        # this test drives (a closed ``retry_wait -> done`` edge, so the row is
        # still THIS run's at THIS generation) still owes the propagation. A stub
        # answering False would pass whatever the predicate did.
        taskq_superseded_by_live_owner = staticmethod(
            _TaskqBridgeMixin.taskq_superseded_by_live_owner
        )
        taskq_child_terminal = staticmethod(lambda child, state: None)
        taskq_cancel_children_of = staticmethod(lambda agent_id, *, reason: [])

    return _Bridge()


def test_taskq_settle_itself_is_what_reads_the_terminal_writes_boolean(
    store: TaskStore, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The report is ``taskq_settle``'s obligation, not just its helper's.

    A pin on ``taskq_report_refused_settle`` alone is satisfied by a
    ``taskq_settle`` that never calls it -- the boolean stays discarded and the
    deliberate refusal stays silent -- so the real method is the subject here.
    ``retry_wait -> done`` is the closed edge that makes ``finish`` answer False
    with the row intact.
    """
    generation = _admitted_row(store)
    assert store.advance("sa-1", m.RUNNING, generation=generation)
    assert store.transition("sa-1", PARK_STATE, generation=generation)
    info = SimpleNamespace(id="sa-1", _taskq_generation=generation, user_stopped=False, error=None)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"):
        _settle_bridge(store).taskq_settle(info)  # type: ignore[attr-defined]

    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert (
        said and "sa-1" in said[0] and PARK_STATE in said[0]
    ), "the settle discarded the store's refusal, so a claimable row is all that is left"
    assert store.state_of("sa-1") == PARK_STATE


def test_a_settle_the_store_took_says_nothing_through_the_real_method(
    store: TaskStore, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The same wiring must stay quiet on the committed path, or the pin above
    is satisfied by a settle that warns unconditionally."""
    generation = _admitted_row(store)
    assert store.advance("sa-1", m.RUNNING, generation=generation)
    info = SimpleNamespace(id="sa-1", _taskq_generation=generation, user_stopped=False, error=None)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"):
        _settle_bridge(store).taskq_settle(info)  # type: ignore[attr-defined]

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert store.state_of("sa-1") == m.DONE
