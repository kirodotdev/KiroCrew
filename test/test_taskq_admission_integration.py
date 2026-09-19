"""SubagentManager admission on top of the durable task queue.

Real ``SubagentManager`` admission and drain; the run itself is a fake worker
that finishes at once. No kiro-cli, no sockets, tmp ``KIROCREW_HOME``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.resource_status as resource_status
import kiro_crew.subagent as subagent_mod
from kiro_crew.dashboard import chat_utils
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import TASK_STORE_UNAVAILABLE_CODE
from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


async def _manager(max_concurrent: int = 2, **kw) -> SubagentManager:
    mgr = SubagentManager(
        sessions=_sessions(), ctx_builder=_ctx(), max_concurrent=max_concurrent, **kw
    )
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    mgr._last_spawn_ts = 0.0
    return mgr


def _store_path() -> Path:
    return Path(os.environ["KIROCREW_HOME"]) / "tasks" / "tasks.db"


async def _fake_run(mgr: SubagentManager, info: SubagentInfo, *, fail: bool = False) -> None:
    """The smallest honest worker: finish, report once, free the slot, pump."""
    await asyncio.sleep(0)
    info.done = True
    if fail:
        info.error = "boom"
    info.result = "ok"
    mgr._claim_finalize(info)
    if mgr._release_slot(info):
        mgr._running_count -= 1
        mgr._drain_queue()


#: Wall-clock BACKSTOP for the drain, and only that. The drain below is paced by
#: completions, so the time it needs is whatever a row costs on the host: an idle
#: dev Linux box drains the whole set in ~18 s, the same box with its core
#: contended took ~4.4 minutes, and the rate a Windows shard measured (648 rows in
#: 60 s) puts the set near 3 minutes there. Sized well above the slowest of those
#: because a host slower still is a slow host, not a broken queue -- a queue that
#: has genuinely stopped is caught sooner and far more cheaply by
#: :data:`_STALLED_PASSES`. Deliberately BELOW the per-test timeout the drain
#: test's marker raises, with room to spare for the submission phase this does NOT
#: wrap, so reaching it fails as an assertion naming the state counts rather than
#: as a killed xdist worker -- on Windows pytest-timeout has no SIGALRM and takes
#: the whole worker with it.
_DRAIN_CEILING_SECS = 720.0

#: Passes that move no row before the queue is declared stalled. Progress resets
#: it, so this bounds a STALL, not the drain: a pass that only settles the drain
#: task advances nothing, which is normal, and returning here lets the state-count
#: assertion report what was left rather than spinning to the ceiling.
_STALLED_PASSES = 100


async def _drain_until_terminal(mgr: SubagentManager, store: TaskStore, *, total: int) -> None:
    """Pump until the STORE says every row is done, waiting on the work itself.

    Paced by completions, never by a clock. A row costs ~9 ms of real store work
    here and around ten times that on a Windows runner sharing four cores with
    the rest of ``-n auto``, so any fixed budget for the whole drain encodes one
    host's speed: the 60 s one this replaces expired with two thirds of the rows
    still queued. The wait is on the drain task and the in-flight runs, which is
    the completion signal itself -- measured, the timer floor was NOT the limit
    (quantising every sleep to Windows's 15.6 ms tick still drained all 2000 rows
    in 23 s), so it is the per-row cost that has to be waited out rather than
    budgeted for.
    """
    stalled = 0
    settled = store.count(state=model.DONE)
    while settled < total and stalled < _STALLED_PASSES:
        pending = [
            task
            for task in (getattr(mgr, "_drain_task", None), *mgr._tasks.values())
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.wait(pending)
        else:
            # Nothing in flight to wait on: only a fresh pass can move the queue.
            mgr._drain_queue()
            await asyncio.sleep(0)
        moved = store.count(state=model.DONE)
        stalled = 0 if moved > settled else stalled + 1
        settled = moved


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


def _noop() -> None:
    return None


async def _settle(store: TaskStore, rounds: int = 12) -> None:
    """Barrier for the loop's posted store work -- SIGNALS, never a sleep.

    ``TaskStore.run`` submits to an executor with exactly ONE worker, so a job
    queued here cannot start before every job queued earlier has finished; a
    ``sleep(0)`` first lets a task that was only just created reach its own
    submission before this one is queued behind it. Repeated because a pump pass
    posts more work from the callback of the work before it, so each round
    settles one link of that chain.
    """
    for _ in range(rounds):
        await asyncio.sleep(0)
        await store.run(_noop)


# ── write-before-ack ──────────────────────────────────────────────────────────


def test_manager_opens_store_under_home(quiet) -> None:
    mgr = SubagentManager(sessions=_sessions(), ctx_builder=_ctx())
    assert mgr._taskq is not None
    assert mgr._taskq.path == _store_path()
    assert _store_path().exists()


@pytest.mark.asyncio
async def test_spawn_persists_before_returning_id(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        started = mgr.spawn("first", parent_session_key="dash:1")
        queued = mgr.spawn("second", parent_session_key="dash:1")
    assert started is not None and not started.queued
    assert queued is not None and queued.queued
    store: TaskStore = mgr._taskq
    assert store.state_of(started.id) == model.STARTING
    assert store.state_of(queued.id) == model.QUEUED
    row = store.get(queued.id)
    assert row.params["task"] == "second" and row.params["_preassigned_id"] == queued.id
    assert row.session_key == "dash:1" and row.kind == model.KIND_SUBAGENT
    assert [e.kind for e in store.events(started.id)][:3] == ["accepted", "claimed", "transition"]


@pytest.mark.asyncio
async def test_store_write_failure_refuses_with_typed_code_and_no_row(quiet) -> None:
    mgr = await _manager()

    def boom(*a, **k):
        raise TaskStoreUnavailable("disk full")

    with (
        patch.object(mgr._taskq, "accept_one", side_effect=boom),
        patch.object(SubagentManager, "_run", new=AsyncMock()),
    ):
        info = mgr.spawn("x", parent_session_key="dash:1")
    assert info is not None and info.done and info.error
    assert info.error_code == TASK_STORE_UNAVAILABLE_CODE
    assert "task store unavailable" in info.error
    assert info.id not in mgr._agents
    assert mgr._taskq.count() == 0
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_policy_refusals_leave_no_row(quiet) -> None:
    mgr = await _manager()
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        empty = mgr.spawn("   ", parent_session_key="dash:1")
        bad_cwd = mgr.spawn("t", parent_session_key="dash:1", cwd="/definitely/not/allowed")
    assert empty.done and bad_cwd.done and bad_cwd.error
    assert mgr._taskq.count() == 0


# ── memory pressure defers ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_pressure_defers_instead_of_refusing(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = await _manager()
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: resource_status.AdmissionDecision(
            admitted=False,
            posture=resource_status.POSTURE_CRITICAL,
            available_gb=0.5,
            reason="host memory critically low",
        ),
    )
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        info = mgr.spawn("later", parent_session_key="dash:1")
    assert info is not None
    assert info.queued is True and info.done is False and not info.error
    store: TaskStore = mgr._taskq
    row = store.get(info.id)
    assert row.state == model.QUEUED
    assert row.next_run_at is not None and row.next_run_at > store.now()
    assert [e.kind for e in store.events(info.id)] == ["accepted", "deferred"]
    assert info.id not in mgr._agents and mgr._running_count == 0
    # not in the window either: it is not eligible yet
    assert mgr._queue == []
    assert mgr.queued_count_for("dash:1") == 1
    # pressure lifts and the clock passes: the pump starts it
    monkeypatch.undo()
    store._clock = lambda: time.time() + 3600
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._drain_queue()
    assert store.state_of(info.id) == model.STARTING
    assert info.id in mgr._agents


@pytest.mark.asyncio
async def test_low_memory_floor_defers_too(quiet, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = await _manager()
    monkeypatch.setattr(
        subagent_mod, "check_memory_available", lambda min_gb=None, path=None: (False, 0.2)
    )
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        info = mgr.spawn("later", parent_session_key="dash:1")
    assert info.queued and not info.done
    assert mgr._taskq.state_of(info.id) == model.QUEUED


@pytest.mark.asyncio
async def test_without_store_pressure_still_refuses(quiet, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = await _manager()
    mgr._taskq = None
    monkeypatch.setattr(
        subagent_mod, "check_memory_available", lambda min_gb=None, path=None: (False, 0.2)
    )
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        info = mgr.spawn("now", parent_session_key="dash:1")
    assert info.done and "refused" in info.error


# ── bounded window / drain from store ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_bounded_and_fifo_across_boundary(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    mgr._taskq._window = 3
    ids = []
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        for i in range(8):
            ids.append(mgr.spawn(f"t{i}", parent_session_key="dash:1").id)
    # one started, 7 queued: 3 in the window, 4 store-only
    assert len(mgr._queue) == 3
    assert [p["_preassigned_id"] for p in mgr._queue] == ids[1:4]
    assert mgr._admission.taskq_overflow() == 4
    assert mgr.queued_count_for("dash:1") == 7
    assert mgr._taskq.count(state=model.QUEUED) == 7
    # a completion frees the slot: the drain takes the OLDEST row and refills
    order: list[str] = []
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._running_count = 0
        mgr._drain_queue()
    started = [i for i in ids[1:] if mgr._taskq.state_of(i) == model.STARTING]
    assert started == [ids[1]]
    assert len(mgr._queue) == 3
    assert [p["_preassigned_id"] for p in mgr._queue] == ids[2:5]
    del order


@pytest.mark.timeout(900)
@pytest.mark.asyncio
async def test_2000_submissions_all_complete_window_never_exceeds_64(quiet) -> None:
    """2000 rows really are drained to DONE, and the in-memory window never grows.

    The only test here that pays the whole burst as real store work, so its wall
    time is the host's per-row cost: ~18 s of drain on an idle dev Linux box, ~4.4
    minutes on the same box with its core contended.

    Two independent bounds, and which one fires names the failure. A queue that has
    stopped moving returns after :data:`_STALLED_PASSES` idle passes, and the
    state-count assertion below reports what was left -- that is the fast signal,
    and it is the one a real defect trips. Only a host still making progress
    reaches :data:`_DRAIN_CEILING_SECS`, which raises those same counts as an
    assertion rather than a bare ``TimeoutError``.

    So the ceiling is a backstop on WALL TIME, not a poll count raised until a
    race stops firing: it wraps no assertion, so no value of it can make a wrong
    answer pass -- it can only stop a slow-but-correct host from being reported as
    a broken queue. The marker sits above the ceiling plus the submission phase,
    which the ceiling does not wrap, so on a slow host the ceiling is what fires;
    the Windows shard's ``--timeout=180`` would otherwise kill the worker, and
    with ``--max-worker-restart=0`` that is a lost run, not a named failure.
    """
    mgr = await _manager(max_concurrent=8)
    store: TaskStore = mgr._taskq
    assert store.window == 64
    peak_window = {"n": 0}

    async def run(self, info):
        peak_window["n"] = max(peak_window["n"], len(mgr._queue))
        await _fake_run(mgr, info)

    with patch.object(SubagentManager, "_run", new=run):
        ids = [mgr.spawn(f"task {i}", parent_session_key="dash:1").id for i in range(2000)]
        assert len(set(ids)) == 2000
        assert store.count() == 2000
        assert len(mgr._queue) <= 64
        try:
            await asyncio.wait_for(
                _drain_until_terminal(mgr, store, total=2000), _DRAIN_CEILING_SECS
            )
        except asyncio.TimeoutError as exc:
            # Report the state the ceiling interrupted, in the same terms the
            # assertions below use: a bare TimeoutError says only "slow", while
            # these counts separate "still draining, host is slow" from "rows are
            # parked in a non-terminal state".
            raise AssertionError(
                f"the drain did not settle every row within {_DRAIN_CEILING_SECS:.0f}s: "
                f"states={store.count_by_state()}, window={len(mgr._queue)}, "
                f"running={mgr._running_count}"
            ) from exc
    by_state = store.count_by_state()
    assert by_state == {model.DONE: 2000}, by_state
    assert peak_window["n"] <= 64
    assert len(mgr._queue) == 0
    assert mgr._running_count == 0
    assert mgr.queued_count_for("dash:1") == 0
    # every id is done exactly once: one terminal transition event each
    sample = store.events(ids[1234])
    assert [e.kind for e in sample].count("transition") == 2  # starting, done (fake run)
    assert sample[-1].data["to"] == model.DONE


@pytest.mark.asyncio
async def test_an_ad_hoc_auto_approval_is_never_persisted_on_the_row(quiet) -> None:
    """One request's consent is not a property of the row it accepted.

    ``approval_mode="auto"`` skips the spawn gate AND pre-approves the run's
    tools, and the pump respawns a recovered row by forwarding its params
    verbatim -- so a persisted copy would let a restart start work and run tools
    on an authorisation nobody renewed. The row still RECORDS it in ``scope_ref``,
    which the schema defines as references rather than grants and which no start
    path reads.
    """
    mgr = await _manager(max_concurrent=2)
    store: TaskStore = mgr._taskq
    record = mgr._admission.taskq_build_record(
        "sa-auto",
        {
            "task": "t",
            "parent_session_key": "dash:1",
            "approval_mode": "auto",
            "_agent_prevalidated": True,
        },
        parent_session_key="dash:1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode="auto",
    )

    assert "approval_mode" not in record.params, "one request's consent was persisted"
    assert "_agent_prevalidated" not in record.params
    # Recorded as a reference, so an operator reading the row still sees what was
    # asked for; nothing on the start path reads it.
    assert record.scope_ref["approval_mode"] == "auto"

    store.accept([record])
    reloaded = store.get("sa-auto")
    assert reloaded is not None and "approval_mode" not in reloaded.params
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_a_row_on_disk_carrying_auto_approval_faces_the_spawn_gate(quiet) -> None:
    """The read side strips the grant too, so the disk cannot hand one back.

    A row is written by one build and started by another, and a row that still
    carries ``approval_mode`` is a request's consent replayed after the process
    that received it is gone -- the spawn gate skipped and the run's tools
    pre-approved on an authorisation nobody renewed. So the window entry drops
    it beside ``_agent_prevalidated`` rather than trusting the row.
    """
    mgr = await _manager(max_concurrent=1)
    store: TaskStore = mgr._taskq
    mgr._sessions.get_approval_policy = MagicMock(return_value="ask")
    mgr._ctx_builder.hooks.auto_approve_subagent_spawn = False
    approvals = AsyncMock(return_value=True)
    mgr._on_spawn_approval = approvals
    store.accept_one(
        model.TaskRecord(
            id="sa-legacy",
            kind=model.KIND_SUBAGENT,
            session_key="dash:1",
            params={"task": "t", "parent_session_key": "dash:1", "approval_mode": "auto"},
        )
    )
    assert "approval_mode" not in mgr._admission._window_entry(store.get("sa-legacy"))
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._drain_queue()
        await _settle(store)
    assert "sa-legacy" in mgr._agents
    assert approvals.await_count == 1, "the spawn gate was skipped on a replayed grant"
    await mgr.cancel_all()


# ── terminal writes ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_and_failure_settle_store_rows(quiet) -> None:
    mgr = await _manager(max_concurrent=4)
    outcomes = {"a": False, "b": True}

    async def run(self, info):
        await _fake_run(mgr, info, fail=outcomes[info.task])

    with patch.object(SubagentManager, "_run", new=run):
        a = mgr.spawn("a", parent_session_key="dash:1")
        b = mgr.spawn("b", parent_session_key="dash:1")
        await asyncio.gather(mgr._tasks[a.id], mgr._tasks[b.id])
    assert mgr._taskq.state_of(a.id) == model.DONE
    assert mgr._taskq.state_of(b.id) == model.FAILED
    ev = mgr._taskq.events(b.id)[-1]
    assert ev.data["error"] == "boom"
    assert mgr._taskq.get(a.id).result_ref.endswith("result.txt")


@pytest.mark.asyncio
async def test_user_stop_settles_as_cancelled(quiet) -> None:
    mgr = await _manager()
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        a = mgr.spawn("a", parent_session_key="dash:1")
    info = mgr._agents[a.id]
    info.user_stopped = True
    info.done = True
    assert mgr._claim_finalize(info) is True
    assert mgr._taskq.state_of(a.id) == model.CANCELLED
    # a second reporter cannot flip it
    info.user_stopped = False
    info.error = "late"
    assert mgr._claim_finalize(info) is False
    assert mgr._taskq.state_of(a.id) == model.CANCELLED


async def _parent_parked_on_one_child(
    mgr: SubagentManager, store: TaskStore
) -> tuple[SubagentInfo, SubagentInfo]:
    """A live parent parked in ``waiting_children`` on ONE live child.

    Stated once so the settle pins below cannot disagree about what the park
    is. A wait is never reachable from ``starting``, so the parent's own
    ``running`` mark has to have landed before it can yield for its children.
    """
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        parent = mgr.spawn("P", parent_session_key="dash:1")
        live_parent = mgr._agents[parent.id]
        mgr._admission.taskq_mark(live_parent, "running")
        await _settle(store)
        assert store.state_of(parent.id) == model.RUNNING
        live_parent._inflight_tool = SimpleNamespace(
            tool_name="@kirocrew-core/spawn_sub_agents", title="call"
        )
        child = mgr.spawn("C", parent_session_key=f"subagent:{parent.id}")
        await _settle(store)
    assert live_parent._slot_released is True
    assert store.state_of(parent.id) == model.WAITING_CHILDREN
    return live_parent, mgr._agents[child.id]


@pytest.mark.parametrize("failure", [TaskStoreUnavailable("disk full"), ValueError("schema drift")])
@pytest.mark.parametrize("pump_off_loop", [False, True])
@pytest.mark.asyncio
async def test_a_terminal_write_the_store_refused_still_resumes_the_parent(
    quiet, monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, failure: Exception
) -> None:
    """A child's failed terminal write is not its parent's sentence.

    ``_claim_finalize`` is one-shot, so the propagation the terminal write
    carries -- a waiting parent's ``request_resume``, and a cancelled parent's
    ``cancel_tree`` -- gets no second attempt from anywhere: run it only when
    the write commits and a transient outage parks the whole tree on a wait no
    wake will ever end. Both settle paths, because each one catches the failure
    in a frame of its own: the inline write, and the one posted to the writer
    thread (``pump_off_loop``, production's).

    Both FAILURE CLASSES on each path, because the token is already spent when
    ``taskq_settle`` runs: an exception that leaves it loses the propagation AND
    the terminal report -- ``_claim_finalize`` never returns True, so its caller
    reports nothing and no second claimer can, which is strictly more harm than
    the outage this guards. ``ValueError`` stands for the class no arm names on
    purpose (a schema drift, a driver bug), so the pin is about the ESCAPE and
    not about ``TaskStoreUnavailable`` being spelled twice.
    """
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", pump_off_loop)
    mgr = await _manager(max_concurrent=2)
    store: TaskStore = mgr._taskq
    live_parent, live_child = await _parent_parked_on_one_child(mgr, store)
    live_child.done = True
    live_child.result = "ok"
    with (
        patch.object(SubagentManager, "_run", new=AsyncMock()),
        patch.object(store, "finish", side_effect=failure),
    ):
        assert mgr._claim_finalize(live_child) is True
        await _settle(store)
    assert store.state_of(live_child.id) != model.DONE, "the failure did not refuse the write"
    assert live_parent._slot_released is False, "the parent was left parked on its wait"
    assert store.state_of(live_parent.id) == model.RUNNING
    await mgr.cancel_all()


@pytest.mark.parametrize("superseded_by", ["a_live_replacement", "a_terminal_owner"])
@pytest.mark.parametrize("pump_off_loop", [False, True])
@pytest.mark.asyncio
async def test_a_fenced_settle_propagates_unless_a_live_owner_holds_the_row(
    quiet, monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, superseded_by: str
) -> None:
    """A generation the row has moved past is not by itself a reason to stay quiet.

    ``WaitLedger.on_child_terminal`` adds the reported child to the terminal set
    UNCONDITIONALLY -- the report is the evidence, not the row -- so propagating
    from a reporter the fence rejected tells the parent its last awaited child
    ended. Both halves are load-bearing and they pull opposite ways, which is
    why the predicate is the row's LIVENESS and not its generation:

    * ``a_live_replacement`` -- the row runs under a newer generation. A
      replacement owns the outcome and holds a ``_claim_finalize`` token of its
      own, so propagating here wakes the parent while its child is still
      running. It must stay parked.
    * ``a_terminal_owner`` -- the generation moved AND the row ended, the shape
      of an operator cancel through ``/api/tasks`` (``store.cancel`` bumps the
      generation). Nobody will report it again and ``WaitLedger.rebuild`` only
      reconciles at boot, so a guard written as "my generation is stale" would
      park this parent until the next restart. It must be woken.

    Both settle paths, because each reads the row in a frame of its own: the
    inline write, and the one posted to the writer thread (production's).
    """
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
    from kiro_crew.taskq.waits import WaitRecord

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", pump_off_loop)
    mgr = await _manager(max_concurrent=4)
    store: TaskStore = mgr._taskq
    live_parent, live_child = await _parent_parked_on_one_child(mgr, store)
    stale_gen = int(live_child._taskq_generation or 0)
    assert stale_gen, "the child never held a generation to be fenced on"

    if superseded_by == "a_live_replacement":
        # Re-dispatch the child through a wait: the wake makes the row claimable
        # and a second claim starts it, so the newer generation is LIVE on it.
        mgr._admission.taskq_mark(live_child, "running")
        await _settle(store)
        record = WaitRecord.input("call-1", since=store.now())
        assert store.enter_wait(live_child.id, record.to_dict())
        assert store.wake_wait(live_child.id, reason="answered") is not None
        assert store.claim(live_child.id) is not None
        assert store.transition(live_child.id, model.STARTING)
        assert store.transition(live_child.id, model.RUNNING)
    else:
        assert store.cancel(live_child.id, reason="operator") is not None
    fenced = store.get(live_child.id)
    assert fenced.generation > stale_gen
    assert fenced.terminal is (superseded_by == "a_terminal_owner")

    live_child.done = True
    live_child.result = "ok"
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        assert mgr._claim_finalize(live_child) is True
        await _settle(store)
    assert store.state_of(live_child.id) == fenced.state, "the generation fence did not hold"

    if superseded_by == "a_live_replacement":
        assert live_parent._slot_released is True, "the parent was woken by a fenced reporter"
        assert live_parent._resume_pending is False
        assert store.state_of(live_parent.id) == model.WAITING_CHILDREN
    else:
        assert live_parent._slot_released is False, "the parent was left parked on its wait"
        assert store.state_of(live_parent.id) == model.RUNNING
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_stale_generation_from_superseded_dispatch_is_ignored(quiet) -> None:
    mgr = await _manager()
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        a = mgr.spawn("a", parent_session_key="dash:1")
    store: TaskStore = mgr._taskq
    old_info = mgr._agents[a.id]
    old_gen = old_info._taskq_generation
    # the runtime is lost and the row is re-dispatched under a new generation
    assert store.transition(a.id, model.RECOVERING, generation=old_gen)
    mgr._agents.pop(a.id)
    mgr._running_count = 0
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._drain_queue()
    new_info = mgr._agents[a.id]
    assert new_info._taskq_generation == old_gen + 1
    assert store.state_of(a.id) == model.STARTING
    # the OLD worker reports failure: fenced
    old_info.done = True
    old_info.error = "late failure"
    mgr._admission.taskq_settle(old_info)
    assert store.state_of(a.id) == model.STARTING
    assert any(e.kind == "stale_result" for e in store.events(a.id))


# ── cancel vs drain ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_store_only_queued_row_never_starts(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    mgr._taskq._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        first = mgr.spawn("run", parent_session_key="dash:1")
        in_window = mgr.spawn("w", parent_session_key="dash:1")
        outside = mgr.spawn("o", parent_session_key="dash:1")
    assert [p["_preassigned_id"] for p in mgr._queue] == [in_window.id]
    assert mgr._taskq.state_of(outside.id) == model.QUEUED
    reported: list[SubagentInfo] = []
    mgr._report_queued_stop = lambda params: reported.append(params)  # type: ignore[method-assign]
    assert await mgr.cancel(outside.id) is True
    assert mgr._taskq.state_of(outside.id) == model.CANCELLED
    assert reported and reported[0]["_preassigned_id"] == outside.id
    # drain everything: the cancelled row is never started
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._running_count = 0
        mgr._drain_queue()
        mgr._running_count = 0
        mgr._drain_queue()
    assert mgr._taskq.state_of(in_window.id) == model.STARTING
    assert mgr._taskq.state_of(outside.id) == model.CANCELLED
    assert outside.id not in mgr._agents
    del first


@pytest.mark.asyncio
async def test_a_queued_row_the_drain_started_is_not_cancelled_under_the_spawn(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``taskq_cancel_queued`` judges the row it read and writes under that read's
    generation, so a drain that claimed and STARTED the row in between keeps it.

    The interleaving is forced inside the read itself -- a real ``claim`` plus the
    real ``starting`` write, no sleeps -- because that is the whole window: a
    cancel landing after it would leave a ``cancelled`` row with a live spawn
    under it whose own later writes the generation bump fences out.
    """
    mgr = await _manager(max_concurrent=1)
    mgr._taskq._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        first = mgr.spawn("run", parent_session_key="dash:1")
        outside = mgr.spawn("o", parent_session_key="dash:1")
    store = mgr._taskq
    assert store.state_of(outside.id) == model.QUEUED
    real_get = store.get

    def _drain_between(task_id: str):
        rec = real_get(task_id)
        if task_id == outside.id and store.state_of(task_id) == model.QUEUED:
            assert store.claim(task_id) is not None
            assert store.transition(task_id, model.STARTING)
        return rec

    monkeypatch.setattr(store, "get", _drain_between)
    assert mgr._admission.taskq_cancel_queued(outside.id) is None
    assert store.state_of(outside.id) == model.STARTING
    del first


@pytest.mark.asyncio
async def test_cancel_in_window_marks_store_and_unqueues(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr.spawn("run", parent_session_key="dash:1")
        waiting = mgr.spawn("w", parent_session_key="dash:1")
    mgr._report_queued_stop = MagicMock()  # type: ignore[method-assign]
    assert await mgr.cancel(waiting.id) is True
    assert mgr._queue == []
    assert mgr._taskq.state_of(waiting.id) == model.CANCELLED


@pytest.mark.asyncio
async def test_cancel_landing_between_claim_and_start_stops_the_spawn(quiet) -> None:
    """A row cancelled in the store after the window popped it: the drain's
    fenced claim fails and nothing registers."""
    mgr = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr.spawn("run", parent_session_key="dash:1")
        waiting = mgr.spawn("w", parent_session_key="dash:1")
    mgr._taskq.cancel(waiting.id, reason="user_stop")  # e.g. an external cancel
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr._running_count = 0
        mgr._drain_queue()
    assert waiting.id not in mgr._agents
    assert mgr._running_count == 0
    assert mgr._taskq.state_of(waiting.id) == model.CANCELLED


@pytest.mark.asyncio
async def test_cancel_for_parent_reaches_store_only_rows(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    mgr._taskq._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr.spawn("run", parent_session_key="dash:1")
        w = mgr.spawn("w", parent_session_key="dash:1")
        o1 = mgr.spawn("o1", parent_session_key="dash:1")
        o2 = mgr.spawn("o2", parent_session_key="dash:2")  # other parent
    mgr._report_queued_stop = MagicMock()  # type: ignore[method-assign]
    mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
    running, queued = await mgr.cancel_for_parent("dash:1")
    assert queued == 2
    assert mgr._taskq.state_of(w.id) == model.CANCELLED
    assert mgr._taskq.state_of(o1.id) == model.CANCELLED
    assert mgr._taskq.state_of(o2.id) == model.QUEUED
    del running


# ── restart survival ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_rows_survive_restart_and_redispatch(quiet) -> None:
    first = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        running = first.spawn("running", parent_session_key="dash:1")
        q1 = first.spawn("q1", parent_session_key="dash:1")
        q2 = first.spawn("q2", parent_session_key="dash:1")
    first._taskq.close()  # crash: no terminal writes, in-memory queue gone
    del first
    second = await _manager(max_concurrent=2)
    store: TaskStore = second._taskq
    # reconcile settled the lost run (subagent default class: unknown)
    assert store.state_of(running.id) == model.UNKNOWN_SIDE_EFFECT
    assert store.state_of(q1.id) == model.QUEUED and store.state_of(q2.id) == model.QUEUED
    assert second._queue == []
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        second._drain_queue()
        second._drain_queue()
    assert store.state_of(q1.id) == model.STARTING
    assert store.state_of(q2.id) == model.STARTING
    assert set(second._agents) == {q1.id, q2.id}
    assert second._agents[q1.id].task == "q1"


@pytest.mark.asyncio
async def test_batch_pending_sees_store_only_members(quiet) -> None:
    mgr = await _manager(max_concurrent=1)
    mgr._taskq._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        a = mgr.spawn("a", batch_id="wv", batch_total=3, parent_session_key="dash:1")
        mgr.spawn("b", batch_id="wv", batch_total=3, parent_session_key="dash:1")
        c = mgr.spawn("c", batch_id="wv", batch_total=3, parent_session_key="dash:1")
    assert mgr.batch_members_pending("wv") is True
    mgr._agents[a.id].done = True
    mgr._queue.clear()  # window member gone; store-only member c still holds the wave
    assert mgr._taskq.state_of(c.id) == model.QUEUED
    assert mgr.batch_members_pending("wv") is True
    mgr._taskq.cancel(c.id)
    (
        mgr._taskq.cancel([r.id for r in mgr._taskq.list_pending(model.KIND_SUBAGENT)][0])
        if mgr._taskq.list_pending(model.KIND_SUBAGENT)
        else None
    )
    assert mgr.batch_members_pending("wv") is False


@pytest.mark.asyncio
async def test_an_unreadable_batch_read_holds_the_wave_open(quiet) -> None:
    """An unreadable batch is unknown members, never no members.

    The wave's bookkeeping is PRUNED when the digest closes, so a digest that
    closes early over store-only members is not one wrong message: a second
    digest can then fire for the same batch.
    """
    mgr = await _manager(max_concurrent=1)
    store: TaskStore = mgr._taskq
    store._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        a = mgr.spawn("a", batch_id="wv", batch_total=3, parent_session_key="dash:1")
        mgr.spawn("b", batch_id="wv", batch_total=3, parent_session_key="dash:1")
        c = mgr.spawn("c", batch_id="wv", batch_total=3, parent_session_key="dash:1")
    mgr._agents[a.id].done = True
    mgr._queue.clear()  # only the store knows c is still queued
    assert store.state_of(c.id) == model.QUEUED
    with patch.object(store, "fetch_pending_by_batch", side_effect=TaskStoreUnavailable("locked")):
        assert mgr.batch_members_pending("wv") is True
        assert await mgr.batch_members_pending_async("wv") is True
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_an_unreadable_overflow_keeps_the_attached_children_guard_closed(quiet) -> None:
    """An unreadable queue is unknown children, never zero children.

    A session reset or teardown that reads "no children pending" over a
    store-only queued child strands that child's completion on a cold-started
    replacement session, so the count answers "some" while the store cannot be
    read -- which is the arm ``subagents_attached`` already documents and could
    never reach while the count answered 0.
    """
    mgr = await _manager(max_concurrent=1)
    store: TaskStore = mgr._taskq
    store._window = 1
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr.spawn("live", parent_session_key="dash:other")
        mgr.spawn("w", parent_session_key="dash:other")
        outside = mgr.spawn("o", parent_session_key="dash:1")
    assert store.state_of(outside.id) == model.QUEUED
    assert mgr.running_agents_for("dash:1") == []
    state = SimpleNamespace(subagents=mgr)
    assert mgr._admission.taskq_overflow("dash:1") == 1
    assert await chat_utils.subagents_attached_async(state, None, "dash:1", "reset") is True
    with patch.object(store, "count_pending", side_effect=TaskStoreUnavailable("locked")):
        assert chat_utils.subagents_attached(state, None, "dash:1", "reset") is True
        assert await chat_utils.subagents_attached_async(state, None, "dash:1", "reset") is True
        assert mgr.has_pending_work_for("dash:1") is True
        assert mgr._admission.taskq_overflow("dash:1") > 0
        assert await mgr._admission.taskq_overflow_async("dash:1") > 0
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_task_queue_disabled_keeps_legacy_queue(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    real_load = KiroCrewConfig.load

    def load_disabled(*a, **k):
        cfg = real_load(*a, **k)
        cfg.agent.task_queue_enabled = False
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(load_disabled))
    mgr = await _manager(max_concurrent=1)
    assert mgr._taskq is None
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr.spawn("a", parent_session_key="dash:1")
        q = mgr.spawn("b", parent_session_key="dash:1")
    assert q.queued and len(mgr._queue) == 1
    assert not (Path(os.environ["KIROCREW_HOME"]) / "tasks").exists()
