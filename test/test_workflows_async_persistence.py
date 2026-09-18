"""Workflow persistence stays off-loop without detaching writes or publication.

The blocking seam substitutes disk latency, not binding/authorization decisions.
See docs/system-specs/modules/workflows.md, registry persistence contract.
"""

import asyncio
import copy
import threading

import pytest

from kiro_crew.workflows.registry import RunHandle, RunRegistry
from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio
NOW = "2026-09-14T00:00:00Z"
SCRIPT = """META = {"name": "checkpoint"}
async def workflow(ctx):
    ctx.log("one")
    ctx.log("two")
    ctx.log("three")
    ctx.log("four")
    return "settled"
"""


class SlowStore:
    def __init__(self, phase):
        self.phase = phase
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.blocked = False
        self.on_loop = []
        self.rows = {}
        self.operations = []

    def load_all(self):
        return []

    def _hold(self, phase):
        self.on_loop.append(threading.get_ident() == self.loop_thread)
        if phase == self.phase and not self.blocked:
            self.blocked = True
            self.loop.call_soon_threadsafe(self.started.set)
            self.release.wait(2)

    def save(self, rid, payload):
        phase = "terminal" if payload["status"] != "running" else "checkpoint"
        if not payload["events"]:
            phase = "registration"
        elif payload["source"] and len(payload["events"]) < 5:
            phase = "source"
        self._hold(phase)
        self.rows[rid] = copy.deepcopy(payload)
        self.operations.append(("save", rid, payload["status"]))

    def delete(self, rid):
        self._hold("eviction")
        self.rows.pop(rid, None)
        self.operations.append(("delete", rid))


@pytest.mark.parametrize("phase", ["registration", "checkpoint", "terminal", "source", "eviction"])
async def test_background_slow_persistence_keeps_loop_responsive(phase):
    store = SlowStore(phase)
    registry = RunRegistry(store=store, max_runs=1)
    if phase == "eviction":
        registry.register(RunHandle("old", "old", status="finished"), persist=False)
        store.rows["old"] = {"status": "finished"}
    done = []
    registry.set_on_done(lambda rid, snap: done.append(snap))
    runner = WorkflowRunner(agent_fn=None, audit=lambda *args, **kw: None)

    async def author(*args, **kwargs):
        return {"ok": True, "source": SCRIPT}

    async def drive():
        rid = await runner.run_background(
            "" if phase == "source" else SCRIPT,
            registry=registry,
            run_id="new",
            now=NOW,
            intent="author" if phase == "source" else "",
            author_fn=author,
        )
        await registry.get(rid).task

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(store.started.wait(), 4)
        await asyncio.sleep(0)
        assert not any(store.on_loop), "store I/O ran on the owning loop"
        assert not task.done(), "the driver detached its outstanding write"
        assert not done, "completion was published before persistence drained"
    finally:
        store.release.set()
        await task
    assert store.rows["new"]["status"] == "finished"
    assert store.rows["new"]["source"] == SCRIPT
    assert [event["seq"] for event in store.rows["new"]["events"]] == list(
        range(len(store.rows["new"]["events"]))
    )
    assert len(done) == 1 and done[0]["result"] == "settled"
    assert "old" not in store.rows
    assert not any(store.on_loop)


async def test_host_registration_eviction_is_off_loop():
    store = SlowStore("eviction")
    service = WorkflowService(sessions=None, store=store)
    service.registry._max_runs = 1
    service.registry.register(RunHandle("old", "old", status="finished"), persist=False)
    task = asyncio.create_task(
        service.begin_host_run(name="plan", source_format="task-plan", driver="taskrunner")
    )
    try:
        await asyncio.wait_for(store.started.wait(), 4)
        assert not any(store.on_loop)
        assert not task.done()
    finally:
        store.release.set()
        rid = await task
    assert service.registry.get(rid).task is None
    assert store.operations[0] == ("delete", "old")


async def test_cancelled_registration_drains_repeated_cancels_and_removes_partial():
    store = SlowStore("registration")
    registry = RunRegistry(store=store)
    runner = WorkflowRunner(agent_fn=None, audit=lambda *args, **kw: None)
    task = asyncio.create_task(
        runner.run_background(SCRIPT, registry=registry, run_id="new", now=NOW)
    )
    try:
        await asyncio.wait_for(store.started.wait(), 4)
        assert not any(store.on_loop)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        store.release.set()
        await asyncio.gather(task, return_exceptions=True)
        handle = registry.get("new")
        if handle is not None and handle.task is not None:
            await asyncio.gather(handle.task, return_exceptions=True)
    assert task.cancelled()
    assert registry.get("new") is None
    assert "new" not in store.rows
    assert store.operations[-1] == ("delete", "new")


async def test_repeated_cancel_cannot_detach_checkpoint_or_let_delete_overtake():
    store = SlowStore("registration")
    registry = RunRegistry(store=store)
    registry.register(RunHandle("new", "new"), persist=False)
    write = asyncio.create_task(registry.persist_async("new"))
    await asyncio.wait_for(store.started.wait(), 4)
    delete = asyncio.create_task(registry.delete_async("new"))
    await asyncio.sleep(0)
    try:
        for _ in range(3):
            write.cancel()
            delete.cancel()
            await asyncio.sleep(0)
        assert not write.done()
        assert not delete.done()
    finally:
        store.release.set()
        await asyncio.gather(write, delete, return_exceptions=True)
    assert store.operations == [("save", "new", "running"), ("delete", "new")]
    assert not store.rows


async def test_terminal_flush_cancellation_keeps_committed_result():
    store = SlowStore("terminal")
    registry = RunRegistry(store=store)
    done = []
    registry.set_on_done(lambda rid, snap: done.append(snap))
    runner = WorkflowRunner(agent_fn=None, audit=lambda *args, **kw: None)
    rid = await runner.run_background(SCRIPT, registry=registry, run_id="new", now=NOW)
    task = registry.get(rid).task
    try:
        await asyncio.wait_for(store.started.wait(), 4)
        assert await registry.cancel(rid) is False
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert not done
    finally:
        store.release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert store.rows[rid]["status"] == "finished"
    assert store.rows[rid]["result"] == "settled"
    assert [event["type"] for event in store.rows[rid]["events"]][-1] == "run_finished"
    assert len(done) == 1 and done[0]["status"] == "finished"


async def test_public_mirror_write_failure_does_not_fail_execution():
    class UnavailableStore:
        def save(self, *args):
            raise OSError("disk unavailable")

    registry = RunRegistry(store=UnavailableStore())
    done = []
    registry.set_on_done(lambda rid, snap: done.append(snap))
    runner = WorkflowRunner(agent_fn=None, audit=lambda *args, **kw: None)
    rid = await runner.run_background(SCRIPT, registry=registry, run_id="new", now=NOW)
    await registry.get(rid).task
    assert done[0]["status"] == "finished"
    assert done[0]["result"] == "settled"


@pytest.mark.parametrize("entry", ["start", "intent", "rerun"])
async def test_admission_closed_during_registration_never_launches(entry):
    from types import SimpleNamespace

    sessions = SimpleNamespace(admission_closed=False)
    store = SlowStore("")
    service = WorkflowService(sessions=sessions, store=store)
    original = None
    if entry == "rerun":
        original = (await service.start(SCRIPT))["run_id"]
        await service.registry.get(original).task
    store.phase = "registration"
    if entry == "start":
        call = service.start(SCRIPT)
    elif entry == "intent":
        call = service.start_from_intent("never dispatch this author")
    else:
        call = service.rerun_subtree(original, 0)
    task = asyncio.create_task(call)
    try:
        await asyncio.wait_for(store.started.wait(), 4)
        sessions.admission_closed = True
    finally:
        store.release.set()
    assert await task == {"error": "gateway admission is closed"}
    assert {row["run_id"] for row in service.list_runs()} == ({original} if original else set())
    assert set(store.rows) == ({original} if original else set())
