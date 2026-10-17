"""The subagent manager's spawn path against the durable queue.

An enabled queue with no store refuses, and a claim the store cannot take leaves the row
queued. A row the store accepted is re-vetted after its awaits, a refused one is failed in
the store, and every wave member is counted once. A committed app spawn queues on capacity,
a drained one re-proves its ownership, and the durable row never carries prevalidation. The
claim reserves its slot before it is awaited, and a refusal or a raise gives the reservation
back. A drain or pump request that lands mid-pass is not lost, posted terminal writes are
drained by ``cancel_all`` and a refused one is replayed by the pump, a nested child of a
blocked parent runs the W3 branch off the loop, a deferred row reaches the pump only after
its defer landed, and a corrupt ``tasks.db`` is quarantined and recreated. The shared
fixtures live in ``overload_integration_glue_helpers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from overload_fakes import settle_store_writes
from overload_integration_glue_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Runner,
    no_memory_pressure_fixture,
)

from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.subagent_manager import admission as admission_mod
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.taskq import model
from kiro_crew.taskq import store as store_mod
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── a claim the store cannot take never starts an untracked run ─────────────


@pytest.mark.asyncio
async def test_claim_unavailable_leaves_the_row_queued_instead_of_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None

    def _busy(*_a: Any, **_k: Any) -> Any:
        raise _taskq.TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(store, "claim", _busy)
    gen, proceed, reason = mgr._admission.taskq_claim("x1")
    assert (gen, proceed, reason) == (0, False, mgr._admission.CLAIM_UNAVAILABLE)

    info = mgr.spawn("do it", parent_session_key="web-1")
    assert info is not None and info.queued and not info.done, info
    assert info.id not in mgr._agents, "an unclaimed row must not start"
    assert mgr._running_count == 0
    assert store.state_of(info.id) == _taskq.QUEUED
    await mgr.cancel_all()


# ── mutable gates re-run after the memory read; a refused row is failed ──────


@pytest.mark.asyncio
async def test_store_accepted_row_is_re_vetted_after_its_awaits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spawn_async`` commits the row after ``prepare_spawn`` passed the policy
    gates, then awaits the commit and the memory read. A governance change
    made during those awaits must still hold: the read's re-entry vets the
    committed row again, refuses it, and fails the row in the store, so the
    refused work can never run."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None

    calls: list[str] = []
    verdict = {"deny": False}

    def _gov(*_a: Any, **_k: Any) -> str | None:
        calls.append("gov")
        return "spawn disabled" if verdict["deny"] else None

    monkeypatch.setattr(subagent_mod, "_vet_spawn_governance", _gov)
    original_accept = store.accept_one

    def _flip_then_accept(record: Any) -> str:
        verdict["deny"] = True  # governance changes DURING the commit
        return original_accept(record)

    monkeypatch.setattr(store, "accept_one", _flip_then_accept)
    info = await mgr.spawn_async("hello", parent_session_key="web-1")
    assert info is not None and info.done, info
    assert "spawn refused by governance" in info.error
    assert calls == ["gov", "gov"], "vetted in prepare_spawn and again after the read"
    assert info.id not in mgr._tasks
    await settle_store_writes(store)  # the fail write is posted to the writer thread
    assert store.state_of(info.id) == _taskq.FAILED, "the refused row is still runnable"
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_a_drained_row_refused_by_the_pump_is_failed_in_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None
    rec = mgr._admission.taskq_build_record(
        "drained1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    monkeypatch.setattr(subagent_mod, "_vet_spawn_governance", lambda *a, **k: "spawn disabled")
    info = mgr.spawn("t", parent_session_key="web-1", _from_queue=True, _preassigned_id="drained1")
    assert info is not None and info.done and "governance" in (info.error or "")
    assert (
        store.state_of("drained1") == _taskq.FAILED
    ), "the caller's refusal is the store's verdict"
    await mgr.cancel_all()


# ── wave accounting counts each member exactly once ──────────────────────────


@pytest.mark.asyncio
async def test_batch_members_are_counted_once_including_a_prepare_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A three-member wave through ``spawn_async``: one member is refused in
    ``prepare_spawn`` (bad cwd). Every member is counted exactly once, so the
    wave can close, and ``counted: true`` on ``/api/spawn`` is true for the
    refused member too."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    assert mgr._admission.taskq_store() is not None

    ok1 = await mgr.spawn_async("a", parent_session_key="web-1", batch_id="w1", batch_total=3)
    bad = await mgr.spawn_async(
        "b", parent_session_key="web-1", batch_id="w1", batch_total=3, cwd="/definitely/not/here"
    )
    ok2 = await mgr.spawn_async("c", parent_session_key="web-1", batch_id="w1", batch_total=3)
    assert ok1 is not None and ok2 is not None and not ok1.queued and not ok2.queued
    assert bad is not None and bad.done and bad.error and "cwd" in bad.error
    assert mgr._batch_submitted["w1"][0] == 3, mgr._batch_submitted
    await mgr.cancel_all()


# ── an enabled queue with no store REFUSES ───────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_is_refused_typed_when_the_enabled_store_is_unavailable() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    # The queue is enabled but its store did not open (what taskq_open records).
    mgr._taskq = None
    mgr._taskq_unavailable = "durable task queue unavailable: disk I/O error"
    assert mgr._admission.taskq_required_but_unavailable()
    for entry in (mgr.spawn, mgr.spawn_async):
        result = entry("do it", parent_session_key="web-1")
        info = await result if asyncio.iscoroutine(result) else result
        assert info is not None and info.done
        assert info.error_code == SpawnAdmissionCoordinator.TASK_STORE_UNAVAILABLE_CODE
        assert "disk I/O error" in (info.error or "")
        assert info.id not in mgr._agents or mgr._agents[info.id].done
    assert mgr._queue == []
    assert mgr._running_count == 0


# ── a committed app spawn queues on capacity; a refused one fails its row ────


async def _app_manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, max_concurrent=1)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    assert mgr._admission.taskq_store() is not None
    return mgr


@pytest.mark.asyncio
async def test_committed_prevalidated_app_spawn_queues_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity is a scheduling fact: an accepted (committed) app spawn that
    finds no slot QUEUES like any other row, with its prevalidation dropped so
    the drain re-validates the agent and re-proves app ownership."""
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    mgr._running_count = mgr._max_concurrent  # no slot
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued and not info.done, info
    assert store.state_of(info.id) == _taskq.QUEUED
    entry = next(p for p in mgr._queue if p["_preassigned_id"] == info.id)
    assert entry["_agent_prevalidated"] is False
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_drained_app_spawn_reproves_ownership_or_fails_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace as NS

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(
        subagent_mod,
        "list_agents",
        lambda project_dir=None: [NS(name="other--bg", filename="other--bg.json")],
    )
    mgr._running_count = mgr._max_concurrent
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued
    mgr._running_count = 0
    # The drain re-enters the row: the agent is not one of the app's own agents.
    drained = mgr.spawn(
        **{k: v for k, v in mgr._queue[0].items() if k != "_lane"}, _from_queue=True
    )
    assert drained is not None and drained.done and "only spawn its OWN" in (drained.error or "")
    assert store.state_of(info.id) == _taskq.FAILED, "a genuine refusal of a committed row fails it"
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_durable_row_never_carries_prevalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store row of a prevalidated app spawn has no ``_agent_prevalidated``:
    a start rebuilt from the row (window refill, restart) runs the gates."""
    from types import SimpleNamespace as NS

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    mgr._running_count = mgr._max_concurrent
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued
    rec = store.get(info.id)
    assert rec is not None and "_agent_prevalidated" not in rec.params
    # The same holds for the sync accept path (``spawn`` off the loop).
    record = mgr._admission.taskq_build_record(
        "sub-sync",
        {"task": "t", "_agent_prevalidated": True, "cwd": ""},
        parent_session_key="web-1",
        memory_store="",
        app="demo",
        model=None,
        allowed_tools=None,
        approval_mode=None,
    )
    assert "_agent_prevalidated" not in record.params
    # A row written by a build that still stored the flag rebuilds without it.
    legacy = _taskq.TaskRecord(
        id="sub-legacy",
        kind=_taskq.KIND_SUBAGENT,
        session_key="web-1",
        params={"task": "t", "agent": "demo--worker", "app": "demo", "_agent_prevalidated": True},
    )
    assert SpawnAdmissionCoordinator._window_entry(legacy).get("_agent_prevalidated") is None
    # The row rebuilt from the store reaches the ownership gate: with a foreign
    # same-named agent under the app's filename the drained start FAILS.
    monkeypatch.setattr(
        subagent_mod,
        "list_agents",
        lambda project_dir=None: [NS(name="other--bg", filename="other--bg.json")],
    )
    mgr._queue.clear()
    mgr._running_count = 0
    entry = SpawnAdmissionCoordinator._window_entry(rec)
    drained = mgr.spawn(**{k: v for k, v in entry.items() if k != "_lane"}, _from_queue=True)
    assert drained is not None and drained.done and "only spawn its OWN" in (drained.error or "")
    assert store.state_of(info.id) == _taskq.FAILED
    await mgr.cancel_all()


# ── a drain request landing mid-drain is not lost ────────────────────────────


@pytest.mark.asyncio
async def test_drain_request_during_a_drain_runs_a_second_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity released while the coroutine pump is mid-pass: the request is
    coalesced into ``_drain_again`` and the SAME task runs one more pass, so
    the row that became startable is started without waiting for another
    trigger."""
    mgr = await _app_manager(monkeypatch)
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    passes: list[int] = []
    original = mgr._drain_queue_pass

    async def _counted_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            mgr._drain_queue()  # a release lands while this pass runs
        await original()

    monkeypatch.setattr(mgr, "_drain_queue_pass", _counted_pass)
    mgr._drain_queue()
    task = getattr(mgr, "_drain_task")
    # flake-ok: the pump task ends after its coalesced second pass, inside the suite timeout; pre-existing, moved verbatim
    await task
    assert passes == [0, 1], "the coalesced request produced exactly one more pass"
    assert getattr(mgr, "_drain_again") is False
    await mgr.cancel_all()


# ── reserve-then-commit: the claim await cannot overshoot the cap ────────────


@pytest.mark.asyncio
async def test_a_spawn_during_the_parked_claim_queues_instead_of_overshooting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ClaimPoint`` reserves the slot BEFORE the claim is awaited. A spawn that
    lands while the claim is parked on the writer thread sees the cap spent and
    queues; when the claim returns, the re-entry consumes the reservation, so
    ``_running_count`` never exceeds ``_max_concurrent``."""
    import threading

    mgr = await _app_manager(monkeypatch)  # max_concurrent=1
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "parked",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    parked = threading.Event()
    release = threading.Event()
    real_claim = store.claim

    def _slow_claim(task_id, *a, **kw):
        parked.set()
        assert release.wait(5), "the test never released the claim"
        return real_claim(task_id, *a, **kw)

    monkeypatch.setattr(store, "claim", _slow_claim)
    mgr._drain_queue()
    drain = getattr(mgr, "_drain_task")
    await async_wait_until(
        parked.is_set, describe=lambda: "slow claim not yet parked on writer thread"
    )
    # The claim is parked on the writer thread; the reservation is held.
    assert mgr._running_count == 1 == mgr._max_concurrent
    loser = mgr.spawn("late", parent_session_key="web-1")  # a sync accept-path spawn
    assert loser is not None and loser.queued and not loser.done, loser
    assert loser.id not in mgr._agents
    assert mgr._running_count == 1
    release.set()
    # flake-ok: the drain ends once the release on the line above lets the parked claim finish; pre-existing, moved verbatim
    await drain
    await async_wait_until(
        lambda: "parked" in mgr._agents, describe=lambda: "spawned agent not yet in _agents"
    )
    assert "parked" in mgr._agents
    assert mgr._running_count <= mgr._max_concurrent
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_a_refused_or_unavailable_claim_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    for rid in ("r-unavailable", "r-cancelled"):
        rec = mgr._admission.taskq_build_record(
            rid,
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None

    def _busy(*_a, **_k):
        raise _taskq.TaskStoreUnavailable("locked")

    # Scoped for the same reason the taskq admission integration test is: a
    # blanket ``monkeypatch.undo()`` here would also revert this module's
    # ``healthy_host_memory`` pins and everything ``_app_manager`` patched, so
    # the second dispatch below would read the runner's real free memory.
    params = {"task": "t", "parent_session_key": "web-1", "_preassigned_id": "r-unavailable"}
    with monkeypatch.context() as busy_claim:
        busy_claim.setattr(store, "claim", _busy)
        info = await mgr._dispatch_async(params)
        assert info is not None and info.queued and not info.done
        assert mgr._running_count == 0, "an unavailable claim gives the reserved slot back"
    store.cancel("r-cancelled", reason="user stop")
    info = await mgr._dispatch_async({**params, "_preassigned_id": "r-cancelled"})
    assert info is not None and info.done and info.user_stopped
    assert mgr._running_count == 0, "a refused claim gives the reserved slot back"
    await mgr.cancel_all()


# ── posted store writes are drained by cancel_all ───────────────────────────


@pytest.mark.asyncio
async def test_posted_terminal_writes_are_tracked_and_drained_by_cancel_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``taskq_settle`` posted to the writer thread is a ``_report_tasks``
    member: ``cancel_all`` waits for it, so a gateway stopping right after a
    run ended does not leave a ``running`` row for the next boot to re-run."""
    import threading

    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "done1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    claimed = store.claim("done1")
    assert claimed is not None
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(
        id="done1", task="t", parent_session_key="web-1", done=True, user_stopped=True
    )
    info._taskq_generation = claimed.generation
    gate = threading.Event()
    real_finish = store.finish

    def _slow_finish(*a, **kw):
        assert gate.wait(5)
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _slow_finish)
    mgr._admission.taskq_settle(info)
    posted = [t for t in mgr._report_tasks if not t.done()]
    assert posted, "the posted settle is tracked"
    gate.set()
    await mgr.cancel_all()
    assert all(t.done() for t in posted)
    assert store.state_of("done1") == _taskq.CANCELLED


# ── the W3 branch (nested child, parent blocked in spawn_sub_agents) off-loop ──


@pytest.mark.asyncio
async def test_nested_child_of_a_blocked_parent_runs_w3_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child accepted through ``/api/spawn`` for a parent parked in
    ``spawn_sub_agents``: the parent yields its slot (``waiting_children``)
    with the ledger read, the deadline read and the wait write all on the
    writer thread -- pinned with the strict guard armed for the whole call."""
    from types import SimpleNamespace as NS

    from kiro_crew import taskq as _taskq
    from kiro_crew.dashboard.handlers import messaging

    mgr = await _app_manager(monkeypatch)
    mgr._max_concurrent = 2
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    # The parent: a live run (a hand-built record, so the fake session cannot
    # end it), in the store as ``running``, blocked in the blocking tool.
    from kiro_crew.subagent import SubagentInfo

    rec = mgr._admission.taskq_build_record(
        "parent1",
        {"task": "parent", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    claimed = store.claim("parent1")
    assert claimed is not None
    store.transition("parent1", _taskq.STARTING, generation=claimed.generation)
    store.transition("parent1", _taskq.RUNNING, generation=claimed.generation)
    live = SubagentInfo(id="parent1", task="parent", parent_session_key="web-1")
    live._taskq_generation = claimed.generation
    live._inflight_tool = NS(tool_name="@kirocrew-core/spawn_sub_agents", title="call-1")
    mgr._agents["parent1"] = live
    mgr._running_count = 1
    parent = live

    async def _hang(info: Any) -> None:  # the child stays live for the scenario
        # flake-ok: the child's run parks here on purpose until the cancel_all that ends the test; pre-existing, moved verbatim
        await asyncio.Event().wait()

    monkeypatch.setattr(mgr, "_run", _hang)
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    child = await messaging._spawn_on_loop(
        NS(subagents=mgr), "child", parent_session_key=f"subagent:{parent.id}"
    )
    await async_wait_until(
        lambda: asyncio.to_thread(lambda: store.state_of("parent1") == _taskq.WAITING_CHILDREN),
        describe=lambda: "W3 branch not yet written WAITING_CHILDREN to store",
    )
    # The accept AND the W3 branch (ledger read, deadline read, enter_wait)
    # completed without a loop-thread store call.
    assert store.loop_thread_calls == before
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    assert child is not None and child.id in mgr._agents
    assert live._slot_released is True, "the parent yielded on the loop, synchronously"
    assert (live._wait_record or {}).get("state") == _taskq.WAITING_CHILDREN
    assert child.id in live._wait_record["resume_condition"]["ids"]
    rec = store.get(parent.id)
    assert rec is not None and rec.state == _taskq.WAITING_CHILDREN
    await mgr.cancel_all()


# ── a raise inside the re-entry never leaks the reserved slot ────────────────


@pytest.mark.asyncio
async def test_a_raise_inside_reenter_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "boom1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    before = mgr._running_count
    point = admission_mod.ClaimPoint("boom1")
    mgr._running_count += 1  # what spawn_impl does when it hands back a ClaimPoint

    seen: dict[str, int] = {}

    def _reenter(claimed):
        seen["generation"] = int(claimed[0])
        raise RuntimeError("unwrapped agents-dir scan")

    with pytest.raises(RuntimeError):
        await mgr._admission.claim_and_start(point, _reenter)
    assert mgr._running_count == before, "the reservation is released when re-entry raises"
    assert "boom1" not in mgr._agents
    # The claim landed and the store took the start (``admitted -> starting``)
    # before the re-entry raised. Nothing runs under that row, so the claimer
    # releases it by one fenced ``starting -> retry_wait`` write at the claim's
    # generation, unleased and claimable at once: the next pump pass retries
    # the accepted work (not lost, not a phantom run, not ended).
    released = store.get("boom1")
    assert released is not None and released.state == _taskq.RETRY_WAIT
    assert released.generation == seen["generation"]
    assert released.lease_owner is None and released.next_run_at is None
    transitions = [e.data for e in store.events("boom1") if e.kind == "transition"]
    assert not any(t.get("to") == _taskq.FAILED for t in transitions)
    assert (transitions[-1].get("from"), transitions[-1].get("to")) == (
        _taskq.STARTING,
        _taskq.RETRY_WAIT,
    )
    # The pump path swallows the same raise without leaking either.
    monkeypatch.setattr(
        mgr,
        "spawn",
        lambda **kw: (
            (_ for _ in ()).throw(RuntimeError("boom"))
            if kw.get("_claimed")
            else (
                admission_mod.ClaimPoint(kw["_preassigned_id"])
                if not (mgr.__dict__.__setitem__("_running_count", mgr._running_count + 1))
                else None
            )
        ),
    )
    rec2 = mgr._admission.taskq_build_record(
        "boom2",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec2) is None
    mgr._queue.append({"task": "t", "parent_session_key": "web-1", "_preassigned_id": "boom2"})
    await mgr._drain_queue_async()
    assert mgr._running_count == before
    await mgr.cancel_all()


# ── the pump sees a deferred row only after its defer landed ─────────────────

#: Lost-run bound for the gated defer and the spawn the test releases. The test's call
#: measured at most 0.04 s in repeats at -n0, so this only ends a broken run, by name,
#: well inside the suite's 120 s timeout.
_DEFER_LOST_RUN_SECS = 30.0


@pytest.mark.asyncio
async def test_admitting_id_is_held_until_the_posted_defer_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from kiro_crew import subagent as subagent_mod

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(subagent_mod, "check_memory_available", lambda min_gb: (False, 0.5))
    gate = threading.Event()
    defer_entered = threading.Event()
    real_defer = store.defer
    seen: dict[str, Any] = {}

    def _slow_defer(task_id, *, wait, reason):
        defer_entered.set()
        assert gate.wait(_DEFER_LOST_RUN_SECS)
        seen["wait"] = wait
        return real_defer(task_id, wait=wait, reason=reason)

    monkeypatch.setattr(store, "defer", _slow_defer)
    task = asyncio.ensure_future(mgr.spawn_async("pressure", parent_session_key="web-1"))
    try:
        # The window under test opens when the posted defer is running and held by
        # the gate. Wait for that, not for a count of loop turns: on a slow executor
        # the spawn's off-loop hops outlast any count, and ``_admitting_ids`` does
        # not exist until the spawn reaches the row it accepted. A spawn that
        # returns without deferring ends the wait too, and fails below.
        await async_wait_until(
            lambda: defer_entered.is_set() or task.done(),
            timeout=_DEFER_LOST_RUN_SECS,
            describe=lambda: f"admitting={getattr(mgr, '_admitting_ids', None)!r}",
        )
        assert defer_entered.is_set(), f"the spawn never posted its defer: {task!r}"
        admitting = getattr(mgr, "_admitting_ids", set())
        assert (
            len(admitting) == 1
        ), "the row stays excluded from the refill while its defer is in flight"
        (aid,) = tuple(admitting)
        assert aid in mgr._admission.taskq_excluded_ids()
    finally:
        # Released on every path, so a failed assertion never leaves the store's
        # writer thread parked on the gate.
        gate.set()
    info = await asyncio.wait_for(task, _DEFER_LOST_RUN_SECS)
    assert info is not None and info.queued
    assert not admitting, "cleared only once the defer landed"
    rec = store.get(info.id)
    assert seen["wait"] == mgr._admission.taskq_admit_wait_secs()
    assert rec is not None and rec.next_run_at > store.now()
    (deferred,) = [e for e in store.events(info.id) if e.kind == "deferred"]
    assert rec.next_run_at == deferred.ts + seen["wait"], "until comes from the defer's own read"
    await mgr.cancel_all()


# ── a corrupt tasks.db does not fail-close spawns: quarantined, recreated ─────


@pytest.mark.asyncio
async def test_corrupt_tasks_db_is_quarantined_and_spawns_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from kiro_crew.taskq.store import TaskStore as _TS

    home = Path(os.environ["KIROCREW_HOME"])
    path = _TS.default_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage, not sqlite\n" * 32)
    mgr = await _app_manager(monkeypatch)  # opens the default store at construction
    store = mgr._admission.taskq_store()
    assert store is not None, "the queue is not fail-closed by a corrupt file"
    assert store.quarantined_to is not None and store.quarantined_to.exists()
    assert mgr._admission.taskq_required_but_unavailable() is None
    info = await mgr.spawn_async("after corruption", parent_session_key="web-1")
    assert info is not None and not info.done and info.id in mgr._agents
    assert store.get(info.id) is not None
    await mgr.cancel_all()


# ── a pump request landing mid-sweep is not lost ──────────────────────────────


@pytest.mark.asyncio
async def test_a_scope_armed_during_a_pump_sweep_gets_its_own_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run parks on a scope the in-flight sweep has already read past.

    The sweep arms from the deadline it read, so the request the park makes
    (``_taskq_pump`` right after ``coordinator.report``) is the ONLY thing that
    can arm the new instant. Dropping it leaves every waiter behind that scope
    to the 60s reaper backstop; coalescing it runs one more pass, which reads
    the schedule the park wrote and arms that scope's own one-shot.
    """
    mgr = await _app_manager(monkeypatch)
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    store = mgr._admission.taskq_store()
    coordinator = await mgr.dependency_coordinator_async()
    assert coordinator is not None

    def _seed() -> int:
        store.accept_one(model.TaskRecord(id="pump-row", kind=model.KIND_TASKRUNNER_STEP))
        claimed = store.claim("pump-row")
        assert claimed is not None
        assert store.transition("pump-row", model.STARTING, generation=claimed.generation)
        assert store.transition("pump-row", model.RUNNING, generation=claimed.generation)
        return int(claimed.generation)

    generation = await store.run(_seed)
    loop = asyncio.get_running_loop()
    reads: list[float | None] = []
    verdicts: list[Any] = []
    real_next_deadline = coordinator.next_deadline

    def _next_deadline() -> float | None:
        value = real_next_deadline()
        reads.append(value)
        if len(reads) == 1:
            # One writer thread, so this park is strictly AFTER the read the
            # first pass arms from, and its pump request reaches the loop
            # before the sweep's own resumption does: the production order of
            # ``run.py``'s park -> ``_taskq_pump``, made deterministic.
            verdicts.append(
                coordinator.report(
                    "pump-row",
                    dep_mod.DependencySignal(
                        kind=dep_mod.KIND_RATE_LIMITED,
                        dependency_scope="github:api",
                        source="gh",
                        retry_at=coordinator.now() + 30.0,
                    ),
                    generation=generation,
                    from_state=model.RUNNING,
                )
            )
            loop.call_soon_threadsafe(mgr._taskq_pump)
        return value

    monkeypatch.setattr(coordinator, "next_deadline", _next_deadline)
    before = store.loop_thread_calls
    try:
        mgr._taskq_pump()
        await await_until(getattr(mgr, "_taskq_tick_task"), what="taskq tick completed")
        assert verdicts and verdicts[0].outcome == "wait"
        assert reads[0] is None, "the first pass read the schedule before the park"
        timer = getattr(mgr, "_taskq_pump_timer", None)
        assert timer is not None, "the scope armed mid-sweep got no one-shot timer"
        assert timer.when() <= loop.time() + 30.0
        assert len(reads) == 2, "the request that landed mid-sweep ran exactly one more pass"
        assert reads[1] is not None
        assert store.loop_thread_calls == before
    finally:
        timer = getattr(mgr, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        await mgr.cancel_all()


# ── the pump replays a terminal write the store refused ──────────────────────


@pytest.mark.asyncio
async def test_the_wired_pump_replays_a_terminal_write_the_store_refused(
    monkeypatch: pytest.MonkeyPatch, no_memory_pressure: None
) -> None:
    """An owed terminal write is accepted work: the row stays ``running`` under
    this incarnation's lease until it lands, and nothing else can take a running
    row, so something in the LIVE process has to replay it. A bound coordinator
    is what stops ``RunnerAdmission.tick`` ever running again, so that something
    is the reaper pump -- on the store's writer thread, never the loop.
    """
    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = mgr
    orch._cfg = SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency_mode="aimd"))
    orch.task_runner = _Runner()
    orch.dashboard_state = SimpleNamespace(workflow_service=_Runner())
    orch._runner_admission = None
    orch._runner_admission_adopted = False
    assert await mgr.dependency_coordinator_async() is not None
    orch._wire_runner_admission()
    await orch._runner_admission_store_ready()
    adm = orch._runner_admission
    assert adm is not None and adm.coordinator is not None
    assert not hasattr(mgr, "_runner_admission_tick"), "the fallback tick is dropped"

    rec = await adm.accept_async(kind=model.KIND_TASKRUNNER_STEP, task_id="taskrunner:r9:task1")
    assert rec is not None
    handle = await adm.admit(rec.id, kind=model.KIND_TASKRUNNER_STEP)
    await handle.running_async()
    real_finish = store.finish

    def _down(*_a: Any, **_kw: Any) -> Any:
        raise store_mod.TaskStoreUnavailable("store down")

    monkeypatch.setattr(store, "finish", _down)
    assert await handle.done_async() is False
    assert adm.stats()["pending_terminal_writes"] == 1
    assert (await store.run(store.get, rec.id)).state == model.RUNNING
    monkeypatch.setattr(store, "finish", real_finish)

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    before = store.loop_thread_calls
    try:
        mgr._taskq_pump()
        await await_until(getattr(mgr, "_taskq_tick_task"), what="taskq tick completed")
        assert adm.stats()["pending_terminal_writes"] == 0, "the owed write was never replayed"
        assert (await store.run(store.get, rec.id)).state == model.DONE
        assert store.loop_thread_calls == before
    finally:
        timer = getattr(mgr, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        orch._unwire_runner_admission()
        await mgr.cancel_all()
